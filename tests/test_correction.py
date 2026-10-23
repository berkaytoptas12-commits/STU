"""Targeted correction: only the statements that failed verification are rewritten (or removed); every other line
of the answer stays exactly as written, and the revised answer is verified again like the draft."""

import json
import re

from fakeserver import _text, sources_in
from techrag.verify import parse_answer, render


def _correction_calls(fake):
    return [c for c in fake.calls if c["path"].endswith("/chat/completions")
            and "CORRECTION REQUEST" in _text(c["body"]["messages"][-1]["content"])]


def test_only_the_failed_statement_is_rewritten(engine, fake):
    def answer(msgs, tools):
        n = min(k for k, v in sources_in(msgs).items() if "295" in v)
        last = _text(msgs[-1]["content"])
        if "CORRECTION REQUEST" in last:
            failed = re.findall(r'^- id (\d+): "(.*)" ->', last, re.M)
            return {"content": json.dumps({"fixes": [
                {"id": int(i), "text": f"The refresh cycle time tRFC for an 8Gb device is 295 ns [{n}]."}
                for i, _ in failed]})}
        return {"content": f"tRFC for an 8Gb device is 295 ns [{n}].\n\n### Documented\n"
                           f"- The refresh cycle time tRFC for an 8Gb device is 123 ns [{n}].\n"
                           f"- Refresh commands are issued for 8Gb devices [{n}]."}

    fake.answer = answer
    fin = [e for e in engine.ask_stream("DDR5 tRFC değeri nedir?") if e["type"] == "final"][0]
    v = fin["verification"]
    assert v["regenerated"] and v["status"] == "supported" and "123" not in fin["answer"]
    lines = fin["answer"].split("\n")
    assert lines[0].startswith("tRFC for an 8Gb device is 295 ns"), "untouched"
    assert any(ln.startswith("- The refresh cycle time tRFC for an 8Gb device is 295 ns") for ln in lines)
    assert any(ln.startswith("- Refresh commands are issued for 8Gb devices") for ln in lines), "untouched"

    calls = _correction_calls(fake)
    assert len(calls) == 1
    body = calls[0]["body"]
    request = _text(body["messages"][-1]["content"])
    assert "123 ns" in request and "Refresh commands" not in request, "only the failed statement is sent"
    assert body["max_tokens"] == 550 and body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["messages"][-2]["role"] == "assistant" and "123 ns" in body["messages"][-2]["content"], "the draft"
    first = next(c for c in fake.calls if "ANSWER CONTRACT" in json.dumps(c["body"].get("messages", [])[:1]))
    assert body["messages"][:-2] == first["body"]["messages"][:len(body["messages"]) - 2], \
        "the same prefix as the answer itself: the server can reuse what it already processed"
    i = fake.calls.index(calls[0])
    rejudged = [_text(c["body"]["messages"][-1]["content"]) for c in fake.calls[i + 1:]
                if "strict fact checker" in json.dumps(c["body"]["messages"][:1])]
    assert rejudged and all(t.count("CLAIM ") == 1 and "295 ns" in t for t in rejudged), \
        "only the corrected statement is judged again"


def test_unusable_correction_leaves_the_failed_statement_to_be_removed(engine, fake):
    def answer(msgs, tools):
        n = min(sources_in(msgs))
        if "CORRECTION REQUEST" in _text(msgs[-1]["content"]):
            return {"content": "Sorry, I rewrote everything instead."}   # no JSON
        return {"content": f"tRFC is 295 ns [{n}].\n\n### Documented\n- tRFC is 295 ns [{n}].\n"
                           f"- tRFC shall never exceed 999 ns [{n}]."}

    fake.answer = answer
    fin = [e for e in engine.ask_stream("DDR5 tRFC?") if e["type"] == "final"][0]
    v = fin["verification"]
    assert not v["regenerated"] and v["status"] == "corrected" and "999" not in fin["answer"]


def test_render_replaces_removes_and_keeps_table_rows():
    p = parse_answer("Lead 295 ns [1].\n\n| Param | Value |\n|---|---|\n| tRFC | 123 ns [1] |\n| tREFI | 3.9 us [1] |\n\n"
                     "### Documented\n- First 1 ns [1]. Second 2 ns [1].")
    ids = {c.text: c.id for c in p.claims}
    row = ids["| tRFC | 123 ns [1] |"]
    keep = lambda c: "keep"   # noqa: E731
    out = render(p, keep, {row: "| tRFC | 295 ns [1] |", ids["Second 2 ns [1]."]: ""})
    assert "| tRFC | 295 ns [1] |" in out and "123" not in out and "Second" not in out and "First 1 ns [1]." in out
    assert render(p, keep, {row: "tRFC is 295 ns [1]."}) == render(p, keep), "a row is only replaced by a row"
    out = render(p, keep, {row: ""})
    assert "tRFC" not in out.split("### Documented")[0] and "| tREFI | 3.9 us [1] |" in out
    assert render(p, keep, {ids["Lead 295 ns [1]."]: "New lead\nover two lines [1]."}).startswith(
        "New lead over two lines [1]."), "one line"


def test_correction_that_changes_nothing_is_not_reported_as_a_correction(engine, fake):
    def answer(msgs, tools):
        n = min(sources_in(msgs))
        last = _text(msgs[-1]["content"])
        if "CORRECTION REQUEST" in last:
            ids = re.findall(r"^- id (\d+):", last, re.M)
            return {"content": json.dumps({"fixes": {i: "not a table row" for i in ids}})}   # dict form
        return {"content": f"tRFC is 295 ns [{n}].\n\n| Param | Value |\n|---|---|\n| tRFC max | 999 ns [{n}] |"}

    fake.answer = answer
    fin = [e for e in engine.ask_stream("DDR5 tRFC?") if e["type"] == "final"][0]
    v = fin["verification"]
    assert not v["regenerated"] and "999" not in fin["answer"] and len(_correction_calls(fake)) == 1
