"""Folder -> index ingestion (incremental: unchanged files are skipped by SHA-256)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import Embedder
from techrag.ingest.chunker import Chunk, chunk_blocks
from techrag.ingest.cleaning import clean_blocks
from techrag.ingest.loaders import SUPPORTED_SUFFIXES, TABLE_KIND, load_document
from techrag.ingest.structure import SECTION_SEP, assign_sections, section_label
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
    seconds: float = 0.0

    def summary(self) -> str:
        return (f"added={len(self.added)} updated={len(self.updated)} skipped={len(self.skipped)} "
                f"removed={len(self.removed)} failed={len(self.failed)} new_chunks={self.chunks} "
                f"time={self.seconds:.1f}s")


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


def embedding_text(title: str, section: tuple[str, ...], chunk: Chunk) -> str:
    """Contextual header + text: the document and section names carry a lot of retrieval signal."""
    label = section_label(section, depth=4)
    kind = "Table" if chunk.kind == TABLE_KIND else ""
    header = " | ".join(x for x in (title, label, kind) if x)
    return f"{header}\n\n{chunk.text}"


def build_chunks(cfg: Config, path: Path):
    doc = load_document(path, extract_tables=cfg.chunking.extract_tables)
    blocks = clean_blocks(doc.blocks, doc.n_pages, cfg.chunking.strip_headers_footers,
                          cfg.chunking.skip_toc_pages)
    sectioned = assign_sections(doc, blocks)
    chunks = chunk_blocks(sectioned, cfg.chunking.target_tokens, cfg.chunking.max_tokens,
                          cfg.chunking.overlap_tokens, cfg.chunking.min_tokens)
    return doc, blocks, chunks


class Ingestor:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder, domains: DomainRegistry,
                 progress: Optional[Progress] = None):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.domains = domains
        self.progress = progress or (lambda msg: None)

    def _rel(self, path: Path) -> str:
        root = self.cfg.sources_dir.resolve()
        p = path.resolve()
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return str(p)

    def run(self, target: Optional[Path] = None, rebuild: bool = False, prune: bool = True) -> IngestReport:
        start = time.time()
        report = IngestReport()
        root = self.cfg.sources_dir
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
                count, warnings = self.ingest_file(path, rel, digest)
                report.chunks += count
                (report.updated if existing else report.added).append(rel)
                if warnings:
                    report.warnings[rel] = warnings
                    for w in warnings[:5]:
                        self.progress(f"    warning: {w}")
            except Exception as exc:  # keep going; one bad PDF must not stop a batch
                report.failed[rel] = f"{exc.__class__.__name__}: {exc}"
                self.progress(f"    FAILED: {exc}")

        if prune and target.resolve() == root.resolve():
            for doc in self.store.documents():
                if doc.path not in seen and not Path(doc.path).is_absolute():
                    self.store.delete_document(doc.id)
                    report.removed.append(doc.path)
                    self.progress(f"removed from index (file deleted): {doc.path}")
        report.seconds = time.time() - start
        return report

    def ingest_file(self, path: Path, rel: str, digest: str) -> tuple[int, list[str]]:
        t0 = time.time()
        doc, blocks, chunks = build_chunks(self.cfg, path)
        sample = "\n".join(b.text for b in blocks[:80])
        domain = self.domains.classify_document(Path(rel), f"{doc.title}\n{sample}")
        warnings = list(doc.warnings)
        if not chunks:
            warnings.append("no text could be extracted (scanned PDF? run OCR first)")

        texts = [embedding_text(doc.title, c.section, c) for c in chunks]
        vectors = []
        batch = max(1, self.cfg.embedding.batch_size) * 4
        for i in range(0, len(texts), batch):
            vectors.append(self.embedder.embed_documents(texts[i:i + batch]))
            done = min(i + batch, len(texts))
            if len(texts) > batch:
                self.progress(f"    embedded {done}/{len(texts)} chunks")
        matrix = np.vstack(vectors) if vectors else np.zeros((0, self.embedder.dim), np.float32)

        rows = [{
            "ordinal": c.ordinal,
            "section": SECTION_SEP.join(c.section),
            "page_start": c.page_start,
            "page_end": c.page_end,
            "kind": c.kind,
            "overlap": c.overlap,
            "text": c.text,
        } for c in chunks]
        self.store.replace_document(
            path=rel, title=doc.title, domain=domain, sha256=digest, n_pages=doc.n_pages,
            toc=[list(t) for t in doc.toc], metadata=doc.metadata, warnings=warnings,
            chunks=rows, embeddings=matrix,
        )
        self.progress(f"    -> {len(chunks)} chunks, collection={domain}, pages={doc.n_pages}, "
                      f"{time.time() - t0:.1f}s")
        return len(chunks), warnings
