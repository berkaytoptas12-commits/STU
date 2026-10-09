"""Library index: one SQLite file with documents, chunks (+FTS5/BM25), embeddings, VLM-extracted tables and a
typed parameter store (+FTS5).

* Writable libraries use WAL on a local disk.
* A shared library is opened read-only and immutable (no locks, safe on network shares).
* ``publish`` writes a clean single-file copy (VACUUM INTO) plus the sources and VLM cache.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import numpy as np

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    n_pages INTEGER,
    n_chunks INTEGER,
    entities TEXT DEFAULT '[]',
    doc_type TEXT DEFAULT 'base',
    revision TEXT DEFAULT '',
    doc_date TEXT DEFAULT '',
    superseded_by INTEGER,
    toc TEXT,
    metadata TEXT,
    warnings TEXT,
    ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    domain TEXT NOT NULL,
    section TEXT,
    page_start INTEGER,
    page_end INTEGER,
    kind TEXT,
    overlap INTEGER DEFAULT 0,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, section, title, tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS tables (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER,
    caption TEXT,
    section TEXT,
    markdown TEXT,
    data TEXT,
    source TEXT,
    verified_ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_tables_doc ON tables(doc_id, page);
CREATE TABLE IF NOT EXISTS parameters (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    table_id INTEGER REFERENCES tables(id) ON DELETE CASCADE,
    domain TEXT,
    page INTEGER,
    section TEXT,
    caption TEXT,
    parameter TEXT,
    symbol TEXT,
    min TEXT, typ TEXT, max TEXT,
    unit TEXT,
    conditions TEXT,
    notes TEXT,
    min_si REAL, typ_si REAL, max_si REAL,
    base_unit TEXT,
    verified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_params_doc ON parameters(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS params_fts USING fts5(
    parameter, symbol, conditions, notes, caption, section, title,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

_STOPWORDS = set("""
a an and are as at be by can do does for from has have how i in is it its of on or shall should that the
their there these this to was what when where which who why will with within without would you your
acaba ama ancak bana bazı belirtilen ben bir biri birkaç bu buna bunda bunu bunun da daha de değil diye
en gibi göre hangi hangisi hem için ile ise kaç kadar ki mi mı mu mü na nasıl ne neden nedir nelerdir
nerede o olan olarak olur olursa on ona onu onun sadece şu şey ve veya ya yani yine çok açıkla anlat
söyle ver verir midir mıdır olmalı olmalıdır gerekir lütfen
""".split())


@dataclass
class ChunkRow:
    id: int
    doc_id: int
    ordinal: int
    domain: str
    section: str
    page_start: int
    page_end: int
    kind: str
    overlap: int
    text: str
    doc_title: str = ""
    doc_path: str = ""


@dataclass
class DocumentRow:
    id: int
    path: str
    title: str
    domain: str
    sha256: str
    n_pages: int
    n_chunks: int
    entities: list
    doc_type: str
    revision: str
    doc_date: str
    superseded_by: Optional[int]
    toc: list
    metadata: dict
    warnings: list
    ingested_at: str

    def to_dict(self, with_toc: bool = False) -> dict:
        d = asdict(self)
        if not with_toc:
            d.pop("toc")
        return d


@dataclass
class ParameterRow:
    id: int
    doc_id: int
    table_id: Optional[int]
    domain: str
    page: int
    section: str
    caption: str
    parameter: str
    symbol: str
    min: str
    typ: str
    max: str
    unit: str
    conditions: str
    notes: str
    verified: bool
    doc_title: str = ""

    def line(self) -> str:
        vals = " | ".join(f"{k}={v}" for k, v in (("min", self.min), ("typ", self.typ), ("max", self.max)) if v)
        parts = [self.parameter + (f" ({self.symbol})" if self.symbol else ""), vals or "-", self.unit or ""]
        if self.conditions:
            parts.append(f"conditions: {self.conditions}")
        if self.notes:
            parts.append(f"notes: {self.notes}")
        return " ; ".join(p for p in parts if p)


@dataclass
class TableRow:
    id: int
    doc_id: int
    page: int
    caption: str
    section: str
    markdown: str
    source: str
    verified_ratio: float
    doc_title: str = ""


def sqlite_ro_uri(path: Path) -> str:
    r"""Read-only, lock-free SQLite URI for a local, drive-letter or UNC path.

    Path.as_uri() turns a UNC share (\\server\share) into file://server/..., which SQLite rejects;
    file:////server/share/... (empty authority) is the accepted form.
    """
    return uri_for_resolved(str(Path(path).resolve()))


def uri_for_resolved(resolved: str) -> str:
    from urllib.parse import quote

    p = resolved.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p  # C:/lib -> /C:/lib
    return "file://" + quote(p, safe="/:") + "?mode=ro&immutable=1"


def fts_query(text: str, max_terms: int = 48) -> str:
    """Free text -> safe FTS5 OR-query of quoted terms (stopwords and 1-char tokens dropped)."""
    seen: list[str] = []
    for tok in re.findall(r"\w+", text.lower()):
        if len(tok) < 2 or tok in _STOPWORDS or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in seen)


class VectorIndex:
    def __init__(self, ids: np.ndarray, matrix: np.ndarray, domains: np.ndarray, doc_ids: np.ndarray):
        self.ids, self.matrix, self.domains, self.doc_ids = ids, matrix, domains, doc_ids

    @property
    def size(self) -> int:
        return len(self.ids)

    def mask(self, domains: Optional[Sequence[str]], doc_ids: Optional[Sequence[int]]) -> Optional[np.ndarray]:
        m = None
        if domains:
            m = np.isin(self.domains, list(domains))
        if doc_ids is not None:
            dm = np.isin(self.doc_ids, list(doc_ids))
            m = dm if m is None else (m & dm)
        return m

    def search(self, query: np.ndarray, k: int, mask: Optional[np.ndarray] = None) -> list[tuple[int, float]]:
        if self.size == 0:
            return []
        scores = self.matrix @ query.astype(np.float32)
        if mask is not None:
            scores = np.where(mask, scores, -np.inf)
        k = min(k, self.size)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(int(self.ids[i]), float(scores[i])) for i in top if np.isfinite(scores[i])]


class Store:
    def __init__(self, db_path: str | Path, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = read_only
        self._vectors: Optional[VectorIndex] = None
        self._vectors_version: Optional[str] = None
        self._lock = threading.Lock()
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(f"library index not found: {self.db_path}")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA)
                if not con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone():
                    con.execute("INSERT INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            con = sqlite3.connect(sqlite_ro_uri(self.db_path), uri=True, timeout=60, check_same_thread=False)
        else:
            con = sqlite3.connect(str(self.db_path), timeout=60)
            con.execute("PRAGMA foreign_keys=ON")
        con.row_factory = sqlite3.Row
        try:
            yield con
            if not self.read_only:
                con.commit()
        finally:
            con.close()

    def _writable(self) -> None:
        if self.read_only:
            raise PermissionError("this library is opened read-only")

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, con: Optional[sqlite3.Connection] = None) -> None:
        self._writable()
        sql = "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if con is not None:
            con.execute(sql, (key, value))
        else:
            with self.connect() as c:
                c.execute(sql, (key, value))

    def _bump_version(self, con: sqlite3.Connection) -> None:
        self.set_meta("index_version", datetime.now(timezone.utc).isoformat(timespec="microseconds"), con)

    def check_embedding_model(self, name: str, dim: int) -> None:
        stored, stored_dim = self.get_meta("embedding_model"), self.get_meta("embedding_dim")
        if stored is None:
            self.set_meta("embedding_model", name)
            self.set_meta("embedding_dim", str(dim))
            return
        if stored != name or int(stored_dim or 0) != dim:
            raise RuntimeError(
                f"The library was indexed with embedding model '{stored}' (dim {stored_dim}) but the current "
                f"settings use '{name}' (dim {dim}). Re-index with: techrag ingest --rebuild"
            )

    def reset(self, embedding_name: str, dim: int) -> None:
        self._writable()
        with self.connect() as con:
            for t in ("params_fts", "parameters", "tables", "chunks_fts", "chunks", "documents"):
                con.execute(f"DELETE FROM {t}")
            self.set_meta("embedding_model", embedding_name, con)
            self.set_meta("embedding_dim", str(dim), con)
            self._bump_version(con)

    # ------------------------------------------------------------- documents
    @staticmethod
    def _doc(r: sqlite3.Row) -> DocumentRow:
        return DocumentRow(
            id=r["id"], path=r["path"], title=r["title"], domain=r["domain"], sha256=r["sha256"],
            n_pages=r["n_pages"] or 0, n_chunks=r["n_chunks"] or 0, entities=json.loads(r["entities"] or "[]"),
            doc_type=r["doc_type"] or "base", revision=r["revision"] or "", doc_date=r["doc_date"] or "",
            superseded_by=r["superseded_by"], toc=json.loads(r["toc"] or "[]"),
            metadata=json.loads(r["metadata"] or "{}"), warnings=json.loads(r["warnings"] or "[]"),
            ingested_at=r["ingested_at"] or "",
        )

    def documents(self) -> list[DocumentRow]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM documents ORDER BY domain, title").fetchall()
        return [self._doc(r) for r in rows]

    def document(self, doc_id: int) -> Optional[DocumentRow]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return self._doc(row) if row else None

    def document_by_path(self, path: str) -> Optional[DocumentRow]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
        return self._doc(row) if row else None

    def delete_document(self, doc_id: int, con: Optional[sqlite3.Connection] = None) -> None:
        self._writable()

        def _delete(c: sqlite3.Connection):
            c.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
            c.execute("DELETE FROM params_fts WHERE rowid IN (SELECT id FROM parameters WHERE doc_id=?)", (doc_id,))
            c.execute("DELETE FROM parameters WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM tables WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            c.execute("UPDATE documents SET superseded_by=NULL WHERE superseded_by=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self._bump_version(c)

        if con is not None:
            _delete(con)
        else:
            with self.connect() as c:
                _delete(c)

    def replace_document(self, *, path: str, title: str, domain: str, sha256: str, n_pages: int,
                         toc: list, metadata: dict, warnings: list, chunks: Sequence[dict],
                         embeddings: np.ndarray, entities: Sequence[str] = (), doc_type: str = "base",
                         revision: str = "", doc_date: str = "", tables: Sequence[dict] = ()) -> int:
        """Atomically (re)write a document with chunks, FTS rows, embeddings, tables and parameters.

        tables: [{"page", "caption", "section", "markdown", "data", "source", "verified_ratio",
                  "parameters": [{"parameter", "symbol", "min", "typ", "max", "unit", "conditions", "notes",
                                  "min_si", "typ_si", "max_si", "base_unit", "verified"}]}]
        """
        self._writable()
        if len(chunks) != len(embeddings):
            raise ValueError("chunks/embeddings length mismatch")
        with self.connect() as con:
            old = con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
            if old:
                self.delete_document(old["id"], con)
            cur = con.execute(
                "INSERT INTO documents(path, title, domain, sha256, n_pages, n_chunks, entities, doc_type, revision,"
                " doc_date, toc, metadata, warnings, ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, title, domain, sha256, n_pages, len(chunks), json.dumps(list(entities)), doc_type, revision,
                 doc_date, json.dumps(toc, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False, default=str),
                 json.dumps(warnings, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            doc_id = cur.lastrowid
            emb16 = np.asarray(embeddings, dtype=np.float16)
            for c, vec in zip(chunks, emb16):
                cur = con.execute(
                    "INSERT INTO chunks(doc_id, ordinal, domain, section, page_start, page_end, kind, overlap, text,"
                    " embedding) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, c["ordinal"], domain, c["section"], c["page_start"], c["page_end"], c["kind"],
                     c.get("overlap", 0), c["text"], vec.tobytes()),
                )
                con.execute("INSERT INTO chunks_fts(rowid, text, section, title) VALUES(?,?,?,?)",
                            (cur.lastrowid, c["text"], c["section"], title))
            for t in tables:
                cur = con.execute(
                    "INSERT INTO tables(doc_id, page, caption, section, markdown, data, source, verified_ratio)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (doc_id, t["page"], t.get("caption", ""), t.get("section", ""), t.get("markdown", ""),
                     json.dumps(t.get("data"), ensure_ascii=False), t.get("source", ""), t.get("verified_ratio")),
                )
                table_id = cur.lastrowid
                for p in t.get("parameters", []):
                    cur = con.execute(
                        "INSERT INTO parameters(doc_id, table_id, domain, page, section, caption, parameter, symbol,"
                        " min, typ, max, unit, conditions, notes, min_si, typ_si, max_si, base_unit, verified)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (doc_id, table_id, domain, t["page"], t.get("section", ""), t.get("caption", ""),
                         p.get("parameter", ""), p.get("symbol", ""), p.get("min", ""), p.get("typ", ""),
                         p.get("max", ""), p.get("unit", ""), p.get("conditions", ""), p.get("notes", ""),
                         p.get("min_si"), p.get("typ_si"), p.get("max_si"), p.get("base_unit", ""),
                         1 if p.get("verified") else 0),
                    )
                    con.execute(
                        "INSERT INTO params_fts(rowid, parameter, symbol, conditions, notes, caption, section, title)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (cur.lastrowid, p.get("parameter", ""), p.get("symbol", ""), p.get("conditions", ""),
                         p.get("notes", ""), t.get("caption", ""), t.get("section", ""), title),
                    )
            self._bump_version(con)
        return doc_id

    def set_superseded(self, pairs: Iterable[tuple[int, Optional[int]]]) -> None:
        self._writable()
        with self.connect() as con:
            for doc_id, newer in pairs:
                con.execute("UPDATE documents SET superseded_by=? WHERE id=?", (newer, doc_id))
            self._bump_version(con)

    # ---------------------------------------------------------------- chunks
    _CHUNK_SELECT = (
        "SELECT c.id, c.doc_id, c.ordinal, c.domain, c.section, c.page_start, c.page_end, c.kind, c.overlap,"
        " c.text, d.title AS doc_title, d.path AS doc_path FROM chunks c JOIN documents d ON d.id = c.doc_id"
    )

    @staticmethod
    def _chunk(r: sqlite3.Row) -> ChunkRow:
        return ChunkRow(r["id"], r["doc_id"], r["ordinal"], r["domain"], r["section"] or "", r["page_start"],
                        r["page_end"], r["kind"], r["overlap"] or 0, r["text"], r["doc_title"], r["doc_path"])

    def chunks_by_ids(self, ids: Iterable[int]) -> dict[int, ChunkRow]:
        ids = list(dict.fromkeys(int(i) for i in ids))
        out: dict[int, ChunkRow] = {}
        with self.connect() as con:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                for r in con.execute(f"{self._CHUNK_SELECT} WHERE c.id IN ({','.join('?' * len(part))})", part):
                    out[r["id"]] = self._chunk(r)
        return out

    def neighbors(self, doc_id: int, ordinal_from: int, ordinal_to: int) -> list[ChunkRow]:
        with self.connect() as con:
            rows = con.execute(
                f"{self._CHUNK_SELECT} WHERE c.doc_id=? AND c.ordinal BETWEEN ? AND ? ORDER BY c.ordinal",
                (doc_id, ordinal_from, ordinal_to)).fetchall()
        return [self._chunk(r) for r in rows]

    def section_chunks(self, doc_id: int, section: str, limit: int = 60) -> list[ChunkRow]:
        with self.connect() as con:
            rows = con.execute(
                f"{self._CHUNK_SELECT} WHERE c.doc_id=? AND c.section=? ORDER BY c.ordinal LIMIT ?",
                (doc_id, section, limit)).fetchall()
        return [self._chunk(r) for r in rows]

    def page_chunks(self, doc_id: int, page: int) -> list[ChunkRow]:
        with self.connect() as con:
            rows = con.execute(
                f"{self._CHUNK_SELECT} WHERE c.doc_id=? AND c.page_start<=? AND c.page_end>=? ORDER BY c.ordinal",
                (doc_id, page, page)).fetchall()
        return [self._chunk(r) for r in rows]

    # -------------------------------------------------------- tables / params
    _PARAM_SELECT = ("SELECT p.*, d.title AS doc_title FROM parameters p JOIN documents d ON d.id = p.doc_id")

    @staticmethod
    def _param(r: sqlite3.Row) -> ParameterRow:
        return ParameterRow(r["id"], r["doc_id"], r["table_id"], r["domain"] or "", r["page"] or 0,
                            r["section"] or "", r["caption"] or "", r["parameter"] or "", r["symbol"] or "",
                            r["min"] or "", r["typ"] or "", r["max"] or "", r["unit"] or "", r["conditions"] or "",
                            r["notes"] or "", bool(r["verified"]), r["doc_title"])

    def search_parameters(self, query: str, k: int, doc_ids: Optional[Sequence[int]] = None,
                          domains: Optional[Sequence[str]] = None, verified_only: bool = False) -> list[ParameterRow]:
        match = fts_query(query)
        if not match:
            return []
        sql = (f"{self._PARAM_SELECT} JOIN params_fts f ON f.rowid = p.id WHERE params_fts MATCH ?")
        params: list = [match]
        if doc_ids is not None:
            sql += f" AND p.doc_id IN ({','.join('?' * len(doc_ids)) or 'NULL'})"
            params += list(doc_ids)
        if domains:
            sql += f" AND p.domain IN ({','.join('?' * len(domains))})"
            params += list(domains)
        if verified_only:
            sql += " AND p.verified = 1"
        sql += " ORDER BY bm25(params_fts, 3.0, 3.0, 1.0, 0.5, 1.0, 0.5, 0.3) LIMIT ?"
        params.append(k)
        with self.connect() as con:
            return [self._param(r) for r in con.execute(sql, params).fetchall()]

    def parameters_by_ids(self, ids: Iterable[int]) -> list[ParameterRow]:
        ids = list(ids)
        if not ids:
            return []
        with self.connect() as con:
            rows = con.execute(f"{self._PARAM_SELECT} WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return [self._param(r) for r in rows]

    def tables_for(self, doc_id: Optional[int] = None, query: str = "", doc_ids: Optional[Sequence[int]] = None,
                   limit: int = 5) -> list[TableRow]:
        sql = ("SELECT t.*, d.title AS doc_title FROM tables t JOIN documents d ON d.id = t.doc_id WHERE 1=1")
        params: list = []
        if doc_id is not None:
            sql += " AND t.doc_id=?"
            params.append(doc_id)
        if doc_ids is not None:
            sql += f" AND t.doc_id IN ({','.join('?' * len(doc_ids)) or 'NULL'})"
            params += list(doc_ids)
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        out = [TableRow(r["id"], r["doc_id"], r["page"], r["caption"] or "", r["section"] or "",
                        r["markdown"] or "", r["source"] or "", r["verified_ratio"] or 0.0, r["doc_title"])
               for r in rows]
        if query:
            terms = [t for t in re.findall(r"\w+", query.lower()) if t not in _STOPWORDS and len(t) > 1]
            ref = re.search(r"\b(?:table|tablo)\s*([\w.\-]+)", query, re.I)

            def score(t: TableRow) -> float:
                cap = f"{t.caption} {t.section}".lower()
                s = sum(2.0 for w in terms if w in cap) + sum(0.2 for w in terms if w in t.markdown.lower())
                if ref and re.search(r"\b" + re.escape(ref.group(1).lower()) + r"\b", cap):
                    s += 10
                return s

            out = sorted((t for t in out if score(t) > 0), key=score, reverse=True)
        return out[:limit]

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict:
        with self.connect() as con:
            docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            params = con.execute("SELECT COUNT(*) FROM parameters").fetchone()[0]
            tables = con.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
            per = con.execute(
                "SELECT d.domain, COUNT(*) AS docs, SUM(d.n_chunks) AS chunks FROM documents d GROUP BY d.domain"
            ).fetchall()
        return {
            "documents": docs, "chunks": chunks, "tables": tables, "parameters": params,
            "per_domain": {r["domain"]: {"documents": r["docs"], "chunks": r["chunks"] or 0} for r in per},
            "embedding_model": self.get_meta("embedding_model"),
            "embedding_dim": self.get_meta("embedding_dim"),
            "index_version": self.get_meta("index_version"),
            "read_only": self.read_only,
        }

    # ---------------------------------------------------------------- search
    def search_bm25(self, query: str, k: int, domains: Optional[Sequence[str]] = None,
                    doc_ids: Optional[Sequence[int]] = None) -> list[tuple[int, float]]:
        match = fts_query(query)
        if not match:
            return []
        sql = ("SELECT c.id AS id, bm25(chunks_fts, 1.0, 0.6, 0.3) AS score FROM chunks_fts"
               " JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
        params: list = [match]
        if domains:
            sql += f" AND c.domain IN ({','.join('?' * len(domains))})"
            params += list(domains)
        if doc_ids is not None:
            sql += f" AND c.doc_id IN ({','.join('?' * len(doc_ids)) or 'NULL'})"
            params += list(doc_ids)
        sql += " ORDER BY score LIMIT ?"
        params.append(k)
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [(r["id"], -float(r["score"])) for r in rows]

    def vectors(self) -> VectorIndex:
        version = self.get_meta("index_version")
        with self._lock:
            if self._vectors is None or version != self._vectors_version:
                self._vectors = self._load_vectors()
                self._vectors_version = version
            return self._vectors

    def _load_vectors(self) -> VectorIndex:
        dim = int(self.get_meta("embedding_dim") or 0)
        with self.connect() as con:
            rows = con.execute("SELECT id, domain, doc_id, embedding FROM chunks ORDER BY id").fetchall()
        if not rows or not dim:
            return VectorIndex(np.zeros(0, np.int64), np.zeros((0, max(dim, 1)), np.float32),
                               np.zeros(0, object), np.zeros(0, np.int64))
        matrix = np.empty((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            matrix[i] = np.frombuffer(r["embedding"], dtype=np.float16)
        return VectorIndex(np.array([r["id"] for r in rows], dtype=np.int64), matrix,
                           np.array([r["domain"] for r in rows], dtype=object),
                           np.array([r["doc_id"] for r in rows], dtype=np.int64))

    # --------------------------------------------------------------- publish
    def publish(self, dest_dir: str | Path, sources_dir: Path, vlm_cache_dir: Optional[Path] = None) -> Path:
        """Write a self-contained copy of the library: clean index (VACUUM INTO) + sources + VLM cache."""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "index.sqlite"
        if target.exists():
            target.unlink()
        with self.connect() as con:
            con.execute("VACUUM INTO ?", (str(target),))
        with sqlite3.connect(str(target)) as con:
            con.execute("PRAGMA journal_mode=DELETE")
        if sources_dir.exists():
            shutil.copytree(sources_dir, dest / "sources", dirs_exist_ok=True)
        if vlm_cache_dir and vlm_cache_dir.exists():
            shutil.copytree(vlm_cache_dir, dest / "cache" / "vlm", dirs_exist_ok=True)
        return target
