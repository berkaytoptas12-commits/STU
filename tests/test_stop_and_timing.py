"""Stopping an answer (the model request is closed, nothing is verified or shown as an answer), where an answer's
time went (stages and model calls), and the backend's "answer ready" signal for the desktop notification."""

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from techrag import askctx
from techrag.config import LLMConfig
from techrag.llm import LLMClient
from techrag.server import create_app


def _sse(events, done=True):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + ("data: [DONE]\n\n" if done else "")


def _client(handler):
    return LLMClient(LLMConfig(base_url="http://llm.test/v1", model="m"), transport=httpx.MockTransport(handler))


def test_stop_closes_the_model_request_mid_stream():
    ctx = askctx.AskContext()
    sent = []

    def body():
        for i in range(50):
            sent.append(i)
            if i == 3:
                ctx.stop()          # the user presses Stop while the model is writing
            yield _sse([{"choices": [{"delta": {"content": f"w{i} "}}]}], done=False).encode()
        yield b"data: [DONE]\n\n"

    llm = _client(lambda req: httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"}))
    got = []
    with askctx.active(ctx), askctx.stage("answer"), pytest.raises(askctx.Cancelled):
        for kind, value in llm.stream([{"role": "user", "content": "q"}]):
            got.append(value)
    assert len(sent) < 10, "the response is closed, not read to the end"
    assert ctx.calls and ctx.calls[0].stage == "answer" and not ctx.calls[0].ok
    with askctx.active(ctx), pytest.raises(askctx.Cancelled):
        llm.chat([{"role": "user", "content": "q"}])     # a stopped question sends no new request
    assert len(sent) < 10


def test_call_stats_use_the_servers_usage_or_an_estimate():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        events = [{"choices": [{"delta": {"reasoning_content": "x" * 70}}]},
                  {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}]
        if seen[-1].get("stream_options"):
            events.append({"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 120,
                                                    "prompt_tokens_details": {"cached_tokens": 800}}})
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    ctx = askctx.AskContext()
    llm = _client(handler)
    with askctx.active(ctx), askctx.stage("judge"):
        llm.chat([{"role": "user", "content": "q"}])
    assert seen[0]["stream_options"] == {"include_usage": True}
    c = ctx.calls[0]
    assert (c.stage, c.completion_tokens, c.prompt_tokens, c.cached_tokens, c.estimated) == ("judge", 120, 900, 800, False)
    assert c.reasoning_tokens == 20, "thinking estimated from its text when the server does not split it"
    s = ctx.summary()["judge"]
    assert s["calls"] == 1 and s["completion_tokens"] == 120 and "tokens_per_second" in s


def test_servers_without_stream_options_are_asked_again_without_it():
    payloads = []

    def handler(req):
        payloads.append(json.loads(req.content))
        if "stream_options" in payloads[-1]:
            return httpx.Response(400, json={"error": "Unrecognized request argument: stream_options"})
        return httpx.Response(200, text=_sse([{"choices": [{"delta": {"content": "hello world"}}]}]),
                              headers={"content-type": "text/event-stream"})

    ctx = askctx.AskContext()
    llm = _client(handler)
    with askctx.active(ctx):
        assert llm.text([{"role": "user", "content": "q"}]) == "hello world"
        llm.text([{"role": "user", "content": "q"}])
    assert len(payloads) == 3 and "stream_options" not in payloads[2], "remembered"
    assert ctx.calls[-1].estimated and ctx.calls[-1].completion_tokens == 3


def test_stopped_answer_is_not_verified(engine, fake):
    def answer(msgs, tools):
        askctx.current().stop()     # Stop while the first draft is written
        return {"content": "tRFC is 295 ns [1]."}

    fake.answer = answer
    ctx = askctx.AskContext()
    events = []
    with askctx.active(ctx), pytest.raises(askctx.Cancelled):
        for ev in engine.ask_stream("DDR5 tRFC?"):
            events.append(ev)
    assert not [e for e in events if e["type"] == "final"]
    judged = [c for c in fake.calls if "strict fact checker" in json.dumps(c["body"].get("messages", [])[:1])]
    assert not judged, "nothing is verified after Stop"


