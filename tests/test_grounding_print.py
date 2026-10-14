"""Cell grounding against how standards are typeset: subscripted symbols (t + RFC), parameter names wrapped over
two lines of their cell, and footnote marks raised after a value ("295" + superscript "1", which the PDF text
layer runs together as "2951"). The rule itself does not change: a value must sit at the crossing of its row
label and its column header."""

import pymupdf

from techrag.geometry import PageGeom, ground_cell, superscript_lines
from techrag.ingest.loaders import load_document
from techrag.ingest.vlm import ExtractedTable, validate


def _page(draw):
    doc = pymupdf.open()
    page = doc.new_page()
    draw(page)
    return page


def _geom(draw):
    return PageGeom.from_page(_page(draw), 1)


def _head(page, cols=(("Parameter", 72), ("Symbol", 250), ("Min", 330), ("Max", 370), ("Unit", 420))):
    for text, x in cols:
        page.insert_text((x, 100), text, fontsize=10)


def _sub(page, x, y, main, sub):
    page.insert_text((x, y), main, fontsize=10)
    page.insert_text((x + pymupdf.get_text_length(main, fontsize=10) + 0.3, y + 2.5), sub, fontsize=7)


def _mark(page, x, y, value, mark):
    page.insert_text((x, y), value, fontsize=10)
    page.insert_text((x + pymupdf.get_text_length(value, fontsize=10) + 0.2, y - 4), mark, fontsize=6)


# ------------------------------------------------------------------------- subscripts

def test_subscripted_symbol_is_found_as_one_label():
    def draw(page):
        _head(page)
        page.insert_text((72, 125), "Refresh cycle time", fontsize=10)
        _sub(page, 250, 125, "t", "RFC")
        page.insert_text((330, 125), "295", fontsize=10)
        page.insert_text((420, 125), "ns", fontsize=10)
    g = _geom(draw)
    assert g.find_phrase("tRFC"), [w.text for w in g.words]
    assert ground_cell(g, "295", ["tRFC"], ["Min"]).status == "grounded"
    assert ground_cell(g, "295", ["t RFC"], ["Min"]).status == "grounded", "VLM wrote the symbol with a space"
    assert ground_cell(g, "295", ["tRFC"], ["Max"]).status != "grounded", "still under its own column only"


def test_words_with_a_space_between_are_not_joined():
    def draw(page):
        page.insert_text((72, 125), "t", fontsize=10)
        page.insert_text((80, 125), "RFC", fontsize=10)
        page.insert_text((72, 150), "1", fontsize=10)
        page.insert_text((82, 150), "2", fontsize=10)
    g = _geom(draw)
    assert not g.find_phrase("tRFC") and not g.find_phrase("12")


# ---------------------------------------------------------------------- wrapped labels

def _wrapped_table(page):
    _head(page, (("Parameter", 72), ("Min", 330), ("Unit", 420)))
    page.insert_text((72, 125), "Refresh cycle time for", fontsize=10)
    page.insert_text((330, 125), "295", fontsize=10)
    page.insert_text((420, 125), "ns", fontsize=10)
    page.insert_text((72, 137), "8Gb devices", fontsize=10)
    page.insert_text((72, 162), "Refresh cycle time for", fontsize=10)
    page.insert_text((330, 162), "410", fontsize=10)
    page.insert_text((420, 162), "ns", fontsize=10)
    page.insert_text((72, 174), "16Gb devices", fontsize=10)


def test_label_wrapped_over_two_lines_finds_its_own_row():
    g = _geom(_wrapped_table)
    assert ground_cell(g, "295", ["Refresh cycle time for 8Gb devices"], ["Min"]).status == "grounded"
    assert ground_cell(g, "410", ["Refresh cycle time for 16Gb devices"], ["Min"]).status == "grounded"
    assert ground_cell(g, "410", ["Refresh cycle time for 8Gb devices"], ["Min"]).status != "grounded", \
        "the second line decides the row: 410 belongs to 16Gb"
    assert ground_cell(g, "295", ["Refresh cycle time for 16Gb devices"], ["Min"]).status != "grounded"


def test_wrapped_label_parts_must_be_stacked_in_one_cell():
    def draw(page):
        _head(page, (("Parameter", 72), ("Min", 330), ("Unit", 420)))
        page.insert_text((72, 125), "Refresh cycle time for", fontsize=10)
        page.insert_text((330, 125), "295", fontsize=10)
        page.insert_text((200, 137), "8Gb devices", fontsize=10)      # another column
        page.insert_text((72, 260), "8Gb devices", fontsize=10)       # far below
    g = _geom(draw)
    assert not g.find_wrapped("Refresh cycle time for 8Gb devices")
    assert ground_cell(g, "295", ["Refresh cycle time for 8Gb devices"], ["Min"]).status == "not_found"


# ---------------------------------------------------------------------- footnote marks

