"""Embedding backends. All return L2-normalised float32 matrices."""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Protocol, Sequence

from pathlib import Path

import numpy as np

from techrag.config import EmbeddingConfig


class Embedder(Protocol):
    name: str

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-12)


class SentenceTransformerEmbedder:
    """Local model directory loaded with sentence-transformers (e.g. BAAI/bge-m3, multilingual-e5-large)."""

    def __init__(self, cfg: EmbeddingConfig):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "sentence-transformers is not installed. Install it (pip install sentence-transformers) "
                "or set embedding.backend: ollama in config.yaml"
            ) from exc
        self.cfg = cfg
        # Name by model folder only, so moving the models directory does not invalidate the index.
        self.name = f"st:{Path(cfg.model).name}"
        self.model = SentenceTransformer(cfg.model, device=cfg.device)
        self.model.max_seq_length = cfg.max_seq_length
        self._lock = threading.Lock()

    @property
    def dim(self) -> int:
        getter = getattr(self.model, "get_sentence_embedding_dimension", None)
        return int(getter()) if getter else int(self.embed_queries(["dim"]).shape[1])

    def _encode(self, texts: Sequence[str], show_progress: bool = False) -> np.ndarray:
        with self._lock:
            out = self.model.encode(list(texts), batch_size=self.cfg.batch_size, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=show_progress)
        return _normalize(out)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode([self.cfg.passage_prefix + t for t in texts])

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode([self.cfg.query_prefix + t for t in texts])


class HTTPEmbedder:
    """Embeddings from a local server: Ollama (/api/embed) or OpenAI-compatible (/v1/embeddings)."""

    def __init__(self, cfg: EmbeddingConfig, transport=None):
        import httpx

        self.cfg = cfg
        self.name = f"{cfg.backend}:{cfg.model}"
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.client = httpx.Client(timeout=cfg.timeout, headers=headers, transport=transport)
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed_queries(["dimension probe"]).shape[1])
        return self._dim

    def _post(self, texts: list[str]) -> np.ndarray:
        base = self.cfg.base_url.rstrip("/")
        if self.cfg.backend == "ollama":
            r = self.client.post(f"{base}/api/embed", json={"model": self.cfg.model, "input": texts,
                                                             "truncate": True})
            r.raise_for_status()
            vecs = r.json()["embeddings"]
        else:
            r = self.client.post(f"{base}/embeddings", json={"model": self.cfg.model, "input": texts})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vecs = [d["embedding"] for d in data]
        return _normalize(np.asarray(vecs, dtype=np.float32))

    def _embed(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        parts = [self._post(texts[i:i + self.cfg.batch_size]) for i in range(0, len(texts), self.cfg.batch_size)]
        out = np.vstack(parts) if parts else np.zeros((0, self.dim), np.float32)
        if len(out):
            self._dim = out.shape[1]
        return out

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed([self.cfg.passage_prefix + t for t in texts])

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed([self.cfg.query_prefix + t for t in texts])


class HashEmbedder:
    """Model-free feature-hashing embedder (words + character trigrams).

    Only for tests and pipeline smoke checks: it has no semantic understanding and must not be used
    for real question answering.
    """

    def __init__(self, dim: int = 512):
        self.name = f"hash:{dim}"
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self._dim, dtype=np.float32)
        words = re.findall(r"\w+", text.lower())
        feats = words + [w[i:i + 3] for w in words if len(w) > 3 for i in range(len(w) - 2)]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return _normalize(np.vstack([self._vec(t) for t in texts])) if texts else np.zeros((0, self._dim))

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed_documents(texts)


def create_embedder(cfg: EmbeddingConfig) -> Embedder:
    backend = cfg.backend.lower()
    if backend in ("sentence_transformers", "sentence-transformers", "st", "local"):
        return SentenceTransformerEmbedder(cfg)
    if backend in ("ollama", "openai"):
        return HTTPEmbedder(cfg)
    if backend == "hash":
        return HashEmbedder()
    raise ValueError(f"Unknown embedding backend: {cfg.backend}")
