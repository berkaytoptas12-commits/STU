"""Cross-encoder reranking (e.g. BAAI/bge-reranker-v2-m3, multilingual: Turkish question vs English text)."""

from __future__ import annotations

import math
import threading
from typing import Optional, Protocol, Sequence

from techrag.config import RerankerConfig


class Reranker(Protocol):
    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, cfg: RerankerConfig):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "sentence-transformers is not installed; install it or set reranker.enabled: false"
            ) from exc
        self.cfg = cfg
        self.model = CrossEncoder(cfg.model, device=cfg.device, max_length=cfg.max_length)
        self._lock = threading.Lock()

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        with self._lock:
            raw = self.model.predict([(query, p) for p in passages], batch_size=self.cfg.batch_size,
                                     show_progress_bar=False)
        scores = [float(s) for s in raw]
        # Report probabilities in [0, 1] regardless of whether the model applied a sigmoid.
        if any(s < 0 or s > 1 for s in scores):
            scores = [1 / (1 + math.exp(-s)) for s in scores]
        return scores


def create_reranker(cfg: RerankerConfig) -> Optional[Reranker]:
    if not cfg.enabled:
        return None
    if cfg.backend in ("sentence_transformers", "sentence-transformers", "st", "local"):
        return CrossEncoderReranker(cfg)
    raise ValueError(f"Unknown reranker backend: {cfg.backend}")
