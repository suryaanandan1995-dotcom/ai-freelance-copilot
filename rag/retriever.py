"""Retriever over the portfolio knowledge base.

Loads the saved JSON store and answers similarity queries, returning
`[{text, source, score}]`. Uses FakeEmbedder + InMemoryVectorStore by default
so it works fully offline with no API key. If the saved store is missing, it
builds one on the fly from the portfolio repos.
"""
from __future__ import annotations

from pathlib import Path

from config import get_settings

from .embedder import Embedder, FakeEmbedder
from .ingest import ingest_portfolio
from .store import InMemoryVectorStore


class Retriever:
    def __init__(self, store: InMemoryVectorStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        query_vec = self._embedder.embed(query)
        results = self._store.search(query_vec, k=k)
        out: list[dict] = []
        for doc, score in results:
            meta = doc.get("metadata", {})
            out.append(
                {
                    "text": doc.get("text", ""),
                    "source": meta.get("source", ""),
                    "kind": meta.get("kind", ""),
                    "score": float(score),
                }
            )
        return out


def build_store(repos_path: str, embedder: Embedder) -> InMemoryVectorStore:
    """Ingest + embed the portfolio into a fresh in-memory store."""
    docs = ingest_portfolio(repos_path)
    store = InMemoryVectorStore(dim=embedder.dim)
    embedded = [
        {
            "text": d["text"],
            "metadata": {"source": d["source"], "kind": d["kind"]},
            "vector": embedder.embed(d["text"]),
        }
        for d in docs
    ]
    store.add(embedded)
    return store


def get_retriever() -> Retriever:
    """Load the saved KB (building from portfolio if the JSON store is missing)."""
    settings = get_settings()
    embedder: Embedder = FakeEmbedder()
    store_path = Path(settings.rag_store_path)

    if store_path.is_file():
        store = InMemoryVectorStore.from_file(str(store_path))
    else:
        store = build_store(settings.portfolio_repos_path, embedder)
    return Retriever(store, embedder)
