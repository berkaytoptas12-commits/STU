"""Standard/version scope and document precedence (synthetic documents + fake model server)."""

import json

from conftest import answer_calls, make_standard_pdf
from techrag.query import QueryPlan
from techrag.retrieval import Passage
from techrag.tools import SourceRegistry, ToolExecutor, parameter_conflicts


def final(engine, question, **kw):
    events = list(engine.ask_stream(question, **kw))
    return events, next(e for e in events if e["type"] == "final")


def _ddr4_only(cfg):
    root = cfg.sources_dir / "ddr"
    root.mkdir(parents=True)
    make_standard_pdf(root / "JESD79-4C_DDR4_Mini.pdf", "DDR4", "350", "1.2", "C")
    (root / "controller_notes.md").write_text(
        "# Controller notes\n\nOur memory controller assumes a refresh cycle time tRFC of 999 ns.\n", encoding="utf-8")


def _executor(engine, question, **kw):
    res = engine.retrieve(question, **kw)
    return res, ToolExecutor(engine.store, engine.retriever, SourceRegistry(), res.plan, engine.render_page,
                             scope=res.scope, resolve_standard=engine.domains.detect_entities, doc_meta=engine.doc_meta)


def test_missing_ddr5_is_reported_not_answered_from_ddr4(cfg, make_engine, fake):
    _ddr4_only(cfg)
    engine = make_engine()
    res = engine.retrieve("DDR5 tRFC değeri nedir?")
    assert res.scope.reason == "entity_missing" and res.scope.missing == ["DDR5"] and not res.passages
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    assert "DDR5" in fin["answer"] and "350" not in fin["answer"] and "999" not in fin["answer"]
    assert fin["verification"]["status"] == "not_found"
    assert not answer_calls(fake), "no answer may be generated from a sibling standard's documents"


def test_untagged_documents_are_not_an_exact_standard_match(cfg, make_engine):
    _ddr4_only(cfg)
    engine = make_engine()
    docs = {d.title: d for d in engine.store.documents()}
    untagged = docs["controller notes"]
    assert untagged.entities == [] and untagged.domain == "ddr"
    res = engine.retrieve("DDR4 tRFC refresh cycle time")
    assert res.scope.reason == "entity" and untagged.id not in res.scope.doc_ids
    assert all("999" not in p.text for p in res.passages)


def test_tools_cannot_widen_a_named_standard(cfg, make_engine):
    _ddr4_only(cfg)
    engine = make_engine()
    res, ex = _executor(engine, "DDR4 tRFC?")
    out = ex.run("get_parameter", {"name": "tRFC", "standard": "DDR5"})
    assert not out.new_sources and "outside this question's scope" in out.text
    out = ex.run("search_docs", {"query": "refresh cycle time", "standard": "DDR5"})
    assert not out.new_sources
    out = ex.run("search_docs", {"query": "refresh cycle time"})
    assert out.new_sources and all(s.entities == ["DDR4"] for s in out.new_sources)
    # a page outside the scope cannot be pulled in by id
    untagged = next(d for d in engine.store.documents() if not d.entities)
    out = ex.run("get_page_image", {"document_id": untagged.id, "page": 1})
    assert "outside this question's scope" in out.text and not out.new_sources


def test_tool_standard_that_is_not_loaded_returns_nothing(engine):
    res, ex = _executor(engine, "refresh cycle time")
    assert res.scope.reason in ("all", "domain")
    out = ex.run("get_parameter", {"name": "tRFC", "standard": "DDR3"})
    assert not out.new_sources and "Do not substitute" in out.text


def test_user_selection_survives_tool_calls(engine):
    i2c = [d.id for d in engine.store.documents() if d.domain == "i2c"]
    res, ex = _executor(engine, "bit rate", doc_ids=i2c)
    assert res.scope.reason == "user"
    out = ex.run("search_docs", {"query": "DDR5 refresh cycle time tRFC"})
    assert all(s.doc_id in i2c for s in out.new_sources)
    out = ex.run("get_parameter", {"name": "tRFC", "standard": "DDR5"})
    assert not out.new_sources and "none of the documents the user selected" in out.text
    # a collection selection (domains) is kept as well
    res, ex = _executor(engine, "speed", domains=["i2c"])
    out = ex.run("search_docs", {"query": "tRFC refresh"})
    assert all(s.doc_id in i2c for s in out.new_sources)


def test_comparison_collects_evidence_per_standard(engine):
    res = engine.retrieve("DDR4 vs DDR5 tRFC farkı")
    assert set(res.scope.per_entity) == {"DDR4", "DDR5"}
    assert {e for p in res.passages for e in p.entities} == {"DDR4", "DDR5"}
    assert {r.doc_title for r in res.parameters} == {"JESD79-4C DDR4 Mini", "JESD79-5 DDR5 Mini"}


def test_ambiguous_scope_asks_back(engine, fake):
    events, fin = final(engine, "tRFC değeri nedir?")
    clar = next(e for e in events if e["type"] == "clarify")
    labels = " ".join(o["label"] for o in clar["options"])
    assert "DDR4" in labels and "DDR5" in labels and len(clar["options"]) == 3
    assert fin["verification"]["status"] == "clarify" and not answer_calls(fake)
    # naming the standard answers directly
    _, fin = final(engine, clar["options"][1]["question"])
    assert fin["verification"]["status"] != "clarify"


