"""Clarifying questions for conditions that change the answer (synthetic documents + fake model server)."""

import json
import re

import pymupdf

from conftest import _draw_table, answer_calls
from techrag import clarify
from techrag.clarify import condition_kind, mentions, merge_reply


def run(engine, question, **kw):
    events = list(engine.ask_stream(question, **kw))
    return events, next(e for e in events if e["type"] == "final")


def plan_of(events):
    return next(e for e in events if e["type"] == "plan")


def pending_of(fin):
    assert fin["verification"]["status"] == "clarify", fin["answer"]
    return fin["clarify"]["pending"]


def test_general_question_is_answered_without_asking_back(engine, fake):
    for q in ("DDR arayüzü nedir?", "What does the refresh operation do?"):
        _, fin = run(engine, q)
        assert fin["verification"]["status"] != "clarify", q
    assert answer_calls(fake), "general questions go straight to the answer"


def test_missing_version_is_asked_with_loaded_options_only(engine, fake):
    events, fin = run(engine, "tRFC değeri nedir?")
    clar = fin["clarify"]
    assert [q["kind"] for q in clar["questions"]] == ["standard_version"]
    values = [o["value"] for o in clar["questions"][0]["options"]]
    assert values[:2] == ["DDR4", "DDR5"] and values[2] == "DDR4 vs DDR5", "only standards that are loaded"
    assert "DDR4" in clar["text"] and "DDR5" in clar["text"] and "bilmiyorum" in clar["text"]
    assert fin["sources"] == [] and fin["cite_claims"] == [] and not answer_calls(fake), \
        "the clarification is a fixed template: no model text, no sources, nothing to verify"
    assert not re.search(r"\d{3}", clar["text"]), "no value from the documents leaks into the question"
    assert fin["verification"]["claims"] == 0


def test_short_reply_is_merged_into_the_pending_question(engine, fake):
    _, fin = run(engine, "tRFC değeri nedir?")
    pending = pending_of(fin)
    events, fin = run(engine, "DDR4", clarification=pending)
    p = plan_of(events)
    assert p["merged_question"] == "tRFC değeri nedir? (DDR4)" == fin["merged_question"]
    assert p["scope"]["reason"] == "entity" and p["scope"]["entities"] == ["DDR4"]
    assert fin["verification"]["status"] != "clarify" and "350" in fin["answer"] and "295" not in fin["answer"]


def test_reply_with_several_conditions_is_merged(engine):
    _, fin = run(engine, "tRFC değeri nedir?")
    events, fin = run(engine, "DDR5, 8Gb", clarification=pending_of(fin))
    assert plan_of(events)["scope"]["entities"] == ["DDR5"] and "295" in fin["answer"]


def test_new_topic_drops_the_pending_question(engine):
    _, fin = run(engine, "tRFC değeri nedir?")
    events, fin = run(engine, "I2C Fast-mode hızı nedir?", clarification=pending_of(fin))
    assert plan_of(events)["merged_question"] is None and fin.get("merged_question") is None
    assert "tRFC" not in plan_of(events)["plan"]["question"]


def test_dont_know_gives_a_conditional_answer_not_another_question(engine, fake):
    _, fin = run(engine, "tRFC değeri nedir?")
    events, fin = run(engine, "bilmiyorum", clarification=pending_of(fin))
    p = plan_of(events)
    assert p["unknown_conditions"] == ["standard_version"]
    assert set(p["scope"]["entities"]) == {"DDR4", "DDR5"}, "both loaded versions, each with its own evidence"
    assert fin["verification"]["status"] != "clarify"
    assert fin["notice"].startswith("Not:") and "standart/sürüm" in fin["notice"]
    prompt = json.dumps(answer_calls(fake)[-1]["body"]["messages"], ensure_ascii=False)
    assert "CONDITIONS NOT KNOWN" in prompt and "yes/no compatibility verdict" in prompt
    assert fin["verification"]["claims"] > 0, "the conditional answer is verified like any other"


