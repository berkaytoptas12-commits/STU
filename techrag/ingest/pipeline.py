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
import time
from collections import defaultdict
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
from techrag.ingest.sources import PlanItem, plan_changes, scan_root
from techrag.ingest.structure import SECTION_SEP, assign_sections, section_label
from techrag.ingest.vlm import PROMPT_VERSION, ExtractedTable, TableExtractor, is_figure_page, select_pages
from techrag.llm import LLMClient
from techrag.store import Store

Progress = Callable[[str], None]

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
    chunks: int = 0
    tables: int = 0
    parameters: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        return (f"added={len(self.added)} updated={len(self.updated)} unchanged={len(self.skipped)} "
                f"reused={len(self.reused)} missing={len(self.missing)} unreachable={len(self.unreachable)} "
                f"failed={len(self.failed)} chunks={self.chunks} vlm_tables={self.tables} "
                f"parameters={self.parameters} time={self.seconds:.1f}s")

    def merge(self, other: "IngestReport") -> None:
        for name in ("added", "updated", "skipped", "reused", "missing", "unreachable", "removed"):
            getattr(self, name).extend(getattr(other, name))
        self.failed.update(other.failed)
        self.warnings.update(other.warnings)
        self.chunks += other.chunks
        self.tables += other.tables
        self.parameters += other.parameters

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "added": self.added, "updated": self.updated, "reused": self.reused,
                "unchanged": len(self.skipped), "missing": self.missing, "unreachable": self.unreachable,
                "failed": self.failed, "warnings": self.warnings}


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
                 vision: Optional[LLMClient] = None, status: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
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

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cfg.sources_dir.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def _check_embedding(self, rebuild: bool) -> None:
        name, dim = self.embedder.name, self.embedder.dim
        if rebuild and self.store.get_meta("embedding_model") not in (None, name):
            self.progress(f"embedding model changed -> clearing the index and re-embedding with {name}")
            self.store.reset(name, dim)
        self.store.check_embedding_model(name, dim)

    # ------------------------------------------------------------------ syncs
    def run(self, target: Optional[Path] = None, rebuild: bool = False, prune: bool = True) -> IngestReport:
        """The library's own sources folder (or one file/folder inside it, or a single outside file)."""
        start = time.time()
        root = self.cfg.sources_dir
        root.mkdir(parents=True, exist_ok=True)
        target = Path(target) if target else root
        if not target.exists():
            raise FileNotFoundError(f"Source path not found: {target}")
        self._check_embedding(rebuild)
        report = IngestReport()
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
        self._finish(report)
        report.seconds = time.time() - start
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
        self.status({"phase": "scan", "root": r["path"]})
        scan = scan_root(r["path"], exclude=(self.cfg.library_dir,))
        if not scan.reachable:
            self.progress(f"folder not reachable, nothing changed: {r['path']} ({scan.error})")
            self.store.set_root_status(root_id, f"unreachable: {scan.error}")
        else:
            self.progress(f"{len(scan.files)} document(s) in {len(scan.buckets())} bucket(s) under {r['path']}")
            for u in scan.unreadable[:10]:
                self.progress(f"    cannot read: {u}")
        items = plan_changes(scan, self.store.root_documents(root_id), self.fingerprint, root_id, sha256_file,
                             rebuild)
        self._apply(items, root_id, lambda it: f"@{root_id}/{it.rel}", report, r["path"])
        if scan.reachable:
            self.store.set_root_status(root_id, "ok" if not scan.unreadable else
                                       f"partly unreadable: {len(scan.unreadable)} item(s)")
        if finish:
            self._finish(report)
        report.seconds = time.time() - start
        return report

    def sync_all(self, rebuild: bool = False) -> IngestReport:
        """Rescan: the library's sources folder and every document folder."""
        start = time.time()
        report = self.run(rebuild=rebuild)
        for r in self.store.roots():
            try:
                self.sync_root(r["id"], rebuild, report, finish=False)
            except Exception as exc:
                report.failed[r["path"]] = f"{exc.__class__.__name__}: {exc}"
        self._finish(report)
        report.seconds = time.time() - start
        return report

    def _finish(self, report: IngestReport) -> None:
        pairs = compute_supersedence([d for d in self.store.documents() if not d.missing])
        self.store.set_superseded(pairs)
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
            stats, warnings = self.ingest_file(path, key, digest, {"rel_path": key, "bucket": "", "subpath": "",
                                                                    "file_size": st.st_size,
                                                                    "file_mtime": st.st_mtime_ns})
            self._count(report, existing is not None, key, stats, warnings)
        except Exception as exc:
            report.failed[key] = f"{exc.__class__.__name__}: {exc}"

    def _count(self, report: IngestReport, existed: bool, key: str, stats: dict, warnings: list) -> None:
        report.chunks += stats["chunks"]
        report.tables += stats["tables"]
        report.parameters += stats["parameters"]
        (report.updated if existed else report.added).append(key)
        if warnings:
            report.warnings[key] = warnings
            for w in warnings[:5]:
                self.progress(f"    warning: {w}")

    def _apply(self, items: list[PlanItem], root_id: Optional[int], key_of, report: IngestReport,
               root_path: str) -> None:
        """Carry out a plan; one failing file never stops the others."""
        work = [it for it in items if it.action != "unchanged"]
        report.skipped += [key_of(it) for it in items if it.action == "unchanged"]
        total = len(work)
        for n, it in enumerate(work, 1):
            key = key_of(it)
            self.status({"phase": "index", "bucket": it.bucket, "file": it.rel, "action": it.action,
                         "done": n - 1, "total": total, "errors": len(report.failed)})
            try:
                if it.action == "missing":
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
                    self.store.update_source(it.doc_id, **source)
                    report.skipped.append(key)
                    continue
                if it.action == "moved":
                    self.store.update_source(it.from_doc, path=key, **source)
                    report.reused.append(key)
                    self.progress(f"[{n}/{total}] {it.rel}: moved, index reused")
                    continue
                if it.action == "duplicate":
                    src_doc = self.store.document(it.from_doc)
                    self.store.clone_document(it.from_doc, key, dict(source, ingest_fp=src_doc.ingest_fp))
                    report.reused.append(key)
                    self.progress(f"[{n}/{total}] {it.rel}: same content as '{src_doc.path}', index reused")
                    continue
                self.progress(f"[{n}/{total}] {it.bucket + ' / ' if it.bucket else ''}{it.rel}"
                              + (f" ({it.reason})" if it.reason else ""))
                digest = it.sha256 or sha256_file(f.path)
                existed = it.doc_id is not None
                stats, warnings = self.ingest_file(Path(f.path), key, digest, source, rel_for_domain=it.rel)
                self._count(report, existed, key, stats, warnings)
            except Exception as exc:  # one bad or unreadable file must not stop a batch
                report.failed[key] = f"{exc.__class__.__name__}: {exc}"
                self.progress(f"    FAILED {it.rel}: {exc}")
        self.status({"phase": "index", "done": total, "total": total, "errors": len(report.failed)})

    # ---------------------------------------------------------------- one file
    def ingest_file(self, path: Path, rel: str, digest: str, source: Optional[dict] = None,
                    rel_for_domain: Optional[str] = None) -> tuple[dict, list[str]]:
        t0 = time.time()
        cfg = self.cfg
        doc: LoadedDocument = load_document(path, extract_tables=cfg.chunking.extract_tables)
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
                warnings.append(f"metadata extraction failed: {exc}")
        meta = build_metadata(self.domains, domain, path.stem, str(doc.metadata.get("title", "")), front, llm_meta)

        figure_pages = [p for p, st in doc.page_stats.items()
                        if is_figure_page(texts.get(p, ""), st.drawings, st.images)]

        accepted: list[ExtractedTable] = []
        if self.extractor is not None and path.suffix.lower() == ".pdf":
            pages = select_pages(texts, doc.page_stats, cfg.vision.min_page_score, cfg.vision.max_pages_per_doc)
            if pages:
                self.progress(f"    {len(pages)} table page(s) -> VLM")
                found, errors = self.extractor.extract(path, digest, pages, texts, self.progress, geoms=doc.pages)
                warnings.extend(errors[:20])
                blocks, accepted, notes = merge_vlm_tables(blocks, found)
                warnings.extend(notes[:20])
                partial = [t for t in accepted if not t.grounded]
                if partial:
                    warnings.append(f"{len(partial)} VLM table(s) could not be tied cell by cell to the PDF text "
                                    "layer: their original PDF text is kept as evidence, the VLM version is "
                                    "search-only")
                params = [p for t in accepted for p in t.parameters]
                unverified = sum(1 for p in params if not p.get("verified"))
                if unverified:
                    warnings.append(f"{unverified}/{len(params)} parameter row(s) not verified (not used in answers)")

        sectioned = assign_sections(doc, blocks)
        chunks = chunk_blocks(sectioned, cfg.chunking.target_tokens, cfg.chunking.max_tokens,
                              cfg.chunking.overlap_tokens, cfg.chunking.min_tokens)
        if not chunks:
            warnings.append("no text could be extracted (scanned PDF? run OCR first)")

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
        emb_texts = [embedding_text(title, c.section, c, meta.entities) for c in chunks]
        vectors = []
        batch = max(1, cfg.embedding.batch_size) * max(1, cfg.embedding.concurrency)
        for i in range(0, len(emb_texts), batch):
            vectors.append(self.embedder.embed_documents(emb_texts[i:i + batch]))
            if len(emb_texts) > batch:
                self.progress(f"    embedded {min(i + batch, len(emb_texts))}/{len(emb_texts)} chunks")
        matrix = np.vstack(vectors) if vectors else np.zeros((0, self.embedder.dim), np.float32)

        rows = [{"ordinal": c.ordinal, "section": SECTION_SEP.join(c.section), "page_start": c.page_start,
                 "page_end": c.page_end, "kind": c.kind, "overlap": c.overlap, "text": c.text,
                 "evidence": c.evidence} for c in chunks]
        metadata = dict(doc.metadata)
        metadata.update({"figure_pages": figure_pages, "llm": meta.llm, "doc_key": meta.doc_key,
                         "version": meta.version, "part": meta.part})
        self.store.replace_document(
            path=rel, title=title, domain=domain, sha256=digest, n_pages=doc.n_pages,
            toc=[list(t) for t in doc.toc], metadata=metadata, warnings=warnings, chunks=rows, embeddings=matrix,
            entities=meta.entities, doc_type=meta.doc_type, revision=meta.revision, doc_date=meta.doc_date,
            tables=table_rows, pages=[(p, g.label, *g.encode()) for p, g in sorted(doc.pages.items())],
            source=dict(source or {"rel_path": rel}, ingest_fp=self.fingerprint),
        )
        n_params = sum(1 for t in table_rows for p in t["parameters"] if p.get("verified"))
        self.progress(f"    -> {len(chunks)} chunks, {len(table_rows)} VLM tables, {n_params} verified parameters, "
                      f"collection={domain}, standard={', '.join(meta.entities) or '-'}, type={meta.doc_type}, "
                      f"rev={meta.revision or '-'}, {time.time() - t0:.1f}s")
        return {"chunks": len(chunks), "tables": len(table_rows), "parameters": n_params}, warnings


