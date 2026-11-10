"""The local copy of a read-only network library's index: taken once, searched locally, renewed in the background
when the library is updated on the network, never used half-written, and still working away from the network."""

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


def test_the_network_library_is_looked_at_on_start_and_on_request_only(shared, engine):
    import time

    mgr, _ = _manager(engine)
    calls = []
    mgr.check = lambda: calls.append(time.time()) or False
    mgr.configure(shared)
    mgr.start()
    try:
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        time.sleep(0.3)
        assert len(calls) == 1, "once when the app starts, no periodic check"
        mgr.sync_now()                      # Admin › "Şimdi denetle"
        for _ in range(100):
            if len(calls) == 2:
                break
            time.sleep(0.02)
        time.sleep(0.2)
        assert len(calls) == 2
    finally:
        mgr.stop()


def test_a_question_during_the_first_copy_is_answered_at_once(shared, engine, tmp_path, monkeypatch):
    """A question asked while the first copy is taken is answered from the network library right away (it used to
    wait for the copy, up to 15 minutes); the app switches to the copy afterwards."""
    import json
    import threading
    import time

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    real_sync = mirror.Mirror.sync
    release = threading.Event()

    def slow_sync(self, progress=None):
        self.status.update(state="copying", progress=0.0)
        release.wait(10)                # the copy is still running while the question is answered
        return real_sync(self, progress)

    monkeypatch.setattr(mirror.Mirror, "sync", slow_sync)
    with TestClient(create_app(shared, engine, warmup=False, desktop=True)) as client:
        mgr = client.app.state.mirrors
        mgr.start()
        try:
            t = time.time()
            with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC değeri nedir?"}) as r:
                events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
            assert time.time() - t < 8, "not waiting for the copy"
            assert "295" in next(e for e in events if e["type"] == "final")["answer"]
            assert not any(e.get("stage") == "mirror" for e in events if e["type"] == "status")
            assert Path(engine.shared.db_path) == shared.db_path, "answered from the network library"
            release.set()
            for _ in range(200):
                if Path(engine.shared.db_path) == (mgr.mirror.current() or Path("-")):
                    break
                time.sleep(0.05)
            assert Path(engine.shared.db_path) == mgr.mirror.current(), "then switched to the copy"
        finally:
            release.set()
            mgr.stop()


def test_the_switch_never_holds_two_sets_of_vectors(shared, engine):
    """Switching to a new copy lets go of the previous library's search vectors before loading the new ones, and
    waits for a running question (which holds them) to finish: two sets never in memory together."""
    import threading
    import time

    network = engine.shared
    engine.retrieve("DDR5 tRFC")                 # a question loaded the network library's vectors
    assert network._vectors is not None
    mgr, swaps = _manager(engine)
    mgr.configure(shared)
    held = threading.Event()
    done = threading.Event()

    def question():
        with engine.activity.running():         # a question still running on the network library
            held.set()
            done.wait(5)

    q = threading.Thread(target=question)
    q.start()
    held.wait(5)
    switch = threading.Thread(target=mgr.check)
    switch.start()
    time.sleep(0.5)
    assert Path(engine.shared.db_path) == shared.db_path and network._vectors is not None, "waits for it"
    done.set()
    q.join(5)
    switch.join(10)
    assert Path(engine.shared.db_path) == swaps[0]
    assert network._vectors is None, "the network library's vectors were let go"
    assert engine.shared._vectors is not None, "the copy's are loaded, ready for the next question"
    assert engine.retrieve("DDR5 tRFC").passages


def test_a_fast_mode_view_follows_the_switch_to_the_copy(shared, engine, fake):
    """An evaluation with the fast model holds a view of the engine for its whole run: when the local copy is taken
    (or renewed) meanwhile, the view answers from the new copy too - the network library's vectors are not loaded
    again beside the copy's, and a renewed copy's old file being cleaned up does not break the view."""
    from techrag.llm import LLMClient

    shared.llm_fast.model = "fast-test"
    engine._fast_llm = LLMClient(shared.chat_config("fast"), transport=fake.transport())
    network = engine.shared
    view = engine.for_mode("fast")                # made before the copy exists
    assert view is not engine and view.shared is network
    mgr, swaps = _manager(engine)
    mgr.configure(shared)
    assert mgr.check()
    assert Path(view.shared.db_path) == swaps[0] and view.store is engine.store, "the view sees the copy"
    assert view.retrieve("DDR5 tRFC").passages
    assert network._vectors is None, "the network library's vectors are not loaded again"
    first = Path(engine.shared.db_path)
    _bump(shared)                                 # the library is updated on the network
    assert mgr.check()                            # renewed copy; the first copy's file is cleaned up
    assert not first.exists() and Path(view.shared.db_path) == swaps[1]
    assert view.retrieve("DDR5 tRFC").passages, "the view keeps answering from the renewed copy"
