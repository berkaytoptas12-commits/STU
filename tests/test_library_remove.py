"""Removing documents on request: a shared library document leaves the index (Admin), its file is never touched
and later rescans leave it out until it is restored; a folder picked as a personal bucket is indexed in place as
one bucket and can be unlinked without touching its files."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from techrag.library import PERSONAL_OFFSET
from techrag.server import create_app
from techrag.store import Store


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


def _titles(client):
    return {d["title"] for d in client.get("/api/documents").json()}


def test_a_shared_document_is_removed_kept_out_of_rescans_and_restored(client, cfg):
    docs = client.get("/api/documents").json()
    doc = docs[0]
    files = sorted(p for p in cfg.sources_dir.rglob("*") if p.is_file())
    before = {p: p.read_bytes() for p in files}
    r = client.delete(f"/api/library/documents/{doc['id']}").json()
    assert r["ok"] and r["title"] == doc["title"]
    assert doc["title"] not in _titles(client) and len(_titles(client)) == len(docs) - 1
    assert {p: p.read_bytes() for p in files} == before, "no file is deleted or changed"
    removed = client.get("/api/library/removed").json()
    assert [e["title"] for e in removed] == [doc["title"]]
    st = _wait(client, client.post("/api/rescan").json()["job"])
    assert st["status"] == "done" and doc["title"] not in _titles(client), "a rescan leaves it out"
    preview = client.post("/api/rescan/preview").json()
    assert preview["counts"].get("new", 0) == 0, "not offered as a new document either"
    r = client.post("/api/library/removed/restore", json={"path": removed[0]["path"]}).json()
    assert _wait(client, r["job"])["status"] == "done"
    assert doc["title"] in _titles(client) and client.get("/api/library/removed").json() == []


def test_removal_is_refused_where_it_must_not_happen(client, cfg):
    doc = client.get("/api/documents").json()[0]
    assert client.delete(f"/api/library/documents/{PERSONAL_OFFSET + doc['id']}").status_code == 404
    assert client.delete("/api/library/documents/99999").status_code == 404
    client.app.state.jobs.running = lambda: True
    assert client.delete(f"/api/library/documents/{doc['id']}").status_code == 409
    client.app.state.jobs.running = lambda: False
    client.app.state.state["cfg"].read_only = True
    assert client.delete(f"/api/library/documents/{doc['id']}").status_code == 403
    assert doc["title"] in _titles(client)


def test_a_document_of_a_folder_root_stays_out_until_restored(client, tmp_path):
    folder = tmp_path / "docs"
    (folder / "Notlar").mkdir(parents=True)
    (folder / "Notlar" / "a.md").write_text("# Alpha\n\nThe QQA1 rail is 0.9 V.\n", encoding="utf-8")
    (folder / "Notlar" / "b.md").write_text("# Beta\n\nThe QQB2 rail is 1.1 V.\n", encoding="utf-8")
    r = client.post("/api/folders", json={"path": str(folder)}).json()
    assert _wait(client, r["job"])["status"] == "done"
    beta = next(d for d in client.get("/api/documents").json() if d["path"].endswith("b.md"))
    client.delete(f"/api/library/documents/{beta['id']}")
    st = _wait(client, client.post("/api/rescan").json()["job"])
    assert st["status"] == "done" and not any(d["path"].endswith("b.md") for d in client.get("/api/documents").json())
    assert (folder / "Notlar" / "b.md").exists()
    entry = client.get("/api/library/removed").json()[0]
    assert entry["rel_path"] == "Notlar/b.md" and entry["bucket"] == "Notlar"
    assert _wait(client, client.post("/api/library/removed/restore", json={"path": entry["path"]}).json()["job"])[
        "status"] == "done"
    assert any(d["path"].endswith("b.md") for d in client.get("/api/documents").json())


def test_an_old_library_gets_the_new_table_and_column_in_place(tmp_path):
    import sqlite3

    db = tmp_path / "index.sqlite"
    Store(db)
    with sqlite3.connect(db) as con:   # as an older version wrote it
        con.execute("DROP TABLE excluded")
        con.execute("CREATE TABLE roots_old AS SELECT id, path, label, added_at, last_scan, last_status FROM roots")
        con.execute("DROP TABLE roots")
        con.execute("ALTER TABLE roots_old RENAME TO roots")
        con.execute("INSERT INTO roots(path, label) VALUES('/x', 'x')")
    ro = Store(db, read_only=True)
    assert ro.excluded() == [] and ro.excluded_rels(None) == set(), "read-only: an older library still opens"
    s = Store(db)
    assert s.roots()[0]["kind"] == "" and s.excluded() == [], "migrated: rows kept, new column and table added"


# ------------------------------------------------------------------ personal folder buckets

def test_a_picked_folder_is_one_personal_bucket_indexed_in_place(client, cfg, tmp_path):
    picked = tmp_path / "Benim Notlarım"
    (picked / "alt" / "derin").mkdir(parents=True)
    (picked / "x.md").write_text("# Xray\n\nThe QQX9 rail is 0.8 V.\n", encoding="utf-8")
    (picked / "alt" / "y.md").write_text("# Yankee\n\nThe QQY9 rail is 0.7 V.\n", encoding="utf-8")
    (picked / "alt" / "derin" / "z.md").write_text("# Zulu\n\nThe QQZ9 rail is 0.6 V.\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in picked.rglob("*") if p.is_file()}
    r = client.post("/api/personal/folder", json={"path": str(picked)}).json()
    assert _wait(client, r["job"])["status"] == "done"
    info = client.get("/api/personal").json()
    assert [b["name"] for b in info["buckets"]] == ["Benim Notlarım"] and info["buckets"][0]["documents"] == 3, \
        "the whole folder is one bucket, sub-folders included"
    assert info["folders"][0]["path"] == str(picked) and info["folders"][0]["bucket_id"] == info["buckets"][0]["id"]
    assert not any(p.name in ("x.md", "y.md") for p in cfg.personal_dir.rglob("*.md")), "nothing is copied"
    found = client.post("/api/search", json={"query": "QQZ9 rail"}).json()["sources"]
    assert found and found[0]["doc_id"] >= PERSONAL_OFFSET
    (picked / "w.md").write_text("# Whiskey\n\nThe QQW9 rail is 0.5 V.\n", encoding="utf-8")
    assert _wait(client, client.post("/api/personal/rescan").json()["job"])["status"] == "done"
    assert client.get("/api/personal").json()["buckets"][0]["documents"] == 4, "a rescan picks up new files"
    # a document of the folder: out of the index, its file stays, a rescan leaves it out
    zulu = next(d for d in client.get("/api/documents").json() if d["path"].endswith("z.md"))
    res = client.delete(f"/api/personal/documents/{zulu['id']}").json()
    assert res["trash"] == "kept" and (picked / "alt" / "derin" / "z.md").exists()
    _wait(client, client.post("/api/personal/rescan").json()["job"])
    assert client.get("/api/personal").json()["buckets"][0]["documents"] == 3
    # unlinking the folder: its documents leave, the folder is not touched
    res = client.delete(f"/api/personal/folders/{info['folders'][0]['id']}").json()
    assert res["ok"] and res["removed"] == 3
    after = client.get("/api/personal").json()
    assert after["buckets"] == [] and after["folders"] == []
    assert all(p.exists() for p in before) and (picked / "w.md").exists()


def test_two_picked_folders_of_the_same_name_stay_two_buckets(client, tmp_path):
    ids = []
    for parent in ("a", "b"):
        d = tmp_path / parent / "Notlar"
        d.mkdir(parents=True)
        (d / f"{parent}.md").write_text(f"# Doc {parent}\n\nThe QQ{parent.upper()}7 rail is 0.4 V.\n", encoding="utf-8")
        r = client.post("/api/personal/folder", json={"path": str(d)}).json()
        _wait(client, r["job"])
        ids.append(r["root_id"])
    buckets = client.get("/api/personal").json()["buckets"]
    assert len(buckets) == 2 and {b["name"] for b in buckets} == {"Notlar"} and len({b["id"] for b in buckets}) == 2


def test_folders_that_overlap_the_personal_folder_are_refused(client, cfg, tmp_path):
    cfg.personal_dir.mkdir(parents=True, exist_ok=True)
    inside = cfg.personal_dir / "Alt"
    inside.mkdir()
    assert client.post("/api/personal/folder", json={"path": str(cfg.personal_dir)}).status_code == 400
    assert client.post("/api/personal/folder", json={"path": str(inside)}).status_code == 400
    assert client.post("/api/personal/folder", json={"path": str(cfg.personal_dir.parent)}).status_code == 400
    assert client.post("/api/personal/folder", json={"path": str(tmp_path / "yok")}).status_code == 400
    assert client.delete("/api/personal/folders/1").status_code == 404, "the personal folder itself is not unlinked"


def test_the_personal_folders_own_documents_still_go_to_the_recycle_bin(client, cfg, tmp_path, monkeypatch):
    from techrag import trash

    monkeypatch.setattr(trash, "_fixed_drive", lambda p: False)
    src = tmp_path / "picked.md"
    src.write_text("# Picked\n\nThe QQP1 rail is 0.3 V.\n", encoding="utf-8")
    _wait(client, client.post("/api/personal/add", json={"paths": [str(src)]}).json()["job"])
    own = next(d for d in client.get("/api/documents").json() if d["path"].endswith("picked.md"))
    res = client.delete(f"/api/personal/documents/{own['id']}").json()
    assert res["trash"] == "folder" and Path(res["path"]).exists() and src.exists()