def test_answer_reports_where_its_time_went(engine):
    ctx = askctx.AskContext()
    with askctx.active(ctx):
        fin = [e for e in engine.ask_stream("DDR5 tRFC değeri nedir?") if e["type"] == "final"][0]
    tm = fin["timings"]
    for k in ("plan", "generate", "verify", "locate", "total", "model_calls"):
        assert k in tm, k
    assert tm["model_calls"] >= 2 and tm["tool_calls"] >= 1 and tm["total"] >= tm["generate"]
    calls = fin["calls"]
    assert {"plan", "answer", "judge"} <= set(calls), calls
    assert calls["answer"]["calls"] == tm["model_calls"] and calls["answer"]["completion_tokens"] > 0
    assert fin["thinking"] in (True, False)


def test_regeneration_does_not_think(engine, fake, cfg):
    cfg.llm.thinking = "on"

    def stubborn(msgs, tools):
        return {"content": "tRFC is 295 ns [1]. tRFC shall never exceed 999 ns [1]."}

    fake.answer = stubborn
    list(engine.ask_stream("DDR5 tRFC?"))
    answers = [c["body"] for c in fake.calls if "ANSWER CONTRACT" in json.dumps(c["body"].get("messages", [])[:1])]
    assert len(answers) >= 2, "a first draft and a regeneration"
    assert answers[0]["chat_template_kwargs"]["enable_thinking"] is True
    assert answers[-1]["chat_template_kwargs"]["enable_thinking"] is False, "the focused rewrite does not think"


# ------------------------------------------------------------------------------------------- server
@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    app = create_app(cfg, engine, warmup=False, desktop=True)
    seen = []
    app.state.on_answer = seen.append
    with TestClient(app) as c:
        c.answers = seen
        yield c


def _events(r):
    return [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]


def test_finished_answer_signals_the_desktop(client):
    with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC değeri nedir?"}) as r:
        events = _events(r)
    assert events[0]["type"] == "started" and events[-1]["type"] == "final"
    for _ in range(50):
        if client.answers:
            break
        time.sleep(0.05)
    assert client.answers == [{"kind": "final", "question": "DDR5 tRFC değeri nedir?", "status": "supported"}]


def test_stop_endpoint_stops_the_answer(client, fake):
    entered, release = threading.Event(), threading.Event()

    def slow(msgs, tools):
        entered.set()
        ctx = askctx.current()
        for _ in range(250):          # the model is busy until the answer is stopped
            if ctx is not None and ctx.stopped:
                break
            time.sleep(0.02)
        release.set()
        return {"content": "tRFC is 295 ns [1]."}

    fake.answer = slow
    stopped = {}

    def press_stop():                 # the user, while the answer is being written
        assert entered.wait(10)
        ask_id = next(iter(client.app.state.asks))
        stopped["id"] = ask_id
        stopped["reply"] = client.post(f"/api/ask/{ask_id}/stop").json()

    th = threading.Thread(target=press_stop)
    th.start()
    with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC?"}) as r:
        events = _events(r)
    th.join(10)
    assert stopped["reply"] == {"ok": True} and release.is_set()
    kinds = [e["type"] for e in events]
    assert kinds[0] == "started" and events[0]["id"] == stopped["id"]
    assert kinds[-1] == "cancelled" and "final" not in kinds
    time.sleep(0.2)
    assert client.answers == [], "a stopped answer is not announced"
    assert client.post(f"/api/ask/{stopped['id']}/stop").json()["ok"] is False, "no longer running"


def test_stop_while_the_reader_is_blocked_is_a_stop_not_an_error():
    """The user stops while the model has not sent the next token yet: the response is closed under the blocked
    reader; whatever error that read raises, the question is reported as stopped."""
    ctx = askctx.AskContext()

    def body():
        yield _sse([{"choices": [{"delta": {"content": "first "}}]}], done=False).encode()
        ctx.stop()                       # stop arrives while the server is still working ...
        raise httpx.StreamClosed()       # ... and the closed stream fails the pending read

    llm = _client(lambda req: httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"}))
    with askctx.active(ctx), pytest.raises(askctx.Cancelled):
        list(llm.stream([{"role": "user", "content": "q"}]))
    assert ctx.calls and not ctx.calls[0].ok
