"""Paragraph answers: the format changes, the verification does not (fake model server)."""

import re

from conftest import answer_calls, judge_calls
from fakeserver import sources_in
from techrag.engine import cite_claims
from techrag.prompts import answer_system_prompt, headings
from techrag.verify import parse_answer


def final(engine, question, **kw):
    events = list(engine.ask_stream(question, **kw))
    return next(e for e in events if e["type"] == "final")


def _src(msgs, needle):
    src = sources_in(msgs)
    return next(n for n in sorted(src) if needle in src[n])


def test_answer_contract_asks_for_paragraphs_with_per_claim_citations():
    for lang in ("tr", "en"):
        p = answer_system_prompt(lang)
        assert "short paragraphs" in p and "never one citation at the end of a paragraph" in p
        assert "only for sequential steps, independent conditions, alternatives/options or a checklist" in p
        assert "When the user asks for a format" in p and "no fixed number" in p
        assert f'"### {headings(lang)[1]}"' in p, "documented facts and engineering inference stay apart"
        assert "{" not in p.replace("{n}", ""), "no placeholder left"
        assert headings(lang)[0] not in p, "no mandatory 'documented' heading: the answer is plain paragraphs"


def test_paragraph_claims_are_verified_one_by_one(engine, fake):
    def answer(msgs, tools):
        n = _src(msgs, "295")
        return {"content": (
            f"For an 8Gb DDR5 device tRFC is 295 ns [{n}].\n\n"
            f"The controller issues a REFRESH command every tREFI = 7.8 us [{n}]. The device wakes up after 999 ns "
            f"[{n}]. The refresh cycle time applies to 8Gb devices [{n}].\n\n"
            f"### Engineering inference (not stated in the sources)\n"
            f"Assumption: the scheduler leaves the 295 ns tRFC window free after each REFRESH [{n}].")}

    fake.answer = answer
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    text = fin["answer"]
    assert "999 ns" not in text, "an unsupported sentence inside a paragraph is removed"
    assert "tREFI = 7.8 us" in text and "applies to 8Gb devices" in text, "the rest of the paragraph stays"
    details = fin["verification"]["details"]
    assert len(details) == 5 and sum(d["outcome"] == "removed" for d in details) == 1
    inf = [d for d in details if d["section"] == "inference"]
    assert len(inf) == 1 and inf[0]["kind"] == "inference" and inf[0]["status"] == "supported"
    # each citation button belongs to the sentence it stands in
    buttons = re.findall(r"\[\d+\]", text)
    assert len(fin["cite_claims"]) == len(buttons) and None not in fin["cite_claims"]
    kept = {d["id"]: d["text"] for d in details if d["outcome"] == "kept"}
    assert [kept[c] for c in fin["cite_claims"]] == [d["text"] for d in details if d["outcome"] == "kept"]


def test_one_citation_at_the_end_does_not_cover_a_whole_paragraph(engine, fake):
    def answer(msgs, tools):
        n = _src(msgs, "295")
        return {"content": f"tRFC is 295 ns. tREFI is 7.8 us. Both values come from the refresh section [{n}]."}

    fake.answer = answer
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    removed = [d for d in fin["verification"]["details"] if d["outcome"] == "removed"]
    assert {d["text"] for d in removed} >= {"tRFC is 295 ns.", "tREFI is 7.8 us."}
    assert all(any("no citation" in r for r in d["reasons"]) for d in removed if "ns." in d["text"] or "us." in d["text"])


def test_paragraph_format_needs_no_extra_model_call(engine, fake):
    def answer(msgs, tools):
        n = _src(msgs, "295")
        return {"content": f"tRFC is 295 ns for an 8Gb device [{n}].\n\nThe controller issues a REFRESH command "
                           f"every tREFI = 7.8 us [{n}]."}

    fake.answer = answer
    before = len(fake.calls)
    fin = final(engine, "DDR5 tRFC değeri nedir?")
    assert fin["verification"]["status"] == "supported"
    chats = [c for c in fake.calls[before:] if c["path"].endswith("/chat/completions")]
    roles = []
    for c in chats:
        system = str(c["body"]["messages"][0]["content"])
        roles.append("plan" if "retrieval plan" in system else "answer" if "ANSWER CONTRACT" in system
                     else "judge" if "strict fact checker" in system else "other")
    assert roles.count("answer") == 1 and "other" not in roles, roles
    assert fin["answer"].count("\n\n") == 1, "the verified paragraphs are kept as written, not rewritten"
    assert len(answer_calls(fake)) >= 1 and len(judge_calls(fake)) >= 1


def test_render_keeps_paragraphs_lists_and_tables_as_written():
    ans = ("Direct answer is 1 V [1].\n\nFirst point is 2 V [1]. Second point is 3 V [2].\n\n"
           "1. Step one sets 4 V [1].\n2. Step two sets 5 V [1].\n\n| P | V |\n|---|---|\n| x | 6 V [1] |")
    parsed = parse_answer(ans)
    assert [c.text for c in parsed.claims][:3] == ["Direct answer is 1 V [1].", "First point is 2 V [1].",
                                                   "Second point is 3 V [2]."]
    from techrag.verify import render

    assert render(parsed, lambda c: "keep") == ans
    out = render(parsed, lambda c: "drop" if "3 V" in c.text else "keep")
    assert "First point is 2 V [1]." in out and "Second point" not in out and "| x | 6 V [1] |" in out
    assert cite_claims(out, []) == [None] * len(re.findall(r"\[\d+\]", out))
