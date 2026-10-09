import json
import time

import pytest
from conftest import FakeLLM
from fastapi.testclient import TestClient

from techrag.engine import RAGEngine
from techrag.ingest.pipeline import Ingestor
from techrag.server import create_app
from techrag.store import Store


@pytest.fixture()
def client(cfg, sources, registry, embedder):
    store = Store(cfg.db_path)
    Ingestor(cfg, store, embedder, registry).run()
    engine = RAGEngine(cfg, store=store, embedder=embedder, reranker=None,
                       llm=FakeLLM(answer="Fast-mode 400 kbit/s'ye kadar destekler [1]."), domains=registry)
    with TestClient(create_app(cfg, engine, warmup=False)) as c:
        yield c


def test_ui_and_info(client):
    r = client.get("/")
    assert r.status_code == 200 and "Standart Asistanı" in r.text
    assert client.get("/static/app.js").status_code == 200
    info = client.get("/api/info").json()
    assert info["stats"]["documents"] == 2
    keys = [d["key"] for d in info["domains"]]
    assert {"arinc", "ddr", "pcie", "ethernet", "displayport", "usb", "rs422", "i2c", "general"} <= set(keys)


def test_documents_and_file(client):
    docs = client.get("/api/documents").json()
    pdf = next(d for d in docs if d["path"].endswith(".pdf"))
    detail = client.get(f"/api/documents/{pdf['id']}").json()
    assert detail["toc"][0][1] == "1 Scope"
    r = client.get(f"/api/documents/{pdf['id']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get("/api/documents/999/file").status_code == 404


def test_ask_json_and_stream(client):
    r = client.post("/api/ask", json={"question": "I2C Fast-mode hızı nedir?", "stream": False})
    body = r.json()
    assert body["routed_domains"] == ["i2c"]
    assert body["verification"]["status"] == "ok"
    assert body["sources"][0]["domain"] == "i2c"

    with client.stream("POST", "/api/ask", json={"question": "I2C Fast-mode hızı nedir?"}) as r:
        events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "plan" and kinds[1] == "sources" and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == events[-1]["answer"]


def test_search_with_scope(client):
    docs = client.get("/api/documents").json()
    pdf = next(d for d in docs if d["path"].endswith(".pdf"))
    r = client.post("/api/search", json={"query": "parity bit", "doc_ids": [pdf["id"]]}).json()
    assert r["sources"] and all(s["doc_id"] == pdf["id"] for s in r["sources"])


def test_upload_then_ingest(client):
    files = {"file": ("rs422 notes.md", b"# RS-422\n\nRS-422 supports data rates up to 10 Mbit/s.\n", "text/markdown")}
    r = client.post("/api/upload", files=files, data={"domain": "rs422"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    for _ in range(100):
        status = client.get(f"/api/jobs/{job}").json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status
    docs = client.get("/api/documents").json()
    assert any(d["domain"] == "rs422" for d in docs)

    bad = client.post("/api/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                      data={"domain": "rs422"})
    assert bad.status_code == 400
    bad = client.post("/api/upload", files=files, data={"domain": "nope"})
    assert bad.status_code == 400


def test_token_auth(cfg, registry, embedder):
    cfg.server.api_token = "s3cret"
    engine = RAGEngine(cfg, embedder=embedder, reranker=None, llm=FakeLLM(), domains=registry)
    with TestClient(create_app(cfg, engine, warmup=False)) as c:
        assert c.get("/api/info").status_code == 401
        assert c.get("/api/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/").status_code == 200  # the UI shell itself is public; it asks for the token