def test_selected_bucket_is_not_a_version_choice_but_selected_documents_are(engine):
    ddr_bucket = next(d.bucket_id for d in engine.store.documents() if d.bucket == "ddr")
    _, fin = run(engine, "tRFC değeri nedir?", buckets=[ddr_bucket])
    assert fin["verification"]["status"] == "clarify", "the DDR bucket holds DDR4 and DDR5"
    ddr4 = [d.id for d in engine.store.documents() if "DDR4" in d.entities]
    _, fin = run(engine, "tRFC değeri nedir?", doc_ids=ddr4)
    assert fin["verification"]["status"] != "clarify" and "350" in fin["answer"]


def test_reply_that_conflicts_with_the_selection_is_not_used_silently(engine, fake):
    ddr_bucket = next(d.bucket_id for d in engine.store.documents() if d.bucket == "ddr")
    _, fin = run(engine, "tRFC değeri nedir?", buckets=[ddr_bucket])
    ddr4 = [d.id for d in engine.store.documents() if "DDR4" in d.entities]
    n = len(answer_calls(fake))
    _, fin = run(engine, "DDR5", clarification=pending_of(fin), doc_ids=ddr4)
    assert fin["verification"]["status"] == "not_found" and "350" not in fin["answer"]
    assert "DDR5" in fin["answer"] and len(answer_calls(fake)) == n, "never answered from the DDR4 selection"


def test_standard_named_earlier_in_the_conversation_is_not_asked_again(engine):
    history = [{"role": "user", "content": "DDR4 tRFC değeri nedir?"},
               {"role": "assistant", "content": "tRFC for an 8Gb device is 350 ns."}]
    events, fin = run(engine, "Peki tREFI değeri?", history=history)
    p = plan_of(events)
    assert p["plan"]["carried_entities"] == ["DDR4"] and p["scope"]["entities"] == ["DDR4"]
    assert fin["verification"]["status"] != "clarify"
    # a question about another collection is a new topic: nothing is carried over
    events, _ = run(engine, "I2C bus speed?", history=history)
    assert plan_of(events)["plan"]["carried_entities"] == []


def _density_library(cfg, fake):
    """A DDR4 document whose timing table gives tRFC per device density (two verified rows)."""
    root = cfg.sources_dir / "ddr"
    root.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "JEDEC STANDARD DDR4 SDRAM", fontsize=14)
    page.insert_text((72, 120), "Revision C", fontsize=11)
    page = doc.new_page()
    page.insert_text((72, 90), "4 Refresh Timing", fontsize=14)
    page.insert_textbox(pymupdf.Rect(72, 110, 540, 200), "The refresh cycle time tRFC of DDR4 devices depends "
                        "on the device density. Table 4-1 Refresh parameters", fontsize=11)
    _draw_table(page, 220, [["Parameter", "Density", "Min", "Unit"], ["tRFC", "8Gb", "350", "ns"],
                            ["tRFC", "16Gb", "550", "ns"]])
    doc.save(str(root / "JESD79-4C_DDR4_Refresh.pdf"))
    doc.close()

    def vlm(page_text):
        if "tRFC" not in page_text or "Density" not in page_text:
            return {"tables": []}
        return {"tables": [{
            "caption": "Table 4-1 Refresh parameters", "columns": ["Parameter", "Density", "Min", "Unit"],
            "rows": [["tRFC", "8Gb", "350", "ns"], ["tRFC", "16Gb", "550", "ns"]], "footnotes": [],
            "parameters": [
                {"parameter": "Refresh cycle time", "symbol": "tRFC", "min": "350", "typ": "", "max": "",
                 "unit": "ns", "conditions": "8Gb", "notes": ""},
                {"parameter": "Refresh cycle time", "symbol": "tRFC", "min": "550", "typ": "", "max": "",
                 "unit": "ns", "conditions": "16Gb", "notes": ""}]}]}
    fake.vlm = vlm


