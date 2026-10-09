"""Verification must never approve what it could not check (fake model server; this does not measure the
accuracy of a real model)."""

import json

import httpx
import pytest

from conftest import judge_calls
from fakeserver import sources_in
from techrag.retrieval import Passage
from techrag.tools import SourceRegistry
from techrag.verify import (NOT_CHECKED, SUPPORTED, UNSUPPORTED, UNVERIFIED, deterministic_check, is_framing,
                            parse_answer, parse_verdicts, split_claims)


def final(engine, question, **kw):
    events = list(engine.ask_stream(question, **kw))
    return events, next(e for e in events if e["type"] == "final")


def _registry(*texts, question=None):
    reg = SourceRegistry()
    for i, t in enumerate(texts):
        reg.add_passage(Passage(1, "Doc", "d.pdf", "ddr", "s", 1, 1, "text", t, 1.0, [i], [i]))
    if question:
        reg.add_user_input(question)
    return reg


# ------------------------------------------------------------------------------ judge failures

@pytest.mark.parametrize("judge", [
    lambda msgs: httpx.Response(500, json={"error": "boom"}),
    lambda msgs: (_ for _ in ()).throw(httpx.ReadTimeout("judge timed out")),
    lambda msgs: "",
    lambda msgs: "this is not json",
    lambda msgs: json.dumps({"verdicts": []}),
    lambda msgs: json.dumps({"result": "ok"}),
    lambda msgs: json.dumps({"verdicts": [{"id": 999, "verdict": "supported"}]}),
    lambda msgs: json.dumps({"verdicts": [{"id": i, "verdict": "probably"} for i in range(1, 20)]}),
    lambda msgs: json.dumps({"verdicts": [{"id": i, "verdict": v} for i in range(1, 20) for v in ("supported", "supported")]}),
    lambda msgs: json.dumps({"verdicts": [{"verdict": "supported"} for _ in range(20)]}),
], ids=["http500", "timeout", "empty", "not-json", "no-verdicts", "wrong-schema", "unknown-id", "unknown-verdict",
        "duplicate-id", "missing-id"])
def test_judge_failure_never_approves(engine, fake, judge):
    fake.judge = judge
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["judge"]["called"] is True and v["judge"]["completed"] is False
    assert v["supported"] == 0 and v["status"] == "unverified"
    assert "295" not in fin["answer"], "unverified statements must not be shown as the answer"
    assert all(c["status"] in (UNVERIFIED, UNSUPPORTED) for c in v["details"])
    assert any(c["judge"]["error"] for c in v["details"])


def test_judge_gets_one_retry_for_missing_verdicts(engine, fake):
    state = {"n": 0}
    real = fake._judge

    def flaky(msgs):
        state["n"] += 1
        return "" if state["n"] == 1 else real(msgs)
    fake.judge = flaky
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["status"] == "supported" and v["judge"]["completed"], "valid verdicts on the retry are accepted"
    assert len(judge_calls(fake)) >= 2


def test_parse_verdicts_is_strict():
    ok = parse_verdicts({"verdicts": [{"id": "1", "verdict": "Supported"}, {"id": 2, "verdict": "partial", "reason": "x"}]}, [1, 2, 3])
    assert ok[1][0] == "supported" and ok[2] == ("partial", "x") and ok[3][0] == "missing"
    assert parse_verdicts(None, [1])[1][0] == "invalid"
    assert parse_verdicts({"verdicts": [{"id": True, "verdict": "supported"}]}, [1])[1][0] == "missing"
    dup = parse_verdicts({"verdicts": [{"id": 1, "verdict": "supported"}, {"id": 1, "verdict": "unsupported"}]}, [1])
    assert dup[1][0] == "invalid"