def pdf_geometry(path: Path) -> dict:
    import pymupdf

    from techrag.geometry import PageGeom

    with pymupdf.open(str(path)) as doc:
        return {i + 1: PageGeom.from_page(page, i + 1) for i, page in enumerate(doc)}


def migrate_index(cfg: Config, store: Store, progress: Progress = lambda m: None) -> dict:
    """Bring documents indexed by an older version up to date WITHOUT re-embedding or VLM calls:
    store page geometry from the (unchanged) source PDFs, then re-run cell-level grounding on the cached
    VLM tables/parameters stored in the index. Tables whose original PDF text the old version dropped and
    that still cannot be grounded need a full re-index (reported)."""
    import json as _json

    from techrag.ingest.metadata import regex_version, series_key
    from techrag.ingest.vlm import ExtractedTable, validate
    from techrag.store import INDEX_FORMAT

    rep = {"documents": 0, "pages": 0, "tables_grounded": 0, "tables_not_grounded": 0, "parameters_verified": 0,
           "parameters_unverified": 0, "skipped": [], "needs_reindex": []}
    for doc in store.legacy_documents():
        path = store.source_path(doc, cfg.sources_dir)
        meta = dict(doc.metadata, index_format=INDEX_FORMAT)
        stem = Path(doc.path).stem
        meta.setdefault("doc_key", series_key(stem, doc.title))
        meta.setdefault("version", regex_version(f"{stem} {doc.title}"))
        if path.suffix.lower() != ".pdf":
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
        store.update_grounding(tables, params, chunk_ev)
        store.set_pages(doc.id, [(p, g.label, *g.encode()) for p, g in sorted(geoms.items())], meta)
        rep["documents"] += 1
        rep["pages"] += len(geoms)
    pairs = compute_supersedence(store.documents())
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
