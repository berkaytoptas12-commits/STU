"""VLM page selection: only pages with a real table signal (caption, a table found by the PDF table detector,
or text aligned in columns) are sent to the vision model; running text full of values is not."""

import pymupdf

from techrag.geometry import PageGeom
from techrag.ingest.loaders import PageStats
from techrag.ingest.vlm import aligned_rows, page_candidates, page_table_score, table_captions

PROSE = ("The minimum input high voltage VIH(min) is 0.7 V and the maximum input low voltage VIL(max) is 0.3 V "
         "under nominal conditions. The typical rise time is 120 ns, the maximum fall time is 150 ns and the "
         "parameter tRFC is 295 ns for 8Gb devices. Values in the range 1.1 V to 1.3 V are the default; the unit "
         "for currents is mA, typically 35 mA, at most 60 mA. Notes: the symbol tREFI denotes 7.8 us, and the "
         "description of each value applies at 85 C. The supply current IDD0 is 52 mA and IDD2N is 38 mA.")


def _page(doc, width=595, height=842):
    return doc.new_page(width=width, height=height)


def _prose_page(doc):
    page = _page(doc)
    page.insert_textbox(pymupdf.Rect(72, 72, 523, 770), (PROSE + "\n\n") * 3, fontsize=11)
    return page


def _borderless_table(page, top=120, x0=72):
    rows = [["Symbol", "Min", "Typ", "Max", "Unit"], ["VIH", "0.7", "-", "1.3", "V"], ["VIL", "-0.3", "-", "0.3", "V"],
            ["tRFC", "295", "-", "-", "ns"], ["tREFI", "-", "7.8", "-", "us"], ["IDD0", "-", "52", "60", "mA"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * 90, top + r * 18), cell, fontsize=10)


def _geom(page, no=1):
    return PageGeom.from_page(page, no)


def test_running_text_full_of_values_is_not_a_table_page():
    doc = pymupdf.open()
    page = _prose_page(doc)
    text = page.get_text()
    assert aligned_rows(_geom(page)) == 0 and table_captions(text) == 0
    assert page_table_score(text) == 0.0, "values and 'min/max/typical' words alone are no table signal"
    assert page_candidates({1: text}, {1: PageStats(drawings=0)}, 4.0, {1: _geom(page)}) == []


def test_two_column_text_is_not_aligned_columns():
    doc = pymupdf.open()
    page = _page(doc)
    page.insert_textbox(pymupdf.Rect(60, 72, 290, 780), PROSE * 2, fontsize=10)
    page.insert_textbox(pymupdf.Rect(310, 72, 540, 780), PROSE * 2, fontsize=10)
    assert aligned_rows(_geom(page)) == 0


def test_borderless_table_is_found_by_its_columns():
    doc = pymupdf.open()
    page = _page(doc)
    page.insert_textbox(pymupdf.Rect(72, 300, 523, 600), PROSE, fontsize=11)
    _borderless_table(page)
    text = page.get_text()
    assert table_captions(text) == 0, "no caption on the page"
    assert aligned_rows(_geom(page)) >= 5
    assert page_candidates({1: text}, {1: PageStats()}, 4.0, {1: _geom(page)}) == [1]


def test_sideways_table_on_a_rotated_page_is_found():
    doc = pymupdf.open()
    page = _page(doc, 842, 595)
    _borderless_table(page)
    page.set_rotation(90)
    assert aligned_rows(_geom(page)) >= 5


def test_caption_lines_and_references_in_running_text():
    assert table_captions("Table 4-2 — DC characteristics") == 1
    assert table_captions("Table 3-1 Timing parameters\nTABLE 12: Pin list\nTablo 5.3 Zamanlama") == 3
    assert table_captions("Table 7 (continued)") == 1
    assert table_captions("the values in\nTable 4-2 shows the limits for each speed bin\nand Table 3 lists") == 0
    assert page_table_score("Table 4-2 shows the limits: VIH 0.7 V, VIL 0.3 V, min 1 V, max 2 V") == 0.0


def test_ruled_table_found_by_the_pdf_detector_counts_without_caption():
    text = "Parameter Min Max Unit\ntRFC 295 - ns\ntREFI - 7.8 us"
    assert page_table_score(text, drawings=12, pdf_tables=1) >= 4.0
    assert page_table_score(text, drawings=12, pdf_tables=0) == 0.0


def test_ingest_sends_only_table_pages_to_the_vlm(cfg, registry, clients, fake):
    """A cover, a prose page full of values, a borderless table page and a ruled table page with caption:
    the VLM sees the last two only."""
    from conftest import _draw_table
    from test_quality import vlm_pages

    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store

    path = cfg.sources_dir / "ddr" / "DDR4_Mixed.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    _page(doc).insert_text((72, 90), "JEDEC STANDARD DDR4 SDRAM Supplement", fontsize=14)
    _prose_page(doc)
    _borderless_table(_page(doc))
    page = _page(doc)
    page.insert_text((72, 130), "Table 2-1 Timing parameters", fontsize=11)
    _draw_table(page, 150, [["Parameter", "Min", "Max", "Unit"], ["tX1", "101", "-", "ns"], ["tY1", "201", "-", "ns"]])
    doc.save(str(path))
    doc.close()
    fake.vlm = lambda text: {"tables": []}
    store = Store(cfg.db_path)
    Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    assert vlm_pages(fake) == [3, 4]
    v = store.quality()["ddr/DDR4_Mixed.pdf"]["data"]["vlm"]
    assert v["selected"] == [3, 4] and v["filtered_out"] == 2
