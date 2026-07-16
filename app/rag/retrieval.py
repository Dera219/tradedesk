"""Retrieval.

Two implementations behind one protocol, mirroring the `BrokerageClient` pattern: the demo must
run with no network and no API key, and the real thing should be a one-line swap.

- `LexicalRetriever` — BM25 over the chunk text. Zero dependencies, deterministic, offline.
- `ChromaRetriever` — embeddings. See the note at the bottom.

## The honesty threshold

The most important behaviour here is **refusing to answer**. A retriever always returns its
nearest chunks, no matter how irrelevant — "what's the capital of France?" will still match
*something* in a brokerage corpus. If the handler then feeds that chunk to the model as context,
the model will dutifully produce a confident answer grounded in nothing.

So retrieval returns scores, and the handler refuses below `min_score`. "That isn't in my docs" is
a correct answer, and a system that cannot say it will instead invent things.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from app.rag.chunking import Chunk

_TOKEN = re.compile(r"[a-z0-9$]+")

#: Words that carry no topical signal.
#:
#: There is a real tension here and it cost a bug in both directions. Too aggressive and you
#: delete domain terms — "day" in "day trade", "no" in "no-fee" — so those stay.
#:
#: But too timid is worse once out-of-vocabulary terms are penalized (see `_oov_idf`). Question
#: scaffolding — "how", "much", "need" — is absent from a corpus written as declarative
#: reference prose. Left in, "How much equity do I need to day trade?" gets charged the full
#: unknown-word penalty three times over and drops to 0.17, so the agent refuses a question it
#: can answer perfectly well.
#:
#: The rule: strip interrogatives, auxiliaries, and quantifiers (never topical). Keep every noun
#: and domain verb.
_STOPWORDS = frozenset(
    {
        # articles, conjunctions, prepositions
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "as",
        "at",
        "by",
        "with",
        "from",
        "about",
        "if",
        "then",
        "than",
        "so",
        "but",
        # pronouns
        "i",
        "you",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "my",
        "me",
        "your",
        "we",
        "they",
        "them",
        "there",
        "here",
        # auxiliaries / copulas
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        # interrogatives — absent from declarative reference prose, so they'd read as OOV
        "what",
        "how",
        "why",
        "when",
        "where",
        "who",
        "whom",
        "which",
        "whose",
        # quantifiers / vague verbs
        "much",
        "many",
        "some",
        "any",
        "need",
        "needs",
        "needed",
        "get",
        "gets",
        "got",
        "tell",
        "explain",
        "mean",
        "means",
        "work",
        "works",
        "happen",
        "happens",
        # contraction fragments left by tokenization ("what's" -> "what", "s")
        "s",
        "t",
        "re",
        "ve",
        "ll",
        "d",
        "m",
    }
)


def _stem(token: str) -> str:
    """Strip a trailing plural/third-person `s`.

    Deliberately the crudest possible stemmer. It exists because "why does the PDT rule exist"
    missed the section literally titled "Why this rule exists" — the corpus says "exists", the
    user typed "exist", and BM25 matches strings. The same gap hits fee/fees, order/orders,
    threshold/thresholds.

    Guards: keep short tokens intact ("is", "as"), and leave "ss" endings alone so "loss" doesn't
    become "los".

    This does NOT solve the general problem — "trading" still won't match "trade". That's not a
    bug to patch with more suffix rules; it's the ceiling of lexical retrieval, and the reason
    to move to embeddings. See `build_chroma_retriever`.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping `$` so "$25,000" stays findable as a money term."""
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True, slots=True)
class Retrieved:
    chunk: Chunk
    score: float


class Retriever(Protocol):
    def search(self, query: str, *, k: int = 3) -> list[Retrieved]:
        """Return the top-k chunks, highest score first. May return fewer, including none."""


class LexicalRetriever:
    """BM25 over chunk embedding-text.

    BM25 rather than raw TF-IDF because it saturates term frequency: a chunk repeating "margin"
    twenty times shouldn't dominate one that uses it twice in a more relevant sentence. It also
    normalizes for length, which matters here since heading sections vary a lot in size.

    Scores are normalized to [0, 1] against the best possible match for the query, so `min_score`
    is a stable threshold rather than something that drifts with corpus size.
    """

    K1 = 1.5  # term-frequency saturation
    B = 0.75  # length-normalization strength

    def __init__(self, chunks: list[Chunk]) -> None:
        if not chunks:
            raise ValueError("LexicalRetriever needs at least one chunk")
        self.chunks = chunks
        self._docs = [tokenize(c.to_embedding_text()) for c in chunks]
        self._lengths = [len(d) for d in self._docs]
        self._avg_length = sum(self._lengths) / len(self._lengths)
        self._term_counts = [Counter(d) for d in self._docs]

        document_frequency: Counter[str] = Counter()
        for doc in self._docs:
            document_frequency.update(set(doc))

        total = len(self._docs)
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in document_frequency.items()
        }
        # IDF for a term that appears in NO chunk — the value the formula gives at freq=0.
        #
        # This is not a detail. Treating an out-of-vocabulary term as idf=0 makes it invisible to
        # the ceiling below, so a query like "capital of France" reduces to just "capital",
        # matches "capital cushion" in the PDT doc, and scores 0.39 — above the honesty
        # threshold. The retriever became MORE confident the more unusual the question was,
        # which is exactly backwards. An unknown word is strong evidence the corpus can't answer.
        self._oov_idf = math.log(1 + (total + 0.5) / 0.5)

    def _idf_for(self, term: str) -> float:
        return self._idf.get(term, self._oov_idf)

    def _score(self, query_terms: list[str], index: int) -> float:
        counts = self._term_counts[index]
        length = self._lengths[index]
        score = 0.0
        for term in query_terms:
            if term not in counts:
                continue
            frequency = counts[term]
            numerator = frequency * (self.K1 + 1)
            denominator = frequency + self.K1 * (1 - self.B + self.B * length / self._avg_length)
            score += self._idf[term] * numerator / denominator
        return score

    def search(self, query: str, *, k: int = 3) -> list[Retrieved]:
        query_terms = tokenize(query)
        if not query_terms:
            return []

        raw = [(index, self._score(query_terms, index)) for index in range(len(self._docs))]
        raw = [(index, score) for index, score in raw if score > 0]
        if not raw:
            return []

        # Normalize against the theoretical max for THIS query: every query term present at
        # saturating frequency. Without this, scores scale with query length and a fixed
        # min_score threshold would mean different things for different questions.
        #
        # `_idf_for` (not `_idf.get(..., 0.0)`) is what makes unmatched terms cost something:
        # a query term absent from the whole corpus contributes its full OOV weight to the
        # ceiling while contributing nothing to the numerator, which is precisely the penalty
        # an unanswerable question deserves.
        ceiling = sum(self._idf_for(term) for term in set(query_terms)) * (self.K1 + 1) / self.K1
        if ceiling <= 0:
            return []

        ranked = sorted(raw, key=lambda pair: pair[1], reverse=True)[:k]
        return [
            Retrieved(chunk=self.chunks[index], score=min(score / ceiling, 1.0))
            for index, score in ranked
        ]


def build_chroma_retriever(chunks: list[Chunk], persist_path: str) -> Retriever:  # pragma: no cover
    """Embedding-based retrieval via Chroma.

    Not wired up yet — Part 1's target. Chroma's default embedding function
    (`all-MiniLM-L6-v2` via onnxruntime) runs locally and needs no API key, which keeps the
    "demo works offline" property that `LexicalRetriever` gives us today.

    Worth knowing before swapping: BM25 matches *words*, embeddings match *meaning*. A user
    asking "how much money do I need to day trade?" never says "equity" or "$25,000", so lexical
    search can miss the PDT chunk entirely while embeddings find it. That's the reason to make
    this swap — and it's a better demo beat than "we used a vector DB because vector DBs are what
    you use."
    """
    raise NotImplementedError(
        "ChromaRetriever is Part 1's target. LexicalRetriever works offline in the meantime; "
        "see this docstring for why the swap matters."
    )
