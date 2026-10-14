"""Wrong-citation repair and structured removal reasons (fake model server)."""

from fakeserver import sources_in
from techrag.retrieval import Passage
from techrag.tools import SourceRegistry
from techrag.verify import UNSUPPORTED, deterministic_check, parse_answer, render, repair_citations


def final(engine, question, **kw):
    return next(e for e in engine.ask_stream(question, **kw) if e["type"] == "final")


def _registry(*texts, docs=None):
    reg = SourceRegistry()
    for i, t in enumerate(texts):
        doc = docs[i] if docs else i + 1
        reg.add_passage(Passage(doc, f"JESD79-{doc}", f"d{doc}.pdf", "ddr", "3.2", 6 + i, 6 + i, "text", t, 1.0,
                                [i], [i]))
    return reg


def _check(answer, reg):
    parsed = parse_answer(answer)
    deterministic_check(parsed.claims, reg)
    return parsed


def test_value_from_another_given_source_gets_its_citation_added():
    reg = _registry("The refresh command is issued periodically.", "tRFC for 8Gb devices is 295 ns.")
    parsed = _check("For an 8Gb device tRFC is 295 ns [1].", reg)
    c = parsed.claims[0]
    assert c.status == UNSUPPORTED and [k["code"] for k in c.codes] == ["value_not_found"]
    assert c.codes[0]["value"] == "295 ns" and c.codes[0]["elsewhere"] == [2]
    fixed = repair_citations(parsed, reg)
    assert fixed == [c] and c.text == "For an 8Gb device tRFC is 295 ns [1][2]."
    assert c.citations == [1, 2] and c.status == "pending", "checked again; the judge still decides"
    assert c.citation_fix == {"added": 2, "values": ["295 ns"]}
    assert render(parsed, lambda _: "keep") == "For an 8Gb device tRFC is 295 ns [1][2]."


def test_a_source_of_the_same_document_is_preferred():
    reg = _registry("Overview.", "tRFC is 295 ns.", "tRFC 8Gb: 295 ns (min).", docs=[7, 3, 7])
    parsed = _check("tRFC is 295 ns [1].", reg)
    repair_citations(parsed, reg)
    assert parsed.claims[0].citations == [1, 3]


def test_no_repair_when_something_else_is_wrong_or_no_source_has_the_value():
    reg = _registry("The refresh command is issued periodically.", "tRFC for 8Gb devices is 295 ns.")
    parsed = _check("tRFC is 999 ns [1]. tRFC is 295 ns. tRFC is 295 ns [1][9]. tRFC is 295 ns and tREFI is "
                    "7.8 us [1].", reg)
    assert repair_citations(parsed, reg) == []
    codes = [[k["code"] for k in c.codes] for c in parsed.claims]
    assert codes[0] == ["value_not_found"] and parsed.claims[0].codes[0]["elsewhere"] == []
    assert codes[1][0] == "no_citation", "an uncited statement is not given a citation"
    assert "invalid_citation" in codes[2]
    assert codes[3] == ["value_not_found", "value_not_found"], "7.8 us is in no source: not repaired"
    assert all(not c.citation_fix and "[2]" not in c.text for c in parsed.claims)


def _wrong_citation_answer(msgs, tools):
    src = sources_in(msgs)
    right = next(k for k in sorted(src) if "295" in src[k])
    wrong = next(k for k in sorted(src) if "295" not in src[k] and k != right)
    return {"content": f"For an 8Gb DDR5 device tRFC is 295 ns [{wrong}]."}


def test_engine_repairs_the_citation_and_the_judge_still_checks(engine, fake, cfg):
    cfg.answer.regenerate = False
    fake.answer = _wrong_citation_answer
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["status"] == "supported" and v["citation_fixes"] == 1, v
    d = v["details"][0]
    assert d["citation_fix"]["values"] == ["295 ns"] and d["judge"]["called"] and d["judge"]["verdict"] == "supported"
    assert f"[{d['citation_fix']['added']}]" in fin["answer"] and "295 ns" in fin["answer"]
    assert len(fin["cite_claims"]) == 2, "both citation buttons belong to the statement"


def test_engine_repaired_claim_can_still_be_rejected_by_the_judge(engine, fake, cfg):
    import json

    cfg.answer.regenerate = False
    fake.answer = _wrong_citation_answer
    fake.judge = lambda msgs: json.dumps({"verdicts": [{"id": 1, "verdict": "unsupported",
                                                         "reason": "different device density"}]})
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    d = fin["verification"]["details"][0]
    assert d["citation_fix"] and d["outcome"] == "removed" and "295" not in fin["answer"]
    assert [k["code"] for k in d["codes"]] == ["judge_unsupported"]
    assert d["codes"][0]["detail"] == "different device density"


def test_engine_repair_can_be_switched_off(engine, fake, cfg):
    cfg.answer.regenerate = False
    cfg.answer.repair_citations = False
    fake.answer = _wrong_citation_answer
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    v = fin["verification"]
    assert v["citation_fixes"] == 0 and v["details"][0]["outcome"] == "removed"
    assert v["details"][0]["codes"][0]["code"] == "value_not_found"


def test_judge_technical_failure_is_labelled_as_such(engine, fake, cfg):
    import httpx

    cfg.answer.regenerate = False
    fake.judge = lambda msgs: httpx.Response(500, json={"error": "down"})
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    d = fin["verification"]["details"][0]
    assert d["status"] == "unverified" and d["codes"][-1]["code"] == "judge_error"
