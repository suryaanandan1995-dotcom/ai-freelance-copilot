"""Embedders for the portfolio knowledge base.

The default `FakeEmbedder` is fully offline and deterministic: it hashes a
bag-of-words into a fixed-dimension float vector and L2-normalizes it. This is
all the tests and the default pipeline ever need — NO API key, NO download.

`SentenceTransformerEmbedder` is a real, optional embedder behind a lazy import;
it is only constructed when explicitly requested and the library is installed.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into a fixed-length float vector."""

    dim: int

    def embed(self, text: str) -> list[float]:
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        ...


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class FakeEmbedder:
    """Deterministic, dependency-free hashing embedder.

    Each token is hashed into a bucket of a `dim`-length vector; the bucket is
    incremented (with a sign derived from the hash) to form a signed hashed
    bag-of-words. The result is L2-normalized so cosine similarity is just a dot
    product. Identical text always yields an identical vector.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def _hash(self, token: str) -> tuple[int, float]:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] & 1 else -1.0
        return bucket, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            bucket, sign = self._hash(token)
            vec[bucket] += sign
        return _l2_normalize(vec)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """Real embedder backed by `sentence-transformers` (lazy, optional).

    Importing the library is deferred to construction time so the package is
    never required for the offline default path or the test suite.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "sentence-transformers is not installed; install the [prod] extra "
                "or use FakeEmbedder for offline use."
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


def get_embedder(use_real: bool = False, dim: int = 256) -> Embedder:
    """Return the default offline embedder, or the real one when requested."""
    if use_real:
        return SentenceTransformerEmbedder()
    return FakeEmbedder(dim=dim)
