"""Folder -> library index ingestion (incremental by SHA-256).

Per document: PyMuPDF parse -> cleanup -> metadata (standard entities, revision, type) -> eager VLM table
extraction on table-like pages -> section assignment -> chunking -> API embeddings -> one atomic write.
After a run, supersedence between revisions of the same standard is recomputed.
"""

from __future__ import annotations

import hashlib
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
from techrag.ingest.structure import SECTION_SEP, assign_sections, section_label
from techrag.ingest.vlm import ExtractedTable, TableExtractor, is_figure_page, select_pages
from techrag.llm import LLMClient
from techrag.store import Store

Progress = Callable[[str], None]

@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    chunks: int = 0
    tables: int = 0
    parameters: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        return (f"added={len(self.added)} updated={len(self.updated)} skipped={len(self.skipped)} "
                f"removed={len(self.removed)} failed={len(self.failed)} chunks={self.chunks} "
                f"vlm_tables={self.tables} parameters={self.parameters} time={self.seconds:.1f}s")


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
                 vision: Optional[LLMClient] = None):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.domains = domains
        self.progress = progress or (lambda msg: None)
        self.llm = llm
        self.extractor = None
        if vision is not None and cfg.vision.enabled:
            self.extractor = TableExtractor(vision, cfg.vlm_cache_dir, cfg.vision.dpi, cfg.vision.max_tokens,
                                            cfg.vision.concurrency)

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cfg.sources_dir.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def run(self, target: Optional[Path] = None, rebuild: bool = False, prune: bool = True) -> IngestReport:
        start = time.time()
        report = IngestReport()
        root = self.cfg.sources_dir
        root.mkdir(parents=True, exist_ok=True)
        target = Path(target) if target else root
        if not target.exists():
            raise FileNotFoundError(f"Source path not found: {target}")

        name, dim = self.embedder.name, self.embedder.dim
        if rebuild and self.store.get_meta("embedding_model") not in (None, name):
            self.progress(f"embedding model changed -> clearing the index and re-embedding with {name}")
            self.store.reset(name, dim)
        self.store.check_embedding_model(name, dim)
        files = discover(target)
        self.progress(f"{len(files)} file(s) found under {target}")

        seen: set[str] = set()
        for n, path in enumerate(files, 1):
            rel = self._rel(path)
            seen.add(rel)
            try:
                digest = sha256_file(path)
                existing = self.store.document_by_path(rel)
                if existing and existing.sha256 == digest and not rebuild:
                    report.skipped.append(rel)
                    continue
                self.progress(f"[{n}/{len(files)}] {rel}")
                stats, warnings = self.ingest_file(path, rel, digest)
                report.chunks += stats["chunks"]
                report.tables += stats["tables"]
                report.parameters += stats["parameters"]
                (report.updated if existing else report.added).append(rel)
                if warnings:
                    report.warnings[rel] = warnings
                    for w in warnings[:5]:
                        self.progress(f"    warning: {w}")
            except Exception as exc:  # one bad PDF must not stop a batch
                report.failed[rel] = f"{exc.__class__.__name__}: {exc}"
                self.progress(f"    FAILED: {exc}")

        if prune and target.resolve() == root.resolve():
            for doc in self.store.documents():
                if doc.path not in seen and not Path(doc.path).is_absolute():
                    self.store.delete_document(doc.id)
                    report.removed.append(doc.path)
                    self.progress(f"removed from index (file deleted): {doc.path}")

        pairs = compute_supersedence(self.store.documents())
        self.store.set_superseded(pairs)
        for doc_id, newer in pairs:
            if newer:
                d, nd = self.store.document(doc_id), self.store.document(newer)
                self.progress(f"superseded: '{d.title}' -> newer revision '{nd.title}'")
        report.seconds = time.time() - start
        return report

    # ---------------------------------------------------------------- one file
    def ingest_file(self, path: Path, rel: str, digest: str) -> tuple[dict, list[str]]:
        t0 = time.time()
        cfg = self.cfg
        doc: LoadedDocument = load_document(path, extract_tables=cfg.chunking.extract_tables)
        blocks = clean_blocks(doc.blocks, doc.n_pages, cfg.chunking.strip_headers_footers,
                              cfg.chunking.skip_toc_pages)
        warnings = list(doc.warnings)
        texts = page_texts(blocks)
        front = "\n".join(texts.get(p, "") for p in sorted(texts)[:3])
        domain = self.domains.classify_document(Path(rel), f"{doc.title}\n{front}")

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
        path = Path(doc.path)
        path = path if path.is_absolute() else cfg.sources_dir / path
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
