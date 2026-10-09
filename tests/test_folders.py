"""Document folders: buckets, incremental rescans, missing/unreachable sources, rate limits, publishing.
(Synthetic files and a fake model server; API cost is counted as requests to the fake server.)"""

import os
import shutil
from pathlib import Path

import pymupdf
import pytest

from conftest import make_standard_pdf
from techrag.ingest.sources import GENERAL_BUCKET_ID, bucket_id_for, scan_root


def simple_pdf(path: Path, *lines: str) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 20
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def tree(tmp_path) -> Path:
    root = tmp_path / "Teknik Belgeler"
    simple_pdf(root / "PCIe" / "Base_Specification.pdf", "PCI Express Base Specification 5.0",
               "The LTSSM controls link training.")
    simple_pdf(root / "PCIe" / "CEM.pdf", "PCI Express Card Electromechanical Specification 5.0", "REFCLK is 100 MHz.")
    simple_pdf(root / "Ethernet" / "Standard.pdf", "IEEE 802.3 Ethernet", "The minimum frame size is 64 octets.")
    simple_pdf(root / "Ethernet" / "Design_Guide.pdf", "Ethernet layout design guide", "Keep pairs matched.")
    make_standard_pdf((root / "DDR" / "DDR4").joinpath("Specification.pdf").parent.mkdir(parents=True) or
                      root / "DDR" / "DDR4" / "Specification.pdf", "DDR4", "350", "1.2", "C")
    (root / "DDR" / "DDR5").mkdir(parents=True)
    make_standard_pdf(root / "DDR" / "DDR5" / "Specification.pdf", "DDR5", "295", "1.1", "A")
    (root / "Haberleşme Arayüzleri").mkdir()
    (root / "Haberleşme Arayüzleri" / "Özel Not.md").write_text("# I2C Notları\n\nI2C Fast-mode 400 kbit/s destekler.\n",
                                                               encoding="utf-8")
    (root / "Genel Notlar.md").write_text("# Notlar\n\nBu klasör test belgelerini içerir.\n", encoding="utf-8")
    (root / "Boş Klasör").mkdir()
    (root / "PCIe" / "tablo.xlsx").write_bytes(b"PK\x03\x04 not a document")
    (root / "DDR" / "resim.png").write_bytes(b"\x89PNG")
    (root / "Ethernet" / "bozuk.pdf").write_bytes(b"%PDF-1.7 this is not really a pdf")
    return root


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _api_calls(fake) -> dict:
    out = {"embeddings": 0, "chat": 0}
    for c in fake.calls:
        if c["path"].endswith("/embeddings"):
            out["embeddings"] += 1
        elif c["path"].endswith("/chat/completions"):
            out["chat"] += 1
    return out


@pytest.fixture()
def lib(cfg, registry, clients, fake):
    from techrag.engine import RAGEngine
    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store

    store = Store(cfg.db_path)

    def ingestor(**kw):
        return Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"], vision=clients["vision"], **kw)

    engine = RAGEngine(cfg, store=store, embedder=clients["embedder"], reranker=clients["reranker"],
                       llm=clients["llm"], vision=clients["vision"], domains=registry)
    return store, ingestor, engine


