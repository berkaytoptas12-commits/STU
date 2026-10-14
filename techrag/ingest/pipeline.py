"""Folder -> library index ingestion (incremental).

Sources: the library's own sources folder (files added one by one are copied there) and any number of
document folders the user picked ("roots"), read in place. Each sync scans a folder, plans what changed
(techrag.ingest.sources.plan_changes) and only then spends API calls:
  new / changed content / changed parsing settings -> full processing
  moved or duplicated identical content             -> index rows reused (no API calls)
  touched (same content), restored                  -> bookkeeping only
  missing file                                      -> kept but excluded from answers, reported
  unreachable folder (network share offline)        -> left untouched, reported
Per document: PyMuPDF parse -> cleanup -> metadata -> eager VLM table extraction on table-like pages ->
section assignment -> chunking -> API embeddings -> one atomic write. Supersedence is recomputed after a sync.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import Embedder
from techrag.geometry import norm_tokens
from techrag.ingest.chunker import Chunk, chunk_blocks
from techrag.ingest.cleaning import clean_blocks
from techrag.ingest.loaders import SUPPORTED_SUFFIXES, TABLE_KIND, Block, LoadedDocument, load_document
from techrag.ingest.metadata import build_metadata, compute_supersedence, llm_metadata
from techrag.ingest.quality import (FATAL_STAGES, DocQuality, IngestFailure, RetryNotBetter, add_document,
                                    empty_summary, failure_record, failure_stage, scrub)
from techrag.ingest.sources import PlanItem, plan_changes, scan_root
from techrag.ingest.structure import SECTION_SEP, assign_sections, section_label
from techrag.ingest.vlm import PROMPT_VERSION, ExtractedTable, TableExtractor, is_figure_page, page_candidates
from techrag.llm import LLMClient
from techrag.store import Store

Progress = Callable[[str], None]

class IngestStopped(BaseException):
    """The application is closing. A BaseException, so that per-file error handling never records it as a failed
    document (the remaining documents are simply processed by the next rescan)."""


class WriteGate:
    """Lets a full exit stop background indexing without leaving the index half-written.

    Jobs check `stop` before every document; every index write runs inside `write()`. `close()` waits for the write
    in progress (a document is written in one SQLite transaction) and keeps the lock, so nothing is written after it
    returns True. Documents whose model calls were still running are not written and are picked up again by the next
    rescan as new/changed."""

    def __init__(self):
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def check(self) -> None:
        if self.stop.is_set():
            raise IngestStopped("the application is closing")

    @contextmanager
    def write(self):
        with self._lock:
            self.check()
            yield

    def close(self, timeout: float) -> bool:
        self.stop.set()
        if not self._closed:
            self._closed = self._lock.acquire(timeout=timeout)
        return self._closed


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)        # unchanged, nothing sent to any API
    reused: list[str] = field(default_factory=list)         # moved/duplicated content, index rows reused
    missing: list[str] = field(default_factory=list)        # source file gone: excluded from answers
    unreachable: list[str] = field(default_factory=list)    # folder could not be read: left untouched
    removed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    partial: list[str] = field(default_factory=list)        # processed, but a step (VLM page, metadata) failed
    chunks: int = 0
    tables: int = 0
    parameters: int = 0
    seconds: float = 0.0
    quality: dict = field(default_factory=empty_summary)    # page / table / stage counts (techrag.ingest.quality)
    retry: dict[str, dict] = field(default_factory=dict)    # retry runs: what was retried per document
    run_id: Optional[int] = None

    def summary(self) -> str:
        return (f"added={len(self.added)} updated={len(self.updated)} unchanged={len(self.skipped)} "
                f"reused={len(self.reused)} partial={len(self.partial)} missing={len(self.missing)} "
                f"unreachable={len(self.unreachable)} failed={len(self.failed)} chunks={self.chunks} "
                f"vlm_tables={self.tables} parameters={self.parameters} time={self.seconds:.1f}s")

    def run_summary(self, vlm_enabled: bool) -> dict:
        q = dict(self.quality, processed=len(self.added) + len(self.updated), added=len(self.added),
                 updated=len(self.updated), skipped_unchanged=len(self.skipped), reused=len(self.reused),
                 partial=len(self.partial), failed=len(self.failed), unreachable=len(self.unreachable),
                 missing=len(self.missing), seconds=round(self.seconds, 1))
        q["vlm"] = dict(q["vlm"], enabled=vlm_enabled)
        q["lists"] = {"failed": sorted(self.failed)[:200], "partial": self.partial[:200],
                      "unreachable": self.unreachable[:200], "missing": self.missing[:200]}
        if self.retry:
            q["retry"] = self.retry
        return q

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "added": self.added, "updated": self.updated, "reused": self.reused,
                "unchanged": len(self.skipped), "missing": self.missing, "unreachable": self.unreachable,
                "failed": self.failed, "warnings": self.warnings, "partial": self.partial,
                "quality": self.run_summary(self.quality["vlm"].get("enabled") is not False), "retry": self.retry,
                "run_id": self.run_id}


def ingest_fingerprint(cfg: Config, vision_model: Optional[str]) -> str:
    """Settings that change what indexing produces. A document indexed with other settings is re-processed
    on the next sync (the embedding model is checked separately: changing it needs a full rebuild)."""
    from dataclasses import asdict

    from techrag.store import INDEX_FORMAT

    v = cfg.vision
    parts = {"format": INDEX_FORMAT, "chunking": asdict(cfg.chunking),
             "vision": {"model": vision_model, "dpi": v.dpi, "min_page_score": v.min_page_score,
                        "max_pages": v.max_pages_per_doc, "prompt": PROMPT_VERSION} if vision_model else None}
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith((".", "~$")))


def embedding_text(title: str, section: tuple[str, ...], chunk: Chunk, entities: list[str]) -> str:
    """Contextual header (standard, document, section path) + text."""
    label = section_label(section, depth=4)
    std = ", ".join(entities)
    kind = "Table" if chunk.kind == TABLE_KIND else ""
    header = " | ".join(x for x in (std, title, label, kind) if x)
    return f"{header}\n\n{chunk.text}"


def page_texts(blocks: list[Block]) -> dict[int, str]:
    out: dict[int, list[str]] = defaultdict(list)
    for b in blocks:
        out[b.page].append(b.text)
    return {p: "\n".join(t) for p, t in out.items()}


_TABLE_REF = re.compile(r"\b(?:table|tablo)\s*([A-Z]?\d+(?:[.\-–]\d+)*)", re.I)


def _tokens(text: str) -> set[str]:
    return set(norm_tokens(text))


def _overlap(a, b) -> float:
    """Intersection area / smaller area of two rects."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return (x1 - x0) * (y1 - y0) / small


