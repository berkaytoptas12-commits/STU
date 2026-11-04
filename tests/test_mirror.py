"""The local copy of a read-only network library's index: taken once, searched locally, renewed in the background
when the library is updated on the network, never used half-written, and still working away from the network."""

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from techrag import mirror
from techrag.server import create_app
from techrag.store import Store


@pytest.fixture()
def shared(cfg, engine):
    """The library as other users open it: read-only, copied locally (paths.mirror on: tests have no network)."""
    cfg.read_only = True
    cfg.paths.mirror = "on"
    return cfg


def _manager(engine):
    swaps = []

    def ready(path):
        engine.replace_shared(Store(path, read_only=True))
        swaps.append(path)

    return mirror.MirrorManager(ready), swaps


def _bump(cfg):
    w = Store(cfg.db_path)
    with w.connect() as con:
        w._bump_version(con)


def test_when_a_copy_is_used():
    from techrag.config import Config

    c = Config()
    c.paths.library_dir = "\\\\sunucu\\paylasim\\library"
    assert mirror.is_network(Path("\\\\sunucu\\paylasim\\library")) and not mirror.wanted(c), "writable: never"
    c.read_only = True
    assert mirror.wanted(c), "read-only on a network folder"
    c.paths.mirror = "off"
    assert not mirror.wanted(c)
    c.paths.mirror, c.paths.library_dir = "auto", "/home/x/library"
    assert not mirror.wanted(c), "a library on this computer needs no copy"


def test_the_copy_is_taken_searched_and_renewed_when_the_library_changes(shared, engine):
    mgr, swaps = _manager(engine)
    assert mgr.configure(shared) is None, "no copy yet: the network library is used meanwhile"
    assert mgr.status()["state"] == "network"
    assert mgr.check() and len(swaps) == 1
    first = swaps[0]
    assert first.is_file() and mirror.mirror_dir(shared) in first.parents
    assert Path(engine.shared.db_path) == first, "answering reads the local copy"
    st = mgr.status()
    assert st["state"] == "ready" and st["size"] == shared.db_path.stat().st_size and st["synced_at"]
    hits = engine.store.search_bm25("DDR5 refresh tRFC", 5)
    assert hits, "searching works on the copy"
    assert not mgr.check(), "nothing changed: nothing copied"
    _bump(shared)                       # the library is updated on the network
    assert mgr.check() and len(swaps) == 2
    assert swaps[1] != first and Path(engine.shared.db_path) == swaps[1]
    assert not first.exists(), "the older copy is removed"
    # a restart finds the copy at once (no waiting for the network)
    again = mirror.MirrorManager(lambda p: None)
    assert again.configure(shared) == swaps[1]


def test_a_library_changing_during_the_copy_is_not_used(shared, engine, monkeypatch):
    mgr, swaps = _manager(engine)
    mgr.configure(shared)
    real = mirror.source_identity
    calls = {"n": 0}

    def moving(db):
        calls["n"] += 1
        return real(db) + (f"|{calls['n']}" if calls["n"] > 1 else "")

    monkeypatch.setattr(mirror, "source_identity", moving)
    assert not mgr.check() and swaps == []
    st = mgr.status()
    assert st["state"] == "error" and "changed while it was being copied" in st["error"]
    assert not list(mirror.mirror_dir(shared).glob("*.part")) and not list(mirror.mirror_dir(shared).glob("index-*"))
    monkeypatch.setattr(mirror, "source_identity", real)
    assert mgr.check() and len(swaps) == 1, "taken on the next round"


def test_a_copy_that_fails_the_integrity_check_is_not_used(shared, engine, monkeypatch):
    mgr, swaps = _manager(engine)
    mgr.configure(shared)
    real_open = open

    def corrupting(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if "b" in mode and "w" in mode and str(path).endswith(".part"):
            orig = f.write

            def write(chunk):
                return orig(b"\0" * len(chunk))   # garbage instead of the index
            f.write = write
        return f

    monkeypatch.setattr("builtins.open", corrupting)
    assert not mgr.check() and swaps == []
    assert mgr.status()["state"] == "error"
    assert Path(engine.shared.db_path) == shared.db_path, "the library itself keeps answering"


def test_away_from_the_network_the_copy_keeps_working(shared, engine, tmp_path):
    mgr, swaps = _manager(engine)
    mgr.configure(shared)
    assert mgr.check()
    away = tmp_path / "library-away"
    shutil.move(str(shared.library_dir), str(away))     # the share cannot be reached
    try:
        assert not mgr.check()
        st = mgr.status()
        assert st["state"] == "offline" and Path(st["path"]).is_file()
        assert engine.store.search_bm25("DDR5 refresh", 5), "still searchable"
    finally:
        shutil.move(str(away), str(shared.library_dir))
    assert not mgr.check() and mgr.status()["state"] == "ready", "back on the network, nothing changed"


def test_the_app_reports_and_checks_the_copy(shared, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(shared, engine, warmup=False, desktop=True)) as client:
        m = client.get("/api/info").json()["mirror"]
        assert m["enabled"] and m["state"] == "network" and m["read_only"]
        assert client.app.state.mirrors.check()
        m = client.get("/api/info").json()["mirror"]
        assert m["state"] == "ready" and m["size"] > 0
        docs = client.get("/api/documents").json()
        assert docs and client.post("/api/search", json={"query": "DDR5 tRFC"}).json()["sources"]
        assert client.post("/api/library/mirror/sync").json()["ok"]


def test_the_writable_library_is_never_copied(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    cfg.paths.mirror = "on"
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as client:
        m = client.get("/api/info").json()["mirror"]
        assert not m["enabled"] and m["state"] == "off" and not m["read_only"]
        assert client.post("/api/library/mirror/sync").status_code == 400
    assert not (Path(cfg.paths.cache_dir) / "mirror").exists()
