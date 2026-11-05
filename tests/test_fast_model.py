"""Two chat models: "Kapsamlı" (the chat model) and "Hızlı" (llm_fast), chosen per question in the chat. With "Hızlı"
everything the chat model does for that question - planning it, the answer, the judge, citation repair - is done by
the fast model; search, reranking and the vision model stay the same, and so do the verification rules."""

import csv
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from techrag import org
from techrag.config import Config
from techrag.llm import LLMClient
from techrag.server import create_app

QUESTION = "DDR5 tRFC değeri nedir?"


def _image(body) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in body.get("messages", []))


def _chat_models(fake, since=0):
    """The models the chat requests went to (page images read by the vision model are not chat requests)."""
    return [c["body"].get("model") for c in fake.calls[since:]
            if c["path"].endswith("/chat/completions") and not _image(c["body"])]


def _vision_models(fake, since=0):
    return [c["body"].get("model") for c in fake.calls[since:]
            if c["path"].endswith("/chat/completions") and _image(c["body"])]


@pytest.fixture()
def fast(cfg, engine, fake):
    """A fast model on the chat model's server (the fake server answers any model name)."""
    cfg.llm_fast.model = "fast-test"
    engine._fast_llm = LLMClient(cfg.chat_config("fast"), transport=fake.transport())
    return engine


def test_the_fast_model_settings():
    c = Config()
    assert c.llm.thinking == "on" and c.llm_fast.thinking == "on", "both models think by default"
    c.llm.base_url, c.llm.model, c.llm.thinking, c.llm.thinking_control = "http://gpu01:8000/v1", "big", "auto", \
        "reasoning_effort"
    assert c.chat_config("fast") is c.llm, "no fast model set: the chat model answers"
    c.llm_fast.model = "small"
    f = c.chat_config("fast")
    assert (f.base_url, f.model, f.thinking, f.thinking_control) == ("http://gpu01:8000/v1", "small", "on",
                                                                     "reasoning_effort")
    assert f.tools == c.llm.tools and f.context_tokens == c.llm.context_tokens, "the other chat settings are shared"
    c.llm_fast.base_url, c.llm_fast.thinking_control, c.llm_fast.verify_ssl = "http://gpu02:8000/v1", "none", False
    f = c.chat_config("fast")
    assert (f.base_url, f.thinking_control, f.verify_ssl) == ("http://gpu02:8000/v1", "none", False)
    assert c.chat_config("deep") is c.llm and c.llm.model == "big", "the chat model itself is not changed"


def test_a_fast_question_uses_the_fast_model_everywhere(fast, fake, cfg):
    assert fast.for_mode("deep") is fast and fast.for_mode("") is fast
    view = fast.for_mode("fast")
    assert view is not fast and view.store is fast.store and view.reranker is fast.reranker
    n = len(fake.calls)
    res = view.ask(QUESTION)
    assert "295" in res["answer"] and res["model"] == {"mode": "fast", "name": "fast-test"}
    models = _chat_models(fake, n)
    assert models and set(models) == {"fast-test"}, "planning, answer and judge: all with the fast model"
    # the page image goes to the vision model ("same as the chat model" = the thorough one), not to the fast model,
    # which may not read images
    assert set(_vision_models(fake, n)) <= {"qwen-test"}
    assert res["verification"]["judge"]["called"], "the judge still checks the statements"
    # the engine itself still answers with the chat model
    assert fast.llm.model == "qwen-test" and fast.cfg.llm.model == "qwen-test"
    n = len(fake.calls)
    res = fast.ask(QUESTION)
    assert res["model"] == {"mode": "deep", "name": "qwen-test"} and set(_chat_models(fake, n)) == {"qwen-test"}


def test_the_fast_model_thinks_by_its_own_setting(fast, fake, cfg):
    cfg.llm.thinking, cfg.llm_fast.thinking = "on", "off"   # the thorough model thinks, the fast one does not
    n = len(fake.calls)
    fast.for_mode("fast").ask(QUESTION)
    answer_calls = [c["body"] for c in fake.calls[n:] if c["path"].endswith("/chat/completions")
                    and c["body"].get("stream") and not c["body"].get("response_format")]
    assert answer_calls and all(not (b.get("chat_template_kwargs") or {}).get("enable_thinking")
                                for b in answer_calls)
    n = len(fake.calls)
    fast.ask(QUESTION)
    assert any((c["body"].get("chat_template_kwargs") or {}).get("enable_thinking")
               for c in fake.calls[n:] if c["path"].endswith("/chat/completions"))


# ---------------------------------------------------------------------------------------------- API

@pytest.fixture()
def client(cfg, fast, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, fast, warmup=False, desktop=True)) as c:
        yield c


def test_the_chat_chooses_per_question(client, fake, cfg, tmp_path):
    assert client.get("/api/info").json()["models"]["llm_fast"] == "fast-test"
    cfg.usage.log_dir = str(tmp_path / "gunluk")
    n = len(fake.calls)
    with client.stream("POST", "/api/ask", json={"question": QUESTION, "mode": "fast"}) as r:
        events = [json.loads(x[5:]) for x in r.iter_lines() if x.startswith("data:")]
    final = events[-1]
    assert final["type"] == "final" and final["model"] == {"mode": "fast", "name": "fast-test"}
    assert set(_chat_models(fake, n)) == {"fast-test"}
    res = client.post("/api/ask", json={"question": QUESTION, "stream": False}).json()
    assert res["model"]["mode"] == "deep", "no choice: the thorough model"
    rows = []
    for f in sorted((tmp_path / "gunluk").glob("*.csv")):
        with open(f, encoding="utf-8-sig", newline="") as fh:
            rows += list(csv.DictReader(fh, delimiter=";"))
    assert [r["model"] for r in rows] == ["fast-test", "qwen-test"], "the usage log names the model that answered"


def test_an_evaluation_can_run_with_the_fast_model(client, tmp_path):
    items = tmp_path / "set.yaml"
    items.write_text("- id: t1\n  kind: value\n  question: DDR5 tRFC değeri nedir?\n"
                     "  facts: [{parameter: [tRFC], value: '295', unit: ns}]\n", encoding="utf-8")
    job = client.post("/api/eval/run", json={"set": "custom", "yaml": items.read_text(encoding="utf-8"),
                                             "name": "set.yaml", "mode": "fast"}).json()["job"]
    import time

    for _ in range(300):
        st = client.get(f"/api/jobs/{job}").json()
        if st["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    rep = client.get(f"/api/eval/reports/{st['report']['name']}").json()
    assert rep["model"] == {"mode": "fast", "name": "fast-test"} and st["report"]["model"]["mode"] == "fast"
    listed = client.get("/api/eval").json()["reports"]
    assert listed[0]["model"] == {"mode": "fast", "name": "fast-test"}


def test_the_fast_model_is_saved_and_shared_with_the_organization(cfg, tmp_path, monkeypatch):
    from techrag.settings import load_user_settings, public_settings, update_settings

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    c = update_settings(cfg, {"llm_fast": {"base_url": "", "model": "small", "thinking": "off"}})
    assert c.llm_fast.model == "small" and load_user_settings()["llm_fast"]["model"] == "small"
    assert public_settings(c)["llm_fast"]["model"] == "small" and "api_key" not in public_settings(c)["llm_fast"]
    share = tmp_path / "share"
    share.mkdir()
    org.save(share, c)
    data = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    assert data["llm_fast"]["model"] == "small" and "api_key" not in data["llm_fast"]