def test_scan_buckets_nested_unicode_and_noise(tree):
    if hasattr(os, "symlink"):
        try:
            os.symlink(tree, tree / "DDR" / "loop", target_is_directory=True)
            os.symlink(Path(__file__), tree / "dışarı.pdf")
        except OSError:
            pass
    scan = scan_root(tree)
    b = {x["name"]: x for x in scan.buckets()}
    assert set(b) == {"PCIe", "Ethernet", "DDR", "Haberleşme Arayüzleri", ""}
    assert b["DDR"]["documents"] == 2 and b["DDR"]["folders"] == ["DDR4", "DDR5"]
    assert b["Ethernet"]["documents"] == 3 and b[""]["general"] and b[""]["id"] == GENERAL_BUCKET_ID
    assert b["Haberleşme Arayüzleri"]["id"] == bucket_id_for("Haberleşme Arayüzleri") != "haberlesme"
    assert scan.unsupported_count == 2 and "Boş Klasör" in scan.empty_dirs
    ddr5 = next(f for f in scan.files if f.rel == "DDR/DDR5/Specification.pdf")
    assert (ddr5.bucket, ddr5.subpath) == ("DDR", "DDR5")
    if (tree / "DDR" / "loop").is_symlink():
        assert "DDR/loop" in scan.skipped_links and "dışarı.pdf" in scan.skipped_links
        assert not any("loop" in f.rel for f in scan.files)
    missing = scan_root(tree / "nope")
    assert not missing.reachable and not missing.files


def test_folder_is_indexed_in_place_with_buckets(tree, lib, fake):
    store, ingestor, engine = lib
    before = _snapshot(tree)
    root_id = store.add_root(str(tree))
    report = ingestor().sync_root(root_id)
    assert _snapshot(tree) == before, "nothing written into, moved or renamed in the source folder"
    assert set(report.failed) == {f"@{root_id}/Ethernet/bozuk.pdf"}, "one broken file does not stop the rest"
    docs = {d.rel_path: d for d in store.documents()}
    assert len(docs) == 8
    d4, d5 = docs["DDR/DDR4/Specification.pdf"], docs["DDR/DDR5/Specification.pdf"]
    assert d4.id != d5.id and (d5.bucket, d5.subpath) == ("DDR", "DDR5")
    assert d4.entities == ["DDR4"] and d5.entities == ["DDR5"], "standard from the document, not the folder"
    assert docs["Haberleşme Arayüzleri/Özel Not.md"].bucket == "Haberleşme Arayüzleri"
    assert docs["Genel Notlar.md"].bucket == "" and docs["Genel Notlar.md"].bucket_id == GENERAL_BUCKET_ID
    assert engine.document_path(d5.id) == tree / "DDR" / "DDR5" / "Specification.pdf"
    assert engine.render_page(d5.id, 1)
    buckets = {b["name"]: b for b in store.buckets()}
    assert buckets["DDR"]["documents"] == 2 and buckets["PCIe"]["documents"] == 2


def test_rescan_costs_nothing_for_unchanged_files(tree, lib, fake):
    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    calls = _api_calls(fake)
    os.utime(tree / "PCIe" / "CEM.pdf")  # touched, same content
    report = ingestor().sync_all()
    assert _api_calls(fake) == calls, "no VLM / LLM / embedding request for unchanged content"
    assert not report.added and not report.updated and len(report.skipped) >= 7


def test_new_changed_moved_missing_and_unreachable(tree, lib, fake):
    store, ingestor, engine = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    # changed content, a new file, a moved file and a deleted file
    make_standard_pdf(tree / "DDR" / "DDR4" / "Specification.pdf", "DDR4", "360", "1.2", "C")
    simple_pdf(tree / "PCIe" / "Errata.pdf", "PCI Express Base 5.0 Errata", "Section 4.2 corrected.")
    (tree / "Ethernet" / "Guides").mkdir()
    shutil.move(tree / "Ethernet" / "Design_Guide.pdf", tree / "Ethernet" / "Guides" / "Design_Guide.pdf")
    os.remove(tree / "DDR" / "DDR5" / "Specification.pdf")
    emb = _api_calls(fake)["embeddings"]
    report = ingestor().sync_root(root_id)
    assert report.updated == [f"@{root_id}/DDR/DDR4/Specification.pdf"]
    assert report.added == [f"@{root_id}/PCIe/Errata.pdf"]
    assert report.reused == [f"@{root_id}/Ethernet/Guides/Design_Guide.pdf"]
    assert report.missing == [f"@{root_id}/DDR/DDR5/Specification.pdf"]
    assert _api_calls(fake)["embeddings"] - emb == 2, "only the changed and the new file are embedded"
    d5 = next(d for d in store.documents() if d.rel_path == "DDR/DDR5/Specification.pdf")
    assert d5.missing, "kept and reported, not silently deleted"
    res = engine.retrieve("DDR5 tRFC refresh cycle time")
    assert res.scope.reason == "entity_missing" and not res.passages, "stale content never answers"
    # the share goes offline: nothing is declared missing
    offline = tree.with_name("offline")
    tree.rename(offline)
    report = ingestor().sync_root(root_id)
    assert not report.missing and len(report.unreachable) == len(store.root_documents(root_id))
    assert sum(d.missing for d in store.documents()) == 1
    offline.rename(tree)
    # the deleted file comes back unchanged: restored without processing
    make_standard_pdf(tree / "DDR" / "DDR5" / "Specification.pdf", "DDR5", "295", "1.1", "A")
    report = ingestor().sync_root(root_id)
    assert not any(d.missing for d in store.documents())
    assert store.purge_missing() == []