def test_judge_switched_off_is_not_reported_as_verified(engine, fake, cfg):
    cfg.answer.judge = False
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["status"] == "not_checked" and v["judge"]["enabled"] is False and v["judge"]["called"] is False
    assert all(c["status"] == NOT_CHECKED for c in v["details"] if c["outcome"] == "kept")
    assert not judge_calls(fake)


def test_verification_switched_off(engine, fake, cfg):
    cfg.answer.verify = False
    _, fin = final(engine, "DDR5 tRFC değeri nedir?")
    assert fin["verification"]["status"] == "not_checked" and fin["verification"]["deterministic"]["enabled"] is False


# ------------------------------------------------------------------ question is not evidence

def test_number_from_the_question_is_not_evidence():
    reg = _registry("The limit tLIM shall be 100 ns.", question="Sınır 999 ns mi?")
    user = reg.items[-1].n
    claims = split_claims(f"Sınır 999 ns [1].\nSınır 999 ns [1][{user}].\nVerdiğiniz 999 ns değeri sınırdan büyüktür [1][{user}].")
    deterministic_check(claims, reg)
    assert claims[0].status == UNSUPPORTED and "not found in the cited sources" in claims[0].reasons[0]
    assert claims[1].status == UNSUPPORTED and "presented as documented" in " ".join(claims[1].reasons)
    assert claims[2].status == "pending", "a clearly attributed user value may be used"
    assert claims[2].uses_user_input and {v["from"] for v in claims[2].values} == {"user", "document"} or \
        any(v["from"] == "user" for v in claims[2].values)


def test_question_value_end_to_end(engine, fake):
    def echo(msgs, tools):
        n = min(k for k, t in sources_in(msgs).items() if "tRFC" in t)
        return {"content": f"Evet, sınır 999 ns [{n}].\n\n### Kaynaklarda belirtilen\n- tRFC sınırı 999 ns [{n}]."}
    fake.answer = echo
    _, fin = final(engine, "DDR5 tRFC sınırı 999 ns mi?")
    assert "999" not in fin["answer"]
    assert fin["verification"]["status"] in ("not_found", "corrected")
    assert any(s["kind"] == "user" for s in fin["sources"])


def test_calculation_with_untraceable_input_is_not_verified(engine, fake):
    def calc(msgs, tools):
        if tools and not any(m.get("role") == "tool" for m in msgs):
            return {"tool_calls": [{"name": "calculate", "arguments": {"expression": "295e-9*1.37e9", "label": "cycles"}}]}
        n = max(sources_in(msgs))
        return {"content": f"tRFC is about 404 clock cycles [{n}]."}
    fake.answer = calc
    events, fin = final(engine, "DDR5 tRFC kaç saat çevrimi?")
    calc_src = next(s for s in fin["sources"] if s["kind"] == "calc")
    assert calc_src["extra"]["traceable"] is False
    assert [i["value"] for i in calc_src["extra"]["inputs"] if not i["sources"]] == ["1.37e9"]
    assert "404" not in fin["answer"]


def test_calculation_inputs_are_traced_to_sources(engine, fake):
    def calc(msgs, tools):
        if tools and not any(m.get("role") == "tool" for m in msgs):
            return {"tool_calls": [{"name": "calculate", "arguments": {"expression": "295e-9*1e9", "label": "ns"}}]}
        src = sources_in(msgs)
        n = max(src)
        doc = min(k for k, t in src.items() if "295" in t and "Calculation" not in t)
        return {"content": f"tRFC equals 295 ns [{doc}], i.e. 295 in nanoseconds [{n}]."}
    fake.answer = calc
    _, fin = final(engine, "DDR5 tRFC kaç nanosaniye?")
    calc_src = next(s for s in fin["sources"] if s["kind"] == "calc")
    assert calc_src["extra"]["traceable"] is True and calc_src["extra"]["inputs"][0]["sources"]
    kept = [c for c in fin["verification"]["details"] if c["outcome"] == "kept"]
    rec = next(e for c in kept for e in c["evidence"] if e["kind"] == "calc")
    assert rec["status"] == "not_applicable" and rec["inputs"][0]["locations"], "inputs point to their sources"


