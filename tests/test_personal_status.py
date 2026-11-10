"""Ayarlar › personal files: the indexing status of every personal document (indexed / partly / waiting / failed /
file missing), when it was indexed, what it holds, and the pieces it was split into. The panel only reads."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from techrag.library import PERSONAL_OFFSET
from techrag.server import create_app

NOTE = """# Lab Notes
## 1 Probe setup
The oscilloscope probe ground spring for the ZQX7 measurement must be shorter than 5 mm.
## 2 Cabling
Keep the coaxial cable between the probe and the scope below 1.5 m for the ZQX7 setup.
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


def _status(client) -> dict:
    r = client.get("/api/personal/status")
    assert r.status_code == 200, r.text
    return r.json()


def _by_file(st: dict) -> dict:
    return {d["file"]: d for d in st["documents"]}


def test_no_personal_library_yet(client):
    st = _status(client)
    assert st["available"] is False and st["documents"] == []


def test_status_of_indexed_waiting_and_missing_documents(client, cfg, tmp_path):
    src = tmp_path / "picked" / "lab_notes.md"
    src.parent.mkdir()
    src.write_text(NOTE, encoding="utf-8")
    st = _wait(client, client.post("/api/personal/add", json={"paths": [str(src)]}).json()["job"])
    assert st["status"] == "done", st["messages"]

    st = _status(client)
    doc = _by_file(st)["lab_notes.md"]
    assert st["available"] and not st["running"]
    assert doc["status"] == "indexed" and doc["reason"] == ""
    assert doc["doc_id"] >= PERSONAL_OFFSET and doc["ingested_at"], "when it was indexed"
    assert doc["n_chunks"] >= 1 and doc["n_pages"] >= 1, "what the index holds of it"
    assert st["counts"] == {"indexed": 1}

    # the pieces it was split into
    ch = client.get(f"/api/personal/documents/{doc['doc_id']}/chunks").json()
    assert ch["total"] == doc["n_chunks"] and len(ch["chunks"]) == doc["n_chunks"]
    assert ch["chunks"][0]["n"] == 1 and "ZQX7" in " ".join(c["text"] for c in ch["chunks"])
    assert all(c["section"] for c in ch["chunks"]) and all(c["tokens"] > 0 for c in ch["chunks"])
    assert client.get("/api/personal/documents/5/chunks").status_code == 404, "a shared document is not listed here"

    # a file copied into the personal folder by hand, and the indexed one changed: both wait for "Yeniden indeksle"
    (cfg.personal_dir / "yeni_not.md").write_text("# New\n\nThe QWV9 clamp torque is 2 Nm.\n", encoding="utf-8")
    kept = cfg.personal_dir / "lab_notes.md"
    kept.write_text(NOTE + "\n## 3 Extra\nA third section.\n", encoding="utf-8")
    os.utime(kept, (time.time() + 5, time.time() + 5))
    files = _by_file(_status(client))
    assert files["yeni_not.md"]["status"] == "pending" and files["yeni_not.md"]["reason"] == "new"
    assert files["yeni_not.md"]["doc_id"] is None and not files["yeni_not.md"]["ingested_at"]
    assert files["lab_notes.md"]["status"] == "pending" and files["lab_notes.md"]["reason"] == "changed"
    assert files["lab_notes.md"]["n_chunks"] >= 1, "its previous index is still there meanwhile"

    # "Yeniden indeksle" (only what changed) brings both in
    st = _wait(client, client.post("/api/personal/rescan").json()["job"])
    assert st["status"] == "done", st["messages"]
    files = _by_file(_status(client))
    assert files["yeni_not.md"]["status"] == "indexed" and files["lab_notes.md"]["status"] == "indexed"

    # a file removed by hand: still in the index, not used in answers - shown as such
    (cfg.personal_dir / "yeni_not.md").unlink()
    st = _wait(client, client.post("/api/personal/rescan").json()["job"])
    gone = _by_file(_status(client))["yeni_not.md"]
    assert gone["status"] == "missing" and gone["reason"] == "missing"


def test_a_document_that_could_not_be_indexed_is_shown_as_failed(client, cfg, tmp_path, monkeypatch):
    from techrag.ingest import pipeline

    real = pipeline.load_document

    def broken(path, **kw):
        if path.name == "bozuk.md":
            raise ValueError("cannot parse this file")
        return real(path, **kw)

    monkeypatch.setattr(pipeline, "load_document", broken)
    cfg.personal_dir.mkdir(parents=True, exist_ok=True)
    (cfg.personal_dir / "bozuk.md").write_text("# x\n\ntext\n", encoding="utf-8")
    (cfg.personal_dir / "iyi.md").write_text(NOTE, encoding="utf-8")
    _wait(client, client.post("/api/personal/rescan").json()["job"])
    files = _by_file(_status(client))
    bad = files["bozuk.md"]
    assert bad["status"] == "failed" and bad["reason"] == "failed_new" and bad["doc_id"] is None
    assert bad["quality"]["issues"] and bad["quality"]["issues"][0]["code"] == "parse_failed"
    assert files["iyi.md"]["status"] == "indexed", "one failing file never stops the others"
    order = [d["status"] for d in _status(client)["documents"]]
    assert order.index("failed") < order.index("indexed"), "what needs attention comes first"