def test_condition_that_changes_the_value_is_asked_with_options_from_the_table(cfg, make_engine, fake):
    _density_library(cfg, fake)
    engine = make_engine()
    rows = [r for r in engine.store.search_parameters("tRFC", 10, None, verified_only=True) if r.symbol == "tRFC"]
    assert {r.conditions for r in rows} == {"8Gb", "16Gb"}, "both rows are verified against the page"
    _, fin = run(engine, "DDR4 tRFC değeri nedir?")
    q = fin["clarify"]["questions"]
    assert [x["kind"] for x in q] == ["density"]
    assert [o["value"] for o in q[0]["options"]][:2] == ["8Gb", "16Gb"] and q[0]["options"][-1].get("all")
    events, fin = run(engine, "16Gb", clarification=pending_of(fin))
    assert plan_of(events)["merged_question"] == "DDR4 tRFC değeri nedir? (16Gb)"
    assert fin["verification"]["status"] != "clarify"
    # the condition already in the question, or a request for all values: answered directly
    for question in ("DDR4 tRFC değeri 8Gb için nedir?", "DDR4 tRFC değerlerini tüm yoğunluklar için listele"):
        _, fin = run(engine, question)
        assert fin["verification"]["status"] != "clarify", question


def test_compatibility_question_asks_for_missing_electrical_conditions(engine, fake):
    _, fin = run(engine, "Bu sürücü çıkışını kartımdaki girişe doğrudan bağlayabilir miyim?")
    kinds = [q["kind"] for q in fin["clarify"]["questions"]]
    assert kinds == ["voltage", "termination"], "roles are already stated (çıkış -> giriş)"
    assert all(q["options"] == [] for q in fin["clarify"]["questions"]), "no invented options: free text"
    assert len(fin["clarify"]["questions"]) <= clarify.MAX_QUESTIONS
    events, fin = run(engine, "3.3 V, 100 ohm", clarification=pending_of(fin))
    assert fin["verification"]["status"] != "clarify", "answered conditions are not asked again"
    _, fin = run(engine, "Bu sürücü çıkışını 3.3 V girişe 100 ohm sonlandırmayla bağlayabilir miyim?")
    assert fin["verification"]["status"] != "clarify", "everything is stated"


def test_at_most_two_clarifications_in_a_row(engine):
    pending = {"question": "tRFC değeri nedir?", "kinds": ["voltage"], "round": clarify.MAX_ROUNDS,
               "options": {}, "all": {}}
    _, fin = run(engine, "evet", clarification=pending)
    assert fin["verification"]["status"] != "clarify"


def test_reply_classification_and_condition_matching():
    pending = {"question": "tRFC nedir?", "kinds": ["standard_version", "density"], "round": 1,
               "options": {"standard_version": ["DDR4", "DDR5"], "density": ["8Gb", "16Gb"]},
               "all": {"standard_version": "DDR4 vs DDR5", "density": "tüm koşullar için"}}
    assert merge_reply("DDR4, 2400 MT/s", pending).question == "tRFC nedir? (DDR4, 2400 MT/s)"
    r = merge_reply("bilmiyorum", pending)
    assert r.unknown == ["standard_version", "density"] and r.question == "tRFC nedir? (DDR4 vs DDR5)"
    assert merge_reply("Peki tREFI nedir?", pending) is None, "a question of its own"
    assert merge_reply("anything", None) is None and merge_reply("x", {"question": ""}) is None
    assert mentions("tCK (DDR4, 2400 MT/s)", "DDR4-2400") and not mentions("DDR4 tCK nedir?", "DDR4-2400")
    assert mentions("tRFC 8 Gb için", "8Gb") and not mentions("128Gb", "8Gb")
    assert (condition_kind("DDR4-2400"), condition_kind("16Gb"), condition_kind("TC = 0 to 85 °C")) == \
        ("speed_grade", "density", "temperature")
    assert set(clarify.TEXTS["tr"]) == set(clarify.TEXTS["en"])


def test_cli_chat_keeps_the_pending_question(engine, capsys):
    from types import SimpleNamespace

    from techrag import cli

    args = SimpleNamespace(domain=None, doc=None, verbose=False)
    answer, pending = cli._stream(engine, "tRFC değeri nedir?", [], args)
    out = capsys.readouterr().out
    assert pending and pending["question"] == "tRFC değeri nedir?" and "DDR4" in out and "Hepsini" in out
    answer, pending = cli._stream(engine, "DDR5", [], args, pending)
    out = capsys.readouterr().out
    assert pending is None and "(question: tRFC değeri nedir? (DDR5))" in out and "295" in answer
