"""Entity-first retrieval.

scope:   UI selection > standards named in the question (hard filter: only documents TAGGED with that
         standard/version, incl. its errata/ECN; untagged documents are not assumed to match) > collections
         named in the question > everything. A named standard that is not loaded is reported, never
         replaced by a sibling (no DDR4 answer to a DDR5 question). Superseded revisions are excluded unless
         the question names that revision or the user selects the document.
search:  BM25 (FTS5) + dense vectors -> reciprocal rank fusion -> cross-encoder rerank (API); questions about
         several standards are retrieved per standard so each gets its own evidence
expand:  small-to-big: a hit grows to its whole section when the section is short, else to neighbours;
         search-only content (unverified VLM tables) is replaced by the original text of its page
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
from techrag.ingest.metadata import AMENDING_TYPES, amended_targets, doc_series
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
    reason: str = "all"                         # user | entity | entity_missing | domain | all
    entities: list[str] = field(default_factory=list)   # named standards that are loaded
    missing: list[str] = field(default_factory=list)    # named standards without any loaded document
    per_entity: dict = field(default_factory=dict)      # standard -> doc ids (separate evidence per standard)
    notes: list[str] = field(default_factory=list)      # disclosed decisions (old revision, widening, ...)
    widened: bool = False
    explicit_revisions: list[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.doc_ids is not None and not self.doc_ids

    def to_dict(self) -> dict:
        return asdict(self)


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
        self._amends: dict[int, list[int]] = {}
        self._lock = threading.Lock()

    def docs(self) -> dict[int, DocumentRow]:
        version = self.store.get_meta("index_version")
        with self._lock:
            if version != self._version:
                self._docs = {d.id: d for d in self.store.documents()}
                docs = list(self._docs.values())
                self._amends = {d.id: amended_targets(d, docs) for d in docs if d.doc_type in AMENDING_TYPES}
                self._version = version
            return self._docs

    def amends(self, doc_id: int) -> list[int]:
        """Base documents an errata/ECN/amendment applies to ([] when not identified)."""
        self.docs()
        return list(self._amends.get(doc_id, []))

    def amended_by(self, doc_id: int) -> list[int]:
        self.docs()
        return [e for e, targets in self._amends.items() if doc_id in targets]

    def revision_mentions(self, text: str, entities: Sequence[str] = ()) -> list[int]:
        """Documents whose specific revision the text names ('JESD79-4B', 'DDR4 rev B', 'PCIe 5.0 r0.9')."""
        if not text:
            return []
        low = text.lower()
        ents = {e.lower() for e in entities}
        out = []
        for d in self.docs().values():
            rev = (d.revision or "").lower()
            if not rev:
                continue
            key = doc_series(d)
            if key.startswith("jesd") and re.search(r"\b" + re.escape(key) + r"\s?" + re.escape(rev) + r"\b", low):
                out.append(d.id)
                continue
            named = bool(ents & {e.lower() for e in d.entities}) or (key and all(t in low for t in key.split()))
            if named and re.search(r"\b(?:rev(?:ision)?|r|version|ver)\.?\s*" + re.escape(rev) + r"(?![\w.])", low):
                out.append(d.id)
        return out

    def resolve_scope(self, plan: Optional[QueryPlan], domains: Optional[Sequence[str]] = None,
                      doc_ids: Optional[Sequence[int]] = None, entity_filter: bool = True,
                      domain_routing: bool = True, standard: str = "",
                      entities: Optional[Sequence[str]] = None) -> Scope:
        docs = self.docs()
        wanted = list(entities) if entities else ([standard] if standard else (plan.entities if plan else []))
        text = f"{plan.question} {plan.standalone}" if plan else standard
        explicit = self.revision_mentions(text, wanted)
        pinned = {doc_series(docs[i]) for i in explicit}
        notes: list[str] = []

        def usable(d: DocumentRow) -> bool:
            if d.id in explicit:
                return True
            if d.doc_type == "base" and doc_series(d) in pinned:
                return False  # another revision of a document whose revision the user named
            return d.superseded_by is None

        pool = [d for d in docs.values() if usable(d)]
        for i in explicit:
            d = docs[i]
            notes.append(f"'{d.title}' (rev {d.revision}) is used because the question names that revision"
                         + (" although a newer revision is loaded" if d.superseded_by else ""))

        def tagged(ds: Sequence[DocumentRow], e: str) -> list[int]:
            return [d.id for d in ds if e.lower() in {x.lower() for x in d.entities}]

        if doc_ids:
            sel = [docs[i] for i in doc_ids if i in docs]
            sc = Scope(doc_ids=list(doc_ids), reason="user", notes=notes, explicit_revisions=explicit)
            if wanted:
                sc.entities = [e for e in wanted if tagged(sel, e)]
                sc.missing = [e for e in wanted if not tagged(sel, e)]
                if sc.missing:
                    sc.notes.append(f"none of the selected documents is tagged {', '.join(sc.missing)}")
            return sc
        if domains:
            pool = [d for d in pool if d.domain in set(domains)]
            if not (entity_filter and wanted):
                return Scope(doc_ids=[d.id for d in pool], domains=list(domains), reason="user", notes=notes,
                             explicit_revisions=explicit)
        if entity_filter and wanted:
            per = {e: tagged(pool, e) for e in wanted}
            found = {e: ids for e, ids in per.items() if ids}
            missing = [e for e in wanted if not per[e]]
            ids = sorted({i for v in found.values() for i in v})
            return Scope(doc_ids=ids, domains=list(domains) if domains else None,
                         reason="entity" if found else "entity_missing", entities=list(found), missing=missing,
                         per_entity=found, notes=notes, explicit_revisions=explicit)
        if domain_routing and plan and plan.domains:
            ids = [d.id for d in pool if d.domain in set(plan.domains)]
            if ids:
                return Scope(doc_ids=ids, domains=list(plan.domains), reason="domain", notes=notes,
                             explicit_revisions=explicit)
        restricted = len(pool) != len(docs)
        return Scope(doc_ids=[d.id for d in pool] if restricted else None, reason="all", notes=notes,
                     explicit_revisions=explicit)


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

    def _rank(self, plan: QueryPlan, scope: Scope, timings: dict) -> tuple[list[tuple[ChunkRow, float]], Optional[float]]:
        rc = self.cfg.retrieval
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
            timings["rerank"] = round(timings.get("rerank", 0) + time.time() - t, 3)
        return ranked, confidence

    def search(self, plan: QueryPlan, domains: Optional[Sequence[str]] = None,
               doc_ids: Optional[Sequence[int]] = None, top_k: Optional[int] = None, standard: str = "",
               budget_tokens: Optional[int] = None, with_parameters: bool = True,
               scope: Optional[Scope] = None) -> RetrievalResult:
        rc = self.cfg.retrieval
        timings: dict = {}
        if scope is None:
            scope = self.catalog.resolve_scope(plan, domains, doc_ids, rc.entity_filter, rc.domain_routing, standard)
        if scope.empty:
            return RetrievalResult(plan, [], scope, [], None, 0, timings)
        k = top_k or rc.final_top_k
        budget = budget_tokens or int(self.cfg.llm.context_tokens * 0.6)
        if len(scope.per_entity) > 1:
            # One evidence pool per named standard: a comparison never answers one side from the other.
            per_k = max(2, -(-k // len(scope.per_entity)))
            selected: list[tuple[ChunkRow, float]] = []
            confidence = None
            n = 0
            for ent, ids in scope.per_entity.items():
                ranked, conf = self._rank(plan, Scope(doc_ids=ids, reason="entity", entities=[ent]), timings)
                selected += ranked[:per_k]
                n += len(ranked)
                if conf is not None:
                    confidence = conf if confidence is None else min(confidence, conf)
            passages = self.build_passages(selected, budget)
            params: list[ParameterRow] = []
            if with_parameters and plan.wants_parameters:
                for ids in scope.per_entity.values():
                    params += self.parameters(plan, Scope(doc_ids=ids, reason="entity"),
                                              k=max(3, rc.parameter_rows // len(scope.per_entity)))
            return RetrievalResult(plan, passages, scope, params, confidence, n, timings)

        ranked, confidence = self._rank(plan, scope, timings)
        if not ranked and scope.reason == "domain":
            # Collection routing is a guess from keywords (not a standard the user named): widen, and say so.
            wider = self.catalog.resolve_scope(None, entity_filter=False, domain_routing=False)
            wider.widened = True
            wider.notes = scope.notes + [f"nothing matched in collection(s) {', '.join(scope.domains or [])}; "
                                         "searched the whole library"]
            scope = wider
            ranked, confidence = self._rank(plan, scope, timings)
        passages = self.build_passages(ranked[:k], budget)
        params = []
        if with_parameters and plan.wants_parameters:
            params = self.parameters(plan, scope)
        return RetrievalResult(plan, passages, scope, params, confidence, len(ranked), timings)

    def parameters(self, plan: QueryPlan, scope: Scope, k: Optional[int] = None) -> list[ParameterRow]:
        k = k or self.cfg.retrieval.parameter_rows
        if scope.empty:
            return []
        query = " ".join(plan.parameters + plan.keywords[:6] + [plan.english])
        return self.store.search_parameters(query, k, scope.doc_ids, verified_only=True)

    # ------------------------------------------------------------- passages
    def _evidence_rows(self, selected: list[tuple[ChunkRow, float]]) -> list[tuple[ChunkRow, float]]:
        """Search-only hits (unverified VLM tables) are replaced by the original text of their page."""
        out: list[tuple[ChunkRow, float]] = []
        seen: set[tuple[int, int]] = set()
        for row, score in selected:
            rows = [row] if row.evidence else self.store.page_chunks(row.doc_id, row.page_start)
            for r in rows:
                if (r.doc_id, r.ordinal) not in seen:
                    seen.add((r.doc_id, r.ordinal))
                    out.append((r, score))
        return out

    def build_passages(self, selected: list[tuple[ChunkRow, float]], budget: int) -> list[Passage]:
        rc = self.cfg.retrieval
        docs = self.catalog.docs()
        selected = self._evidence_rows(selected)
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
                if nkey in included or not nb.evidence:
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
