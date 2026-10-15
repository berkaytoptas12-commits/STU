"""Question history: the questions asked in the chat and the answers shown, kept per user (fake model server)."""

import json

import pytest
from fastapi.testclient import TestClient

from techrag.server import create_app


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def _ask(client, question):
    with client.stream("POST", "/api/ask", json={"question": question}) as r:
        return [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")][-1]


def test_answers_shown_in_the_chat_are_kept(client, cfg):
    fin = _ask(client, "DDR5 tRFC değeri nedir?")
    h = client.get("/api/history").json()
    assert h["total"] == 1 and h["enabled"]
    d = h["items"][0]
    assert d["question"] == "DDR5 tRFC değeri nedir?" and d["answer"] == fin["answer"] and "295" in d["answer"]
    assert d["status"] == "supported" and d["counts"]["supported"] >= 1
    assert d["sources"] and all(s["doc_title"] and s["page_start"] for s in d["sources"])
    stored = (cfg.user_data_dir / "history.json").read_text(encoding="utf-8")
    assert "text" not in json.loads(stored)[0]["sources"][0], "no source text is kept"
    assert str(cfg.library_dir) not in stored and "http://" not in stored
    assert not list(cfg.library_dir.rglob("history.json")), "nothing is written into the library"


def test_search_delete_clear_and_order(client):
    _ask(client, "DDR5 tRFC değeri nedir?")
    _ask(client, "I2C Fast-mode bit hızı nedir?")
    items = client.get("/api/history").json()["items"]
    assert [i["question"] for i in items] == ["I2C Fast-mode bit hızı nedir?", "DDR5 tRFC değeri nedir?"]
    assert client.get("/api/history", params={"q": "trfc"}).json()["total"] == 1
    assert client.get("/api/history", params={"limit": 1, "offset": 1}).json()["items"][0]["question"].startswith("DDR5")
    assert client.delete(f"/api/history/{items[0]['id']}").json()["ok"]
    assert client.get("/api/history").json()["total"] == 1
    assert client.delete("/api/history/nope").status_code == 404
    assert client.delete("/api/history").json()["deleted"] == 1
    assert client.get("/api/history").json()["total"] == 0


def test_history_can_be_switched_off_and_scripts_are_not_kept(client, cfg):
    client.post("/api/ask", json={"question": "DDR5 tRFC değeri nedir?", "stream": False})
    assert client.get("/api/history").json()["total"] == 0, "the non-streaming form (scripts) is not kept"
    cfg.answer.keep_history = False
    _ask(client, "DDR5 tRFC değeri nedir?")
    h = client.get("/api/history").json()
    assert h["total"] == 0 and h["enabled"] is False


def test_history_works_with_a_read_only_library(client, cfg):
    cfg.read_only = True
    _ask(client, "DDR5 tRFC değeri nedir?")
    assert client.get("/api/history").json()["total"] == 1


def test_history_is_capped(tmp_path):
    from techrag.history import MAX_ITEMS, HistoryStore

    hs = HistoryStore(tmp_path)
    for i in range(MAX_ITEMS + 5):
        hs.add({"id": str(i), "question": f"q{i}", "answer": ""})
    items = hs.items()
    assert len(items) == MAX_ITEMS and items[0]["id"] == "5", "the oldest go first"
