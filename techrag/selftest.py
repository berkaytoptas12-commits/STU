"""Self-test for packaged builds (`TechRAG.exe selftest result.json`): exercises PDF parsing, indexing,
retrieval, the bundled web UI/resources and the API without any model server."""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path


def _make_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "3.2 Refresh Timing", fontsize=14)
    page.insert_textbox(pymupdf.Rect(72, 100, 540, 200),
                        "DDR4 SDRAM. The refresh cycle time tRFC for an 8Gb device is 350 ns.", fontsize=11)
    rows = [["Parameter", "Min", "Max", "Unit"], ["tRFC", "350", "-", "ns"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((76 + c * 110, 255 + r * 22), cell, fontsize=10)
    for r in range(3):
        page.draw_line((72, 240 + r * 22), (512, 240 + r * 22))
    for c in range(5):
        page.draw_line((72 + c * 110, 240), (72 + c * 110, 284))
    doc.set_toc([[1, "3.2 Refresh Timing", 1]])
    doc.save(str(path))


def run() -> dict:
    from fastapi.testclient import TestClient

    from techrag import __version__
    from techrag.config import Config
    from techrag.domains import DomainRegistry
    from techrag.embeddings import HashEmbedder
    from techrag.engine import RAGEngine
    from techrag.ingest.pipeline import Ingestor
    from techrag.server import create_app
    from techrag.store import Store

    checks: dict = {"version": __version__}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.paths.library_dir = str(Path(tmp) / "library")
        cfg.paths.cache_dir = str(Path(tmp) / "cache")
        cfg.embedding.backend = "hash"
        cfg.reranker.enabled = False
        cfg.vision.enabled = False
        cfg.retrieval.query_rewrite = False
        (cfg.sources_dir / "ddr").mkdir(parents=True)
        _make_pdf(cfg.sources_dir / "ddr" / "JESD79-4_selftest.pdf")
        (cfg.sources_dir / "i2c").mkdir(parents=True)
        (cfg.sources_dir / "i2c" / "i2c.md").write_text("# I2C\n\nFast-mode supports up to 400 kbit/s.\n", "utf-8")

        domains = DomainRegistry.load(cfg.domains_file)
        store = Store(cfg.db_path)
        report = Ingestor(cfg, store, HashEmbedder(), domains).run()
        checks["ingest"] = report.summary()
        assert not report.failed, report.failed
        engine = RAGEngine(cfg, store=store, embedder=HashEmbedder(), reranker=None, vision=None, domains=domains)
        res = engine.retrieve("DDR4 tRFC refresh cycle time")
        checks["retrieval_scope"] = res.scope.reason
        assert res.passages and "350" in res.passages[0].text
        with TestClient(create_app(cfg, engine, warmup=False)) as client:
            assert "TechRAG" in client.get("/").text
            assert client.get("/static/app.js").status_code == 200
            info = client.get("/api/info").json()
            checks["documents"] = info["stats"]["documents"]
            png = client.get("/api/documents/1/page/1.png")
            assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
        store.publish(Path(tmp) / "published", cfg.sources_dir)
        checks["publish"] = (Path(tmp) / "published" / "index.sqlite").exists()
    try:
        import webview  # noqa: F401

        checks["pywebview"] = True
    except Exception as exc:  # only the desktop build needs it
        checks["pywebview"] = f"missing: {exc}"
    checks["ok"] = True
    return checks


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        result = run()
        code = 0
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
        code = 1
    text = json.dumps(result, indent=2, default=str)
    if argv:
        Path(argv[0]).write_text(text, encoding="utf-8")
    if sys.stdout:
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
