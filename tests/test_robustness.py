"""Small fixes that change no answer: search vectors reloaded without two sets in memory, errors the user can act on,
a dropped connection to the embedding / rerank server tried again, and the build pinned to tested versions."""
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from techrag import api
from techrag.api import APIClient, APIError, error_code
from techrag.config import ServiceConfig
from techrag.llm import LLMError

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ vectors reloaded one set at a time
def test_changed_library_vectors_are_reloaded_without_keeping_the_old_set(engine, monkeypatch):
    store = engine.shared
    old = store.vectors()
    seen = []
    real = store._load_vectors

    def load():
        seen.append(store._vectors)               # what the store still holds while the new set is read
        return real()

    monkeypatch.setattr(store, "_load_vectors", load)
    with store.connect() as con:
        store._bump_version(con)                  # the library changed (indexing, removal ...)
    new = store.vectors()
    assert seen == [None], "the previous set is let go before the new one is loaded"
    assert new is not old and new.size == old.size
    assert [i for i, _ in new.search(old.matrix[0].astype("float32"), 3)] == \
        [i for i, _ in old.search(old.matrix[0].astype("float32"), 3)], "the same results"


def test_a_failed_reload_is_tried_again_by_the_next_question(engine, monkeypatch):
    store = engine.shared
    store.vectors()
    with store.connect() as con:
        store._bump_version(con)
    monkeypatch.setattr(store, "_load_vectors", lambda: (_ for _ in ()).throw(MemoryError("no room")))
    with pytest.raises(MemoryError):
        store.vectors()
    monkeypatch.undo()
    assert store.vectors().size > 0


# ------------------------------------------------------------------ errors the user can act on
def _cause(outer: BaseException, inner: BaseException) -> BaseException:
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize("exc, code", [
    (MemoryError("Unable to allocate 809. MiB for an array with shape (51749, 4096)"), "memory"),
    (LLMError("LLM server unreachable: cannot connect", "connect"), "connect"),
    (LLMError("LLM server unreachable: certificate", "tls_untrusted"), "tls"),
    (httpx.ReadTimeout("timed out"), "timeout"),
    (_cause(LLMError("LLM HTTP 401"), APIError("LLM HTTP 401: unauthorized", 401)), "auth"),
    (_cause(RuntimeError("embedding failed"), APIError("down", code="connect")), "connect"),
    (ValueError("something else"), ""),
])
def test_error_codes(exc, code):
    assert error_code(exc) == code


def test_an_answer_that_fails_for_lack_of_memory_says_so(cfg, engine, tmp_path, monkeypatch):
    import json

    from techrag.server import create_app

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))

    def no_memory(*a, **kw):
        raise MemoryError("Unable to allocate 809. MiB for an array with shape (51749, 4096) and data type float32")
        yield  # pragma: no cover

    monkeypatch.setattr(engine, "ask_stream", no_memory)
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as client:
        with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC?", "stream": True}) as r:
            events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    err = next(e for e in events if e["type"] == "error")
    assert err["code"] == "memory" and "MemoryError" in err["message"], "the technical message stays as detail"


# ------------------------------------------------------------------ a dropped connection tried again
def _client(handler, monkeypatch, waits) -> APIClient:
    monkeypatch.setattr(api, "_sleep", lambda s: waits.append(s))
    svc = ServiceConfig(base_url="http://emb:8001/v1", model="m")
    return APIClient(svc, transport=httpx.MockTransport(handler))


def test_a_refused_or_dropped_connection_is_tried_again(monkeypatch):
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("[Errno 111] Connection refused")
        if len(calls) == 2:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    out = _client(handler, monkeypatch, waits).post("embeddings", {"input": ["x"]})
    assert out["data"][0]["embedding"] == [1.0, 0.0] and len(calls) == 3 and waits == [1.0, 2.0]


def test_a_connection_that_stays_down_still_fails_after_two_retries(monkeypatch):
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("[Errno 111] Connection refused")

    with pytest.raises(APIError) as e:
        _client(handler, monkeypatch, waits).post("embeddings", {"input": ["x"]})
    assert len(calls) == 3 and e.value.code == "connect"


def test_certificate_problems_and_timeouts_are_not_repeated(monkeypatch):
    calls, waits = [], []

    def cert(request):
        calls.append(1)
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")

    with pytest.raises(APIError) as e:
        _client(cert, monkeypatch, waits).post("rerank", {"query": "q"})
    assert len(calls) == 1 and e.value.code.startswith("tls_") and not waits

    calls.clear()

    def slow(request):
        calls.append(1)
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(httpx.ReadTimeout):
        _client(slow, monkeypatch, waits).post("rerank", {"query": "q"})
    assert len(calls) == 1 and not waits


def test_a_busy_server_is_waited_for_as_before(monkeypatch):
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="busy")
        return httpx.Response(200, json={"results": []})

    _client(handler, monkeypatch, waits).post("rerank", {"query": "q"})
    assert waits == [1.0], "the first wait for HTTP 429 is still 1 s"


# ------------------------------------------------------------------ the build pinned to tested versions
def _names(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]
        out.add(name.lower().replace("_", "-"))
    return out


def test_every_requirement_is_pinned_for_the_build():
    pins = {}
    for line in (ROOT / "packaging" / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            assert version, f"not an exact pin: {line}"
            pins[name.lower().replace("_", "-")] = version
    wanted = set()
    for req in ("requirements.txt", "requirements-dev.txt", "requirements-desktop.txt"):
        wanted |= _names(ROOT / req)
    wanted |= {"pyinstaller"}
    assert not wanted - set(pins), f"not pinned: {sorted(wanted - set(pins))}"
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")
    installs = [ln for ln in workflow.splitlines() if "pip install" in ln and "-r " in ln]
    assert installs and all("-c packaging/constraints.txt" in ln for ln in installs), installs
