"""Cell-level table grounding, per-table VLM merge and evidence locations on synthetic PDFs.

These use synthetic PDFs and a fake VLM; they check the mechanics, not the accuracy of a real VLM on real
standards."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from techrag.evidence import EvidenceLocator
from techrag.geometry import PageGeom
from techrag.ingest.loaders import TABLE_KIND, Block
from techrag.ingest.pipeline import merge_vlm_tables
from techrag.ingest.vlm import ExtractedTable, ground_parameter, validate
from techrag.tools import Source

HEAD = ["Parameter", "Symbol", "Min", "Max", "Unit"]


def table(page, rows, top=120.0, x0=60.0, col_w=95.0, row_h=20.0):
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 4, top + r * row_h + 14), cell, fontsize=9)
    for r in range(len(rows) + 1):
        page.draw_line((x0, top + r * row_h), (x0 + col_w * len(rows[0]), top + r * row_h))
    for c in range(len(rows[0]) + 1):
        page.draw_line((x0 + c * col_w, top), (x0 + c * col_w, top + row_h * len(rows)))


def one_page(rows, body="", rotation=0, crop=None):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    if body:
        page.insert_textbox(pymupdf.Rect(60, 40, 540, 110), body, fontsize=10)
    if rows:
        table(page, rows)
    if crop:
        page.set_cropbox(pymupdf.Rect(*crop))
    if rotation:
        page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    d = pymupdf.open(stream=data, filetype="pdf")
    return d, PageGeom.from_page(d[0], 1)


def param(**kw):
    p = {"parameter": "", "symbol": "", "min": "", "typ": "", "max": "", "unit": "", "conditions": "", "notes": ""}
    p.update(kw)
    return p


# -------------------------------------------------------------------- relation-level checks

def test_min_max_swap_and_wrong_unit_are_not_verified():
    _, g = one_page([HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"], ["Exit time", "tXS", "-", "410", "ns"]])
    good = param(parameter="Refresh cycle", symbol="tRFC", min="350", unit="ns")
    swapped = param(parameter="Refresh cycle", symbol="tRFC", max="350", unit="ns")
    unit = param(parameter="Refresh cycle", symbol="tRFC", min="350", unit="us")
    row = param(parameter="Exit time", symbol="tXS", min="350", unit="ns")   # value of another row
    for p in (good, swapped, unit, row):
        ground_parameter(p, g)
    assert good["verified"] and good["status"] == "grounded"
    assert {i["role"] for i in good["evidence"]["items"]} >= {"value", "label", "header", "unit"}
    assert not swapped["verified"] and swapped["status"] == "conflict"
    assert not unit["verified"] and unit["status"] == "unit_mismatch"
    assert not row["verified"] and row["status"] in ("not_found", "conflict")


def test_expressions_units_and_number_formats_are_not_collapsed():
    _, g = one_page([HEAD, ["Exit time", "tXS", "max(10 ns, 4 tCK)", "-", "-"], ["Skew", "tSK", "1,200", "-", "ps"],
                     ["Length", "L", "3", "-", "furlong"]])
    expr = param(parameter="Exit time", symbol="tXS", min="max(10 ns, 4 tCK)")
    amb = param(parameter="Skew", symbol="tSK", min="1,200", unit="ps")
    unknown = param(parameter="Length", symbol="L", min="3", unit="furlong")
    for p in (expr, amb, unknown):
        ground_parameter(p, g)
    assert expr["value_kind"] == "expression" and expr["min_si"] is None and expr["verified"]
    assert amb["min_si"] is None and any("ambiguous" in f for f in amb["evidence"]["flags"])
    assert unknown["min_si"] is None and unknown["base_unit"] == "" and any("no known SI" in f for f in unknown["evidence"]["flags"])


def test_repeated_numbers_bind_to_the_right_cell():
    body = "The value 350 also appears in this paragraph, and tXS is discussed here too."
    _, g = one_page([HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"], ["Exit time", "tXS", "350", "-", "ns"]], body)
    p = param(parameter="Exit time", symbol="tXS", min="350", unit="ns")
    ground_parameter(p, g)
    assert p["verified"]
    value = next(i for i in p["evidence"]["items"] if i["role"] == "value")
    label = next(i for i in p["evidence"]["items"] if i["role"] == "label")
    assert abs((value["rect"][1] + value["rect"][3]) / 2 - (label["rect"][1] + label["rect"][3]) / 2) < 3, "same row as tXS"
    trfc_row = g.find_phrase("tRFC")[0]
    assert value["rect"][1] > trfc_row.y1, "not the tRFC row's 350"


def test_table_continued_from_previous_page_uses_its_header():
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    table(p1, [HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"]])
    p2 = doc.new_page(width=595, height=842)
    table(p2, [["Exit time", "tXS", "-", "410", "ns"]])
    g1, g2 = PageGeom.from_page(doc[0], 1), PageGeom.from_page(doc[1], 2)
    ok = param(parameter="Exit time", symbol="tXS", max="410", unit="ns")
    bad = param(parameter="Exit time", symbol="tXS", min="410", unit="ns")
    ground_parameter(ok, g2, prev=g1)
    ground_parameter(bad, g2, prev=g1)
    assert ok["verified"] and any(i["role"] == "header" and i["page"] == 1 for i in ok["evidence"]["items"])
    assert not bad["verified"]


def test_footnote_on_the_next_page_is_linked():
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    table(p1, [HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"]])
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((60, 80), "NOTE 1 tRFC applies to all bank groups of the device.", fontsize=9)
    g1, g2 = PageGeom.from_page(doc[0], 1), PageGeom.from_page(doc[1], 2)
    p = param(parameter="Refresh cycle", symbol="tRFC", min="350", unit="ns",
              notes="NOTE 1 tRFC applies to all bank groups of the device.")
    ground_parameter(p, g1, nxt=g2)
    assert p["verified"] and p["evidence"]["notes_located"]
    assert any(i["role"] == "footnote" and i["page"] == 2 for i in p["evidence"]["items"])
    hallucinated = param(parameter="Refresh cycle", symbol="tRFC", min="350", unit="ns", notes="Only valid at 105 C.")
    ground_parameter(hallucinated, g1, nxt=g2)
    assert not hallucinated["evidence"]["notes_located"]


def test_scanned_page_gets_no_grounding():
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(0.5, 0.5, 0.5))
    g = PageGeom.from_page(page, 1)
    t = validate(ExtractedTable(1, "Table 1", ["P", "Min"], [["a", "1"]], parameters=[param(parameter="a", min="1")]), g)
    assert t.status == "no_text_layer" and not t.parameters[0]["verified"]


# ------------------------------------------------------------------------- merge per table

def test_failed_table_on_the_same_page_is_not_lost():
    a = Block(3, "| P | Min |\n|---|---|\n| tRFC | 350 |", TABLE_KIND, bbox=(60, 100, 300, 160))
    b = Block(3, "| P | Max |\n|---|---|\n| tREFI | 7.8 |", TABLE_KIND, bbox=(60, 400, 300, 460))
    text = Block(3, "Some text between the tables.")
    ta = ExtractedTable(3, "Table A", ["P", "Min"], [["tRFC", "350"]], status="grounded",
                        grounding={"bbox": [60, 100, 300, 160]})
    tb = ExtractedTable(3, "Table B", ["P", "Max"], [["tREFI", "7.9"]], status="partial")
    blocks, stored, _ = merge_vlm_tables([a, text, b], {3: [ta, tb]})
    kinds = [(bl.ref, bl.evidence, bl.text.split("\n")[-1]) for bl in blocks if bl.kind == TABLE_KIND]
    assert (0, True, "| tRFC | 350 |") in kinds, "A replaced by its grounded VLM version"
    assert (None, True, "| tREFI | 7.8 |") in kinds, "B's original PDF text stays as evidence"
    assert (1, False, "| tREFI | 7.9 |") in kinds, "B's VLM version is search-only"
    assert len(stored) == 2
    blocks, _, _ = merge_vlm_tables([a, text, b], {3: [ta]})
    assert any(bl.text == b.text and bl.evidence for bl in blocks), "only A extracted: B untouched"


def test_ambiguous_match_keeps_originals():
    a = Block(1, "| P | Min |\n|---|---|\n| tRFC | 350 |", TABLE_KIND)
    b = Block(1, "| P | Min |\n|---|---|\n| tRFC | 350 |", TABLE_KIND)
    t = ExtractedTable(1, "", ["P", "Min"], [["tRFC", "350"]], status="grounded")
    blocks, _, _ = merge_vlm_tables([a, b], {1: [t]})
    assert sum(1 for bl in blocks if bl.ref is None and bl.kind == TABLE_KIND) == 2


def test_ingestion_keeps_second_table_when_vlm_reads_only_one(cfg, make_engine, fake):
    root = cfg.sources_dir / "ddr"
    root.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 90), "Table 1 Refresh parameters DDR4", fontsize=10)
    table(page, [HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"]], top=100)
    page.insert_text((60, 300), "Table 2 Interval parameters", fontsize=10)
    table(page, [HEAD, ["Refresh interval", "tREFI", "-", "7.8", "us"]], top=310)
    doc.save(str(root / "JESD79-4_two_tables.pdf"))
    fake.vlm = lambda txt: {"tables": [{"caption": "Table 1 Refresh parameters DDR4", "columns": HEAD,
                                        "rows": [["Refresh cycle", "tRFC", "350", "-", "ns"]],
                                        "parameters": [param(parameter="Refresh cycle", symbol="tRFC", min="350", unit="ns")]}]}
    engine = make_engine()
    with engine.store.connect() as con:
        texts = [r["text"] for r in con.execute("SELECT text FROM chunks WHERE evidence=1")]
    assert any("7.8" in t and "tREFI" in t for t in texts), "table 2 must survive"
    assert [r.symbol for r in engine.store.search_parameters("tRFC tREFI", 5, verified_only=True)] == ["tRFC"]


def test_unverified_rows_never_become_citable_through_tools(cfg, sources, registry, clients, fake):
    from techrag.engine import RAGEngine
    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store
    from techrag.tools import SourceRegistry, ToolExecutor

    fake.vlm_hallucinate = True
    store = Store(cfg.db_path)
    Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    engine = RAGEngine(cfg, store=store, embedder=clients["embedder"], reranker=clients["reranker"],
                       llm=clients["llm"], vision=clients["vision"], domains=registry)
    res = engine.retrieve("DDR4 tREFI")
    reg = SourceRegistry()
    ex = ToolExecutor(store, engine.retriever, reg, res.plan, engine.render_page, scope=res.scope,
                      resolve_standard=registry.detect_entities)
    out = ex.run("get_parameter", {"name": "tREFI"})
    assert not out.new_sources and "could not be verified" in out.text
    assert all(s.kind != "parameter" or s.verified for s in reg.items)
    out = ex.run("get_table", {"query": "Timing parameters"})
    assert all(s.kind == "passage" for s in out.new_sources), "unverified table -> original page text instead"
    assert not any("7.9" in s.text for s in out.new_sources)


# ----------------------------------------------------------------------------- locations

class _Store:
    def __init__(self, geoms):
        self.geoms = geoms
        self.has_geometry = True

    def page_geom(self, doc_id, page):
        return self.geoms.get(page)


def _locator(geoms, legacy=False, path="x.pdf"):
    doc = SimpleNamespace(id=1, path=path, legacy=legacy, sha256="ab" * 32)
    return EvidenceLocator(_Store(geoms), SimpleNamespace(docs=lambda: {1: doc}))


def _dark_box(pix):
    import numpy as np

    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].mean(axis=2)
    ys, xs = np.nonzero(a < 128)
    return pix.width, pix.height, xs.min(), ys.min(), xs.max(), ys.max()


@pytest.mark.parametrize("rotation,crop", [(0, None), (90, None), (180, (40, 30, 560, 700)), (270, (20, 25, 500, 800)),
                                           (90, (50, 30, 550, 700))])
def test_highlight_stays_on_the_text_for_rotated_and_cropped_pages(rotation, crop):
    d, g = one_page([], body="The refresh cycle time tRFC is 295 ns.", rotation=rotation, crop=crop)
    src = Source(1, "passage", "The refresh cycle time tRFC is 295 ns.", doc_id=1, page_start=1, page_end=1)
    rec = _locator({1: g}).locate("tRFC is 295 ns [1].", src)
    assert rec["status"] == "located"
    rects = [rc for r in rec["regions"] for rc in r["rects"]]
    for dpi in (72, 150):  # any render scale: the overlay uses fractions of the rendered page
        w, h, x0, y0, x1, y1 = _dark_box(d[0].get_pixmap(dpi=dpi))
        ux0, uy0 = min(r[0] for r in rects) * w, min(r[1] for r in rects) * h
        ux1, uy1 = max(r[2] for r in rects) * w, max(r[3] for r in rects) * h
        tol = 0.012 * max(w, h)
        assert ux0 - tol <= x0 and uy0 - tol <= y0 and x1 <= ux1 + tol and y1 <= uy1 + tol, (rotation, dpi)
        assert (ux1 - ux0) * (uy1 - uy0) < 0.2 * w * h, "a tight region, not the whole page"


def test_pdf_coordinates_are_unrotated_user_space():
    d, g = one_page([], body="Anchor text 12345 here.", rotation=90, crop=(50, 30, 550, 700))
    box = g.find_phrase("12345")[0]
    x0, y0, x1, y1 = g.to_pdf(box.rect())
    # the word was placed near x=60..., y=40...55 from the top of an 842 pt page (PDF y grows upwards)
    assert 60 <= x0 < x1 < 300 and 842 - 60 < y0 < y1 < 842 - 35


def test_repeated_number_in_a_table_passage_highlights_the_right_row():
    _, g = one_page([HEAD, ["Refresh cycle", "tRFC", "350", "-", "ns"], ["Exit time", "tXS", "350", "-", "ns"]],
                    body="Note that 350 appears twice.")
    md = "| " + " | ".join(HEAD) + " |\n|---|---|---|---|---|\n| Refresh cycle | tRFC | 350 | - | ns |\n| Exit time | tXS | 350 | - | ns |"
    src = Source(1, "table", md, doc_id=1, page_start=1, page_end=1)
    rec = _locator({1: g}).locate("The minimum exit time tXS is 350 ns [1].", src)
    value = next(r for r in rec["regions"] if r["role"] == "value")
    txs = g.to_display(g.find_phrase("tXS")[0].rect())
    trfc = g.to_display(g.find_phrase("tRFC")[0].rect())
    vy = (value["rects"][0][1] + value["rects"][0][3]) / 2
    assert abs(vy - (txs[1] + txs[3]) / 2) < 0.01 and abs(vy - (trfc[1] + trfc[3]) / 2) > 0.01
    assert {r["role"] for r in rec["regions"]} >= {"value", "label", "header"}


def test_evidence_spanning_two_pages():
    doc = pymupdf.open()
    for text in ("The supply voltage VDD shall be 1.2 V.", "The termination voltage VTT shall be 0.6 V."):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 200), text, fontsize=11)
    geoms = {i + 1: PageGeom.from_page(doc[i], i + 1) for i in range(2)}
    src = Source(1, "passage", "The supply voltage VDD shall be 1.2 V.\n\nThe termination voltage VTT shall be 0.6 V.",
                 doc_id=1, page_start=1, page_end=2)
    rec = _locator(geoms).locate("VDD is 1.2 V and VTT is 0.6 V [1].", src)
    assert rec["status"] == "located" and {r["page"] for r in rec["regions"]} == {1, 2}


def test_missing_coordinates_are_stated_not_invented():
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(0.3, 0.3, 0.3))
    scanned = {1: PageGeom.from_page(page, 1)}
    src = Source(1, "passage", "tRFC is 295 ns.", doc_id=1, page_start=1, page_end=1)
    rec = _locator(scanned).locate("tRFC is 295 ns [1].", src)
    assert rec["status"] == "no_text_layer" and rec["regions"] == []
    assert _locator(scanned, legacy=True).locate("tRFC is 295 ns [1].", src)["status"] == "legacy_index"
    assert _locator({}, path="notes.md").locate("tRFC is 295 ns [1].", src)["status"] == "not_pdf"
    _, g = one_page([], body="Completely different text on this page.")
    rec = _locator({1: g}).locate("tRFC is 295 ns [1].", src)
    assert rec["status"] == "not_located" and rec["regions"] == [] and rec["quotes"]
    calc = Source(2, "calc", "295e-9*1e9 = 295", extra={"inputs": [{"value": "295e-9", "sources": [1]}]})
    assert _locator({1: g}).locate("295 [2]", calc)["status"] == "not_applicable"


# -------------------------------------------------------------------- integration / safety

def _digest(paths):
    return {p: (hashlib.sha256(Path(p).read_bytes()).hexdigest(), Path(p).stat().st_mtime_ns) for p in paths}


def test_highlighting_never_modifies_the_original_pdf(cfg, sources, make_engine):
    pdfs = sorted(str(p) for p in Path(sources).rglob("*.pdf"))
    before = _digest(pdfs)
    engine = make_engine()
    fin = list(engine.ask_stream("DDR5 tRFC değeri nedir?"))[-1]
    regions = [r for c in fin["verification"]["details"] for e in c["evidence"] for r in e["regions"]]
    assert regions, "the answer's evidence is located"
    for d in engine.store.documents():
        engine.render_page(d.id, 1)
    assert _digest(pdfs) == before
    for pdf in pdfs:
        with pymupdf.open(pdf) as doc:
            assert not any(list(page.annots()) for page in doc), "no annotations written into the PDF"


def test_answer_regions_carry_persistent_anchors(engine):
    fin = list(engine.ask_stream("DDR5 tRFC değeri nedir?"))[-1]
    rec = next(e for c in fin["verification"]["details"] if c["outcome"] == "kept" for e in c["evidence"]
               if e["regions"])
    r = rec["regions"][0]
    doc = engine.store.document(r["doc_id"])
    assert r["doc_sha256"] == doc.sha256 and r["page"] >= 1 and r["pdf_rects"] and r["quote"]
    assert all(0 <= v <= 1 for rc in r["rects"] for v in rc)
    assert len(fin["cite_claims"]) == len([1 for _ in __import__("re").finditer(r"\[\d+\]", fin["answer"])])


def test_page_labels_are_kept_apart_from_physical_pages(cfg, make_engine):
    root = cfg.sources_dir / "general"
    root.mkdir(parents=True)
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"Page body {i}", fontsize=11)
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 2, "prefix": "A-", "style": "D", "firstpagenum": 1}])
    doc.save(str(root / "labels.pdf"))
    engine = make_engine()
    d = engine.store.documents()[0]
    assert engine.store.page_labels(d.id) == {1: "i", 2: "ii", 3: "A-1"}


def test_legacy_index_is_migrated_without_reembedding(cfg, sources, registry, clients, fake):
    from techrag.ingest.pipeline import Ingestor, migrate_index
    from techrag.store import Store

    store = Store(cfg.db_path)
    Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    with store.connect() as con:  # turn it into an index written by the previous version
        for sql in ("ALTER TABLE chunks DROP COLUMN evidence", "ALTER TABLE tables DROP COLUMN status",
                    "ALTER TABLE tables DROP COLUMN grounding", "ALTER TABLE parameters DROP COLUMN status",
                    "ALTER TABLE parameters DROP COLUMN evidence", "ALTER TABLE parameters DROP COLUMN value_kind",
                    "DROP TABLE pages", "UPDATE parameters SET verified = 1",
                    "UPDATE documents SET metadata = json_remove(metadata, '$.index_format', '$.doc_key')",
                    "UPDATE meta SET value = '2' WHERE key = 'schema_version'"):
            con.execute(sql)
    ro = Store(cfg.db_path, read_only=True)
    assert ro.search_parameters("tRFC", 5, verified_only=True) == [], "old 'verified' rows are not trusted"
    assert ro.stats()["legacy_documents"] == 3 and not ro.has_geometry
    store = Store(cfg.db_path)  # writable open migrates the schema
    assert store.search_parameters("tRFC", 5, verified_only=True) == []
    assert {r.status for r in store.search_parameters("tRFC tREFI", 10)} == {"legacy_unchecked"}
    n_emb = sum(1 for c in fake.calls if c["path"].endswith("/embeddings"))
    n_vlm = sum(1 for c in fake.calls if "extract tables" in json.dumps(c["body"].get("messages", [])))
    rep = migrate_index(cfg, store)
    assert rep["documents"] == 3 and rep["pages"] == 14 and rep["parameters_verified"] == 4
    assert sum(1 for c in fake.calls if c["path"].endswith("/embeddings")) == n_emb, "no re-embedding"
    assert sum(1 for c in fake.calls if "extract tables" in json.dumps(c["body"].get("messages", []))) == n_vlm
    assert store.stats()["legacy_documents"] == 0 and store.page_geom(1, 6) is not None
    assert {r.symbol for r in store.search_parameters("tRFC tREFI", 10, verified_only=True)} == {"tRFC", "tREFI"}


def test_multi_level_headers_bind_values_to_their_column_group():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    x0, w, top, h = 40.0, 85.0, 120.0, 20.0
    # group header centred over its two sub-columns (as merged cells usually are)
    for label, first in (("DDR4-1600", 2), ("DDR4-3200", 4)):
        cx = x0 + (first + 1) * w
        page.insert_text((cx - 22, top + 14), label, fontsize=9)
    for r, row in enumerate([["Parameter", "Symbol", "Min", "Max", "Min", "Max"], ["Refresh", "tRFC", "350", "-", "260", "-"]]):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * w + 30, top + (r + 1) * h + 14), cell, fontsize=9)
    g = PageGeom.from_page(page, 1)
    ok = param(parameter="Refresh", symbol="tRFC", min="260", conditions="DDR4-3200")
    wrong_group = param(parameter="Refresh", symbol="tRFC", min="260", conditions="DDR4-1600")
    ok2 = param(parameter="Refresh", symbol="tRFC", min="350", conditions="DDR4-1600")
    for p in (ok, wrong_group, ok2):
        ground_parameter(p, g)
    assert ok["verified"] and any(i["role"] == "condition" for i in ok["evidence"]["items"])
    assert ok2["verified"]
    assert not wrong_group["verified"] and wrong_group["status"] == "conflict"


def test_row_conditions_identify_rows_under_a_merged_symbol_cell():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    x0, w, top, h = 40.0, 95.0, 120.0, 20.0
    for c, cell in enumerate(["Symbol", "Density", "Min", "Unit"]):
        page.insert_text((x0 + c * w + 4, top + 14), cell, fontsize=9)
    page.insert_text((x0 + 4, top + h + 24), "tRFC", fontsize=9)   # merged cell, vertically centred on 2 rows
    for r, (dens, val) in enumerate((("8 Gb", "350"), ("16 Gb", "550"))):
        y = top + (r + 1) * h + 14
        for c, cell in ((1, dens), (2, val), (3, "ns")):
            page.insert_text((x0 + c * w + 4, y), cell, fontsize=9)
    g = PageGeom.from_page(page, 1)
    p16 = param(symbol="tRFC", parameter="Refresh cycle time", min="550", unit="ns", conditions="16 Gb")
    p8 = param(symbol="tRFC", parameter="Refresh cycle time", min="550", unit="ns", conditions="8 Gb")
    ground_parameter(p16, g)
    ground_parameter(p8, g)
    assert p16["verified"]
    assert not p8["verified"], "550 is the 16 Gb row, not the 8 Gb row"


def test_row_condition_must_be_in_the_value_row():
    _, g = one_page([["Symbol", "Density", "Min", "Unit"], ["tRFC", "8 Gb", "350", "ns"], ["tRFC", "16 Gb", "550", "ns"]])
    wrong = param(symbol="tRFC", min="550", unit="ns", conditions="8 Gb")
    right = param(symbol="tRFC", min="550", unit="ns", conditions="16 Gb")
    ground_parameter(wrong, g)
    ground_parameter(right, g)
    assert right["verified"] and not wrong["verified"]
