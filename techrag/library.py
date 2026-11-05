"""The shared library and the user's personal library, seen as one index.

The shared library may be opened read-only (a network folder); the personal library is the user's own: the files
they pick are copied into their personal folder and indexed there (an index of its own, ``<folder>/_indeks``).
Answering reads both through ``LibraryView``, which has the read side of ``Store``: ids of the personal library are
shifted by ``PERSONAL_OFFSET`` so documents, chunks, tables and parameters of the two never collide, and its buckets
get ids of their own. Indexing always writes to one of the two stores directly, never through the view.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from techrag.config import PERSONAL_INDEX_DIR  # noqa: F401  (re-exported)
from techrag.store import ChunkRow, DocumentRow, ParameterRow, Store, TableRow, VectorIndex

log = logging.getLogger("techrag.library")

PERSONAL_OFFSET = 1_000_000_000      # personal ids in the view: own id + offset
PERSONAL_BUCKET_PREFIX = "kisisel:"  # bucket ids of the personal library in the view
PERSONAL_BUCKET_NAME = "Kişisel"     # the bucket of files directly in the personal folder


def is_personal(doc_id: Optional[int]) -> bool:
    return doc_id is not None and doc_id >= PERSONAL_OFFSET


def _split(ids: Optional[Iterable[int]]) -> tuple[Optional[list[int]], Optional[list[int]]]:
    """Ids of the view -> (shared ids, personal ids); None (no filter) stays None for both."""
    if ids is None:
        return None, None
    ids = [int(i) for i in ids]
    return [i for i in ids if i < PERSONAL_OFFSET], [i - PERSONAL_OFFSET for i in ids if i >= PERSONAL_OFFSET]


def _shift(i: Optional[int]) -> Optional[int]:
    return None if i is None else i + PERSONAL_OFFSET


class MultiVectorIndex:
    """Exact search over the shared and the personal vectors without copying them into one matrix."""

    def __init__(self, parts: Sequence[tuple[VectorIndex, int]]):
        self.parts = [(ix, off) for ix, off in parts if ix.size]

    @property
    def size(self) -> int:
        return sum(ix.size for ix, _ in self.parts)

    def mask(self, domains: Optional[Sequence[str]], doc_ids: Optional[Sequence[int]]) -> list:
        shared, personal = _split(doc_ids)
        return [ix.mask(domains, personal if off else shared) for ix, off in self.parts]

    def search(self, query: np.ndarray, k: int, mask: Optional[list] = None) -> list[tuple[int, float]]:
        hits: list[tuple[int, float]] = []
        for n, (ix, off) in enumerate(self.parts):
            if ix.matrix.shape[1] != query.shape[0]:
                continue  # indexed with another embedding model: only its text search is used
            m = mask[n] if mask is not None else None
            hits += [(cid + off, s) for cid, s in ix.search(query, k, m)]
        hits.sort(key=lambda h: -h[1])
        return hits[:k]


class LibraryView:
    """Read-only view of the shared store plus the personal store (see the module docstring)."""

    def __init__(self, shared: Store, personal: Optional[Store] = None,
                 personal_sources: Optional[Path] = None):
        self.shared = shared
        self.personal = personal
        self.personal_sources = personal_sources

    # ------------------------------------------------------------------ store-like attributes
    @property
    def read_only(self) -> bool:
        return self.shared.read_only

    @property
    def db_path(self) -> Path:
        return self.shared.db_path

    @property
    def has_geometry(self) -> bool:
        return self.shared.has_geometry or bool(self.personal and self.personal.has_geometry)

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if key == "index_version":  # caches over the view refresh when either library changes
            parts = [self.shared.get_meta(key)] + ([self.personal.get_meta(key)] if self.personal else [])
            return "|".join(p or "" for p in parts) if any(parts) else default
        value = self.shared.get_meta(key)
        if value is None and self.personal is not None:
            value = self.personal.get_meta(key)
        return value if value is not None else default

    # ------------------------------------------------------------------ id mapping
    def _route(self, doc_id: int) -> tuple[Optional[Store], int]:
        if is_personal(doc_id):
            return self.personal, doc_id - PERSONAL_OFFSET
        return self.shared, doc_id

    @staticmethod
    def _doc(d: DocumentRow) -> DocumentRow:
        return replace(d, id=d.id + PERSONAL_OFFSET, superseded_by=_shift(d.superseded_by), root_id=None,
                       bucket=d.bucket or PERSONAL_BUCKET_NAME, bucket_id=PERSONAL_BUCKET_PREFIX + d.bucket_id)

    @staticmethod
    def _chunk(c: ChunkRow) -> ChunkRow:
        return replace(c, id=c.id + PERSONAL_OFFSET, doc_id=c.doc_id + PERSONAL_OFFSET)

    @staticmethod
    def _param(p: ParameterRow) -> ParameterRow:
        return replace(p, id=p.id + PERSONAL_OFFSET, doc_id=p.doc_id + PERSONAL_OFFSET, table_id=_shift(p.table_id))

    @staticmethod
    def _table(t: TableRow) -> TableRow:
        return replace(t, id=t.id + PERSONAL_OFFSET, doc_id=t.doc_id + PERSONAL_OFFSET)

    def _chunks(self, store: Optional[Store], rows: list[ChunkRow]) -> list[ChunkRow]:
        return [self._chunk(c) for c in rows] if store is self.personal and store is not None else rows

    # ------------------------------------------------------------------ documents
    def documents(self) -> list[DocumentRow]:
        docs = self.shared.documents()
        if self.personal is not None:
            docs += [self._doc(d) for d in self.personal.documents()]
        return docs

    def document(self, doc_id: int) -> Optional[DocumentRow]:
        store, i = self._route(doc_id)
        d = store.document(i) if store is not None else None
        return self._doc(d) if d is not None and store is self.personal else d

    def legacy_documents(self) -> list[DocumentRow]:
        return [d for d in self.documents() if d.legacy]

    def buckets(self) -> list[dict]:
        out = [dict(b, personal=False) for b in self.shared.buckets()]
        if self.personal is not None:
            out += [dict(b, id=PERSONAL_BUCKET_PREFIX + b["id"], name=b["name"] or PERSONAL_BUCKET_NAME,
                         general=False, personal=True) for b in self.personal.buckets()]
        return out

    def roots(self) -> list[dict]:
        return self.shared.roots()          # the shared library's document folders

    def root(self, root_id: int) -> Optional[dict]:
        return self.shared.root(root_id)

    def source_path(self, doc: DocumentRow, sources_dir: Path) -> Path:
        if is_personal(doc.id) and self.personal is not None:
            own = self.personal.document(doc.id - PERSONAL_OFFSET)
            if own is not None:
                return self.personal.source_path(own, self.personal_sources or sources_dir)
        return self.shared.source_path(doc, sources_dir)

    # ------------------------------------------------------------------ pages
    def page_geom(self, doc_id: int, page: int):
        store, i = self._route(doc_id)
        return store.page_geom(i, page) if store is not None else None

    def page_labels(self, doc_id: int) -> dict[int, str]:
        store, i = self._route(doc_id)
        return store.page_labels(i) if store is not None else {}

    # ------------------------------------------------------------------ chunks
    def chunks_by_ids(self, ids: Iterable[int]) -> dict[int, ChunkRow]:
        shared, personal = _split(ids)
        out = self.shared.chunks_by_ids(shared or [])
        if self.personal is not None and personal:
            out.update({c.id: c for c in map(self._chunk, self.personal.chunks_by_ids(personal).values())})
        return out

    def neighbors(self, doc_id: int, ordinal_from: int, ordinal_to: int) -> list[ChunkRow]:
        store, i = self._route(doc_id)
        return self._chunks(store, store.neighbors(i, ordinal_from, ordinal_to)) if store is not None else []

    def section_chunks(self, doc_id: int, section: str, limit: int = 60) -> list[ChunkRow]:
        store, i = self._route(doc_id)
        return self._chunks(store, store.section_chunks(i, section, limit)) if store is not None else []

    def chunks_containing(self, doc_id: int, needle: str, limit: int = 40) -> list[ChunkRow]:
        store, i = self._route(doc_id)
        return self._chunks(store, store.chunks_containing(i, needle, limit)) if store is not None else []

    def doc_sections(self, doc_id: int) -> list[str]:
        store, i = self._route(doc_id)
        return store.doc_sections(i) if store is not None else []

    def page_chunks(self, doc_id: int, page: int, evidence_only: bool = True) -> list[ChunkRow]:
        store, i = self._route(doc_id)
        return self._chunks(store, store.page_chunks(i, page, evidence_only)) if store is not None else []

    # ------------------------------------------------------------------ tables / parameters
    def search_parameters(self, query: str, k: int, doc_ids: Optional[Sequence[int]] = None,
                          domains: Optional[Sequence[str]] = None, verified_only: bool = False) -> list[ParameterRow]:
        shared, personal = _split(doc_ids)
        rows = self.shared.search_parameters(query, k, shared, domains, verified_only)
        if self.personal is not None and (personal is None or personal):
            own = self.personal.search_parameters(query, k, personal, domains, verified_only)
            rows = _interleave(rows, [self._param(p) for p in own], k)
        return rows

    def parameters_by_ids(self, ids: Iterable[int]) -> list[ParameterRow]:
        shared, personal = _split(ids)
        rows = self.shared.parameters_by_ids(shared or [])
        if self.personal is not None and personal:
            rows += [self._param(p) for p in self.personal.parameters_by_ids(personal)]
        return rows

    def tables_for(self, doc_id: Optional[int] = None, query: str = "", doc_ids: Optional[Sequence[int]] = None,
                   limit: int = 5) -> list[TableRow]:
        if doc_id is not None:
            store, i = self._route(doc_id)
            if store is None:
                return []
            rows = store.tables_for(i, query, None, limit)
            return [self._table(t) for t in rows] if store is self.personal else rows
        shared, personal = _split(doc_ids)
        rows = self.shared.tables_for(None, query, shared, limit)
        if self.personal is not None and (personal is None or personal):
            rows = _interleave(rows, [self._table(t) for t in self.personal.tables_for(None, query, personal, limit)],
                               limit)
        return rows

    # ------------------------------------------------------------------ search
    def search_bm25(self, query: str, k: int, domains: Optional[Sequence[str]] = None,
                    doc_ids: Optional[Sequence[int]] = None) -> list[tuple[int, float]]:
        shared, personal = _split(doc_ids)
        hits = self.shared.search_bm25(query, k, domains, shared)
        if self.personal is not None and (personal is None or personal):
            hits += [(cid + PERSONAL_OFFSET, s) for cid, s in self.personal.search_bm25(query, k, domains, personal)]
            hits.sort(key=lambda h: -h[1])
        return hits[:k]

    def vectors(self):
        shared = self.shared.vectors()
        if self.personal is None:
            return shared
        return MultiVectorIndex([(shared, 0), (self.personal.vectors(), PERSONAL_OFFSET)])

    # ------------------------------------------------------------------ statistics
    def stats(self) -> dict:
        st = self.shared.stats()
        own = self.personal.stats() if self.personal is not None else None
        st["personal"] = {k: (own or {}).get(k, 0) for k in ("documents", "chunks", "tables", "parameters")}
        if own:
            for k in ("documents", "chunks", "tables", "parameters", "verified_parameters", "legacy_documents"):
                st[k] = (st.get(k) or 0) + (own.get(k) or 0)
            for dom, v in own["per_domain"].items():
                cur = st["per_domain"].setdefault(dom, {"documents": 0, "chunks": 0})
                cur["documents"] += v["documents"]
                cur["chunks"] += v["chunks"]
            st["index_version"] = self.get_meta("index_version")
        return st


def _interleave(a: list, b: list, k: int) -> list:
    """Two ranked lists from separate indexes (their scores are not comparable): alternate, best first."""
    out = []
    for i in range(max(len(a), len(b))):
        out += [x[i] for x in (a, b) if i < len(x)]
    return out[:k]
