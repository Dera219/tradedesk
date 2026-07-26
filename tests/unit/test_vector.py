"""ChromaRetriever: same protocol, same honesty guarantees, no network.

The injected embedder is a deterministic bag-of-tokens vector (reusing the corpus tokenizer,
so stemming behaves identically). Cosine similarity over it equals token overlap — crude as
semantics, but exactly right for testing what these tests test: ranking order, score range,
protocol compliance, and the fail-closed behavior on out-of-domain queries. The production
embedding model changes the geometry, not the contract.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from app.graph.state import ConversationState
from app.rag.chunking import chunk_corpus
from app.rag.retrieval import tokenize
from app.rag.vector import ChromaRetriever

CORPUS = Path(__file__).resolve().parent.parent.parent / "corpus"
DIM = 256


class BagOfTokensEmbedder:
    """Deterministic, dependency-free embedding: hash each token into one of DIM buckets,
    L2-normalize. Shared tokens → cosine similarity; disjoint tokens → similarity 0."""

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — chroma's API name
        out = []
        for text in input:
            vec = [0.0] * DIM
            for token in tokenize(text):
                digest = hashlib.md5(token.encode(), usedforsecurity=False).hexdigest()
                bucket = int(digest, 16) % DIM
                vec[bucket] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    # chromadb >= 1.x routes through these instead of __call__ directly.
    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self(input)

    @staticmethod
    def name() -> str:
        return "bag-of-tokens-test-embedder"


@pytest.fixture(scope="module")
def retriever() -> ChromaRetriever:
    return ChromaRetriever(chunk_corpus(CORPUS), embedding_function=BagOfTokensEmbedder())


def test_empty_corpus_refused() -> None:
    with pytest.raises(ValueError, match="at least one chunk"):
        ChromaRetriever([], embedding_function=BagOfTokensEmbedder())


def test_finds_the_pdt_doc_for_a_pdt_query(retriever: ChromaRetriever) -> None:
    results = retriever.search("what is the pattern day trader rule?", k=3)
    assert results, "expected at least one hit"
    top = results[0]
    text = (top.chunk.doc_title + " " + top.chunk.body).lower()
    assert "pattern day" in text or "pdt" in text


def test_scores_are_normalized_and_descending(retriever: ChromaRetriever) -> None:
    results = retriever.search("margin and buying power", k=5)
    scores = [r.score for r in results]
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores == sorted(scores, reverse=True)


def test_k_larger_than_corpus_does_not_crash(retriever: ChromaRetriever) -> None:
    results = retriever.search("fees", k=10_000)
    assert len(results) <= len(retriever.chunks)


def test_blank_query_returns_nothing(retriever: ChromaRetriever) -> None:
    assert retriever.search("   ") == []


def test_out_of_domain_query_scores_below_threshold(retriever: ChromaRetriever) -> None:
    """The honesty property: a question the corpus can't answer must not produce a confident
    score. With disjoint vocabulary, cosine similarity collapses toward zero — far below the
    0.35 refusal threshold the educate handler applies."""
    results = retriever.search("recette de ratatouille provencale aubergines courgettes", k=3)
    assert all(r.score < 0.35 for r in results)


async def test_drop_in_behind_the_educate_handler(retriever: ChromaRetriever) -> None:
    """The protocol claim, tested end-to-end: the handler answers in-scope questions with
    citations and refuses out-of-domain ones, with no lexical retriever anywhere in sight."""
    from app.graph.handlers import handle_educate

    state = ConversationState()
    state.user_message = "explain the pattern day trader rule"
    await handle_educate(state, retriever=retriever)
    assert state.citations, "in-scope question should cite corpus docs"

    state2 = ConversationState()
    state2.user_message = "quelle est la meilleure recette de ratatouille aubergine"
    await handle_educate(state2, retriever=retriever)
    assert not state2.citations, "out-of-domain question must not cite anything"
