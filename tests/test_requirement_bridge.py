"""Questions in the words of design practice ("is X needed?") whose answer the standard states as a requirement
in its own terms: the planner searches for the governing requirement, and before a "not found" answer the model
is asked once to look for it (fake model server)."""

import json

from conftest import answer_calls
from fakeserver import sources_in
from techrag.prompts import NOT_FOUND, answer_system_prompt

QUESTION = "DDR5 controller'da iki REFRESH arasında boşluk bırakmak gerekir mi?"


def _events(engine, question=QUESTION):
    return list(engine.ask_stream(question))


def _final(events):
    return next(e for e in events if e["type"] == "final")


def _bridging_model(msgs, tools):
    last = msgs[-1]
    if last.get("role") == "tool":
        src = sources_in(msgs)
        n = next(k for k in sorted(src) if "295" in src[k])
        return {"content": f"The refresh cycle time tRFC for an 8Gb DDR5 device is 295 ns [{n}]."}
    if "Before concluding" in str(last.get("content")):
        return {"tool_calls": [{"name": "search_docs", "arguments": {"query": "DDR5 refresh cycle time tRFC"}}]}
    return {"content": NOT_FOUND["tr"]}


def test_model_is_asked_once_to_search_for_the_governing_requirement(engine, fake):
    fake.answer = _bridging_model
    events = _events(engine)
    fin = _final(events)
    assert "295 ns" in fin["answer"] and fin["verification"]["status"] == "supported", fin["answer"]
    assert any(e["type"] == "status" and e.get("stage") == "searching_more" for e in events)
    calls = answer_calls(fake)
    assert len(calls) == 3, "first answer, the search after the reminder, the answer"
    second = calls[1]["body"]["messages"]
    assert second[-2]["role"] == "assistant" and NOT_FOUND["tr"] in second[-2]["content"]
    assert second[-1]["role"] == "user" and "Before concluding" in second[-1]["content"]
    assert NOT_FOUND["tr"] in second[-1]["content"], "the not-found sentence stays the exact allowed answer"


def test_a_question_the_sources_do_not_answer_still_ends_as_not_found(engine, fake):
    fake.answer = lambda msgs, tools: {"content": NOT_FOUND["tr"]}
    fin = _final(_events(engine, "DDR5 için kart kalınlığı kaç mm olmalı?"))
    assert fin["verification"]["status"] == "not_found"
    assert len(answer_calls(fake)) == 2, "one reminder only"


def test_no_reminder_without_tools_or_when_switched_off(engine, fake, cfg):
    fake.answer = lambda msgs, tools: {"content": NOT_FOUND["tr"]}
    cfg.llm.tools = "off"
    _final(_events(engine))
    assert len(answer_calls(fake)) == 1
    cfg.llm.tools = "auto"
    cfg.answer.search_before_not_found = False
    before = len(answer_calls(fake))
    _final(_events(engine))
    assert len(answer_calls(fake)) == before + 1


def test_planner_phrasing_is_searched_but_carries_no_values(engine, fake):
    fake.plan_extra = {"spec_phrasing": "The refresh cycle time tRFC of an 8Gb device shall be at least 295 ns.",
                       "search_queries": ["DDR5 refresh cycle time tRFC", "refresh command interval tREFI"]}
    res = engine.retrieve(QUESTION)
    assert res.plan.spec_phrasing == "The refresh cycle time tRFC of an 8Gb device shall be at least X."
    assert res.plan.spec_phrasing in res.plan.dense_queries()
    assert "refresh command interval tREFI" in res.plan.bm25_queries()
    emb = [c for c in fake.calls if c["path"].endswith("/embeddings")]
    assert any(res.plan.spec_phrasing in json.dumps(c["body"]) for c in emb), "used as a dense search query"


def test_contracts_ask_for_the_governing_requirement():
    from techrag.query import PLANNER_SYSTEM

    assert "govern" in PLANNER_SYSTEM and '"spec_phrasing"' in PLANNER_SYSTEM
    p = answer_system_prompt("tr")
    assert "requirements of the standard that govern it" in p and NOT_FOUND["tr"] in p
    assert "is not given as a number" in p, "no unsourced numeric conversion"
