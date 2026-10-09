"""Folder -> library index ingestion (incremental by SHA-256).

Per document: PyMuPDF parse -> cleanup -> metadata (standard entities, revision, type) -> eager VLM table
extraction on table-like pages -> section assignment -> chunking -> API embeddings -> one atomic write.
After a run, supersedence between revisions of the same standard is recomputed.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import Embedder
from techrag.ingest.chunker import Chunk, chunk_blocks
from techrag.ingest.cleaning import clean_blocks
from techrag.ingest.loaders import SUPPORTED_SUFFIXES, TABLE_KIND, Block, LoadedDocument, load_document
from techrag.ingest.metadata import build_metadata, compute_supersedence, llm_metadata
from techrag.ingest.structure import SECTION_SEP, assign_sections, section_label
from techrag.ingest.vlm import ExtractedTable, TableExtractor, is_figure_page, select_pages
from techrag.llm import LLMClient
from techrag.store import Store

Progress = Callable[[str], None]

# A VLM table replaces the PDF-extracted text of that table only when most of its numbers check out.
MIN_TABLE_VERIFIED = 0.8


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


def merge_vlm_tables(blocks: list[Block], tables: dict[int, list[ExtractedTable]]) -> tuple[list[Block], list[ExtractedTable]]:
    """Replace PDF-detected table blocks on pages with verified VLM tables and insert the VLM tables after
    their caption block (or at the end of the page). Returns new blocks and the accepted tables (index = ref)."""
    accepted: list[ExtractedTable] = []
    good_pages = {p for p, ts in tables.items() if any(t.verified_ratio >= MIN_TABLE_VERIFIED for t in ts)}
    out: list[Block] = []
    by_page: dict[int, list[Block]] = defaultdict(list)
    order: list[int] = []
    for b in blocks:
        if b.page not in by_page:
            order.append(b.page)
        by_page[b.page].append(b)
    for page in sorted(set(order) | set(tables)):
        page_blocks = [b for b in by_page.get(page, []) if not (page in good_pages and b.kind == TABLE_KIND)]
        for t in tables.get(page, []):
            if t.verified_ratio < MIN_TABLE_VERIFIED or not t.markdown:
                continue
            accepted.append(t)
            block = Block(page, t.markdown, TABLE_KIND, ref=len(accepted) - 1)
            cap = t.caption.split(":")[0].strip().lower()[:40]
            pos = next((i + 1 for i, b in enumerate(page_blocks)
                        if cap and b.kind != TABLE_KIND and cap in b.text.lower()), len(page_blocks))
            page_blocks.insert(pos, block)
        out.extend(page_blocks)
    return out, accepted


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
                found, errors = self.extractor.extract(path, digest, pages, texts, self.progress)
                warnings.extend(errors[:20])
                blocks, accepted = merge_vlm_tables(blocks, found)
                rejected = sum(1 for ts in found.values() for t in ts if t.verified_ratio < MIN_TABLE_VERIFIED)
                if rejected:
                    warnings.append(f"{rejected} VLM table(s) failed value verification and were not used")

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
            "verified_ratio": t.verified_ratio, "parameters": t.parameters,
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
                 "page_end": c.page_end, "kind": c.kind, "overlap": c.overlap, "text": c.text} for c in chunks]
        metadata = dict(doc.metadata)
        metadata.update({"figure_pages": figure_pages, "llm": meta.llm})
        self.store.replace_document(
            path=rel, title=title, domain=domain, sha256=digest, n_pages=doc.n_pages,
            toc=[list(t) for t in doc.toc], metadata=metadata, warnings=warnings, chunks=rows, embeddings=matrix,
            entities=meta.entities, doc_type=meta.doc_type, revision=meta.revision, doc_date=meta.doc_date,
            tables=table_rows,
        )
        n_params = sum(len(t["parameters"]) for t in table_rows)
        self.progress(f"    -> {len(chunks)} chunks, {len(table_rows)} VLM tables, {n_params} parameters, "
                      f"collection={domain}, standard={', '.join(meta.entities) or '-'}, type={meta.doc_type}, "
                      f"rev={meta.revision or '-'}, {time.time() - t0:.1f}s")
        return {"chunks": len(chunks), "tables": len(table_rows), "parameters": n_params}, warnings


def build_chunks(cfg: Config, path: Path):
    """Parse + chunk one file without indexing (used by `techrag inspect`)."""
    doc = load_document(path, extract_tables=cfg.chunking.extract_tables)
    blocks = clean_blocks(doc.blocks, doc.n_pages, cfg.chunking.strip_headers_footers, cfg.chunking.skip_toc_pages)
    sectioned = assign_sections(doc, blocks)
    chunks = chunk_blocks(sectioned, cfg.chunking.target_tokens, cfg.chunking.max_tokens,
                          cfg.chunking.overlap_tokens, cfg.chunking.min_tokens)
    return doc, blocks, chunks
