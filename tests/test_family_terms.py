"""Family names and document versions.

* "LPDDR" (a family name) finds the documents that only ever write "LPDDR4" / "LPDDR5": the keyword search also
  looks for the versioned forms the library itself uses. Nothing is filtered; a question without such a name
  searches exactly as before, and a term the library writes as it is ("SPI") is left alone.
* DisplayPort documents are tagged with their own version (VESA's "Version 1, Revision 2" is 1.2), not with the
  versions their front matter mentions, and an indexed library is re-tagged once without indexing again."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_folders import simple_pdf
from techrag.config import resource_path
from techrag.domains import DomainRegistry
from techrag.ingest.metadata import build_metadata, compute_supersedence, own_version_names
from techrag.retrieval import with_variants

REGISTRY = DomainRegistry.load(resource_path("resources", "domains.yaml"))


@pytest.fixture()
def memory_library(cfg, make_engine):
    src = cfg.sources_dir
    simple_pdf(src / "Bellek" / "JESD209-4.pdf", "JEDEC Standard JESD209-4",
               "LPDDR4 SDRAM devices use two 16-bit channels per die.",
               "The LPDDR4 interface lowers I/O power with LVSTL signalling.")
    simple_pdf(src / "Bellek" / "JESD209-5.pdf", "JEDEC Standard JESD209-5",
               "LPDDR5 SDRAM devices reach 6400 Mbps per pin.",
               "LPDDR5 adds data copy and write X for lower power.")
    simple_pdf(src / "Islemciler" / "RT1170_Datasheet.pdf", "i.MX RT1170 Crossover Processors Data Sheet",
               "The SEMC supports SDRAM memory with low power modes and a high data rate.",
               "FlexSPI double data rate memory mode; low power memory; data rate of the memory interface.",
               "Low power run mode keeps memory data. SDRAM memory data rate and low power memory standby.")
    (src / "Haberlesme").mkdir(parents=True, exist_ok=True)
    (src / "Haberlesme" / "SPI_Notlari.md").write_text(
        "# SPI\n\nThe SPI interface has SPI1 and SPI2 instances. SPI clock polarity is set per SPI transfer.\n",
        encoding="utf-8")
    return make_engine()


def _doc_of(store, chunk_ids):
    rows = store.chunks_by_ids(chunk_ids)
    return [rows[c].doc_path for c in chunk_ids if c in rows]


def test_a_family_name_finds_the_documents_that_write_it_with_a_version(memory_library):
    e = memory_library
    v = e.store.term_variants(["LPDDR", "SPI", "memory", "rate", "nedir"])
    assert set(v) == {"lpddr"} and {"lpddr4", "lpddr5"} <= set(v["lpddr"]), v
    assert e.store.search_bm25("LPDDR nedir", 5) == [], "as written, the term is in no document"
    found = _doc_of(e.store, [c for c, _ in e.store.search_bm25(with_variants("LPDDR nedir", v), 5)])
    assert set(found) == {"Bellek/JESD209-4.pdf", "Bellek/JESD209-5.pdf"}, found
    res = e.retrieve("LPDDR nedir?")
    assert res.passages and "JESD209" in res.passages[0].doc_path, [p.doc_path for p in res.passages]


def test_searches_without_such_names_are_unchanged(memory_library):
    """Only a name the library never writes as it is, while it writes its versioned forms, changes the keyword
    query; every other question's queries stay exactly as they were."""
    from techrag.evaluation import bundled_questions, load_items

    r = memory_library.retriever
    questions = [i.question for i in load_items(bundled_questions())] + [
        "SPI clock polarity nedir?", "DDR5 tRFC değeri nedir?", "low power memory data rate",
        "RT1170 SEMC hangi belleği destekler?", "LPDDR4 kanal genişliği nedir?", "I2C Fast-mode hızı nedir?"]
    changed = {}
    for q in questions:
        plan = SimpleNamespace(question=q, standalone=q, english=q)
        v = r._variants(plan)
        if v:
            changed[q] = v
        else:
            assert with_variants(q, v) == q
    assert set().union(*map(set, changed.values())) <= {"lpddr", "ddr"}, changed


