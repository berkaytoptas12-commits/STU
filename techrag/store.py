"""Index storage: one SQLite file holding documents, chunks, an FTS5 (BM25) index and the embeddings.

A single file is easy to copy across an air gap, back up, and inspect. Embeddings are kept as float16
blobs and loaded into an in-memory matrix for exact (brute-force) cosine search, which is fast enough
for a few hundred thousand chunks on a CPU.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import numpy as np

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    n_pages INTEGER,
    n_chunks INTEGER,
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
    toc: list
    metadata: dict
    warnings: list
    ingested_at: str


def fts_query(text: str, max_terms: int = 48) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms (stopwords and 1-char tokens dropped)."""
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
        self.ids = ids
        self.matrix = matrix
        self.domains = domains
        self.doc_ids = doc_ids

    @property
    def size(self) -> int:
        return len(self.ids)

    def mask(self, domains: Optional[Sequence[str]], doc_ids: Optional[Sequence[int]]) -> Optional[np.ndarray]:
        m = None
        if domains:
            m = np.isin(self.domains, list(domains))
        if doc_ids:
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
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._vectors: Optional[VectorIndex] = None
        self._vectors_version: Optional[str] = None
        self._lock = threading.Lock()
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.db_path), timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, con: Optional[sqlite3.Connection] = None) -> None:
        sql = "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if con is not None:
            con.execute(sql, (key, value))
        else:
            with self.connect() as c:
                c.execute(sql, (key, value))

    def _bump_version(self, con: sqlite3.Connection) -> None:
        self.set_meta("index_version", datetime.now(timezone.utc).isoformat(timespec="microseconds"), con)

    def reset(self, embedding_name: str, dim: int) -> None:
        """Drop every document and bind the (empty) index to a new embedding model."""
        with self.connect() as con:
            con.execute("DELETE FROM chunks_fts")
            con.execute("DELETE FROM chunks")
            con.execute("DELETE FROM documents")
            self.set_meta("embedding_model", embedding_name, con)
            self.set_meta("embedding_dim", str(dim), con)
            self._bump_version(con)

    def check_embedding_model(self, name: str, dim: int) -> None:
        """Refuse to mix vectors from different embedding models in one index."""
        stored = self.get_meta("embedding_model")
        stored_dim = self.get_meta("embedding_dim")
        if stored is None:
            self.set_meta("embedding_model", name)
            self.set_meta("embedding_dim", str(dim))
            return
        if stored != name or int(stored_dim or 0) != dim:
            raise RuntimeError(
                f"Index was built with embedding model '{stored}' (dim {stored_dim}) but the current "
                f"config uses '{name}' (dim {dim}). Rebuild with: techrag ingest --rebuild"
            )

    # ------------------------------------------------------------- documents
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

    @staticmethod
    def _doc(r: sqlite3.Row) -> DocumentRow:
        return DocumentRow(
            id=r["id"], path=r["path"], title=r["title"], domain=r["domain"], sha256=r["sha256"],
            n_pages=r["n_pages"] or 0, n_chunks=r["n_chunks"] or 0, toc=json.loads(r["toc"] or "[]"),
            metadata=json.loads(r["metadata"] or "{}"), warnings=json.loads(r["warnings"] or "[]"),
            ingested_at=r["ingested_at"] or "",
        )

    def delete_document(self, doc_id: int, con: Optional[sqlite3.Connection] = None) -> None:
        def _delete(c: sqlite3.Connection):
            c.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self._bump_version(c)

        if con is not None:
            _delete(con)
        else:
            with self.connect() as c:
                _delete(c)

    def replace_document(self, *, path: str, title: str, domain: str, sha256: str, n_pages: int,
                         toc: list, metadata: dict, warnings: list, chunks: Sequence[dict],
                         embeddings: np.ndarray) -> int:
        """Atomically (re)write a document with its chunks, FTS rows and embeddings."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks/embeddings length mismatch")
        with self.connect() as con:
            old = con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
            if old:
                self.delete_document(old["id"], con)
            cur = con.execute(
                "INSERT INTO documents(path, title, domain, sha256, n_pages, n_chunks, toc, metadata, warnings,"
                " ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (path, title, domain, sha256, n_pages, len(chunks), json.dumps(toc, ensure_ascii=False),
                 json.dumps(metadata, ensure_ascii=False, default=str), json.dumps(warnings, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
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
            self._bump_version(con)
        return doc_id

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
                q = f"{self._CHUNK_SELECT} WHERE c.id IN ({','.join('?' * len(part))})"
                for r in con.execute(q, part):
                    out[r["id"]] = self._chunk(r)
        return out

    def neighbors(self, doc_id: int, ordinal_from: int, ordinal_to: int) -> list[ChunkRow]:
        with self.connect() as con:
            rows = con.execute(
                f"{self._CHUNK_SELECT} WHERE c.doc_id=? AND c.ordinal BETWEEN ? AND ? ORDER BY c.ordinal",
                (doc_id, ordinal_from, ordinal_to),
            ).fetchall()
        return [self._chunk(r) for r in rows]

    def stats(self) -> dict:
        with self.connect() as con:
            docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            per = con.execute(
                "SELECT d.domain, COUNT(DISTINCT d.id) AS docs, COUNT(c.id) AS chunks FROM documents d"
                " LEFT JOIN chunks c ON c.doc_id = d.id GROUP BY d.domain ORDER BY d.domain"
            ).fetchall()
        return {
            "documents": docs,
            "chunks": chunks,
            "per_domain": {r["domain"]: {"documents": r["docs"], "chunks": r["chunks"]} for r in per},
            "embedding_model": self.get_meta("embedding_model"),
            "embedding_dim": self.get_meta("embedding_dim"),
            "index_version": self.get_meta("index_version"),
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
        if doc_ids:
            sql += f" AND c.doc_id IN ({','.join('?' * len(doc_ids))})"
            params += list(doc_ids)
        sql += " ORDER BY score LIMIT ?"
        params.append(k)
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        # FTS5 bm25() is "lower is better"; flip the sign so higher is better everywhere.
        return [(r["id"], -float(r["score"])) for r in rows]

    def vectors(self) -> VectorIndex:
        """In-memory embedding matrix, reloaded automatically when the index changes."""
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
        return VectorIndex(
            np.array([r["id"] for r in rows], dtype=np.int64),
            matrix,
            np.array([r["domain"] for r in rows], dtype=object),
            np.array([r["doc_id"] for r in rows], dtype=np.int64),
        )
