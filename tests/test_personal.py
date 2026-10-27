"""The personal library: picked files are copied into the user's own folder, indexed into an index of their own and
searched together with the shared library - also when the shared library is opened read-only."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_standard_pdf
from techrag.library import PERSONAL_BUCKET_PREFIX, PERSONAL_OFFSET, LibraryView, _interleave
from techrag.server import create_app

NOTE = """# Lab Notes

## 1 Probe setup

The oscilloscope probe ground spring for the ZQX7 measurement must be shorter than 5 mm.
"""


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def _wait(client, job):
    for _ in range(600):
        st = client.get(f"/api/jobs/{job}").json()
        if st["status"] in ("done", "failed", "cancelled"):
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def _add_note(client, tmp_path) -> dict:
    src = tmp_path / "picked" / "lab_notes.md"
    src.parent.mkdir()
    src.write_text(NOTE, encoding="utf-8")
    r = client.post("/api/personal/add", json={"paths": [str(src)]}).json()
    st = _wait(client, r["job"])
    assert st["status"] == "done", st["messages"]
    return r


def test_picked_file_is_copied_indexed_and_searched_with_the_shared_library(client, engine, cfg, tmp_path):
    assert client.get("/api/personal").json()["indexed"] is False, "no personal index before the first file"
    r = _add_note(client, tmp_path)
    copied = Path(r["copied"][0])
    assert copied.parent == cfg.personal_dir and copied.read_text(encoding="utf-8") == NOTE, \
        "the file is kept in the user's personal folder"
    assert (cfg.personal_dir / "_indeks" / "index.sqlite").exists(), "the personal index lives in that folder"
    assert not (cfg.library_dir / "sources" / "lab_notes.md").exists(), "nothing goes into the shared library"

    info = client.get("/api/personal").json()
    assert info["indexed"] and info["documents"] == 1 and info["buckets"][0]["name"] == "Kişisel"
    assert info["buckets"][0]["id"].startswith(PERSONAL_BUCKET_PREFIX) and info["buckets"][0]["personal"]
    docs = client.get("/api/documents").json()
    own = next(d for d in docs if d["title"].lower().startswith("lab notes") or "lab_notes" in d["path"])
    assert own["id"] >= PERSONAL_OFFSET and own["bucket"] == "Kişisel"
    assert len(docs) == 4, "the three shared documents are still there"
    assert client.get(f"/api/documents/{own['id']}/file").content.decode("utf-8") == NOTE

    found = client.post("/api/search", json={"query": "ZQX7 probe ground spring"}).json()["sources"]
    assert found and found[0]["doc_id"] == own["id"], "the personal document is found"
    shared = client.post("/api/search", json={"query": "DDR4 tRFC refresh"}).json()["sources"]
    assert shared and all(s["doc_id"] < PERSONAL_OFFSET for s in shared[:2]), "the shared library still answers"
    only_own = client.post("/api/search", json={"query": "probe", "buckets": [own["bucket_id"]]}).json()["sources"]
    assert only_own and {s["doc_id"] for s in only_own} == {own["id"]}, "the personal bucket can be selected alone"
    assert engine.store.stats()["personal"]["documents"] == 1 and engine.store.stats()["documents"] == 4


def test_personal_files_can_be_added_to_a_read_only_shared_library(client, tmp_path):
    client.app.state.state["cfg"].read_only = True
    pdf = make_standard_pdf(tmp_path / "x.pdf", "DDR4")
    assert client.post("/api/sources/add", json={"paths": [str(pdf)], "collection": "ddr"}).status_code == 403
    _add_note(client, tmp_path)
    assert client.get("/api/personal").json()["documents"] == 1


def test_files_dropped_into_the_folder_are_indexed_by_a_rescan(client, cfg, tmp_path):
    _add_note(client, tmp_path)
    (cfg.personal_dir / "Projeler").mkdir()
    (cfg.personal_dir / "Projeler" / "board.md").write_text("# Board\n\nThe XKCD9 rail is 0.9 V.\n", encoding="utf-8")
    st = _wait(client, client.post("/api/personal/rescan").json()["job"])
    assert st["status"] == "done"
    names = sorted(b["name"] for b in client.get("/api/personal").json()["buckets"])
    assert names == ["Kişisel", "Projeler"], "a subfolder is a personal bucket of its own"


def test_settings_change_the_personal_folder(client, cfg, tmp_path):
    _add_note(client, tmp_path)
    other = tmp_path / "elsewhere"
    r = client.put("/api/settings", json={"paths": {"personal_dir": str(other)}}).json()
    assert r["settings"]["paths"]["personal_dir"] == str(other)
    info = client.get("/api/personal").json()
    assert info["dir"] == str(other) and not info["indexed"], "another folder is another personal library"


def test_view_routes_ids_to_their_library(engine, cfg, tmp_path, clients, registry):
    from techrag.ingest.pipeline import Ingestor

    store, root = engine.ensure_personal()
    (cfg.personal_dir / "n.md").write_text(NOTE, encoding="utf-8")
    Ingestor(cfg.personal_config(), store, clients["embedder"], registry, llm=clients["llm"]).sync_root(root)
    view = engine.store
    assert isinstance(view, LibraryView)
    own = next(d for d in view.documents() if d.id >= PERSONAL_OFFSET)
    assert view.document(own.id).title == own.title and view.document(own.id - PERSONAL_OFFSET) != own
    chunks = view.section_chunks(own.id, view.doc_sections(own.id)[-1])
    assert chunks and all(c.doc_id == own.id and c.id >= PERSONAL_OFFSET for c in chunks)
    assert set(view.chunks_by_ids([c.id for c in chunks])) == {c.id for c in chunks}
    hits = view.search_bm25("ZQX7", 5, doc_ids=[own.id])
    assert hits and all(cid >= PERSONAL_OFFSET for cid, _ in hits)
    assert view.search_bm25("ZQX7", 5, doc_ids=[d.id for d in view.documents() if d.id < PERSONAL_OFFSET]) == []
    vec = view.vectors()
    q = clients["embedder"].embed_queries(["ZQX7 probe"])[0]
    hits = vec.search(q, 50, vec.mask(None, [own.id]))
    assert hits and all(cid >= PERSONAL_OFFSET for cid, _ in hits), "the mask keeps only the personal document"
    assert vec.size == len(view.shared.vectors().ids) + len(store.vectors().ids)
    assert view.source_path(own, cfg.sources_dir) == cfg.personal_dir / "n.md"
    assert "|" in view.get_meta("index_version"), "caches refresh when either library changes"


def test_interleave_keeps_both_rankings():
    assert _interleave([1, 2, 3], ["a"], 3) == [1, "a", 2]
