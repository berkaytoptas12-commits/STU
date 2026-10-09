"""Entity-first retrieval.

scope:   UI selection > standards named in the question (hard filter, + errata/ECN of those standards,
         + untagged documents of the same collections) > collections named in the question > everything;
         superseded revisions are excluded unless explicitly selected
search:  BM25 (FTS5) + dense vectors -> reciprocal rank fusion -> cross-encoder rerank (API)
expand:  small-to-big: a hit grows to its whole section when the section is short, else to neighbours
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.config import Config
from techrag.embeddings import Embedder
from techrag.ingest.chunker import estimate_tokens
from techrag.query import QueryPlan
from techrag.reranker import Reranker
from techrag.store import ChunkRow, DocumentRow, ParameterRow, Store

_FIGURE_REF = re.compile(r"\b(figure|fig\.|şekil|timing diagram|waveform|eye diagram|pin ?out|pinout|"
                         r"block diagram|state diagram)\b", re.I)


@dataclass
class Passage:
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
    entities: list[str] = field(default_factory=list)
    doc_type: str = "base"
    revision: str = ""
    figure_page: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Scope:
    doc_ids: Optional[list[int]] = None        # None = no restriction
    domains: Optional[list[str]] = None
    reason: str = "all"                         # user | entity | domain | all
    entities: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    plan: QueryPlan
    passages: list[Passage]
    scope: Scope
    parameters: list[ParameterRow] = field(default_factory=list)
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
    """Concatenate consecutive chunks, dropping the overlap each repeats from its predecessor."""
    parts: list[str] = []
    prev: Optional[ChunkRow] = None
    for r in rows:
        text = r.text
        if prev is not None and r.ordinal == prev.ordinal + 1 and r.overlap:
            text = text[r.overlap:]
        if r.kind == "table" and parts:
            caption, _, rest = text.partition("\n")
            if rest and parts[-1].rstrip().endswith(caption.strip()):
                text = rest
        parts.append(text.strip())
        prev = r
    return "\n\n".join(p for p in parts if p)


class DocCatalog:
    """Small in-memory view of the documents table, refreshed when the index changes."""

    def __init__(self, store: Store):
        self.store = store
        self._version: Optional[str] = None
        self._docs: dict[int, DocumentRow] = {}
        self._lock = threading.Lock()

    def docs(self) -> dict[int, DocumentRow]:
        version = self.store.get_meta("index_version")
        with self._lock:
            if version != self._version:
                self._docs = {d.id: d for d in self.store.documents()}
                self._version = version
            return self._docs

    def resolve_scope(self, plan: Optional[QueryPlan], domains: Optional[Sequence[str]] = None,
                      doc_ids: Optional[Sequence[int]] = None, entity_filter: bool = True,
                      domain_routing: bool = True, standard: str = "") -> Scope:
        docs = self.docs()
        active = [d for d in docs.values() if d.superseded_by is None]
        if doc_ids:
            return Scope(doc_ids=list(doc_ids), reason="user")
        if domains:
            ids = [d.id for d in active if d.domain in set(domains)]
            return Scope(doc_ids=ids, domains=list(domains), reason="user")
        wanted = [standard] if standard else (plan.entities if plan else [])
        if entity_filter and wanted:
            wanted_set = {w.lower() for w in wanted}
            tagged = [d for d in active if wanted_set & {e.lower() for e in d.entities}]
            if tagged:
                doms = {d.domain for d in tagged}
                untagged = [d for d in active if d.domain in doms and not d.entities]
                return Scope(doc_ids=[d.id for d in tagged + untagged], reason="entity",
                             entities=[e for e in wanted if e.lower() in {x.lower() for d in tagged for x in d.entities}])
        if domain_routing and plan and plan.domains:
            ids = [d.id for d in active if d.domain in set(plan.domains)]
            if ids:
                return Scope(doc_ids=ids, domains=list(plan.domains), reason="domain")
        superseded = len(active) != len(docs)
        return Scope(doc_ids=[d.id for d in active] if superseded else None, reason="all")


class Retriever:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder, reranker: Optional[Reranker],
                 catalog: Optional[DocCatalog] = None):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.catalog = catalog or DocCatalog(store)

    # ----------------------------------------------------------- candidates
    def _candidates(self, plan: QueryPlan, scope: Scope, timings: dict) -> list[tuple[int, float]]:
        rc = self.cfg.retrieval
        lists: list[list[tuple[int, float]]] = []
        t = time.time()
        for q in plan.bm25_queries():
            lists.append(self.store.search_bm25(q, rc.bm25_top_k, None, scope.doc_ids))
        timings["bm25"] = round(timings.get("bm25", 0) + time.time() - t, 3)
        t = time.time()
        index = self.store.vectors()
        if index.size:
            qvecs = self.embedder.embed_queries(plan.dense_queries())
            mask = index.mask(None, scope.doc_ids)
            for row in qvecs:
                lists.append(index.search(row, rc.dense_top_k, mask))
        timings["dense"] = round(timings.get("dense", 0) + time.time() - t, 3)
        return rrf_fuse(lists, rc.rrf_k)

    def search(self, plan: QueryPlan, domains: Optional[Sequence[str]] = None,
               doc_ids: Optional[Sequence[int]] = None, top_k: Optional[int] = None, standard: str = "",
               budget_tokens: Optional[int] = None, with_parameters: bool = True) -> RetrievalResult:
        rc = self.cfg.retrieval
        timings: dict = {}
        scope = self.catalog.resolve_scope(plan, domains, doc_ids, rc.entity_filter, rc.domain_routing, standard)
        fused = self._candidates(plan, scope, timings)
        if len(fused) < rc.min_results_for_filter and scope.reason in ("entity", "domain"):
            scope = self.catalog.resolve_scope(None, entity_filter=False, domain_routing=False)
            fused = self._candidates(plan, scope, timings)

        n_cand = self.cfg.reranker.candidates if self.reranker else max(2 * rc.final_top_k, 16)
        fused = fused[:n_cand]
        rows = self.store.chunks_by_ids(cid for cid, _ in fused)
        ranked: list[tuple[ChunkRow, float]] = [(rows[cid], s) for cid, s in fused if cid in rows]

        confidence = None
        if self.reranker and ranked:
            t = time.time()
            docs = self.catalog.docs()
            texts = [f"{', '.join(docs[r.doc_id].entities) if r.doc_id in docs else ''} | {r.doc_title} | "
                     f"{r.section}\n{r.text}" for r, _ in ranked]
            scores = self.reranker.score(plan.rerank_query, texts)
            ranked = sorted(((r, s) for (r, _), s in zip(ranked, scores)), key=lambda x: -x[1])
            confidence = ranked[0][1]
            timings["rerank"] = round(time.time() - t, 3)

        budget = budget_tokens or int(self.cfg.llm.context_tokens * 0.6)
        passages = self.build_passages(ranked[: top_k or rc.final_top_k], budget)
        params: list[ParameterRow] = []
        if with_parameters and plan.wants_parameters:
            params = self.parameters(plan, scope)
        return RetrievalResult(plan, passages, scope, params, confidence, len(ranked), timings)

    def parameters(self, plan: QueryPlan, scope: Scope, k: Optional[int] = None) -> list[ParameterRow]:
        k = k or self.cfg.retrieval.parameter_rows
        query = " ".join(plan.parameters + plan.keywords[:6] + [plan.english])
        return self.store.search_parameters(query, k, scope.doc_ids, verified_only=True)

    # ------------------------------------------------------------- passages
    def build_passages(self, selected: list[tuple[ChunkRow, float]], budget: int) -> list[Passage]:
        rc = self.cfg.retrieval
        docs = self.catalog.docs()
        used = 0
        included: dict[tuple[int, int], ChunkRow] = {}
        hit_score: dict[tuple[int, int], float] = {}
        for row, score in selected:
            key = (row.doc_id, row.ordinal)
            if key in included:
                hit_score[key] = max(hit_score.get(key, 0.0), score)
                continue
            t = estimate_tokens(row.text)
            if included and used + t > budget:
                break
            included[key] = row
            hit_score[key] = score
            used += t

        # Small-to-big: whole section if short, else neighbours within the section.
        for key in sorted(hit_score, key=lambda k: -hit_score[k]):
            row = included[key]
            section = self.store.section_chunks(row.doc_id, row.section) if row.section else []
            sec_tokens = sum(estimate_tokens(c.text) for c in section)
            if section and sec_tokens <= rc.section_expand_tokens:
                extra = section
            else:
                w = rc.neighbor_window
                extra = [c for c in self.store.neighbors(row.doc_id, row.ordinal - w, row.ordinal + w)
                         if c.section == row.section]
            for nb in extra:
                nkey = (nb.doc_id, nb.ordinal)
                if nkey in included:
                    continue
                t = estimate_tokens(nb.text)
                if used + t > budget:
                    continue
                included[nkey] = nb
                used += t

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
            first = g[0]
            doc = docs.get(first.doc_id)
            text = join_chunks(g)
            fig_pages = set((doc.metadata.get("figure_pages") or []) if doc else [])
            fig = None
            if fig_pages and _FIGURE_REF.search(text):
                fig = next((p for r in g for p in range(r.page_start, r.page_end + 1) if p in fig_pages), None)
            passages.append(Passage(
                doc_id=first.doc_id, doc_title=first.doc_title, doc_path=first.doc_path, domain=first.domain,
                section=first.section, page_start=min(r.page_start for r in g), page_end=max(r.page_end for r in g),
                kind="table" if all(r.kind == "table" for r in g) else "text", text=text,
                score=float(max((hit_score[(r.doc_id, r.ordinal)] for r in hits), default=0.0)),
                chunk_ids=[r.id for r in g], hit_chunk_ids=[r.id for r in hits],
                entities=list(doc.entities) if doc else [], doc_type=doc.doc_type if doc else "base",
                revision=doc.revision if doc else "", figure_page=fig,
            ))
        passages.sort(key=lambda p: -p.score)
        return passages