def _match_score(t: ExtractedTable, block: Block, before: str) -> float:
    """How surely a PDF-detected table block is the table the VLM read: content, position and caption."""
    vt = _tokens(" ".join([*t.columns, *(c for r in t.rows for c in r)]))
    bt = _tokens(block.text)
    score = len(vt & bt) / len(vt | bt) if vt and bt else 0.0
    bbox = (t.grounding or {}).get("bbox")
    if bbox and block.bbox:
        score += 0.5 * _overlap(bbox, block.bbox)
    ref = _TABLE_REF.search(t.caption or "")
    if ref and re.search(r"\b" + re.escape(ref.group(1)) + r"\b", before + "\n" + block.text.split("\n", 1)[0], re.I):
        score += 0.3
    return score


def merge_vlm_tables(blocks: list[Block], tables: dict[int, list[ExtractedTable]]
                     ) -> tuple[list[Block], list[ExtractedTable], list[str]]:
    """Per table, not per page: a grounded VLM table replaces the one PDF table block it reliably matches
    (content + position + caption); when the match is uncertain the original is kept and the VLM table is
    added beside it. A table that is not grounded never replaces anything: the original PDF text stays the
    evidence and the VLM version is kept only as search-only content.

    Returns (blocks, stored tables (index = Block.ref), notes)."""
    stored: list[ExtractedTable] = []
    notes: list[str] = []
    by_page: dict[int, list[Block]] = defaultdict(list)
    for b in blocks:
        by_page[b.page].append(b)
    out: list[Block] = []
    for page in sorted(set(by_page) | set(tables)):
        page_blocks = list(by_page.get(page, []))
        claimed: set[int] = set()
        for t in tables.get(page, []):
            if not t.markdown:
                continue
            stored.append(t)
            ref = len(stored) - 1
            scores = []
            for i, b in enumerate(page_blocks):
                if b.kind == TABLE_KIND and b.ref is None and i not in claimed:
                    before = page_blocks[i - 1].text if i else ""
                    scores.append((_match_score(t, b, before), i))
            scores.sort(reverse=True)
            match = None
            if scores and scores[0][0] >= 0.5 and (len(scores) == 1 or scores[1][0] < scores[0][0] - 0.15):
                match = scores[0][1]
            if t.grounded and match is not None:
                claimed.add(match)
                page_blocks[match] = Block(page, t.markdown, TABLE_KIND, ref, page_blocks[match].bbox, True)
                continue
            if t.grounded:
                if scores:
                    notes.append(f"p.{page}: '{t.caption or 'table'}' matched no PDF table reliably; original kept")
                cap = t.caption.split(":")[0].strip().lower()[:40]
                pos = next((i + 1 for i, b in enumerate(page_blocks)
                            if cap and b.kind != TABLE_KIND and cap in b.text.lower()), len(page_blocks))
                page_blocks.insert(pos, Block(page, t.markdown, TABLE_KIND, ref, None, True))
                continue
            # Not grounded: keep every original block; the VLM table is search-only.
            pos = (match + 1) if match is not None else len(page_blocks)
            page_blocks.insert(pos, Block(page, t.markdown, TABLE_KIND, ref, None, False))
        out.extend(page_blocks)
    return out, stored, notes


