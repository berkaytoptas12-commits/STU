"""Cross-encoder reranking over HTTP: vLLM/Jina-style /v1/rerank, falling back to vLLM /score.

Qwen3-Reranker is served by vLLM as a sequence classifier and expects its yes/no judging prompt to be
applied by the client; that template is added automatically when the model name says Qwen3-Reranker.
"""

from __future__ import annotations

import math
from typing import Optional, Protocol, Sequence

from techrag.api import APIClient, APIError
from techrag.config import RerankerConfig

_Q3_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
              "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
_Q3_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class Reranker(Protocol):
    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


def _to_prob(scores: list[float]) -> list[float]:
    if any(s < 0 or s > 1 for s in scores):
        return [1 / (1 + math.exp(-max(min(s, 50), -50))) for s in scores]
    return scores


class APIReranker:
    def __init__(self, cfg: RerankerConfig, transport=None):
        if not cfg.model:
            raise RuntimeError("no reranker model configured (Settings > Reranker)")
        self.cfg = cfg
        self.api = APIClient(cfg, transport)
        tmpl = cfg.template
        if tmpl == "auto":
            tmpl = "qwen3" if "qwen3-reranker" in cfg.model.lower().replace("_", "-") else "none"
        self.template = tmpl
        self._endpoint = cfg.endpoint

    def _format(self, query: str, docs: Sequence[str]) -> tuple[str, list[str]]:
        if self.template == "qwen3":
            q = f"{_Q3_PREFIX}<Instruct>: {self.cfg.instruction}\n<Query>: {query}\n"
            return q, [f"<Document>: {d}{_Q3_SUFFIX}" for d in docs]
        return query, list(docs)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        q, docs = self._format(query, passages)
        if self._endpoint in ("auto", "rerank"):
            try:
                data = self.api.post("rerank", {"model": self.cfg.model, "query": q, "documents": docs,
                                                "top_n": len(docs)})
                scores = [0.0] * len(docs)
                for r in data.get("results", []):
                    scores[r["index"]] = float(r.get("relevance_score", r.get("score", 0.0)))
                self._endpoint = "rerank"
                return _to_prob(scores)
            except APIError as exc:
                if self._endpoint == "rerank" or exc.status not in (404, 405):
                    raise
                self._endpoint = "score"
        data = self.api.post("score", {"model": self.cfg.model, "text_1": q, "text_2": docs})
        scores = [0.0] * len(docs)
        for r in data.get("data", []):
            scores[r["index"]] = float(r.get("score", 0.0))
        return _to_prob(scores)


def create_reranker(cfg: RerankerConfig) -> Optional[Reranker]:
    if not cfg.enabled or not cfg.model:
        return None
    return APIReranker(cfg)
