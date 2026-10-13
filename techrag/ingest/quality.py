"""Indexing quality: what happened to every document and every indexing run, kept in the index (schema v5).

Per document (the last time it was processed): status ok | partial | failed, the stage that failed, every issue
with its PDF page / table, the VLM page accounting (selected, answered with tables, examined without a table,
failed, filtered out by the cheap table score, over the page limit, VLM switched off) and the table / parameter
row verification counts. Per run: the counts over all documents. Issues are stored as codes (the UI words them
in Turkish or English) plus a short technical detail with server addresses, keys and tokens removed.

Nothing here is a measure of answer accuracy: the counts describe the indexing only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# fatal stages: the document was not (re)written
FATAL_STAGES = ("read", "parse", "embedding", "index_write", "other")
# issues that leave a document partially processed (a retry may fix them)
PARTIAL_CODES = {"vlm_failed", "metadata_failed"}
# issues that need something else than a retry (OCR, settings)
NOTE_CODES = {"no_text", "vlm_disabled", "vlm_over_limit", "table_not_grounded", "rows_unverified",
              "table_match_uncertain"}

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>)\]]+", re.I)
_SECRET = re.compile(r"(?i)\b(bearer|api[_-]?key|x-api-key|token|authorization|password|passwd|secret)\b"
                     r"(\s*[:=]\s*|\s+)[^\s,;'\"]+")
_KEYLIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|\b[A-Za-z0-9_-]{32,}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?\b")
_HOSTPORT = re.compile(r"\b[A-Za-z][\w.-]*:\d{2,5}\b")


def scrub(text: object, limit: int = 300) -> str:
    """An error text as it may be stored and shown in the report: no server address, key or token."""
    t = str(text or "")
    t = _SECRET.sub(lambda m: f"{m.group(1)} <redacted>", t)
    t = _URL.sub("<server>", t)
    t = _KEYLIKE.sub("<redacted>", t)
    t = _IP.sub("<address>", t)
    t = _HOSTPORT.sub("<server>", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IngestFailure(Exception):
    """A document could not be processed; `stage` says where (read | parse | embedding | index_write)."""

    def __init__(self, stage: str, cause: BaseException):
        super().__init__(f"{cause.__class__.__name__}: {cause}")
        self.stage = stage
        self.cause = cause


class RetryNotBetter(Exception):
    """A retry would replace a document with a result that has more failed pages: the existing index is kept."""


def failure_stage(exc: BaseException) -> str:
    return exc.stage if isinstance(exc, IngestFailure) else "other"


@dataclass
class DocQuality:
    """Built while one document is processed."""
    pdf: bool = False
    n_pages: int = 0
    vlm: dict = field(default_factory=dict)
    tables: dict = field(default_factory=lambda: {"extracted": 0, "grounded": 0, "not_grounded": 0, "rows": 0,
                                                  "rows_verified": 0, "rows_unverified": 0, "items": []})
    issues: list = field(default_factory=list)
    chunks: int = 0
    embedding: dict = field(default_factory=lambda: {"embedded": 0, "reused": 0})

    def issue(self, code: str, stage: str, page: Optional[int] = None, table: str = "", detail: str = "",
              **extra) -> None:
        it = {"code": code, "stage": stage}
        if page is not None:
            it["page"] = page
        if table:
            it["table"] = table[:120]
        if detail:
            it["detail"] = scrub(detail)
        it.update(extra)
        self.issues.append(it)

    @property
    def status(self) -> str:
        return "partial" if any(i["code"] in PARTIAL_CODES for i in self.issues) else "ok"

    def record(self, source: dict, title: str, run_id: Optional[int], retry: Optional[dict] = None) -> dict:
        status = self.status
        data = {"title": title, "pdf": self.pdf, "n_pages": self.n_pages, "vlm": self.vlm, "tables": self.tables,
                "issues": self.issues, "chunks": self.chunks, "embedding": self.embedding,
                "source": _source(source)}
        if retry:
            data["retry"] = retry
        ts = now()
        return {"run_id": run_id, "status": status, "stage": "", "processed_at": ts, "last_ok_at": ts,
                "index_kept": False, "needs_reprocess": status == "partial", "data": data}


def _source(source: Optional[dict]) -> dict:
    src = dict(source or {})
    return {k: src.get(k) for k in ("root_id", "rel_path", "bucket", "bucket_id", "subpath") if k in src}


def failure_record(exc: BaseException, source: dict, title: str, run_id: Optional[int], previous: Optional[dict],
                   indexed_at: str = "") -> dict:
    """A document that could not be processed. Its index rows (if any) were not touched: they stay usable and
    the record says so."""
    stage = failure_stage(exc)
    cause = exc.cause if isinstance(exc, IngestFailure) else exc
    kept = bool(indexed_at)
    last_ok = ""
    if previous and previous.get("status") in ("ok", "partial"):
        last_ok = previous.get("processed_at") or ""
    elif previous:
        last_ok = previous.get("last_ok_at") or ""
    if kept and not last_ok:
        last_ok = indexed_at
    data = {"title": title, "source": _source(source),
            "issues": [{"code": f"{stage}_failed", "stage": stage,
                        "detail": scrub(f"{cause.__class__.__name__}: {cause}")}]}
    if previous and previous.get("data"):  # what the usable index still holds (from its last processing)
        prev = previous["data"]
        data["previous"] = {k: prev.get(k) for k in ("vlm", "tables", "chunks") if prev.get(k) is not None}
    return {"run_id": run_id, "status": "failed", "stage": stage, "processed_at": now(), "last_ok_at": last_ok,
            "index_kept": kept, "needs_reprocess": True, "data": data}


def empty_summary() -> dict:
    return {"processed": 0, "added": 0, "updated": 0, "skipped_unchanged": 0, "reused": 0, "partial": 0,
            "failed": 0, "unreachable": 0, "missing": 0,
            "vlm": {"enabled": None, "documents": 0, "selected": 0, "with_tables": 0, "no_table": 0, "failed": 0,
                    "filtered_out": 0, "over_limit": 0, "cached": 0},
            "tables": {"extracted": 0, "grounded": 0, "not_grounded": 0, "rows": 0, "rows_verified": 0,
                       "rows_unverified": 0},
            "stage_failures": {s: 0 for s in FATAL_STAGES},
            "embedding": {"embedded": 0, "reused": 0}}


def add_document(summary: dict, q: DocQuality) -> None:
    v = q.vlm or {}
    sv = summary["vlm"]
    if v.get("enabled"):
        sv["documents"] += 1
        for k in ("selected", "with_tables", "no_table", "over_limit"):
            sv[k] += len(v.get(k) or [])
        sv["failed"] += len(v.get("failed") or {})
        sv["filtered_out"] += int(v.get("filtered_out") or 0)
        sv["cached"] += int(v.get("cached") or 0)
    for k in summary["tables"]:
        summary["tables"][k] += int(q.tables.get(k) or 0)
    for k in ("embedded", "reused"):
        summary["embedding"][k] += int(q.embedding.get(k) or 0)
