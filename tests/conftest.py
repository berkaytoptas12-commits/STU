from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf
import pytest

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import HashEmbedder

ROOT = Path(__file__).resolve().parents[1]

HEADER = "ACME Avionics Bus Standard - Revision 1.0"

# (level, title, body paragraphs) - one section per page after the printed TOC.
SECTIONS = [
    (1, "1 Scope", ["This standard defines the ACME serial bus used between line replaceable units. "
                    "It specifies the electrical interface, word format and timing requirements."]),
    (1, "2 Word Format", ["Each ACME word shall contain 32 bits. Bits 1 through 8 contain the label. "
                          "Bits 9 and 10 are the Source/Destination Identifier (SDI).",
                          "Bits 30 and 31 contain the Sign/Status Matrix (SSM). Bit 32 is the parity bit and "
                          "the word shall use odd parity."]),
    (2, "2.1 Label Encoding", ["The label is transmitted most significant bit first. Label 310 carries the "
                               "present position latitude."]),
    (1, "3 Electrical Interface", ["The driver output differential voltage shall be 10.0 V +/- 1.0 V when "
                                   "transmitting a HI state. The receiver input resistance shall be at least 12000 ohms."]),
    (2, "3.2 Refresh Timing", ["The controller shall issue a REFRESH command every tREFI = 7.8 us. "
                               "The refresh cycle time tRFC for an 8Gb device is 350 ns.",
                               "Table 3-1 Timing parameters"]),
]

TABLE = [["Parameter", "Min", "Max", "Unit"],
         ["tRFC", "350", "-", "ns"],
         ["tREFI", "-", "7.8", "us"],
         ["Bit rate HS", "99", "101", "kbps"]]


def _draw_table(page, top: float) -> None:
    x0, col_w, row_h = 72, 110, 22
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 4, top + r * row_h + 15), cell, fontsize=10)
    for r in range(len(TABLE) + 1):
        y = top + r * row_h
        page.draw_line((x0, y), (x0 + col_w * len(TABLE[0]), y))
    for c in range(len(TABLE[0]) + 1):
        x = x0 + c * col_w
        page.draw_line((x, top), (x, top + row_h * len(TABLE)))


def make_standard_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    # Page 1: title page, page 2: printed table of contents (must be skipped).
    pages_text = [["ACME Avionics Bus Standard", "Revision 1.0"]]
    toc_lines = [f"{title} {'.' * 30} {i + 3}" for i, (_, title, _) in enumerate(SECTIONS)]
    pages_text.append(["Table of Contents", *toc_lines])
    for _, title, paras in SECTIONS:
        pages_text.append([title, *paras])

    for i, lines in enumerate(pages_text):
        page = doc.new_page()
        page.insert_text((72, 40), HEADER, fontsize=8)
        y = 90
        for j, line in enumerate(lines):
            rect = pymupdf.Rect(72, y, 540, y + 120)
            page.insert_textbox(rect, line, fontsize=14 if j == 0 and i >= 2 else 11)
            y += 26 if j == 0 else 70
        if i == len(pages_text) - 1:
            _draw_table(page, y + 10)
        page.insert_text((280, 800), f"Page {i + 1}", fontsize=8)

    toc = [[lvl, title, idx + 3] for idx, (lvl, title, _) in enumerate(SECTIONS)]
    doc.set_toc(toc)
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
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.paths.sources_dir = str(tmp_path / "sources")
    c.paths.index_dir = str(tmp_path / "index")
    c.paths.domains_file = str(ROOT / "config" / "domains.yaml")
    c.embedding.backend = "hash"
    c.reranker.enabled = False
    c.retrieval.query_rewrite = False
    return c


@pytest.fixture()
def sources(cfg: Config) -> Path:
    root = Path(cfg.paths.sources_dir)
    (root / "arinc").mkdir(parents=True)
    (root / "i2c").mkdir(parents=True)
    make_standard_pdf(root / "arinc" / "ACME_Bus_Standard.pdf")
    (root / "i2c" / "i2c_notes.md").write_text(I2C_MD, encoding="utf-8")
    return root


@pytest.fixture()
def registry() -> DomainRegistry:
    return DomainRegistry.load(ROOT / "config" / "domains.yaml")


@pytest.fixture()
def embedder() -> HashEmbedder:
    return HashEmbedder()


class FakeLLM:
    """Stands in for LLMClient. Plans with a fixed JSON and answers by quoting the first source."""

    def __init__(self, answer: str | None = None, plan: dict | None = None):
        self.answer = answer
        self.plan = plan
        self.calls: list[list[dict]] = []

    def health(self):
        return {"ok": True, "models": ["fake"], "error": None}

    def chat(self, messages, json_mode=False, max_tokens=None, temperature=None):
        return "".join(self.stream(messages, json_mode=json_mode))

    def stream(self, messages, json_mode=False, max_tokens=None, temperature=None):
        self.calls.append(list(messages))
        if json_mode:
            yield json.dumps(self.plan or {"standalone_question": "", "english_question": "",
                                           "search_queries": [], "keywords": []})
            return
        if self.answer is not None:
            text = self.answer
        else:
            user = messages[-1]["content"]
            m = re.search(r"\[1\][^\n]*\n(.+?)(?:\n\n---|\n\n=====)", user, re.S)
            first = m.group(1).split(".")[0] if m else "nothing"
            text = f"{first}. [1]"
        for i in range(0, len(text), 7):
            yield text[i:i + 7]
