"""Ingestion with metadata, VLM tables and the typed parameter store; library publishing."""

from techrag.ingest.metadata import DocMeta, compute_supersedence, regex_doc_type, regex_revision
from techrag.ingest.pipeline import Ingestor
from techrag.ingest.vlm import ExtractedTable, page_table_score, validate, value_in_text
from techrag.store import Store


def test_documents_are_tagged_with_standard_revision_and_figures(engine):
    docs = {d.title: d for d in engine.store.documents()}
    d4, d5 = docs["JESD79-4C DDR4 Mini"], docs["JESD79-5 DDR5 Mini"]
    assert d4.entities == ["DDR4"] and d5.entities == ["DDR5"]
    assert d4.revision == "C" and d4.doc_type == "base"
    assert d4.metadata["figure_pages"] == [7]
    assert docs["UM10204 i2c notes"].entities == ["I2C"]


def test_vlm_tables_become_typed_parameters(engine):
    stats = engine.store.stats()
    assert stats["tables"] == 2 and stats["parameters"] == 4
    rows = engine.store.search_parameters("tRFC", 10)
    by_doc = {r.doc_title: r for r in rows if r.symbol == "tRFC"}
    assert by_doc["JESD79-5 DDR5 Mini"].min == "295" and by_doc["JESD79-5 DDR5 Mini"].verified
    assert by_doc["JESD79-4C DDR4 Mini"].min == "350"
    with engine.store.connect() as con:
        si = con.execute("SELECT min_si, base_unit FROM parameters WHERE symbol='tRFC' AND min='295'").fetchone()
    assert abs(si["min_si"] - 295e-9) < 1e-15 and si["base_unit"] == "s"
    # VLM table replaced the PDF-detected table in the chunk stream (no duplicate table chunk)
    with engine.store.connect() as con:
        n = con.execute("SELECT COUNT(*) FROM chunks WHERE kind='table'").fetchone()[0]
    assert n == 2


def test_vlm_results_are_cached(cfg, sources, registry, clients, fake):
    store = Store(cfg.db_path)
    Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    vlm_calls = sum(1 for c in fake.calls if "extract tables" in str(c["body"].get("messages", [{}])[0].get("content", "")))
    Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run(rebuild=True)
    vlm_calls_after = sum(1 for c in fake.calls if "extract tables" in str(c["body"].get("messages", [{}])[0].get("content", "")))
    assert vlm_calls == 2 and vlm_calls_after == 2, "second run must be served from the VLM cache"


def test_hallucinated_vlm_values_are_rejected(cfg, sources, registry, clients, fake):
    fake.vlm_hallucinate = True  # VLM 'reads' 7.9 where the page says 7.8
    store = Store(cfg.db_path)
    report = Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"]).run()
    assert store.search_parameters("tREFI tRFC", 10) == []
    # 1 of 4 numeric cells is wrong -> table verified ratio 0.75 < 0.8 -> table not used
    assert store.stats()["tables"] == 0
    assert any("failed value verification" in w for ws in report.warnings.values() for w in ws)


def test_validation_helpers():
    assert value_in_text("1.2", "VDD = 1.2 V") and not value_in_text("1.2", "VDD = 1.25 V")
    assert value_in_text("0,4", "0.4 V")
    t = validate(ExtractedTable(3, "Table 1", ["P", "Max"], [["a", "10"], ["b", "20"]],
                                parameters=[{"parameter": "a", "max": "10", "unit": "ns"},
                                            {"parameter": "b", "max": "21", "unit": "ns"}]), "a 10 b 20 ns")
    assert t.verified_ratio == 1.0
    assert t.parameters[0]["verified"] and not t.parameters[1]["verified"]
    assert t.parameters[0]["max_si"] == 10e-9


def test_page_selection_scores_tables_above_prose():
    table_page = "Table 4-2 DC characteristics\nParameter Symbol Min Typ Max Unit\nVIH 0.7 - - V\ntR - - 120 ns\n" * 2
    prose = "The transmitter shall enter Electrical Idle. The receiver detects the exit within 10 ns."
    assert page_table_score(table_page) >= 4.0 > page_table_score(prose)


def test_metadata_helpers_and_supersedence():
    assert regex_doc_type("PCIe Base 5.0 Errata") == "errata"
    assert regex_doc_type("ECN: Link equalization") == "ecn"
    assert regex_revision("JESD79-4C") == "C"
    assert regex_revision("Revision 5.0 Version 1.0") == "5.0"

    class D(DocMeta):
        pass

    def doc(i, rev, date="", dtype="base"):
        d = D(entities=["DDR4"], doc_type=dtype, revision=rev, doc_date=date)
        d.id, d.domain = i, "ddr"
        return d

    pairs = dict(compute_supersedence([doc(1, "B"), doc(2, "C"), doc(3, "", dtype="errata")]))
    assert pairs == {1: 2, 2: None, 3: None}
    assert dict(compute_supersedence([doc(1, "B", "2019-01"), doc(2, "A", "2020-06")]))[1] == 2


def test_publish_and_open_read_only(engine, cfg, tmp_path):
    dest = tmp_path / "share"
    engine.store.publish(dest, cfg.sources_dir, cfg.vlm_cache_dir)
    assert (dest / "index.sqlite").exists() and (dest / "sources" / "ddr").exists()
    assert any((dest / "cache" / "vlm").rglob("*.json"))
    ro = Store(dest / "index.sqlite", read_only=True)
    assert ro.stats()["documents"] == 3
    assert ro.search_bm25("refresh cycle time", 5)
    try:
        ro.delete_document(1)
    except PermissionError:
        pass
    else:
        raise AssertionError("read-only store must refuse writes")
