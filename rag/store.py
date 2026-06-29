"""Vector stores for the portfolio knowledge base.

`InMemoryVectorStore` is the offline default: it keeps documents and their
vectors in memory, ranks by cosine similarity, and persists to a plain JSON
file. `QdrantVectorStore` is an optional, lazy-import stub for production.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# A document is {"text": str, "metadata": dict, "vector": list[float]}.
Doc = dict[str, Any]


@runtime_checkable
class VectorStore(Protocol):
    def add(self, docs: list[Doc]) -> None:
        ...

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[Doc, float]]:
        ...

    def save(self, path: str) -> None:
        ...

    def load(self, path: str) -> None:
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorStore:
    """Cosine-similarity search over in-memory (text, metadata, vector) docs."""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim
        self._docs: list[Doc] = []

    def __len__(self) -> int:
        return len(self._docs)

    @property
    def docs(self) -> list[Doc]:
        return self._docs

    def add(self, docs: list[Doc]) -> None:
        for doc in docs:
            if "vector" not in doc:
                raise ValueError("each doc must carry a 'vector'")
            vec = doc["vector"]
            if self.dim is None:
                self.dim = len(vec)
            self._docs.append(
                {
                    "text": doc.get("text", ""),
                    "metadata": dict(doc.get("metadata", {})),
                    "vector": list(vec),
                }
            )

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[Doc, float]]:
        scored = [(doc, _cosine(query_vec, doc["vector"])) for doc in self._docs]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(0, k)]

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"dim": self.dim, "docs": self._docs}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, path: str) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.dim = payload.get("dim")
        self._docs = payload.get("docs", [])

    @classmethod
    def from_file(cls, path: str) -> InMemoryVectorStore:
        store = cls()
        store.load(path)
        return store


class QdrantVectorStore:  # pragma: no cover - optional production backend
    """Lazy-import stub for an external Qdrant backend.

    Not used in the offline default path or tests. Constructing it requires the
    `qdrant-client` package (installed via the `[prod]` extra).
    """

    def __init__(self, url: str = "http://localhost:6333", collection: str = "portfolio_kb") -> None:
        try:
            from qdrant_client import QdrantClient  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "qdrant-client is not installed; install the [prod] extra or use "
                "InMemoryVectorStore for offline use."
            ) from exc
        self._client = QdrantClient(url=url)
        self._collection = collection

    def add(self, docs: list[Doc]) -> None:
        raise NotImplementedError("QdrantVectorStore is a stub; use InMemoryVectorStore offline.")

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[Doc, float]]:
        raise NotImplementedError("QdrantVectorStore is a stub; use InMemoryVectorStore offline.")

    def save(self, path: str) -> None:
        raise NotImplementedError

    def load(self, path: str) -> None:
        raise NotImplementedError