def _dp_docs():
    """Three DisplayPort standards as VESA titles them, file names that do not say which (front matter of the
    newer ones mentions the older versions a lot)."""
    return [
        ("VESA_DP_A", "VESA DisplayPort Standard\nVersion 1, Revision 2\nJanuary 5, 2010\nHBR2 and MST are added.",
         {"title": "VESA DisplayPort Standard", "standard": "VESA DisplayPort Standard", "version": "1",
          "revision": "2", "date": "2010-01"}),
        ("VESA_DP_B", "VESA DisplayPort (DP) Standard\nVersion 1.4\nMarch 2016\nDP 1.2 sources and DP 1.2 sinks; "
                      "DP 1.2a and DP 1.3 devices.",
         {"title": "VESA DisplayPort (DP) Standard", "standard": "VESA DisplayPort (DP) Standard", "version": "1.4",
          "revision": "", "date": "2016-03"}),
        ("VESA_DP_C", "VESA DisplayPort (DP) Standard\nVersion 2.1\nOctober 2022\nCompatible with DP 1.2 and DP 1.4 "
                      "devices; DP 1.2 HBR2; DP 1.2 MST.",
         {"title": "VESA DisplayPort (DP) Standard", "standard": "VESA DisplayPort (DP) Standard", "version": "2.1",
          "revision": "", "date": "2022-10"}),
    ]


@pytest.mark.parametrize("with_model", [True, False], ids=["model metadata", "no model metadata"])
def test_displayport_documents_are_tagged_with_their_own_version(with_model):
    rows = []
    for i, (name, front, llm) in enumerate(_dp_docs(), 1):
        m = build_metadata(REGISTRY, "displayport", name, "", front, llm if with_model else {})
        rows.append(SimpleNamespace(id=i, path=name, title=m.title, doc_key=m.doc_key, doc_type=m.doc_type,
                                    entities=m.entities, domain="displayport", version=m.version,
                                    revision=m.revision, doc_date=m.doc_date or llm["date"]))
    assert [r.entities for r in rows] == [["DisplayPort 1.2"], ["DisplayPort 1.4"], ["DisplayPort 2.x"]]
    assert all(newer is None for _, newer in compute_supersedence(rows)), "different versions, not revisions"


def test_versions_are_recognised_as_people_write_them():
    for q, want in (("display port 1.2'de HBR2 hızı", "DisplayPort 1.2"), ("DP v1.2 MST", "DisplayPort 1.2"),
                    ("DP1.2", "DisplayPort 1.2"), ("DisplayPort 1.2a", "DisplayPort 1.2"),
                    ("DP 1.3 DSC", "DisplayPort 1.3"), ("DisplayPort 1.1a", "DisplayPort 1.1"),
                    ("DisplayPort 2.1 UHBR20", "DisplayPort 2.x")):
        assert REGISTRY.detect_entities(q) == [want], q
    assert REGISTRY.detect_entities("DP 1.0V supply rail") == [] and REGISTRY.detect_entities("eDP 1.4b") == ["eDP"]
    # other standards' covers are not read as "Version N, Revision M" or as a version cover line
    assert own_version_names({}, "", "PCI Express Base Specification Revision 5.0, Version 1.0") == []
    assert own_version_names({}, "", "JEDEC STANDARD\nDDR5 SDRAM\nJESD79-5B") == []
    assert own_version_names({"standard": "VESA DisplayPort (DP) Standard", "version": "1.4"}, "",
                             "VESA DisplayPort (DP) Standard\nVersion 1.4") == [], "the model already said it"


# ---------------------------------------------------------------------------------- re-tagging a library

