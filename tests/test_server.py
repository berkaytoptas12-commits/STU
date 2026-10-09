import json
import time

import pytest
from fastapi.testclient import TestClient

from techrag.server import create_app
from techrag.settings import MASK, load_user_settings


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def test_ui_info_and_documents(client):
    r = client.get("/")
    assert r.status_code == 200 and "TechRAG" in r.text
    info = client.get("/api/info").json()
    assert info["stats"]["documents"] == 3 and info["stats"]["parameters"] == 4
    assert {"ddr", "i2c"} <= {d["key"] for d in info["domains"] if d["documents"]}
    docs = client.get("/api/documents").json()
    d5 = next(d for d in docs if "DDR5" in d["entities"])
    assert d5["revision"] == "A" and "toc" not in d5
    assert client.get(f"/api/documents/{d5['id']}").json()["toc"][0][1] == "1 Scope"


def test_page_image(client):
    d = client.get("/api/documents").json()[0]
    r = client.get(f"/api/documents/{d['id']}/page/1.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/api/documents/{d['id']}/page/999.png").status_code == 404


def test_ask_stream_events(client):
    with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC değeri nedir?"}) as r:
        events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and "plan" in kinds and "tool" in kinds and kinds[-1] == "final"
    fin = events[-1]
    assert "295 ns" in fin["answer"] and fin["verification"]["status"] == "supported"


def test_search_endpoint(client):
    r = client.post("/api/search", json={"query": "DDR4 tRFC"}).json()
    assert r["scope"]["reason"] == "entity" and r["sources"] and r["parameters"]


def test_settings_roundtrip_masks_keys(client, tmp_path):
    s = client.get("/api/settings").json()
    assert set(s) >= {"llm", "vision", "embedding", "reranker", "ui"}
    r = client.put("/api/settings", json={"llm": {"api_key": "secret-1", "thinking": "on"}, "ui": {"language": "en"}}).json()
    assert r["settings"]["llm"]["api_key"] == MASK and r["settings"]["llm"]["thinking"] == "on"
    assert load_user_settings()["llm"]["api_key"] == "secret-1"
    client.put("/api/settings", json={"llm": {"api_key": MASK}})
    assert load_user_settings()["llm"]["api_key"] == "secret-1", "masked value keeps the stored key"
    assert client.put("/api/settings", json={"retrieval": {"final_top_k": "abc"}}).status_code == 400


def test_model_listing_reports_unreachable_servers(client):
    r = client.post("/api/settings/models", json={"service": "embedding", "base_url": "http://127.0.0.1:9/v1"}).json()
    assert r["ok"] is False and r["models"] == []
    assert client.post("/api/settings/models", json={"service": "nope"}).status_code == 400