def _noted_table(page):
    _head(page)
    page.insert_text((72, 125), "Refresh cycle time", fontsize=10)
    page.insert_text((250, 125), "tRFC", fontsize=10)
    _mark(page, 330, 125, "295", "1")
    page.insert_text((420, 125), "ns", fontsize=10)
    page.insert_text((72, 150), "Exit time", fontsize=10)
    page.insert_text((250, 150), "tXS", fontsize=10)
    page.insert_text((370, 150), "1", fontsize=10)
    page.insert_text((420, 150), "us", fontsize=10)


def test_footnote_mark_is_split_from_the_value():
    page = _page(_noted_table)
    assert "2951" in [w[4] for w in page.get_text("words")], "the text layer runs the mark into the value"
    g = PageGeom.from_page(page, 1)
    assert [(w.text, w.sup) for w in g.words if w.text in ("295", "1")] == [("295", False), ("1", True), ("1", False)]
    assert ground_cell(g, "295", ["tRFC"], ["Min"]).status == "grounded"
    assert ground_cell(g, "295 (1)", ["tRFC"], ["Min"]).status == "grounded", "the value written with its mark"
    assert ground_cell(g, "2951", ["tRFC"], ["Min"]).status == "not_found", "2951 is not printed on the page"
    assert ground_cell(g, "1", ["tRFC"], ["Min"]).status == "not_found", "a footnote mark is never a value"
    assert ground_cell(g, "1", ["tXS"], ["Max"]).status == "grounded"
    again = PageGeom.decode(1, "", *g.encode())
    assert [w.sup for w in again.words] == [w.sup for w in g.words], "the mark survives the index"


def test_marks_are_set_apart_in_the_text_and_tables(tmp_path):
    def draw(page):
        page.insert_text((72, 80), "Table 1 Refresh", fontsize=11)
        _noted_table(page)
        for y in (88, 112, 137, 162):
            page.draw_line((70, y), (460, y))
        for x in (70, 245, 325, 365, 415, 460):
            page.draw_line((x, 88), (x, 162))
        _mark(page, 72, 220, "The cycle time is 295", "1")
        page.insert_text((180, 220), " ns.", fontsize=10)
    path = tmp_path / "noted.pdf"
    doc = pymupdf.open()
    draw(doc.new_page())
    doc.save(str(path))
    loaded = load_document(path)
    text = "\n".join(b.text for b in loaded.blocks)
    assert "295⁽1⁾" in text and "2951" not in text, text
    table = next(b.text for b in loaded.blocks if b.kind == "table")
    assert "295⁽1⁾" in table and "2951" not in table


def test_other_raised_text_is_left_alone():
    def draw(page):
        page.insert_text((72, 130), "The I", fontsize=10)
        page.insert_text((96.1, 126), "2", fontsize=6)
        page.insert_text((99.5, 130), "C bus, area 4 mm", fontsize=10)
        page.insert_text((178.5, 126), "2", fontsize=6)
        page.insert_text((182, 130), " and the 1", fontsize=10)
        page.insert_text((227.2, 126), "st", fontsize=6)
        page.insert_text((235, 130), " entry, BER 10", fontsize=10)
        page.insert_text((302.4, 126), "-12", fontsize=6)
        page.insert_text((312, 130), " max.", fontsize=10)
    lines = superscript_lines(_page(draw))
    marks = [(m.base, m.text, m.kind) for ln in lines for m in ln.marks]
    assert marks == [("10", "-12", "power")], marks
    assert lines[0].rendered.count("10^-12") == 1 and "mm2" in lines[0].rendered and "1st" in lines[0].rendered


# ------------------------------------------------------------------- whole table check

def test_vlm_table_with_print_details_is_grounded_cell_by_cell():
    def draw(page):
        _head(page)
        page.insert_text((72, 125), "Refresh cycle time for", fontsize=10)
        page.insert_text((72, 137), "8Gb devices", fontsize=10)
        _sub(page, 250, 125, "t", "RFC1")
        _mark(page, 330, 125, "295", "2")
        page.insert_text((420, 125), "ns", fontsize=10)
    g = _geom(draw)
    t = validate(ExtractedTable(1, "Table 1", ["Parameter", "Symbol", "Min", "Max", "Unit"],
                                [["Refresh cycle time for 8Gb devices", "tRFC1", "295", "-", "ns"]],
                                parameters=[{"parameter": "Refresh cycle time for 8Gb devices", "symbol": "tRFC1",
                                             "min": "295", "unit": "ns"},
                                            {"parameter": "Refresh cycle time for 8Gb devices", "symbol": "tRFC1",
                                             "max": "295", "unit": "ns"}]), g)
    assert t.status == "grounded", t.grounding
    assert t.parameters[0]["verified"], t.parameters[0]["evidence"]
    assert not t.parameters[1]["verified"] and t.parameters[1]["status"] == "conflict", "295 is under Min, not Max"
