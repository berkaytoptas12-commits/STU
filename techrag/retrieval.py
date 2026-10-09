"""Hybrid retrieval: BM25 (FTS5) + dense vectors -> reciprocal-rank fusion -> cross-encoder rerank
-> neighbour expansion -> numbered source passages."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.config import Config
from techrag.embeddings import Embedder
from techrag.ingest.chunker import estimate_tokens
from techrag.query import QueryPlan
from techrag.reranker import Reranker
from techrag.store import ChunkRow, Store


@dataclass
class Passage:
    number: int
    doc_id: int
    doc_title: str
    doc_path: str
    domain: str
    section: str
    page_start: int
    page_end: int
    kind: str
    text: str
    score: float
    chunk_ids: list[int]
    hit_chunk_ids: list[int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalResult:
    plan: QueryPlan
    passages: list[Passage]
    routed_domains: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    candidates: int = 0
    timings: dict = field(default_factory=dict)


def rrf_fuse(ranked_lists: Sequence[Sequence[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, (cid, _) in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def join_chunks(rows: Sequence[ChunkRow]) -> str:
    """Concatenate consecutive chunks, dropping the overlap each chunk repeats from its predecessor."""
    parts: list[str] = []
    prev: Optional[ChunkRow] = None
    for r in rows:
        text = r.text
        if prev is not None and r.ordinal == prev.ordinal + 1 and r.overlap:
            text = text[r.overlap:]
        if r.kind == "table" and parts:
            # Table chunks carry their caption, which usually also ends the preceding text chunk.
            caption, _, rest = text.partition("\n")
            if rest and parts[-1].rstrip().endswith(caption.strip()):
                text = rest
        parts.append(text.strip())
        prev = r
    return "\n\n".join(p for p in parts if p)


class Retriever:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder, reranker: Optional[Reranker]):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.reranker = reranker

    # ----------------------------------------------------------- candidates
    def _candidates(self, plan: QueryPlan, domains: Optional[Sequence[str]], doc_ids: Optional[Sequence[int]],
                    timings: dict) -> list[tuple[int, float]]:
        rc = self.cfg.retrieval
        lists: list[list[tuple[int, float]]] = []
        t = time.time()
        for q in plan.bm25_queries():
            lists.append(self.store.search_bm25(q, rc.bm25_top_k, domains, doc_ids))
        timings["bm25"] = timings.get("bm25", 0) + time.time() - t

        t = time.time()
        index = self.store.vectors()
        if index.size:
            qvecs = self.embedder.embed_queries(plan.dense_queries())
            mask = index.mask(domains, doc_ids)
            for row in qvecs:
                lists.append(index.search(row, rc.dense_top_k, mask))
        timings["dense"] = timings.get("dense", 0) + time.time() - t
        return rrf_fuse(lists, rc.rrf_k)

    # --------------------------------------------------------------- search
    def search(self, plan: QueryPlan, domains: Optional[Sequence[str]] = None,
               doc_ids: Optional[Sequence[int]] = None, top_k: Optional[int] = None) -> RetrievalResult:
        rc = self.cfg.retrieval
        timings: dict = {}
        routed: list[str] = []
        if domains or doc_ids:
            fused = self._candidates(plan, domains, doc_ids, timings)
            routed = list(domains or [])
        elif rc.domain_routing and plan.domains:
            routed = list(plan.domains)
            fused = self._candidates(plan, routed, None, timings)
            if len(fused) < rc.min_results_for_filter:
                routed = []
                fused = self._candidates(plan, None, None, timings)
        else:
            fused = self._candidates(plan, None, None, timings)

        n_cand = self.cfg.reranker.candidates if self.reranker else max(2 * rc.final_top_k, 16)
        fused = fused[:n_cand]
        rows = self.store.chunks_by_ids(cid for cid, _ in fused)
        ranked: list[tuple[ChunkRow, float]] = [(rows[cid], s) for cid, s in fused if cid in rows]

        confidence = None
        if self.reranker and ranked:
            t = time.time()
            texts = [f"{r.doc_title} | {r.section}\n{r.text}" for r, _ in ranked]
            scores = self.reranker.score(plan.rerank_query, texts)
            ranked = sorted(((r, s) for (r, _), s in zip(ranked, scores)), key=lambda x: -x[1])
            confidence = ranked[0][1]
            timings["rerank"] = time.time() - t

        passages = self._build_passages(ranked[: top_k or rc.final_top_k])
        return RetrievalResult(plan, passages, routed, confidence, len(ranked),
                               {k: round(v, 3) for k, v in timings.items()})

    # ------------------------------------------------------------- passages
    def _build_passages(self, selected: list[tuple[ChunkRow, float]]) -> list[Passage]:
        rc = self.cfg.retrieval
        budget = rc.max_context_tokens
        used = 0
        included: dict[tuple[int, int], ChunkRow] = {}
        hit_score: dict[tuple[int, int], float] = {}

        for row, score in selected:
            key = (row.doc_id, row.ordinal)
            if key in included:
                continue
            t = estimate_tokens(row.text)
            if included and used + t > budget:
                break
            included[key] = row
            hit_score[key] = score
            used += t

        if rc.neighbor_window > 0:
            for key in list(hit_score):
                row = included[key]
                w = rc.neighbor_window
                for nb in self.store.neighbors(row.doc_id, row.ordinal - w, row.ordinal + w):
                    nkey = (nb.doc_id, nb.ordinal)
                    if nkey in included or nb.section != row.section:
                        continue
                    t = estimate_tokens(nb.text)
                    if used + t > budget:
                        continue
                    included[nkey] = nb
                    used += t

        # Group consecutive chunks of the same document + section into one passage.
        groups: list[list[ChunkRow]] = []
        for key in sorted(included):
            row = included[key]
            last = groups[-1][-1] if groups else None
            if last and last.doc_id == row.doc_id and last.ordinal + 1 == row.ordinal and last.section == row.section:
                groups[-1].append(row)
            else:
                groups.append([row])

        passages = []
        for g in groups:
            hits = [r for r in g if (r.doc_id, r.ordinal) in hit_score]
            score = max((hit_score[(r.doc_id, r.ordinal)] for r in hits), default=0.0)
            first = g[0]
            passages.append(Passage(
                number=0, doc_id=first.doc_id, doc_title=first.doc_title, doc_path=first.doc_path,
                domain=first.domain, section=first.section,
                page_start=min(r.page_start for r in g), page_end=max(r.page_end for r in g),
                kind="table" if all(r.kind == "table" for r in g) else "text",
                text=join_chunks(g), score=float(score), chunk_ids=[r.id for r in g],
                hit_chunk_ids=[r.id for r in hits],
            ))
        passages.sort(key=lambda p: -p.score)
        for i, p in enumerate(passages, 1):
            p.number = i
        return passages