@pytest.fixture()
def dp_library(cfg, make_engine, fake, monkeypatch):
    for name, front, _ in _dp_docs():
        simple_pdf(cfg.sources_dir / "Goruntu" / f"{name}.pdf", *front.split("\n"))
    simple_pdf(cfg.sources_dir / "Bellek" / "DDR5_notes.pdf", "DDR5 SDRAM notes", "DDR5 tRFC is 295 ns.")
    metas = {name: llm for name, _, llm in _dp_docs()}

    def meta(msgs):
        txt = msgs[-1]["content"] if isinstance(msgs[-1]["content"], str) else ""
        for name, front, llm in _dp_docs():
            if front.split("\n")[1] in txt:
                return json.dumps({**llm, "doc_type": "base"})
        return real(msgs)

    real = fake._meta
    monkeypatch.setattr(fake, "_meta", meta)
    assert metas
    return make_engine()


def _tags(store):
    return {Path(d.rel_path or d.path).stem: d for d in store.documents()}


def test_an_indexed_library_is_retagged_once_without_indexing_again(dp_library, cfg, registry, clients, fake):
    from techrag.ingest.pipeline import TAG_RULES_META, Ingestor, refresh_standard_tags
    from techrag.store import Store

    store = dp_library.shared
    docs = _tags(store)
    assert docs["VESA_DP_A"].entities == ["DisplayPort 1.2"] and docs["VESA_DP_C"].entities == ["DisplayPort 2.x"]
    ddr_before = docs["DDR5_notes"]
    # as an older version left it: newer versions tagged with the version they mention, 1.4 "older revision" of 2.1
    with store.connect() as con:
        con.execute("UPDATE documents SET entities=? WHERE id=?", ('[]', docs["VESA_DP_A"].id))
        for n in ("VESA_DP_B", "VESA_DP_C"):
            con.execute("UPDATE documents SET entities=? WHERE id=?", ('["DisplayPort 1.2"]', docs[n].id))
        con.execute("UPDATE documents SET superseded_by=? WHERE id=?", (docs["VESA_DP_C"].id, docs["VESA_DP_B"].id))
        con.execute("DELETE FROM meta WHERE key=?", (TAG_RULES_META,))
    calls_before = len(fake.calls)

    ro = Store(store.db_path, read_only=True)
    assert refresh_standard_tags(ro, registry)["changed"] == 0, "a read-only library is left as it is"
    res = refresh_standard_tags(store, registry)
    assert res == {"changed": 3, "collections": ["displayport"]}
    docs = _tags(store)
    assert [docs[n].entities for n in ("VESA_DP_A", "VESA_DP_B", "VESA_DP_C")] == \
        [["DisplayPort 1.2"], ["DisplayPort 1.4"], ["DisplayPort 2.x"]]
    assert all(docs[n].superseded_by is None for n in ("VESA_DP_A", "VESA_DP_B", "VESA_DP_C"))
    assert docs["VESA_DP_B"].ingest_fp.startswith("retag") and \
        docs["VESA_DP_B"].metadata["embedded_entities"] == ["DisplayPort 1.2"]
    ddr = docs["DDR5_notes"]
    assert (ddr.entities, ddr.ingest_fp, ddr.metadata, ddr.superseded_by) == \
        (ddr_before.entities, ddr_before.ingest_fp, ddr_before.metadata, ddr_before.superseded_by), "untouched"
    assert json.loads(store.get_meta(TAG_RULES_META))["displayport"]
    assert refresh_standard_tags(store, registry)["changed"] == 0, "once"
    assert len(fake.calls) == calls_before, "no model call, no indexing"
    # the next scan makes the search vectors of those three again (with the right names); the rest is skipped
    ing = Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"])
    report = ing.run()
    assert sorted(Path(k).stem for k in report.updated) == ["VESA_DP_A", "VESA_DP_B", "VESA_DP_C"]
    docs = _tags(store)
    assert not docs["VESA_DP_B"].ingest_fp.startswith("retag") and "embedded_entities" not in docs["VESA_DP_B"].metadata
    assert docs["VESA_DP_B"].entities == ["DisplayPort 1.4"]


def test_a_dp_question_is_answered_from_its_own_version(dp_library):
    res = dp_library.retrieve("DisplayPort 1.2 HBR2 nedir?")
    assert res.scope.entities == ["DisplayPort 1.2"]
    assert {Path(p.doc_path).stem for p in res.passages} == {"VESA_DP_A"}