class Ingestor:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder, domains: DomainRegistry,
                 progress: Optional[Progress] = None, llm: Optional[LLMClient] = None,
                 vision: Optional[LLMClient] = None, status: Optional[Callable[[dict], None]] = None,
                 gate: Optional[WriteGate] = None):
        self.cfg = cfg
        self.gate = gate or WriteGate()
        self.store = store
        self.embedder = embedder
        self.domains = domains
        self.progress = progress or (lambda msg: None)
        self.status = status or (lambda st: None)
        self.llm = llm
        self.extractor = None
        if vision is not None and cfg.vision.enabled:
            self.extractor = TableExtractor(vision, cfg.vlm_cache_dir, cfg.vision.dpi, cfg.vision.max_tokens,
                                            cfg.vision.concurrency)
        self.fingerprint = ingest_fingerprint(cfg, self.extractor.client.model if self.extractor else None)
        self.run_kind = "sync"     # how the run is listed in the quality report (sync | rescan | folder | retry ...)
        self._run_id: Optional[int] = None

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cfg.sources_dir.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def _check_embedding(self, rebuild: bool) -> None:
        name, dim = self.embedder.name, self.embedder.dim
        with self.gate.write():
            if rebuild and self.store.get_meta("embedding_model") not in (None, name):
                self.progress(f"embedding model changed -> clearing the index and re-embedding with {name}")
                self.store.reset(name, dim)
            self.store.check_embedding_model(name, dim)

    # ------------------------------------------------------------------ syncs
    def run(self, target: Optional[Path] = None, rebuild: bool = False, prune: bool = True,
            report: Optional[IngestReport] = None, finish: bool = True) -> IngestReport:
        """The library's own sources folder (or one file/folder inside it, or a single outside file)."""
        start = time.time()
        root = self.cfg.sources_dir
        root.mkdir(parents=True, exist_ok=True)
        target = Path(target) if target else root
        if not target.exists():
            raise FileNotFoundError(f"Source path not found: {target}")
        self._check_embedding(rebuild)
        report = report if report is not None else IngestReport()
        self._begin_run(report)
        rel_target = self._rel(target)
        if Path(rel_target).is_absolute():  # a single file outside the library, indexed by its absolute path
            if target.is_dir():
                raise ValueError("folders outside the library are added as document folders (add-folder)")
            self._outside_file(target, rel_target, rebuild, report)
        else:
            scan = scan_root(root)
            docs = [d for d in self.store.root_documents(None) if not Path(d.path).is_absolute()]
            if target.resolve() != root.resolve():
                prefix = "" if rel_target == "." else rel_target
                scan.files = [f for f in scan.files if f.rel == prefix or f.rel.startswith(prefix + "/")]
                docs = [d for d in docs if d.rel_path == prefix or d.rel_path.startswith(prefix + "/")]
            items = plan_changes(scan, docs, self.fingerprint, None, sha256_file, rebuild)
            if not prune:
                items = [it for it in items if it.action not in ("missing", "unreachable")]
            self.progress(f"{len(scan.files)} file(s) found under {target}")
            self._apply(items, None, lambda it: it.rel, report, "")
        report.seconds = time.time() - start
        if finish:
            self._finish(report)
        return report

    def sync_root(self, root_id: int, rebuild: bool = False, report: Optional[IngestReport] = None,
                  finish: bool = True) -> IngestReport:
        """A document folder the user picked: read in place, buckets from its top-level folders."""
        start = time.time()
        r = self.store.root(root_id)
        if r is None:
            raise ValueError(f"unknown document folder #{root_id}")
        self._check_embedding(rebuild)
        report = report if report is not None else IngestReport()
        self._begin_run(report)
        self.status({"phase": "scan", "root": r["path"]})
        scan = scan_root(r["path"], exclude=(self.cfg.library_dir,))
        if not scan.reachable:
            self.progress(f"folder not reachable, nothing changed: {r['path']} ({scan.error})")
            with self.gate.write():
                self.store.set_root_status(root_id, f"unreachable: {scan.error}")
        else:
            self.progress(f"{len(scan.files)} document(s) in {len(scan.buckets())} bucket(s) under {r['path']}")
            for u in scan.unreadable[:10]:
                self.progress(f"    cannot read: {u}")
        items = plan_changes(scan, self.store.root_documents(root_id), self.fingerprint, root_id, sha256_file,
                             rebuild)
        self._apply(items, root_id, lambda it: f"@{root_id}/{it.rel}", report, r["path"])
        if scan.reachable:
            with self.gate.write():
                self.store.set_root_status(root_id, "ok" if not scan.unreadable else
                                           f"partly unreadable: {len(scan.unreadable)} item(s)")
        else:
            report.quality.setdefault("unreachable_folders", []).append(r["path"])
        report.seconds = time.time() - start
        if finish:
            self._finish(report)
        return report

    def sync_all(self, rebuild: bool = False) -> IngestReport:
        """Rescan: the library's sources folder and every document folder."""
        start = time.time()
        report = self.run(rebuild=rebuild, finish=False)
        for r in self.store.roots():
            try:
                self.sync_root(r["id"], rebuild, report, finish=False)
            except Exception as exc:
                report.failed[r["path"]] = scrub(f"{exc.__class__.__name__}: {exc}")
        report.seconds = time.time() - start
        self._finish(report)
        return report

    def _begin_run(self, report: IngestReport) -> None:
        """One quality-report run per top-level call (a rescan of several folders is one run)."""
        if self._run_id is None:
            with self.gate.write():
                self._run_id = self.store.start_ingest_run(self.run_kind)
        report.run_id = self._run_id

    def abort_run(self, status: str = "failed") -> None:
        """A run that ended with an error is listed as such (a run cut off by closing the app cannot be written
        any more; the next start lists it as interrupted)."""
        if self._run_id is None:
            return
        try:
            with self.gate.write():
                self.store.finish_ingest_run(self._run_id, status, {})
        except BaseException:
            pass
        self._run_id = None

    def _finish(self, report: IngestReport) -> None:
        pairs = compute_supersedence([d for d in self.store.documents() if not d.missing])
        with self.gate.write():
            self.store.set_superseded(pairs)
            status = "done" if not (report.failed or report.unreachable or
                                    report.quality.get("unreachable_folders")) else "done_with_errors"
            self.store.finish_ingest_run(self._run_id, status, report.run_summary(self.extractor is not None))
        self._run_id = None
        for doc_id, newer in pairs:
            if newer:
                d, nd = self.store.document(doc_id), self.store.document(newer)
                self.progress(f"superseded: '{d.title}' -> newer revision '{nd.title}'")
        self.status({"phase": "done", "file": "", "errors": len(report.failed)})

    def _outside_file(self, path: Path, key: str, rebuild: bool, report: IngestReport) -> None:
        try:
            digest = sha256_file(path)
            existing = self.store.document_by_path(key)
            st = path.stat()
            if existing and existing.sha256 == digest and not rebuild and \
                    (not existing.ingest_fp or existing.ingest_fp == self.fingerprint):
                report.skipped.append(key)
                return
            source = {"rel_path": key, "bucket": "", "subpath": "", "file_size": st.st_size,
                      "file_mtime": st.st_mtime_ns}
            stats, warnings = self.ingest_file(path, key, digest, source)
            self._count(report, existing is not None, key, stats, warnings)
        except Exception as exc:
            self._failed(report, key, path.stem, {"rel_path": key, "bucket": ""}, exc)

    def _count(self, report: IngestReport, existed: bool, key: str, stats: dict, warnings: list) -> None:
        report.chunks += stats["chunks"]
        report.tables += stats["tables"]
        report.parameters += stats["parameters"]
        (report.updated if existed else report.added).append(key)
        q: Optional[DocQuality] = stats.get("quality")
        if q is not None:
            add_document(report.quality, q)
            if q.status == "partial":
                report.partial.append(key)
        if warnings:
            report.warnings[key] = warnings
            for w in warnings[:5]:
                self.progress(f"    warning: {w}")

    def _failed(self, report: IngestReport, key: str, title: str, source: dict, exc: BaseException) -> None:
        """A document could not be processed: reported, and recorded in the quality report. Its existing index
        rows (an earlier, successful processing) are not touched and stay usable."""
        cause = exc.cause if isinstance(exc, IngestFailure) else exc
        report.failed[key] = scrub(f"{cause.__class__.__name__}: {cause}")
        stage = failure_stage(exc)
        report.quality["stage_failures"][stage if stage in FATAL_STAGES else "other"] += 1
        try:
            doc = self.store.document_by_path(key)
            previous = (self.store.quality() or {}).get(key)
            rec = failure_record(exc, source, doc.title if doc else title, self._run_id, previous,
                                 doc.ingested_at if doc else "")
            with self.gate.write():
                self.store.set_quality(key, rec)
        except IngestStopped:
            raise
        except Exception as rec_exc:  # the report must never hide the original failure
            self.progress(f"    (could not record the failure in the quality report: {rec_exc})")

    def _apply(self, items: list[PlanItem], root_id: Optional[int], key_of, report: IngestReport,
               root_path: str) -> None:
        """Carry out a plan; one failing file never stops the others."""
        work = [it for it in items if it.action != "unchanged"]
        report.skipped += [key_of(it) for it in items if it.action == "unchanged"]
        total = len(work)
        for n, it in enumerate(work, 1):
            self.gate.check()
            key = key_of(it)
            self.status({"phase": "index", "bucket": it.bucket, "file": it.rel, "action": it.action,
                         "done": n - 1, "total": total, "errors": len(report.failed)})
            try:
                if it.action == "missing":
                    with self.gate.write():
                        self.store.set_missing([it.doc_id], True)
                    report.missing.append(key)
                    self.progress(f"source file not found (kept, excluded from answers): {it.rel}")
                    continue
                if it.action == "unreachable":
                    report.unreachable.append(key)
                    continue
                f = it.file
                source = {"root_id": root_id, "rel_path": it.rel, "bucket": it.bucket, "bucket_id": it.bucket_id,
                          "subpath": f.subpath, "file_size": f.size, "file_mtime": f.mtime_ns}
                if it.action in ("touched", "restored"):
                    with self.gate.write():
                        self.store.update_source(it.doc_id, **source)
                    report.skipped.append(key)
                    continue
                if it.action == "moved":
                    with self.gate.write():
                        self.store.update_source(it.from_doc, path=key, **source)
                    report.reused.append(key)
                    self.progress(f"[{n}/{total}] {it.rel}: moved, index reused")
                    continue
                if it.action == "duplicate":
                    src_doc = self.store.document(it.from_doc)
                    # The original may have been re-indexed earlier in this run (its file changed): only
                    # content that is still exactly this file's may be copied; otherwise index this file.
                    if src_doc is not None and src_doc.sha256 == it.sha256 and not src_doc.missing:
                        with self.gate.write():
                            self.store.clone_document(it.from_doc, key, dict(source, ingest_fp=src_doc.ingest_fp))
                        report.reused.append(key)
                        self.progress(f"[{n}/{total}] {it.rel}: same content as '{src_doc.path}', index reused")
                        continue
                self.progress(f"[{n}/{total}] {it.bucket + ' / ' if it.bucket else ''}{it.rel}"
                              + (f" ({it.reason})" if it.reason else ""))
                try:
                    digest = it.sha256 or sha256_file(f.path)
                except OSError as exc:
                    raise IngestFailure("read", exc) from exc
                existed = it.doc_id is not None
                stats, warnings = self.ingest_file(Path(f.path), key, digest, source, rel_for_domain=it.rel)
                self._count(report, existed, key, stats, warnings)
            except Exception as exc:  # one bad or unreadable file must not stop a batch
                src = {"root_id": root_id, "rel_path": it.rel, "bucket": it.bucket, "bucket_id": it.bucket_id,
                       "subpath": it.file.subpath if it.file else ""}
                self._failed(report, key, Path(it.rel).stem, src, exc)
                self.progress(f"    FAILED {it.rel}: {report.failed[key]}")
        self.status({"phase": "index", "done": total, "total": total, "errors": len(report.failed)})

    # ---------------------------------------------------------------- one file
    def ingest_file(self, path: Path, rel: str, digest: str, source: Optional[dict] = None,
                    rel_for_domain: Optional[str] = None, retry_of: Optional[dict] = None) -> tuple[dict, list[str]]:
        """Process one file and write it in one transaction (with its quality record). Any failure raises before
        the write: the document's existing index rows stay as they were. ``retry_of``: the quality record of a
        retried document - the new result is only written when it has no more failed VLM pages than the old one."""
        t0 = time.time()
        cfg = self.cfg
        q = DocQuality(pdf=path.suffix.lower() == ".pdf")
        try:
            doc: LoadedDocument = load_document(path, extract_tables=cfg.chunking.extract_tables)
        except OSError as exc:
            raise IngestFailure("read", exc) from exc
        except Exception as exc:
            raise IngestFailure("parse", exc) from exc
        q.n_pages = doc.n_pages
        blocks = clean_blocks(doc.blocks, doc.n_pages, cfg.chunking.strip_headers_footers,
                              cfg.chunking.skip_toc_pages)
        warnings = list(doc.warnings)
        texts = page_texts(blocks)
        front = "\n".join(texts.get(p, "") for p in sorted(texts)[:3])
        # Routing collection from the folder (or content for loose files); the bucket is stored separately and
        # neither is used as evidence of the document's standard/revision.
        domain = self.domains.classify_document(Path(rel_for_domain or rel), f"{doc.title}\n{front}")

        llm_meta = {}
        if self.llm is not None:
            try:
                llm_meta = llm_metadata(self.llm, front, digest, cfg.vlm_cache_dir)
            except Exception as exc:
                warnings.append(f"metadata extraction failed: {scrub(exc)}")
                q.issue("metadata_failed", "metadata", detail=f"{exc.__class__.__name__}: {exc}")
        meta = build_metadata(self.domains, domain, path.stem, str(doc.metadata.get("title", "")), front, llm_meta)

        figure_pages = [p for p, st in doc.page_stats.items()
                        if is_figure_page(texts.get(p, ""), st.drawings, st.images)]

        accepted: list[ExtractedTable] = []
        if not q.pdf:
            q.vlm = {"enabled": False, "off_reason": "not_pdf"}
        elif self.extractor is None:
            q.vlm = {"enabled": False, "off_reason": "disabled"}
            q.issue("vlm_disabled", "vlm")
        else:
            cands = page_candidates(texts, doc.page_stats, cfg.vision.min_page_score, doc.pages)
            pages = sorted(cands[:cfg.vision.max_pages_per_doc])
            over = sorted(cands[cfg.vision.max_pages_per_doc:])
            q.vlm = {"enabled": True, "selected": pages, "over_limit": over,
                     "filtered_out": max(0, len(texts) - len(cands)),
                     "cached": sum(1 for p in pages if self.extractor.cached(digest, p))}
            if over:
                q.issue("vlm_over_limit", "vlm", pages=over[:50], count=len(over))
            failed: dict[int, str] = {}
            if pages:
                self.progress(f"    {len(pages)} table page(s) -> VLM"
                              + (f" ({q.vlm['cached']} from the cache)" if q.vlm["cached"] else ""))
                found, errors = self.extractor.extract(path, digest, pages, texts, self.progress, geoms=doc.pages)
                failed = dict(self.extractor.failed)
                warnings.extend(scrub(e) for e in errors[:20])
                for page, why in sorted(failed.items()):
                    q.issue("vlm_failed", "vlm", page=page, detail=why,
                            cut_off="cut off" in why)
                q.vlm.update(with_tables=[p for p in pages if found.get(p)],
                             no_table=[p for p in pages if p not in failed and not found.get(p)],
                             failed={str(p): scrub(why, 200) for p, why in failed.items()})
                blocks, accepted, notes = merge_vlm_tables(blocks, found)
                warnings.extend(notes[:20])
                for n in notes:
                    m = re.match(r"p\.(\d+): '(.*)' matched", n)
                    q.issue("table_match_uncertain", "vlm", page=int(m.group(1)) if m else None,
                            table=m.group(2) if m else "")
                partial = [t for t in accepted if not t.grounded]
                if partial:
                    warnings.append(f"{len(partial)} VLM table(s) could not be tied cell by cell to the PDF text "
                                    "layer: their original PDF text is kept as evidence, the VLM version is "
                                    "search-only")
                params = [p for t in accepted for p in t.parameters]
                unverified = sum(1 for p in params if not p.get("verified"))
                if unverified:
                    warnings.append(f"{unverified}/{len(params)} parameter row(s) not verified (not used in answers)")
            else:
                q.vlm.update(with_tables=[], no_table=[], failed={})
            if retry_of is not None:
                before = len(((retry_of.get("data") or {}).get("vlm") or {}).get("failed") or {})
                if retry_of.get("status") != "failed" and len(failed) > before:
                    raise RetryNotBetter(f"{len(failed)} VLM page(s) failed now, {before} before")
        self._table_quality(q, accepted)

        sectioned = assign_sections(doc, blocks)
        chunks = chunk_blocks(sectioned, cfg.chunking.target_tokens, cfg.chunking.max_tokens,
                              cfg.chunking.overlap_tokens, cfg.chunking.min_tokens)
        if not chunks:
            warnings.append("no text could be extracted (scanned PDF? run OCR first)")
            q.issue("no_text", "parse")
        q.chunks = len(chunks)

        table_sections: dict[int, str] = {}
        for sb in sectioned:
            if sb.ref is not None:
                table_sections[sb.ref] = SECTION_SEP.join(sb.section)
        table_rows = [{
            "page": t.page, "caption": t.caption, "section": table_sections.get(i, ""), "markdown": t.markdown,
            "data": {"columns": t.columns, "rows": t.rows, "footnotes": t.footnotes}, "source": "vlm",
            "verified_ratio": t.verified_ratio, "status": t.status, "grounding": t.grounding,
            "parameters": t.parameters,
        } for i, t in enumerate(accepted)]

        title = doc.title
        try:
            matrix = self._embed(rel, title, meta.entities, chunks, q)
        except IngestStopped:
            raise
        except Exception as exc:
            raise IngestFailure("embedding", exc) from exc

        rows = [{"ordinal": c.ordinal, "section": SECTION_SEP.join(c.section), "page_start": c.page_start,
                 "page_end": c.page_end, "kind": c.kind, "overlap": c.overlap, "text": c.text,
                 "evidence": c.evidence} for c in chunks]
        metadata = dict(doc.metadata)
        metadata.update({"figure_pages": figure_pages, "llm": meta.llm, "doc_key": meta.doc_key,
                         "version": meta.version, "part": meta.part})
        retry_info = None
        if retry_of is not None:
            failed_before = sorted(int(p) for p in (((retry_of.get("data") or {}).get("vlm") or {})
                                                    .get("failed") or {}))
            retry_info = {"scope": "pages" if retry_of.get("status") == "partial" else "document",
                          "pages": failed_before, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        record = q.record(source or {"rel_path": rel}, title, self._run_id, retry_info)
        with self.gate.write():  # one transaction per document, with its quality record
            try:
                self.store.replace_document(
                    path=rel, title=title, domain=domain, sha256=digest, n_pages=doc.n_pages,
                    toc=[list(t) for t in doc.toc], metadata=metadata, warnings=warnings, chunks=rows,
                    embeddings=matrix, entities=meta.entities, doc_type=meta.doc_type, revision=meta.revision,
                    doc_date=meta.doc_date, tables=table_rows,
                    pages=[(p, g.label, *g.encode()) for p, g in sorted(doc.pages.items())],
                    source=dict(source or {"rel_path": rel}, ingest_fp=self.fingerprint), quality=record,
                )
            except Exception as exc:  # the transaction was rolled back: the old rows are still there
                raise IngestFailure("index_write", exc) from exc
        n_params = sum(1 for t in table_rows for p in t["parameters"] if p.get("verified"))
        self.progress(f"    -> {len(chunks)} chunks, {len(table_rows)} VLM tables, {n_params} verified parameters, "
                      f"collection={domain}, standard={', '.join(meta.entities) or '-'}, type={meta.doc_type}, "
                      f"rev={meta.revision or '-'}, {time.time() - t0:.1f}s")
        return {"chunks": len(chunks), "tables": len(table_rows), "parameters": n_params, "quality": q}, warnings

    @staticmethod
    def _table_quality(q: DocQuality, accepted: list[ExtractedTable]) -> None:
        t = q.tables
        for tb in accepted:
            rows = len(tb.parameters)
            ok = sum(1 for p in tb.parameters if p.get("verified"))
            t["extracted"] += 1
            t["grounded" if tb.grounded else "not_grounded"] += 1
            t["rows"] += rows
            t["rows_verified"] += ok
            t["rows_unverified"] += rows - ok
            if not tb.grounded or ok < rows:
                t["items"].append({"page": tb.page, "table": tb.caption[:120], "status": tb.status, "rows": rows,
                                   "rows_verified": ok})
            if not tb.grounded:
                q.issue("table_not_grounded", "vlm", page=tb.page, table=tb.caption, status=tb.status)
            if ok < rows:
                q.issue("rows_unverified", "vlm", page=tb.page, table=tb.caption, count=rows - ok, total=rows)

    def _embed(self, rel: str, title: str, entities: list[str], chunks: list[Chunk], q: DocQuality) -> np.ndarray:
        """Embeddings of the chunks. When the document is processed again (a retry, a changed setting), chunks whose
        text, section and header did not change keep their stored embedding: only new or changed chunks are sent
        to the embedding service."""
        cfg = self.cfg
        known: dict[tuple, np.ndarray] = {}
        old = self.store.document_by_path(rel)
        if old is not None and old.title == title and list(old.entities) == list(entities):
            known = self.store.chunk_embeddings(old.id)
        out: list[Optional[np.ndarray]] = []
        todo: list[int] = []
        for i, c in enumerate(chunks):
            vec = known.get((SECTION_SEP.join(c.section), c.kind, c.text))
            if vec is not None and vec.shape[0] == self.embedder.dim:
                out.append(vec.astype(np.float32))
            else:
                out.append(None)
                todo.append(i)
        texts = [embedding_text(title, chunks[i].section, chunks[i], entities) for i in todo]
        batch = max(1, cfg.embedding.batch_size) * max(1, cfg.embedding.concurrency)
        for start in range(0, len(texts), batch):
            vecs = self.embedder.embed_documents(texts[start:start + batch])
            for j, v in zip(todo[start:start + batch], vecs):
                out[j] = np.asarray(v, dtype=np.float32)
            if len(texts) > batch:
                self.progress(f"    embedded {min(start + batch, len(texts))}/{len(texts)} chunks")
        q.embedding = {"embedded": len(todo), "reused": len(chunks) - len(todo)}
        if known and len(todo) < len(chunks):
            self.progress(f"    {len(chunks) - len(todo)} unchanged chunk(s) kept their embedding")
        return np.vstack(out) if out else np.zeros((0, self.embedder.dim), np.float32)

    # ---------------------------------------------------------------- retry
    def retry_failed(self, paths: Optional[list[str]] = None) -> IngestReport:
        """Retry what failed, as narrowly as possible: a partially processed document repeats only its failed
        VLM pages (pages answered before come from the cache; unchanged chunks keep their embeddings); a failed
        document is processed again as a whole; an unreachable document folder is scanned again. A retry that
        fails, or that would end with more failed pages than before, leaves the existing index as it is."""
        start = time.time()
        report = IngestReport()
        self.run_kind = "retry"
        self._check_embedding(False)
        self._begin_run(report)
        records = self.store.quality() or {}
        todo = [r for r in records.values() if r.get("needs_reprocess") and (paths is None or r["path"] in paths)]
        total = len(todo)
        for n, rec in enumerate(todo, 1):
            self.gate.check()
            key = rec["path"]
            data = rec.get("data") or {}
            src = dict(data.get("source") or {})
            doc = self.store.document_by_path(key)
            if doc is not None:
                src = {"root_id": doc.root_id, "rel_path": doc.rel_path or key, "bucket": doc.bucket,
                       "bucket_id": doc.bucket_id, "subpath": doc.subpath}
                file = self.store.source_path(doc, self.cfg.sources_dir)
            elif src.get("root_id") is not None and self.store.root(src["root_id"]):
                file = Path(self.store.root(src["root_id"])["path"]) / src.get("rel_path", "")
            else:
                file = Path(key) if Path(key).is_absolute() else self.cfg.sources_dir / key
            pages = sorted(int(p) for p in ((data.get("vlm") or {}).get("failed") or {}))
            scope = "pages" if rec.get("status") == "partial" and doc is not None else "document"
            report.retry[key] = {"scope": scope, "pages": pages if scope == "pages" else []}
            self.status({"phase": "index", "bucket": src.get("bucket", ""), "file": src.get("rel_path") or key,
                         "action": "retry", "done": n - 1, "total": total, "errors": len(report.failed)})
            if not file.is_file():
                report.retry[key]["result"] = "file_missing"
                report.missing.append(key)
                self.progress(f"[{n}/{total}] {key}: the source file is not there; nothing retried")
                continue
            self.progress(f"[{n}/{total}] retry {key}: "
                          + (f"VLM page(s) {', '.join(map(str, pages))}" if scope == "pages" and pages
                             else "the whole document"))
            try:
                try:
                    digest = sha256_file(file)
                    st = file.stat()
                except OSError as exc:
                    raise IngestFailure("read", exc) from exc
                if doc is not None and digest != doc.sha256:
                    report.retry[key]["scope"] = "document"  # the file changed meanwhile: all of it again
                source = dict(src, file_size=st.st_size, file_mtime=st.st_mtime_ns)
                stats, warnings = self.ingest_file(file, key, digest, source, rel_for_domain=src.get("rel_path"),
                                                   retry_of=rec if doc is not None else None)
                self._count(report, doc is not None, key, stats, warnings)
                qd: DocQuality = stats["quality"]
                report.retry[key]["result"] = qd.status
                report.retry[key]["still_failed"] = sorted(int(p) for p in (qd.vlm.get("failed") or {}))
            except RetryNotBetter as exc:
                report.retry[key]["result"] = "kept_existing"
                report.retry[key]["detail"] = str(exc)
                self.progress(f"    retry not better ({exc}): the existing index is kept")
            except Exception as exc:
                self._failed(report, key, Path(key).stem, src, exc)
                report.retry[key]["result"] = "failed"
                self.progress(f"    FAILED again: {scrub(exc)}")
        if paths is None:  # document folders that could not be read last time
            for r in self.store.roots():
                if str(r.get("last_status") or "").startswith("unreachable"):
                    report.retry[r["path"]] = {"scope": "folder"}
                    self.sync_root(r["id"], report=report, finish=False)
        report.seconds = time.time() - start
        self._finish(report)
        return report


def retry_plan(store: Store) -> list[dict]:
    """What 'retry failed' would do, per document (shown to the user before it runs)."""
    out = []
    paths = {d.path for d in store.documents()}
    for r in (store.quality() or {}).values():
        if not r.get("needs_reprocess"):
            continue
        pages = sorted(int(p) for p in (((r.get("data") or {}).get("vlm") or {}).get("failed") or {}))
        scope = "pages" if r.get("status") == "partial" and r["path"] in paths else "document"
        out.append({"path": r["path"], "scope": scope, "pages": pages if scope == "pages" else []})
    out += [{"path": r["path"], "scope": "folder", "pages": []} for r in store.roots()
            if str(r.get("last_status") or "").startswith("unreachable")]
    return out


def pdf_geometry(path: Path) -> dict:
    import pymupdf

    from techrag.geometry import PageGeom

    with pymupdf.open(str(path)) as doc:
        return {i + 1: PageGeom.from_page(page, i + 1) for i, page in enumerate(doc)}


def migrate_index(cfg: Config, store: Store, progress: Progress = lambda m: None,
                  gate: Optional[WriteGate] = None) -> dict:
    """Bring documents indexed by an older version up to date WITHOUT re-embedding or VLM calls:
    store page geometry from the (unchanged) source PDFs, then re-run cell-level grounding on the cached
    VLM tables/parameters stored in the index. Tables whose original PDF text the old version dropped and
    that still cannot be grounded need a full re-index (reported)."""
    import json as _json

    from techrag.ingest.metadata import regex_version, series_key
    from techrag.ingest.vlm import ExtractedTable, validate
    from techrag.store import INDEX_FORMAT

    gate = gate or WriteGate()
    rep = {"documents": 0, "pages": 0, "tables_grounded": 0, "tables_not_grounded": 0, "parameters_verified": 0,
           "parameters_unverified": 0, "skipped": [], "needs_reindex": []}
    for doc in store.legacy_documents():
        gate.check()
        path = store.source_path(doc, cfg.sources_dir)
        meta = dict(doc.metadata, index_format=INDEX_FORMAT)
        stem = Path(doc.path).stem
        meta.setdefault("doc_key", series_key(stem, doc.title))
        meta.setdefault("version", regex_version(f"{stem} {doc.title}"))
        if path.suffix.lower() != ".pdf":
            with gate.write():
                store.set_pages(doc.id, [], meta)
            rep["documents"] += 1
            continue
        if not path.exists():
            rep["skipped"].append(f"{doc.path} (file missing)")
            continue
        if sha256_file(path) != doc.sha256:
            rep["needs_reindex"].append(f"{doc.path} (file changed since indexing)")
            continue
        progress(f"migrating {doc.path}")
        geoms = pdf_geometry(path)
        tables, params, chunk_ev = [], [], []
        for t in store.raw_tables(doc.id):
            data = _json.loads(t["data"] or "{}") or {}
            et = ExtractedTable(t["page"], t["caption"] or "", data.get("columns") or [], data.get("rows") or [],
                                data.get("footnotes") or [])
            raw_params = store.raw_parameters(t["id"])
            et.parameters = [{k: (r[k] or "") for k in ("parameter", "symbol", "min", "typ", "max", "unit",
                                                         "conditions", "notes")} for r in raw_params]
            validate(et, geoms.get(t["page"]), geoms.get(t["page"] - 1), geoms.get(t["page"] + 1))
            tables.append((t["id"], et.status, _json.dumps(et.grounding, ensure_ascii=False), et.verified_ratio))
            rep["tables_grounded" if et.grounded else "tables_not_grounded"] += 1
            header = next((ln for ln in (t["markdown"] or "").split("\n") if ln.startswith("|")), "")
            for c in store.table_chunks(doc.id, t["page"]):
                if header and header in c["text"]:
                    chunk_ev.append((c["id"], 1 if et.grounded else 0))
            if not et.grounded:
                rep["needs_reindex"].append(f"{doc.path} p.{t['page']} '{et.caption}' (table not grounded; its "
                                            "original PDF text was not kept by the old version)")
            for r, p in zip(raw_params, et.parameters):
                params.append((r["id"], p["verified"], p["status"], _json.dumps(p["evidence"], ensure_ascii=False),
                               p["value_kind"], p.get("min_si"), p.get("typ_si"), p.get("max_si"), p.get("base_unit", "")))
                rep["parameters_verified" if p["verified"] else "parameters_unverified"] += 1
        with gate.write():
            store.update_grounding(tables, params, chunk_ev)
            store.set_pages(doc.id, [(p, g.label, *g.encode()) for p, g in sorted(geoms.items())], meta)
        rep["documents"] += 1
        rep["pages"] += len(geoms)
    pairs = compute_supersedence(store.documents())
    with gate.write():
        store.set_superseded(pairs)
    return rep


def build_chunks(cfg: Config, path: Path):
    """Parse + chunk one file without indexing (used by `techrag inspect`)."""
    doc = load_document(path, extract_tables=cfg.chunking.extract_tables)
    blocks = clean_blocks(doc.blocks, doc.n_pages, cfg.chunking.strip_headers_footers, cfg.chunking.skip_toc_pages)
    sectioned = assign_sections(doc, blocks)
    chunks = chunk_blocks(sectioned, cfg.chunking.target_tokens, cfg.chunking.max_tokens,
                          cfg.chunking.overlap_tokens, cfg.chunking.min_tokens)
    return doc, blocks, chunks
