"""Chroma-backed vector retrieval — the embeddings upgrade the stemmer docstring promised.

`LexicalRetriever`'s ceiling is string matching: "trading" doesn't match "trade", and the
crude stemmer only patches the plural case. Embeddings retrieve by meaning, so "how do I not
lose all my money" can find the risk doc without sharing a single content word with it.

## Score semantics — the honesty threshold must survive the swap

The educate handler refuses to answer when the top score is below `min_score`, and that
refusal is a safety feature, not a UX nicety. So scores here must behave like the lexical
ones: [0, 1], higher is better, and *unrelated queries must score low*. We use cosine
similarity clipped at zero. Embeddings are actually better at the honesty part than BM25 —
an out-of-domain query lands far from every chunk in embedding space, whereas BM25 needed
special-case OOV handling to avoid growing confident on unknown words.

## Why the embedding function is injectable

The default (Chroma's ONNX MiniLM) downloads a model on first use, which is right for
production and wrong for tests: unit tests must not depend on the network or 80 MB of
weights. Tests inject a deterministic bag-of-tokens embedder instead; the ranking and
threshold logic they exercise is identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import chromadb
from chromadb.config import Settings

from app.rag.chunking import Chunk
from app.rag.retrieval import Retrieved

if TYPE_CHECKING:
    from chromadb.api.types import EmbeddingFunction

COLLECTION = "tradedesk-corpus"


def _default_embedding_function() -> Any:
    """Chroma's bundled ONNX MiniLM. Imported lazily — it pulls model weights on first use,
    and constructing it at import time would make `import app.rag.vector` a network event."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


class ChromaRetriever:
    """Drop-in for `LexicalRetriever` behind the same `Retriever` protocol.

    In-memory by default: the corpus is 14 markdown docs that index in well under a second at
    startup, so persistence would only add a staleness bug (edit a doc, serve stale vectors).
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        embedding_function: EmbeddingFunction[Any] | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("ChromaRetriever needs at least one chunk")
        self.chunks = chunks

        client = chromadb.EphemeralClient(
            settings=Settings(anonymized_telemetry=False, allow_reset=True)
        )
        self._collection = client.create_collection(
            COLLECTION,
            embedding_function=embedding_function or _default_embedding_function(),
            # Cosine space so distance = 1 - similarity and our score math below holds.
            metadata={"hnsw:space": "cosine"},
        )
        self._collection.add(
            ids=[f"chunk-{i}" for i in range(len(chunks))],
            documents=[c.to_embedding_text() for c in chunks],
        )

    def search(self, query: str, *, k: int = 3) -> list[Retrieved]:
        if not query.strip():
            return []
        result = self._collection.query(query_texts=[query], n_results=min(k, len(self.chunks)))
        ids = result["ids"][0]
        distances = (result.get("distances") or [[]])[0]

        out: list[Retrieved] = []
        for chunk_id, distance in zip(ids, distances, strict=True):
            index = int(chunk_id.removeprefix("chunk-"))
            # Cosine distance = 1 - similarity. Clip at 0: a negative similarity means
            # "actively unrelated", and the threshold logic wants [0, 1].
            score = max(0.0, 1.0 - float(distance))
            out.append(Retrieved(chunk=self.chunks[index], score=score))

        # Chroma returns ascending-distance order already; assert rather than trust.
        out.sort(key=lambda r: r.score, reverse=True)
        return out