# --------------------------------------------------------------- claims cannot dodge the check

def test_uncited_lead_and_heading_placement_do_not_skip_checks():
    reg = _registry("DDR5 devices support on-die ECC. tRFC is 295 ns.")
    ans = ("DDR5 tRFC 999 ns'dir.\nDDR5 devices support on-die ECC.\n\n### Documented\n- tRFC is 295 ns [1].\n\n"
           "### Engineering inference (not stated in the sources)\n- Use a longer refresh window.")
    claims = split_claims(ans)
    deterministic_check(claims, reg)
    st = {c.text: c for c in claims}
    assert st["DDR5 tRFC 999 ns'dir."].status == UNSUPPORTED
    assert st["DDR5 devices support on-die ECC."].status == UNSUPPORTED, "non-numeric technical claims need a citation"
    assert "no citation" in st["Use a longer refresh window."].reasons[0], "inference must cite its premises"
    assert st["tRFC is 295 ns [1]."].status == "pending"


def test_framing_vs_technical():
    assert is_framing("Merhaba!") and is_framing("Detaylar aşağıda:") and is_framing("### Kaynaklarda belirtilen")
    assert is_framing("Bu bilgi yüklenen dokümanlarda bulunamadı.")
    assert not is_framing("DDR5 devices support on-die ECC.")
    assert not is_framing("tRFC 295 ns.")


def test_answer_without_checkable_statements_is_not_a_success(engine, fake):
    fake.answer = lambda msgs, tools: {"content": "Here you go:\n\n### Documented"}
    _, fin = final(engine, "DDR5 tRFC?")
    v = fin["verification"]
    assert v["status"] == "unverified" and v["parse_failed"] and v["supported"] == 0


def test_answer_is_rebuilt_from_verified_claims_only(engine, fake):
    def mixed(msgs, tools):
        n = min(k for k, t in sources_in(msgs).items() if "295" in t)
        return {"content": f"tRFC is 295 ns [{n}]. It is 999 ns in turbo mode [{n}].\n\n### Documented\n"
                           f"- tRFC is 295 ns [{n}].\n- DDR5 has no refresh at all [{n}]."}
    fake.answer = mixed
    fake.judge = lambda msgs: json.dumps({"verdicts": [
        {"id": int(b.split(":")[0]), "verdict": "unsupported" if "no refresh" in b else "supported"}
        for b in msgs[-1]["content"].split("CLAIM ")[1:]]})
    _, fin = final(engine, "DDR5 tRFC?")
    assert "999" not in fin["answer"] and "no refresh" not in fin["answer"]
    assert "295 ns" in fin["answer"] and fin["verification"]["status"] == "corrected"
    removed = [c for c in fin["verification"]["details"] if c["outcome"] == "removed"]
    assert {c["status"] for c in removed} == {UNSUPPORTED}


def test_parse_answer_keeps_structure():
    p = parse_answer("Lead [1].\n\n```\ncode line\n```\n| A | B |\n|---|---|\n| x | 1 V [1] |")
    kinds = [ln.kind for ln in p.lines]
    assert kinds == ["text", "blank", "fence", "code", "fence", "table_head", "table_sep", "table_row"]
    assert SUPPORTED == "supported"


def test_values_only_in_a_page_image_transcription_are_unverified():
    reg = SourceRegistry()
    reg.add_page(1, 7, "Doc", "(VLM transcription) Figure 3-2: tRFC window 412 ns\n\nPage text (text layer):\nFigure 3-2 Refresh timing",
                 {}, evidence_text="Figure 3-2 Refresh timing")
    claims = split_claims("The refresh window is 412 ns [1].")
    deterministic_check(claims, reg)
    assert claims[0].status == UNVERIFIED and "page-image transcription" in claims[0].reasons[0]