def test_explicit_old_revision_is_used_only_when_named(cfg, make_engine):
    root = cfg.sources_dir / "ddr"
    root.mkdir(parents=True)
    make_standard_pdf(root / "JESD79-4B_DDR4_Mini.pdf", "DDR4", "360", "1.2", "B")
    make_standard_pdf(root / "JESD79-4C_DDR4_Mini.pdf", "DDR4", "350", "1.2", "C")
    engine = make_engine()
    docs = {d.revision: d for d in engine.store.documents()}
    assert docs["B"].superseded_by == docs["C"].id
    res = engine.retrieve("DDR4 tRFC refresh cycle time")
    assert docs["B"].id not in res.scope.doc_ids and docs["C"].id in res.scope.doc_ids
    res = engine.retrieve("JESD79-4B DDR4 tRFC refresh cycle time")
    assert docs["B"].id in res.scope.doc_ids and docs["C"].id not in res.scope.doc_ids
    assert res.scope.notes and "names that revision" in res.scope.notes[0]
    assert all("360" in r.min for r in res.parameters if r.symbol == "tRFC")


def _pcie_library(cfg):
    root = cfg.sources_dir / "pcie"
    root.mkdir(parents=True)
    files = {
        "PCIe_Base_5.0_r0.9.md": "# PCI Express Base Specification 5.0\n\n## 4.2.6.3 Polling.Active\n\nThe timeout is 24 ms.\n",
        "PCIe_Base_5.0_r1.0.md": "# PCI Express Base Specification 5.0\n\n## 4.2.6.3 Polling.Active\n\nThe timeout is 24 ms.\n",
        "PCIe_CEM_5.0_r1.0.md": "# PCI Express Card Electromechanical Specification 5.0\n\nREFCLK is provided by the system.\n",
        "PCIe_Base_5.0_Errata.md": "# PCI Express Base 5.0 Errata\n\nSection 4.2.6.3: the Polling.Active timeout text is corrected.\n",
        "PCIe_5.0_Layout_Design_Guide.md": "# PCIe 5.0 Layout Design Guide\n\nKeep stubs short.\n",
    }
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")


def test_document_types_and_precedence(cfg, make_engine):
    _pcie_library(cfg)
    engine = make_engine()
    d = {doc.title: doc for doc in engine.store.documents()}
    base09, base10 = d["PCIe Base 5.0 r0.9"], d["PCIe Base 5.0 r1.0"]
    cem, errata, guide = d["PCIe CEM 5.0 r1.0"], d["PCIe Base 5.0 Errata"], d["PCIe 5.0 Layout Design Guide"]
    assert (errata.doc_type, guide.doc_type, cem.doc_type) == ("errata", "guide", "base")
    # only revisions of the SAME document supersede each other
    assert base09.superseded_by == base10.id
    assert cem.superseded_by is None and base10.superseded_by is None and guide.superseded_by is None
    # errata amends the base spec of its series and version - not the CEM spec, not the guide
    assert engine.catalog.amends(errata.id) == [base10.id], "the current revision of the same series and version"
    assert cem.id not in engine.catalog.amends(errata.id) and guide.id not in engine.catalog.amends(errata.id)
    reg = SourceRegistry()
    s_guide, _ = reg.add_passage(Passage(guide.id, guide.title, guide.path, "pcie", "", 1, 1, "text", "Keep stubs short.",
                                         1.0, [1], [1]), engine.doc_meta(guide.id))
    s_err, _ = reg.add_passage(Passage(errata.id, errata.title, errata.path, "pcie", "", 1, 1, "text",
                                       "Section 4.2.6.3: the Polling.Active timeout text is corrected.", 1.0, [2], [2]),
                               engine.doc_meta(errata.id))
    s_base, _ = reg.add_passage(Passage(base10.id, base10.title, base10.path, "pcie",
                                        "PCI Express Base Specification 5.0 > 4.2.6.3 Polling.Active", 1, 1, "text",
                                        "The timeout is 24 ms.", 1.0, [3], [3]), engine.doc_meta(base10.id))
    assert "overrides" not in s_guide.header() and "informative" in s_guide.header()
    assert "Amends: 'PCIe Base 5.0 r1.0'" in s_err.header() and "CEM" not in s_err.header()
    engine._link_clauses(reg)
    assert f"errata [{s_err.n}] addresses §4.2.6.3" in s_base.header()
    assert f"addresses §4.2.6.3 of [{s_base.n}]" in s_err.header()


def test_conflicting_values_are_surfaced():
    from techrag.store import ParameterRow

    reg = SourceRegistry()
    for i, (title, val) in enumerate((("JESD79-4B", "360"), ("JESD79-4C", "350"))):
        reg.add_parameter(ParameterRow(i + 1, i + 1, None, "ddr", 6, "", "", "Refresh cycle time", "tRFC", val, "", "",
                                       "ns", "8Gb", "", True, title, "grounded"), {})
    notes = parameter_conflicts(reg)
    assert notes and "360" in notes[0] and "350" in notes[0]


def test_scope_is_reported_to_the_ui(engine):
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    assert fin["scope"]["reason"] == "entity" and fin["scope"]["entities"] == ["DDR5"]
    assert json.dumps(fin["scope"])
    plan = QueryPlan("x", "x", "x", "en")
    assert plan.wants_parameters is False