def test_unreadable_subfolder_is_not_treated_as_deleted(tree, lib, monkeypatch):
    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    real = os.scandir

    def scandir(path):
        if str(path).endswith("PCIe"):
            raise PermissionError("access denied")
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    report = ingestor().sync_root(root_id)
    assert not report.missing and {p.rsplit("/", 1)[-1] for p in report.unreachable} == {"Base_Specification.pdf", "CEM.pdf"}
    assert not any(d.missing for d in store.documents())


def test_settings_change_triggers_reprocessing_but_reuses_vlm_cache(cfg, tree, lib, fake):
    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    vlm = sum(1 for c in fake.calls if "extract tables" in str(c["body"].get("messages", [{}])[0].get("content", "")))
    emb = _api_calls(fake)["embeddings"]
    cfg.chunking.target_tokens = 200
    report = ingestor().sync_root(root_id)
    assert len(report.updated) == 8, "every successfully indexed document is re-processed"
    assert _api_calls(fake)["embeddings"] > emb
    vlm2 = sum(1 for c in fake.calls if "extract tables" in str(c["body"].get("messages", [{}])[0].get("content", "")))
    assert vlm2 == vlm, "table extraction results come from the cache"
    assert not ingestor().sync_root(root_id).updated, "and only once"


def test_rate_limits_are_honoured_and_bounded(tree, lib, fake, monkeypatch):
    from techrag import api

    waits = []
    monkeypatch.setattr(api, "_sleep", waits.append)
    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    fake.rate_limited, fake.retry_after = 2, "7"
    report = ingestor().sync_root(root_id)
    assert waits[:2] == [7.0, 7.0] and len(report.added) == 8
    fake.rate_limited, fake.retry_after = 100, "1000"
    make_standard_pdf(tree / "DDR" / "DDR4" / "Specification.pdf", "DDR4", "370", "1.2", "C")
    waits.clear()
    report = ingestor().sync_root(root_id)
    assert len(waits) == 3 and max(waits) == 60.0, "at most max_retries waits, each capped"
    assert list(report.failed) == [f"@{root_id}/DDR/DDR4/Specification.pdf"] or \
        any("429" in v for v in report.failed.values())


def test_bucket_selection_limits_search_and_keeps_standard_checks(tree, lib):
    store, ingestor, engine = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    ddr, pcie = bucket_id_for("DDR"), bucket_id_for("PCIe")
    res = engine.retrieve("refresh cycle time", buckets=[ddr])
    assert res.passages and {p.doc_path.split("/")[1] for p in res.passages} == {"DDR"}
    res = engine.retrieve("DDR5 tRFC refresh", buckets=[ddr])
    assert res.scope.reason == "entity" and all(p.entities == ["DDR5"] for p in res.passages)
    res = engine.retrieve("DDR5 tRFC refresh", buckets=[pcie])
    assert res.scope.reason == "entity_missing" and not res.passages


