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
    assert client.get(f"/api/documents/{own['id']}/file").content == copied.read_bytes(), "the file is served as is"

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


def test_a_personal_document_can_be_removed_and_its_copy_recovered(client, cfg, tmp_path, monkeypatch):
    from techrag import trash

    monkeypatch.setattr(trash, "_fixed_drive", lambda p: False)   # no Recycle Bin: the copy goes to _indeks/silinenler
    r = _add_note(client, tmp_path)
    picked = tmp_path / "picked" / "lab_notes.md"
    own = next(d for d in client.get("/api/documents").json() if d["id"] >= PERSONAL_OFFSET)
    res = client.delete(f"/api/documents/{own['id']}")
    assert res.status_code in (404, 405), "not through the general documents API"
    res = client.delete(f"/api/personal/documents/{own['id']}").json()
    assert res["ok"] and res["trash"] == "folder"
    moved = Path(res["path"])
    assert moved.parent == cfg.personal_dir / "_indeks" / "silinenler" and moved.read_text(encoding="utf-8") == NOTE
    assert not Path(r["copied"][0]).exists() and picked.exists(), "the picked original is not touched"
    assert all(d["id"] < PERSONAL_OFFSET for d in client.get("/api/documents").json())
    assert client.get("/api/personal").json()["documents"] == 0
    assert not client.post("/api/search", json={"query": "ZQX7 probe ground spring"}).json()["sources"] or \
        all(s["doc_id"] < PERSONAL_OFFSET for s in client.post("/api/search", json={"query": "ZQX7"}).json()["sources"])
    st = _wait(client, client.post("/api/personal/rescan").json()["job"])
    assert st["status"] == "done" and client.get("/api/personal").json()["documents"] == 0, \
        "a rescan does not bring it back (the silinenler folder is not scanned)"


def test_only_personal_documents_can_be_removed(client, cfg, tmp_path):
    _add_note(client, tmp_path)
    shared = client.get("/api/documents").json()[0]
    assert client.delete(f"/api/personal/documents/{shared['id']}").status_code == 404
    assert client.delete(f"/api/personal/documents/{PERSONAL_OFFSET + 999}").status_code == 404
    assert len([d for d in client.get("/api/documents").json() if d["id"] < PERSONAL_OFFSET]) == 3


def test_a_file_outside_the_personal_folder_is_never_moved(client, engine, cfg, tmp_path):
    _add_note(client, tmp_path)
    own = next(d for d in client.get("/api/documents").json() if d["id"] >= PERSONAL_OFFSET)
    outside = tmp_path / "elsewhere" / "kept.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    engine.personal.add_root(str(outside.parent))
    with engine.personal.connect() as con:   # an index that points elsewhere (e.g. edited by hand)
        rid = con.execute("SELECT id FROM roots WHERE path=?", (str(outside.parent),)).fetchone()[0]
        con.execute("UPDATE documents SET root_id=?, rel_path='kept.md' WHERE id=?", (rid, own["id"] - PERSONAL_OFFSET))
    res = client.delete(f"/api/personal/documents/{own['id']}").json()
    assert res["trash"] == "kept" and outside.exists()


def test_removal_waits_for_indexing(client, tmp_path, monkeypatch):
    _add_note(client, tmp_path)
    own = next(d for d in client.get("/api/documents").json() if d["id"] >= PERSONAL_OFFSET)
    monkeypatch.setattr(client.app.state.jobs, "running", lambda: True)
    assert client.delete(f"/api/personal/documents/{own['id']}").status_code == 409


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Recycle Bin")
def test_recycle_bin_on_windows(tmp_path):
    import threading

    from techrag.trash import FOLDER, RECYCLE_BIN, move_to_trash

    f = tmp_path / "geri_donusum.txt"
    f.write_text("x", encoding="utf-8")
    out = {}
    th = threading.Thread(target=lambda: out.update(r=move_to_trash(f, tmp_path / "silinenler")), daemon=True)
    th.start()
    th.join(30)
    assert "r" in out, "never waits for a dialog"
    kind, where = out["r"]
    assert not f.exists() and (kind == RECYCLE_BIN or (kind == FOLDER and where.exists()))
