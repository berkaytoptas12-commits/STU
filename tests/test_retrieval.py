from pathlib import Path

from conftest import FakeLLM

from techrag.engine import RAGEngine
from techrag.ingest.pipeline import Ingestor
from techrag.query import QueryPlanner, detect_language
from techrag.retrieval import rrf_fuse
from techrag.store import Store, fts_query


def _ingest(cfg, registry, embedder):
    store = Store(cfg.db_path)
    report = Ingestor(cfg, store, embedder, registry).run()
    return store, report


def test_ingest_classifies_by_folder_and_is_incremental(cfg, sources, registry, embedder):
    store, report = _ingest(cfg, registry, embedder)
    assert sorted(report.added) == ["arinc/ACME_Bus_Standard.pdf", "i2c/i2c_notes.md"]
    assert not report.failed
    docs = {d.path: d for d in store.documents()}
    assert docs["arinc/ACME_Bus_Standard.pdf"].domain == "arinc"
    assert docs["i2c/i2c_notes.md"].domain == "i2c"

    _, again = _ingest(cfg, registry, embedder)
    assert len(again.skipped) == 2 and not again.added and not again.updated

    md = sources / "i2c" / "i2c_notes.md"
    md.write_text(md.read_text() + "\nHigh-speed mode supports up to 3.4 Mbit/s.\n")
    _, changed = _ingest(cfg, registry, embedder)
    assert changed.updated == ["i2c/i2c_notes.md"]

    md.unlink()
    store, pruned = _ingest(cfg, registry, embedder)
    assert pruned.removed == ["i2c/i2c_notes.md"]
    assert [d.path for d in store.documents()] == ["arinc/ACME_Bus_Standard.pdf"]
    # FTS rows of the removed document are gone too.
    assert store.search_bm25("Fast-mode START condition", 10) == []
    with store.connect() as con:
        n_fts = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n_fts == n_chunks


def test_embedding_model_mismatch_is_refused(cfg, sources, registry, embedder):
    store, _ = _ingest(cfg, registry, embedder)
    from techrag.embeddings import HashEmbedder

    try:
        Ingestor(cfg, store, HashEmbedder(dim=256), registry).run()
    except RuntimeError as exc:
        assert "--rebuild" in str(exc)
    else:
        raise AssertionError("expected a mismatch error")


def test_hybrid_search_finds_section_and_page(cfg, sources, registry, embedder):
    _ingest(cfg, registry, embedder)
    engine = RAGEngine(cfg, embedder=embedder, reranker=None, llm=FakeLLM(), domains=registry)

    res = engine.retrieve("What is the refresh cycle time tRFC?")
    top = res.passages[0]
    assert "tRFC" in top.text
    assert top.section.endswith("3.2 Refresh Timing")
    assert top.page_start == 7

    # Turkish question: term map adds "label"/"word"/"bit" so BM25 still matches the English text.
    res = engine.retrieve("ARINC kelimesinde SSM bitleri hangileri?")
    assert res.routed_domains == ["arinc"]
    assert any("Sign/Status Matrix" in p.text for p in res.passages[:2])

    # Explicit I2C mention routes to the i2c collection only.
    res = engine.retrieve("I2C Fast-mode maksimum hız nedir?")
    assert res.routed_domains == ["i2c"]
    assert {p.domain for p in res.passages} == {"i2c"}
    assert "400 kbit/s" in res.passages[0].text


def test_routing_falls_back_when_collection_is_empty(cfg, sources, registry, embedder):
    _ingest(cfg, registry, embedder)
    engine = RAGEngine(cfg, embedder=embedder, reranker=None, llm=FakeLLM(), domains=registry)
    res = engine.retrieve("PCIe word parity bit")  # no PCIe documents indexed
    assert res.routed_domains == []
    assert res.passages


def test_neighbor_expansion_merges_without_duplicate_overlap(cfg, sources, registry, embedder):
    cfg.chunking.target_tokens = 60
    cfg.chunking.overlap_tokens = 20
    long = " ".join(f"Requirement {i}: the transmitter shall hold state {i} for {i * 10} ns." for i in range(40))
    (sources / "arinc" / "long.md").write_text(f"# 5 Long Section\n\n{long}\n", encoding="utf-8")
    _ingest(cfg, registry, embedder)
    engine = RAGEngine(cfg, embedder=embedder, reranker=None, llm=FakeLLM(), domains=registry)
    res = engine.retrieve("Requirement 20 transmitter hold state 200 ns", domains=["arinc"])
    merged = [p for p in res.passages if len(p.chunk_ids) > 1]
    assert merged, "neighbouring chunks of the same section should be merged"
    for p in merged:
        assert p.text.count("Requirement 20:") <= 1


def test_planner_uses_llm_json_and_resolves_followups(registry):
    llm = FakeLLM(plan={"standalone_question": "DDR5 için tRFC değeri nedir?",
                        "english_question": "What is tRFC for DDR5?",
                        "search_queries": ["DDR5 tRFC refresh cycle time"], "keywords": ["tRFC", "tRFC1"]})
    planner = QueryPlanner(registry, llm, use_llm=True)
    history = [{"role": "user", "content": "DDR4 tRFC nedir?"}, {"role": "assistant", "content": "350 ns [1]"}]
    plan = planner.plan("Peki DDR5 için?", history)
    assert plan.rewritten_by_llm
    assert plan.standalone == "DDR5 için tRFC değeri nedir?"
    assert plan.english == "What is tRFC for DDR5?"
    assert "ddr" in plan.domains
    assert "tRFC1" in plan.keywords
    assert any("tRFC" in q for q in plan.bm25_queries())


def test_planner_survives_garbage_output(registry):
    class Broken(FakeLLM):
        def stream(self, messages, json_mode=False, max_tokens=None, temperature=None):
            yield "I cannot produce JSON today"

    plan = QueryPlanner(registry, Broken(), use_llm=True).plan("RS-422 maksimum kablo uzunluğu?")
    assert not plan.rewritten_by_llm
    assert plan.domains == ["rs422"]
    assert "cable" in plan.expansions and "length" in plan.expansions


def test_helpers():
    assert detect_language("Fast-mode hızı nedir?") == "tr"
    assert detect_language("What is the maximum cable length?") == "en"
    assert fts_query('the "tRFC" of DDR4 ve bir') == '"trfc" OR "ddr4"'
    fused = rrf_fuse([[(1, 9.0), (2, 8.0)], [(2, 0.9), (3, 0.8)]], k=60)
    assert fused[0][0] == 2


def test_join_chunks_drops_duplicate_table_caption():
    from techrag.retrieval import join_chunks
    from techrag.store import ChunkRow

    text = ChunkRow(1, 1, 0, "ddr", "s", 7, 7, "text", 0, "Refresh rules.\n\nTable 3-1 Timing parameters")
    table = ChunkRow(2, 1, 1, "ddr", "s", 7, 7, "table", 0, "Table 3-1 Timing parameters\n| a | b |\n|---|---|")
    joined = join_chunks([text, table])
    assert joined.count("Table 3-1 Timing parameters") == 1
    assert join_chunks([table]).startswith("Table 3-1")


def test_rebuild_with_new_embedding_model(cfg, sources, registry, embedder):
    from techrag.embeddings import HashEmbedder

    store, _ = _ingest(cfg, registry, embedder)
    report = Ingestor(cfg, store, HashEmbedder(dim=256), registry).run(rebuild=True)
    assert not report.failed and len(report.added) == 2
    assert store.get_meta("embedding_model") == "hash:256"
    assert store.vectors().matrix.shape[1] == 256