def test_add_sources_and_ingest_job(client, tmp_path):
    f = tmp_path / "TIA-422_notes.md"
    f.write_text("# RS-422\n\nRS-422 supports data rates up to 10 Mbit/s with up to 10 receivers.\n")
    r = client.post("/api/sources/add", json={"paths": [str(f)], "collection": "rs422"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    for _ in range(200):
        st = client.get(f"/api/jobs/{job}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    docs = client.get("/api/documents").json()
    assert any(d["domain"] == "rs422" and d["entities"] == ["TIA-422"] for d in docs)
    assert client.post("/api/sources/add", json={"paths": [str(tmp_path / "x.exe")]}).status_code == 400


def test_publish_and_open_read_only(client, tmp_path):
    dest = tmp_path / "shared"
    assert client.post("/api/library/publish", json={"dest": str(dest)}).json()["ok"]
    r = client.post("/api/library/open", json={"path": str(dest), "read_only": True})
    assert r.status_code == 200
    info = client.get("/api/info").json()
    assert info["read_only"] and info["allow_upload"] is False
    assert client.post("/api/ingest", json={}).status_code in (403, 503)


def test_token_auth(cfg, engine):
    cfg.server.api_token = "t0k"
    with TestClient(create_app(cfg, engine, warmup=False)) as c:
        assert c.get("/api/info").status_code == 401
        assert c.get("/api/info", headers={"Authorization": "Bearer t0k"}).status_code == 200
        d = c.get("/api/documents?token=t0k").json()[0]
        assert c.get(f"/api/documents/{d['id']}/page/1.png?token=t0k").status_code == 200


def test_page_info_and_migrate_endpoint(client):
    d = next(x for x in client.get("/api/documents").json() if x["path"].endswith(".pdf"))
    info = client.get(f"/api/documents/{d['id']}/page/6/info").json()
    assert info["geometry"] and info["has_text"] and not info["legacy"] and info["sha256"] == d["sha256"]
    assert client.get(f"/api/documents/{d['id']}/page/999/info").status_code == 404
    assert "page_labels" in client.get(f"/api/documents/{d['id']}").json()
    job = client.post("/api/library/migrate").json()["job"]
    for _ in range(200):
        st = client.get(f"/api/jobs/{job}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done" and st["report"]["documents"] == 0, "nothing to migrate in a current index"


def _wait_job(client, job):
    for _ in range(400):
        st = client.get(f"/api/jobs/{job}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def test_document_folder_endpoints(client, tmp_path):
    from test_folders import simple_pdf

    root = tmp_path / "Teknik Belgeler"
    simple_pdf(root / "PCIe" / "Base.pdf", "PCI Express Base Specification 5.0", "LTSSM.")
    simple_pdf(root / "DDR" / "DDR5" / "Specification.pdf", "JEDEC DDR5 SDRAM", "tRFC is 295 ns.")
    (root / "notes.xlsx").write_bytes(b"x")
    pv = client.post("/api/folders/preview", json={"path": str(root)}).json()
    assert pv["scan"]["documents"] == 2 and pv["scan"]["unsupported_count"] == 1 and not pv["known"]
    assert {b["name"] for b in pv["scan"]["buckets"]} == {"PCIe", "DDR"}
    r = client.post("/api/folders", json={"path": str(root)}).json()
    st = _wait_job(client, r["job"])
    assert st["status"] == "done" and st["progress"]["total"] == 2 and len(st["report"]["added"]) == 2
    info = client.get("/api/info").json()
    assert {"PCIe", "DDR"} <= {b["name"] for b in info["buckets"]} and info["roots"][0]["documents"] == 2
    docs = client.get("/api/documents").json()
    d5 = next(d for d in docs if d.get("subpath") == "DDR5")
    assert d5["bucket"] == "DDR" and client.get(f"/api/documents/{d5['id']}/page/1.png").status_code == 200
    pv = client.post("/api/folders/preview", json={"path": str(root)}).json()
    assert pv["known"] and pv["plan"]["counts"] == {"unchanged": 2}
    (root / "PCIe" / "Base.pdf").unlink()
    pv = client.post("/api/rescan/preview", json={}).json()
    assert pv["counts"]["missing"] == 1
    st = _wait_job(client, client.post("/api/rescan", json={}).json()["job"])
    assert st["report"]["missing"] and client.get("/api/info").json()["missing_documents"] == 1
    assert client.post("/api/documents/purge-missing").json()["removed"]
    assert client.post("/api/folders", json={"path": str(tmp_path / "yok")}).status_code == 400


def test_single_files_with_the_same_name_do_not_overwrite(client, tmp_path):
    a, b = tmp_path / "a" / "Spec.md", tmp_path / "b" / "Spec.md"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_text("# A\n\nFirst specification text about I2C.\n", encoding="utf-8")
    b.write_text("# B\n\nSecond specification text about SPI.\n", encoding="utf-8")
    r1 = client.post("/api/sources/add", json={"paths": [str(a)], "collection": "Arayüz Notları"}).json()
    _wait_job(client, r1["job"])
    r2 = client.post("/api/sources/add", json={"paths": [str(b)], "collection": "Arayüz Notları"}).json()
    _wait_job(client, r2["job"])
    assert r1["copied"][0] != r2["copied"][0] and r2["copied"][0].endswith("Spec (2).md")
    names = {d["rel_path"] for d in client.get("/api/documents").json() if d["bucket"] == "Arayüz Notları"}
    assert names == {"Arayüz Notları/Spec.md", "Arayüz Notları/Spec (2).md"}
