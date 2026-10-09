"""Embeddings over an OpenAI-compatible /v1/embeddings endpoint (vLLM pooling runner, SGLang, TEI ...).

Query/passage formatting is derived from the model name when set to "auto":
  Qwen3-Embedding / gte-Qwen : "Instruct: <task>\\nQuery: <q>" for queries, raw passages
  E5 family                  : "query: " / "passage: "
  BGE v1.5 (en/zh)           : retrieval instruction on queries
  bge-m3 and others          : no prefix
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Protocol, Sequence

import numpy as np

from techrag.api import APIClient
from techrag.config import EmbeddingConfig

TASK = "Given a question about a hardware interface or design standard, retrieve passages of the standard that answer it"


class Embedder(Protocol):
    name: str

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(m) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def prefixes_for(model: str, query_instruction: str = "auto", passage_prefix: str = "auto") -> tuple[str, str]:
    name = model.lower()
    q = p = ""
    if "qwen3-embedding" in name or "gte-qwen" in name or "qwen3_embedding" in name:
        q = f"Instruct: {TASK}\nQuery: "
    elif re.search(r"(^|[/_-])(multilingual-)?e5", name):
        q, p = "query: ", "passage: "
    elif "bge" in name and "m3" not in name and ("en" in name or "zh" in name):
        q = "Represent this sentence for searching relevant passages: "
    if query_instruction != "auto":
        q = query_instruction
    if passage_prefix != "auto":
        p = passage_prefix
    return q, p


class APIEmbedder:
    def __init__(self, cfg: EmbeddingConfig, transport=None):
        if not cfg.model:
            raise RuntimeError("no embedding model configured (Settings > Embedding)")
        self.cfg = cfg
        self.api = APIClient(cfg, transport)
        self.name = f"api:{cfg.model}"
        self.query_prefix, self.passage_prefix = prefixes_for(cfg.model, cfg.query_instruction, cfg.passage_prefix)
        self._dim: Optional[int] = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed_queries(["dimension probe"]).shape[1])
        return self._dim

    def _batch(self, texts: list[str]) -> np.ndarray:
        data = self.api.post("embeddings", {"model": self.cfg.model, "input": texts})
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        return _normalize([r["embedding"] for r in rows])

    def _embed(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        bs = max(1, self.cfg.batch_size)
        batches = [texts[i:i + bs] for i in range(0, len(texts), bs)]
        if len(batches) == 1 or self.cfg.concurrency <= 1:
            parts = [self._batch(b) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
                parts = list(pool.map(self._batch, batches))
        out = np.vstack(parts)
        self._dim = out.shape[1]
        return out

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed([self.passage_prefix + t for t in texts])

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed([self.query_prefix + t for t in texts])


class HashEmbedder:
    """Model-free feature hashing (words + char trigrams). For tests and smoke checks only."""

    def __init__(self, dim: int = 512):
        self.name = f"hash:{dim}"
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self._dim, dtype=np.float32)
        words = re.findall(r"\w+", text.lower())
        for f in words + [w[i:i + 3] for w in words if len(w) > 3 for i in range(len(w) - 2)]:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), np.float32)
        return _normalize(np.vstack([self._vec(t) for t in texts]))

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed_documents(texts)


def create_embedder(cfg: EmbeddingConfig) -> Embedder:
    if cfg.backend == "hash":
        return HashEmbedder()
    if cfg.backend in ("api", "openai", "vllm"):
        return APIEmbedder(cfg)
    raise ValueError(f"Unknown embedding backend '{cfg.backend}' (use 'api')")
