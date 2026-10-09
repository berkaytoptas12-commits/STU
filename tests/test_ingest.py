from pathlib import Path

from conftest import HEADER, make_standard_pdf

from techrag.config import Config
from techrag.ingest.chunker import chunk_blocks, estimate_tokens, split_table
from techrag.ingest.cleaning import normalize_text, remove_headers_footers, toc_like_pages
from techrag.ingest.loaders import Block, load_document
from techrag.ingest.pipeline import build_chunks
from techrag.ingest.structure import SectionedBlock, detect_heading


def test_pdf_pipeline_cleans_and_structures(tmp_path: Path):
    pdf = make_standard_pdf(tmp_path / "acme.pdf")
    doc, blocks, chunks = build_chunks(Config(), pdf)

    assert doc.n_pages == 7
    assert len(doc.toc) == 5
    text = "\n".join(c.text for c in chunks)
    assert HEADER not in text, "running header must be removed"
    assert "Page 4" not in text, "footer must be removed"
    assert "Table of Contents" not in text, "printed TOC page must be skipped"

    by_section = {c.section[-1]: c for c in chunks if c.section and c.kind == "text"}
    assert "Label 310" in by_section["2.1 Label Encoding"].text
    assert by_section["2.1 Label Encoding"].page_start == 5
    assert by_section["2.1 Label Encoding"].section == ("2 Word Format", "2.1 Label Encoding")
    assert "10.0 V" in by_section["3 Electrical Interface"].text

    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert tables[0].text.startswith("Table 3-1 Timing parameters"), "caption is attached to the table"
    assert "| tRFC | 350 | - | ns |" in tables[0].text
    assert tables[0].page_start == 7


def test_normalize_text_dehyphenates_and_fixes_ligatures():
    assert normalize_text("termi-\nnation ﬁeld") == "termination field"
    assert normalize_text("a    b") == "a b"


def test_header_footer_removal_keeps_body():
    blocks = []
    for p in range(1, 8):
        blocks += [Block(p, "PCI Express Base Specification, Rev. 5.0"), Block(p, f"Body text page {p} unique."),
                   Block(p, f"Page {p} of 7")]
    out = remove_headers_footers(blocks, 7)
    texts = [b.text for b in out]
    assert texts == [f"Body text page {p} unique." for p in range(1, 8)]


def test_toc_page_detection():
    lines = "\n".join(f"4.{i} Section title ........ {10 + i}" for i in range(10))
    blocks = [Block(2, lines), Block(3, "Normal paragraph.\nAnother line.\nMore.\nAnd more.\nEnd.")]
    assert toc_like_pages(blocks) == {2}


def test_heading_detection():
    assert detect_heading("4.2.6.3 Polling", True) == (4, "4.2.6.3 Polling")
    assert detect_heading("Appendix B Compliance", True) == (1, "Appendix B Compliance")
    assert detect_heading("3.3 V supply rail", True) is None
    assert detect_heading("2. The device shall transmit", False) is None
    assert detect_heading("1 100 200 300", True) is None


def test_markdown_headings_become_sections(tmp_path: Path):
    md = tmp_path / "notes.md"
    md.write_text("# Top\n\nIntro text here.\n\n## 3.1 Speeds\n\nFast-mode is 400 kbit/s.\n", encoding="utf-8")
    _, _, chunks = build_chunks(Config(), md)
    assert any(c.section == ("Top", "3.1 Speeds") and "400 kbit/s" in c.text for c in chunks)


def test_chunker_respects_sections_and_overlap():
    sent = "The receiver shall tolerate a common mode voltage within the specified range. "
    blocks = [SectionedBlock(1, "1 A", "text", ("1 A",))]
    blocks += [SectionedBlock(1 + i // 3, sent * 6, "text", ("1 A",)) for i in range(9)]
    blocks += [SectionedBlock(9, "2 B\n\n" + sent * 3, "text", ("2 B",))]
    chunks = chunk_blocks(blocks, target_tokens=200, max_tokens=300, overlap_tokens=40, min_tokens=20)
    assert all(c.tokens <= 360 for c in chunks)
    assert {c.section for c in chunks} == {("1 A",), ("2 B",)}
    a_chunks = [c for c in chunks if c.section == ("1 A",)]
    assert len(a_chunks) >= 3
    assert any(c.overlap > 0 for c in a_chunks[1:])
    for c in a_chunks[1:]:
        if c.overlap:
            assert c.text[:c.overlap].strip() in a_chunks[a_chunks.index(c) - 1].text
    assert a_chunks[0].page_start == 1 and a_chunks[-1].page_end == 3


def test_split_table_repeats_header():
    rows = ["| P | V |", "|---|---|"] + [f"| param{i} | {i} ns |" for i in range(200)]
    parts = split_table("\n".join(rows), max_tokens=120)
    assert len(parts) > 1
    assert all(p.startswith("| P | V |\n|---|---|") for p in parts)
    assert all(estimate_tokens(p) <= 140 for p in parts)


def test_text_loader_form_feed_pages(tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("page one text\fpage two text", encoding="utf-8")
    doc = load_document(f)
    assert [b.page for b in doc.blocks] == [1, 2]
