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
    assert "295 ns" in fin["answer"] and fin["verification"]["status"] == "ok"


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
