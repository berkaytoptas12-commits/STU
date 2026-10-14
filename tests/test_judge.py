"""The independent judge with real-world limits: requests sized to the model's context, a failed request split
into single-claim requests, context-length errors and cut-off replies (fake model server)."""

import json
import re

import httpx

from conftest import judge_calls
from fakeserver import sources_in
from techrag.engine import JUDGE_REPLY_BASE, JUDGE_REPLY_PER_CLAIM
from techrag.prompts import JUDGE_SYSTEM
from techrag.retrieval import Passage
from techrag.tools import SourceRegistry
from techrag.verify import parse_answer


def final(engine, question, **kw):
    return next(e for e in engine.ask_stream(question, **kw) if e["type"] == "final")


def _claims_in(msgs) -> int:
    return len(re.findall(r"^CLAIM \d+:", msgs[-1]["content"], re.M))


def _many_claims_answer(n_claims=6):
    def answer(msgs, tools):
        src = sources_in(msgs)
        n = next(k for k in sorted(src) if "295" in src[k])
        lines = [f"For an 8Gb DDR5 device tRFC is 295 ns [{n}]."]
        words = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"]
        lines += [f"The {words[i]} reading: the DDR5 refresh cycle time is 295 ns [{n}]." for i in range(n_claims - 1)]
        return {"content": " ".join(lines)}
    return answer


def test_requests_fit_the_model_context(engine, cfg):
    cfg.llm.context_tokens = 3000
    reg = SourceRegistry()
    long_text = "\n".join(f"Line {i}: the refresh cycle time tRFC of the 8Gb device is 295 ns under condition {i}."
                          for i in range(400))
    for k in range(3):
        reg.add_passage(Passage(1, "JESD79-5", "d.pdf", "ddr", "3.2", 6, 6, "text", long_text + f"\nsrc {k}", 1.0,
                                [k], [k]))
    claims = parse_answer(" ".join(f"tRFC is 295 ns in case {i} [1][2][3]." for i in range(8))).claims
    batches = engine._judge_batches(claims, reg, 8, 6000)
    assert sum(len(b) for b, _ in batches) == 8 and len(batches) > 1
    for batch, items in batches:
        from techrag.prompts import judge_user_prompt

        prompt = engine._tokens(JUDGE_SYSTEM) + engine._tokens(judge_user_prompt(items))
        assert prompt + JUDGE_REPLY_BASE + JUDGE_REPLY_PER_CLAIM * len(batch) <= 3000 + 60, (prompt, len(batch))


def test_a_failed_request_is_retried_claim_by_claim(engine, fake):
    fake.answer = _many_claims_answer(6)
    real = fake._judge

    def judge(msgs):
        if _claims_in(msgs) > 1:
            return httpx.Response(500, json={"error": "overloaded"})
        return real(msgs)
    fake.judge = judge
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["status"] == "supported" and v["judge"]["completed"], v["judge"]
    assert v["supported"] == 6 and "295" in fin["answer"]
    assert sum(1 for c in judge_calls(fake) if _claims_in(c["body"]["messages"]) == 1) >= 6


def test_context_length_error_retries_with_shorter_excerpts(engine, fake):
    fake.answer = _many_claims_answer(3)
    real = fake._judge
    sizes = []

    def judge(msgs):
        n = len(msgs[-1]["content"])
        sizes.append(n)
        if n > 1200:
            return httpx.Response(400, json={"error": {"message": "This model's maximum context length is 2048 tokens"}})
        return real(msgs)
    fake.judge = judge
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["judge"]["completed"] and v["status"] == "supported", (v["judge"], sizes)
    assert min(sizes) <= 1200 < max(sizes), "the last round sends shorter excerpts"


def test_a_cut_off_reply_is_not_a_verdict(engine, fake):
    fake.answer = _many_claims_answer(2)
    real = fake._judge
    state = {"n": 0}

    def judge(msgs):
        state["n"] += 1
        if state["n"] == 1:   # the JSON stops in the middle: finish_reason "length"
            events = [{"choices": [{"delta": {"content": '{"verdicts": [{"id": 1, "verdict": "supp'}}]},
                      {"choices": [{"delta": {}, "finish_reason": "length"}]}]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return real(msgs)
    fake.judge = judge
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["status"] == "supported" and any("cut off" in e for e in v["judge"]["errors"])


def test_server_context_length_is_used_when_smaller(engine, fake, cfg, monkeypatch):
    cfg.llm.context_tokens = 16000
    monkeypatch.setattr(engine.llm.api, "model_entries", lambda: [{"id": "qwen-test", "max_model_len": 8192}])
    engine.llm._context_probed = False
    assert engine.context_tokens() == 8192
    monkeypatch.setattr(engine.llm.api, "model_entries", lambda: [{"id": "qwen-test", "max_model_len": 65536}])
    engine.llm._context_probed = False
    assert engine.context_tokens() == 16000, "a larger server context does not grow the prompts past the setting"
    monkeypatch.setattr(engine.llm.api, "model_entries", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    engine.llm._context_probed = False
    assert engine.context_tokens() == 16000
