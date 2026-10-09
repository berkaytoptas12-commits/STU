from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from fakeserver import FakeOpenAI
from techrag.config import Config, EmbeddingConfig, LLMConfig, RerankerConfig, resource_path
from techrag.domains import DomainRegistry
from techrag.embeddings import APIEmbedder
from techrag.llm import LLMClient
from techrag.reranker import APIReranker

# Never touch the developer's real settings.json from tests.
os.environ["TECHRAG_NO_USER_SETTINGS"] = "1"

HEADER = "JEDEC Standard - Mini SDRAM Specification"


def _draw_table(page, top: float, rows: list[list[str]]) -> None:
    x0, col_w, row_h = 72, 110, 22
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 4, top + r * row_h + 15), cell, fontsize=10)
    for r in range(len(rows) + 1):
        page.draw_line((x0, top + r * row_h), (x0 + col_w * len(rows[0]), top + r * row_h))
    for c in range(len(rows[0]) + 1):
        page.draw_line((x0 + c * col_w, top), (x0 + c * col_w, top + row_h * len(rows)))


def make_standard_pdf(path: Path, std: str = "DDR4", trfc: str = "350", vdd: str = "1.2", rev: str = "C") -> Path:
    """A small 'standard' with title page, printed TOC, running header/footer, outline, a ruled timing table
    and a figure page - everything the ingestion pipeline has to cope with."""
    sections = [
        (1, "1 Scope", [f"This standard defines the {std} SDRAM interface. It specifies the electrical "
                        "interface, refresh operation and timing requirements."]),
        (1, "2 Electrical Interface", [f"The supply voltage VDD shall be {vdd} V +/- 0.06 V for {std} devices.",
                                       "The receiver input resistance shall be at least 12000 ohms."]),
        (1, "3 Refresh", [f"Refresh operation of {std} devices is described in this section."]),
        (2, "3.2 Refresh Timing", [f"The controller shall issue a REFRESH command every tREFI = 7.8 us. "
                                   f"The refresh cycle time tRFC for an 8Gb device is {trfc} ns.",
                                   "Table 3-1 Timing parameters"]),
        (2, "3.3 Refresh Timing Diagram", ["Figure 3-2 shows the refresh timing diagram.", "Figure 3-2 Refresh timing"]),
    ]
    doc = pymupdf.open()
    pages = [[f"JEDEC STANDARD {std} SDRAM", f"Revision {rev}"],
             ["Table of Contents", *[f"{t} {'.' * 30} {i + 3}" for i, (_, t, _) in enumerate(sections)]]]
    pages += [[title, *paras] for _, title, paras in sections]
    for i, lines in enumerate(pages):
        page = doc.new_page()
        page.insert_text((72, 40), HEADER, fontsize=8)
        y = 90
        for j, line in enumerate(lines):
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 120), line, fontsize=14 if j == 0 and i >= 2 else 11)
            y += 26 if j == 0 else 70
        if i == 5:
            _draw_table(page, y + 10, [["Parameter", "Min", "Max", "Unit"], ["tRFC", trfc, "-", "ns"],
                                       ["tREFI", "-", "7.8", "us"]])
        if i == 6:
            for k in range(40):  # a "timing diagram"
                page.draw_line((72 + k * 10, 400), (77 + k * 10, 380 if k % 2 else 420))
        page.insert_text((280, 800), f"Page {i + 1}", fontsize=8)
    doc.set_toc([[lvl, title, idx + 3] for idx, (lvl, title, _) in enumerate(sections)])
    doc.save(str(path))
    doc.close()
    return path


I2C_MD = """# I2C Bus Notes

## 3.1 Bus Speeds

Standard-mode supports bit rates up to 100 kbit/s. Fast-mode supports up to 400 kbit/s.
Fast-mode Plus (Fm+) supports up to 1 Mbit/s.

## 3.2 START and STOP Conditions

A HIGH to LOW transition on the SDA line while SCL is HIGH defines a START condition.
A LOW to HIGH transition on the SDA line while SCL is HIGH defines a STOP condition.
"""


@pytest.fixture()
def fake() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.paths.library_dir = str(tmp_path / "library")
    c.paths.cache_dir = str(tmp_path / "cache")
    for svc, model in (("llm", "qwen-test"), ("embedding", "emb-test"), ("reranker", "rr-test")):
        getattr(c, svc).base_url = "http://fake/v1"
        getattr(c, svc).model = model
    c.vision.min_page_score = 4.0
    return c


@pytest.fixture()
def sources(cfg: Config) -> Path:
    root = cfg.sources_dir
    (root / "ddr").mkdir(parents=True)
    (root / "i2c").mkdir(parents=True)
    make_standard_pdf(root / "ddr" / "JESD79-4C_DDR4_Mini.pdf", "DDR4", "350", "1.2", "C")
    make_standard_pdf(root / "ddr" / "JESD79-5_DDR5_Mini.pdf", "DDR5", "295", "1.1", "A")
    (root / "i2c" / "UM10204_i2c_notes.md").write_text(I2C_MD, encoding="utf-8")
    return root


@pytest.fixture()
def registry() -> DomainRegistry:
    return DomainRegistry.load(resource_path("resources", "domains.yaml"))


@pytest.fixture()
def clients(cfg: Config, fake: FakeOpenAI):
    t = fake.transport()
    llm = LLMClient(cfg.llm, transport=t)
    vision = LLMClient(cfg.llm, transport=t, service=cfg.vision_service())
    emb = APIEmbedder(cfg.embedding, transport=t)
    rr = APIReranker(cfg.reranker, transport=t)
    return {"llm": llm, "vision": vision, "embedder": emb, "reranker": rr}


@pytest.fixture()
def engine(cfg, sources, registry, clients):
    from techrag.engine import RAGEngine
    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store

    store = Store(cfg.db_path)
    report = Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    assert not report.failed, report.failed
    return RAGEngine(cfg, store=store, embedder=clients["embedder"], reranker=clients["reranker"],
                     llm=clients["llm"], vision=clients["vision"], domains=registry)
