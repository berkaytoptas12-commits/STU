"""Chat history: conversations saved per user in the settings folder, listed newest first, deleted one by one or all."""

import json

import pytest
from fastapi.testclient import TestClient

from techrag.chats import ChatStore
from techrag.server import create_app


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def _conv(q="DDR5 tRFC değeri nedir?"):
    return {"title": "", "created": "2026-11-02T10:00:00+03:00",
            "turns": [{"q": q, "asked": q, "final": {"answer": "tRFC 295 ns [1]", "sources": [{"n": 1}]}}]}


def test_conversations_are_saved_listed_and_deleted(client, cfg):
    a, b = "a" * 24, "b" * 24
    r = client.put(f"/api/chats/{a}", json=dict(_conv(), title="İlk sohbet")).json()
    assert r["id"] == a and r["turns"] == 1 and r["title"] == "İlk sohbet"
    client.put(f"/api/chats/{b}", json=_conv("I2C hızı?"))
    listed = client.get("/api/chats").json()["chats"]
    assert [c["id"] for c in listed] == [b, a], "newest first"
    got = client.get(f"/api/chats/{a}").json()
    assert got["turns"][0]["final"]["answer"] == "tRFC 295 ns [1]" and got["title"] == "İlk sohbet"
    folder = cfg.user_data_dir / "chats"
    assert (folder / f"{a}.json").exists() and not (cfg.library_dir / "chats").exists(), \
        "kept in the user's own folder, never in the library"
    # a conversation grows: the same id is saved again
    conv = got
    conv["turns"].append({"q": "Peki DDR4?", "final": {"answer": "350 ns"}})
    assert client.put(f"/api/chats/{a}", json=conv).json()["turns"] == 2
    assert client.delete(f"/api/chats/{a}").json()["ok"]
    assert client.get(f"/api/chats/{a}").status_code == 404
    assert client.delete(f"/api/chats/{a}").status_code == 404
    assert [c["id"] for c in client.get("/api/chats").json()["chats"]] == [b]
    assert client.delete("/api/chats").json()["removed"] == 1
    assert client.get("/api/chats").json()["chats"] == []


def test_ids_and_contents_are_checked(client, cfg):
    for bad in ("..", "x" * 24, "ABCDEF12", "a" * 41):
        r = client.put(f"/api/chats/{bad}", json=_conv())
        assert r.status_code in (400, 404, 405), bad
    assert client.get("/api/chats/..%2Fsettings").status_code in (400, 404)
    assert client.put(f"/api/chats/{'c' * 24}", json={"turns": []}).status_code == 400, "an empty conversation is not kept"
    assert not any((cfg.user_data_dir / "chats").glob("*.json")) if (cfg.user_data_dir / "chats").exists() else True


def test_the_list_is_rebuilt_when_the_index_is_lost(tmp_path):
    store = ChatStore(tmp_path / "chats")
    store.save("d" * 24, _conv())
    (tmp_path / "chats" / "index.json").write_text("{broken", encoding="utf-8")
    assert [c["id"] for c in store.list()] == ["d" * 24]
    (tmp_path / "chats" / "index.json").unlink()
    store.save("e" * 24, _conv("ikinci"))
    assert {c["id"] for c in store.list()} == {"d" * 24, "e" * 24}
    assert json.loads((tmp_path / "chats" / ("e" * 24 + ".json")).read_text(encoding="utf-8"))["turns"][0]["q"] == "ikinci"