def test_same_file_twice_reuses_the_index(tree, lib, fake):
    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    shutil.copy2(tree / "PCIe" / "CEM.pdf", tree / "Ethernet" / "CEM.pdf")
    emb = _api_calls(fake)["embeddings"]
    report = ingestor().sync_root(root_id)
    assert report.reused == [f"@{root_id}/Ethernet/CEM.pdf"] and _api_calls(fake)["embeddings"] == emb
    copy = next(d for d in store.documents() if d.rel_path == "Ethernet/CEM.pdf")
    assert copy.bucket == "Ethernet" and copy.n_chunks > 0 and store.page_geom(copy.id, 1) is not None


def test_publish_copies_folder_documents(tree, lib, cfg, tmp_path):
    from techrag.store import Store

    store, ingestor, _ = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    dest = tmp_path / "paylaşım"
    store.publish(dest, cfg.sources_dir, cfg.vlm_cache_dir)
    assert (dest / "sources" / "DDR" / "DDR5" / "Specification.pdf").exists()
    assert (dest / "sources" / "Haberleşme Arayüzleri" / "Özel Not.md").exists()
    ro = Store(dest / "index.sqlite", read_only=True)
    assert ro.roots() == [] and all(d.root_id is None for d in ro.documents())
    d5 = next(d for d in ro.documents() if d.rel_path == "DDR/DDR5/Specification.pdf")
    assert ro.source_path(d5, dest / "sources").is_file() and d5.bucket == "DDR"
    assert store.last_publish["copied"] == 8


def test_v3_index_gets_buckets_on_open(cfg, sources, lib):
    from techrag.store import Store

    store, ingestor, _ = lib
    ingestor().run()
    with store.connect() as con:
        for col in ("root_id", "rel_path", "bucket", "bucket_id", "subpath", "file_size", "file_mtime", "missing",
                    "ingest_fp"):
            con.execute(f"ALTER TABLE documents DROP COLUMN {col}")
        con.execute("DROP TABLE roots")
    ro = Store(cfg.db_path, read_only=True)
    assert {d.bucket for d in ro.documents()} == {"ddr", "i2c"}
    store = Store(cfg.db_path)
    assert {d.bucket for d in store.documents()} == {"ddr", "i2c"} and store.roots() == []
    report = ingestor().run()
    assert not report.updated and not report.added, "documents from 0.2 are not re-processed"


def test_library_sources_deleted_file_is_marked_not_deleted(cfg, sources, lib):
    store, ingestor, _ = lib
    ingestor().run()
    os.remove(sources / "i2c" / "UM10204_i2c_notes.md")
    report = ingestor().run()
    assert report.missing == ["i2c/UM10204_i2c_notes.md"]
    assert any(d.missing for d in store.documents()) and len(store.documents()) == 3
    assert store.purge_missing() == ["i2c/UM10204_i2c_notes.md"] and len(store.documents()) == 2


def test_standard_outside_the_selected_bucket_is_reported_as_such(tree, lib, fake):
    store, ingestor, engine = lib
    root_id = store.add_root(str(tree))
    ingestor().sync_root(root_id)
    fin = list(engine.ask_stream("DDR5 tRFC değeri nedir?", buckets=[bucket_id_for("PCIe")]))[-1]
    assert "seçili kapsamda" in fin["answer"] and "Specification" in fin["answer"]
    assert fin["verification"]["status"] == "not_found"


def test_library_inside_the_picked_folder_is_skipped(tmp_path, cfg):
    root = tmp_path / "Belgeler"
    simple_pdf(root / "PCIe" / "Base.pdf", "PCI Express Base Specification 5.0")
    lib = root / "TechRAG" / "library" / "sources" / "x"
    simple_pdf(lib / "Copy.pdf", "copy inside the library")
    scan = scan_root(root, exclude=(root / "TechRAG" / "library",))
    assert [f.rel for f in scan.files] == ["PCIe/Base.pdf"]
