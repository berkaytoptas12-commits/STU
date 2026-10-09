"""Retrieval scoping, the tool-using agent loop and the verification pipeline."""

import json

import pytest

from fakeserver import sources_in
from techrag.tools import SourceRegistry, safe_eval
from techrag.verify import Claim, apply_failures, deterministic_check, split_claims


def final(engine, question, **kw):
    events = list(engine.ask_stream(question, **kw))
    return events, next(e for e in events if e["type"] == "final")


# ------------------------------------------------------------------------- retrieval

def test_entity_hard_filter_keeps_sibling_standard_out(engine):
    res = engine.retrieve("DDR5 tRFC refresh cycle time")
    assert res.scope.reason == "entity" and res.scope.entities == ["DDR5"]
    assert res.passages and all(p.entities == ["DDR5"] for p in res.passages)
    assert all("295" in r.min for r in res.parameters if r.symbol == "tRFC")
    both = engine.retrieve("DDR4 vs DDR5 tRFC farkı")
    assert {e for p in both.passages for e in p.entities} == {"DDR4", "DDR5"}


def test_unnamed_standard_searches_everything_and_ui_scope_wins(engine):
    res = engine.retrieve("refresh cycle time")
    assert res.scope.reason == "all"
    i2c = [d.id for d in engine.store.documents() if d.domain == "i2c"]
    res = engine.retrieve("DDR5 tRFC", doc_ids=i2c)
    assert res.scope.reason == "user" and {p.domain for p in res.passages} <= {"i2c"}


def test_small_to_big_expands_to_whole_short_section(engine):
    res = engine.retrieve("DDR4 refresh cycle time tRFC 8Gb")
    top = next(p for p in res.passages if "tRFC" in p.text and p.kind == "text")
    assert "tREFI = 7.8 us" in top.text and top.section.endswith("3.2 Refresh Timing")


def test_figure_pages_are_attached_as_images(engine, fake):
    events, fin = final(engine, "DDR4 refresh timing diagram figure")
    answer_calls = [c for c in fake.calls if "ANSWER CONTRACT" in json.dumps(c["body"].get("messages", [{}])[:1])]
    first = answer_calls[0]["body"]["messages"][-1]["content"]
    assert isinstance(first, list) and any(p.get("type") == "image_url" for p in first)


# ----------------------------------------------------------------------------- agent

def test_agent_uses_tools_verifies_and_regenerates(engine, fake):
    events, fin = final(engine, "DDR5 tRFC değeri nedir?")
    kinds = [e["type"] for e in events]
    assert "tool" in kinds and kinds.index("tool") < kinds.index("final")
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["name"] == "get_parameter" and tool["args"]["standard"] == "DDR5"
    v = fin["verification"]
    assert v["regenerated"] and v["judge_used"] and v["status"] == "ok"
    assert "295 ns" in fin["answer"] and "3.9 us" not in fin["answer"] and "350" not in fin["answer"]
    cited = [s for s in fin["sources"] if s["cited"]]
    assert cited and all(s["entities"] == ["DDR5"] for s in cited)


def test_failing_claim_is_stripped_when_regeneration_does_not_fix_it(engine, fake, cfg):
    def stubborn(msgs, tools):
        n = min(sources_in(msgs))
        return {"content": f"tRFC is 295 ns [{n}].\n\n### Documented\n- tRFC is 295 ns [{n}].\n"
                           f"- tRFC shall never exceed 999 ns [{n}]."}
    fake.answer = stubborn
    _, fin = final(engine, "DDR5 tRFC?")
    v = fin["verification"]
    assert v["status"] == "corrected" and len(v["removed"]) == 1 and "999" not in fin["answer"]
    assert any("999" in c["text"] and c["status"] == "removed" for c in v["details"])

    cfg.answer.failed_claims = "flag"
    _, fin = final(engine, "DDR5 tRFC?")
    assert "999 ns" in fin["answer"] and "⚠" in fin["answer"] and fin["verification"]["status"] == "warning"


def test_all_claims_unverifiable_becomes_not_found(engine, fake):
    fake.answer = lambda msgs, tools: {"content": "### Documented\n- tRFC is 123 ns [1].\n- tREFI is 4.4 us [1]."}
    _, fin = final(engine, "DDR5 tRFC?")
    assert fin["verification"]["not_found"] and "not found" in fin["answer"].lower() or "bulunamadı" in fin["answer"]


def test_tools_unsupported_server_falls_back(engine, fake):
    fake.tools_supported = False
    events, fin = final(engine, "DDR5 tRFC değeri nedir?")
    assert any(e["type"] == "status" and e["stage"] == "tools_unsupported" for e in events)
    assert "295 ns" in fin["answer"]
    assert engine.llm.tools_supported is False


def test_thinking_switch_and_reasoning_stream(engine, fake, cfg):
    fake.reasoning = "Let me compare both standards."
    events, fin = final(engine, "DDR4 vs DDR5 tRFC farkı nedir?")
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["thinking"] is True
    assert any(e["type"] == "reasoning" for e in events)
    answer_call = next(c for c in fake.calls if "ANSWER CONTRACT" in json.dumps(c["body"].get("messages", [{}])[:1]))
    assert answer_call["body"]["chat_template_kwargs"] == {"enable_thinking": True}
    planner_call = next(c for c in fake.calls if "retrieval plan" in json.dumps(c["body"].get("messages", [{}])[:1]))
    assert planner_call["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_library(cfg, registry, clients):
    from techrag.engine import RAGEngine

    e = RAGEngine(cfg, embedder=clients["embedder"], reranker=clients["reranker"], llm=clients["llm"],
                  vision=clients["vision"], domains=registry)
    _, fin = final(e, "I2C hızı nedir?")
    assert "bulunamadı" in fin["answer"]


# ---------------------------------------------------------------------- verification

def _registry(*texts):
    from techrag.retrieval import Passage

    reg = SourceRegistry()
    for i, t in enumerate(texts):
        reg.add_passage(Passage(1, "Doc", "d.pdf", "ddr", "s", 1, 1, "text", t, 1.0, [i], [i]))
    return reg


def test_split_claims_sections_and_tables():
    ans = ("Short answer is 350 ns [1].\n\n### Documented\n- tRFC is 350 ns [1]. tREFI is 7.8 us [2].\n\n"
           "| Parameter | Value |\n|---|---|\n| tRFC | 350 ns [1] |\n\n### Engineering inference (not stated in the sources)\n"
           "- Margin is about 10 percent.")
    claims = split_claims(ans)
    assert [c.section for c in claims] == ["lead", "documented", "documented", "documented", "inference"]
    assert claims[2].citations == [2]
    assert claims[3].text.startswith("| tRFC")


def test_deterministic_check_units_and_citations():
    reg = _registry("tRFC is 350 ns for 8Gb.", "tREFI is 7.8 us.")
    claims = split_claims("### Documented\n- tRFC is 0.35 µs [1].\n- tREFI is 7.8 us [1].\n- tRFC is 350 ns [3].\n"
                          "- tRFC is 260 ns [1].")
    deterministic_check(claims, reg)
    st = {c.text: c for c in claims}
    assert st["tRFC is 0.35 µs [1]."].status == "ok", "unit-normalised match"
    assert "wrong citation" in " ".join(st["tREFI is 7.8 us [1]."].reasons)
    assert "non-existent" in " ".join(st["tRFC is 350 ns [3]."].reasons)
    assert st["tRFC is 260 ns [1]."].status == "fail"


def test_apply_failures_strip_and_flag():
    ans = "### Documented\n- A is 1 V [1]. B is 2 V [1].\n- C is 3 V [1].\n| x | 4 V [1] |"
    claims = split_claims(ans)
    for c in claims:
        c.status = "fail" if ("B is" in c.text or "C is" in c.text or "4 V" in c.text) else "ok"
    out, removed = apply_failures(ans, claims, "strip")
    assert out == "### Documented\n- A is 1 V [1]." and len(removed) == 3
    out, _ = apply_failures(ans, claims, "flag")
    assert out.count("⚠") == 3


def test_safe_calculator():
    assert safe_eval("1/(400e3)") == pytest.approx(2.5e-6)
    assert safe_eval("sqrt(16) + 2^3") == 12
    for bad in ("__import__('os')", "open('x')", "(1).real", "9**999"):
        with pytest.raises(Exception):
            safe_eval(bad)


def test_eval_recall_with_expected_pages(engine, tmp_path):
    from techrag.evaluation import load_items, run_eval

    f = tmp_path / "gold.yaml"
    f.write_text("""
- id: ddr5-trfc
  question: DDR5 tRFC 8Gb refresh cycle time
  expected: ["295"]
  expected_doc: DDR5
  expected_pages: [6]
- id: i2c-fm
  question: I2C Fast-mode bit rate
  expected: ["400"]
  expected_doc: UM10204
""", encoding="utf-8")
    report = run_eval(engine, load_items(f), retrieval_only=True)
    s = report["summary"]
    assert s["recall@5"] == 1.0 and s["errors"] == 0
    full = run_eval(engine, load_items(f)[:1])
    assert full["summary"]["answer_accuracy"] == 1.0 and full["summary"]["faithfulness"] == 1.0


def test_tools_inherit_question_scope(engine, fake):
    # The fake model calls get_parameter WITHOUT a standard: rows must still come from DDR5 only.
    def no_std(msgs, tools):
        if tools and not any(m.get("role") == "tool" for m in msgs):
            return {"tool_calls": [{"name": "get_parameter", "arguments": {"name": "tRFC"}}]}
        src = sources_in(msgs)
        return {"content": "Sources: " + " ".join(f"[{n}] {t[:60]}" for n, t in src.items())}
    fake.answer = no_std
    events, fin = final(engine, "DDR5 tRFC?")
    added = [s for e in events if e["type"] == "sources_add" for s in e["sources"]]
    assert all(s["entities"] == ["DDR5"] for s in added)


def test_read_only_uri_forms():
    from techrag.store import sqlite_ro_uri

    assert sqlite_ro_uri("/srv/lib/index.sqlite") == "file:///srv/lib/index.sqlite?mode=ro&immutable=1"
    assert sqlite_ro_uri("/srv/my lib/index.sqlite").startswith("file:///srv/my%20lib/")
    from techrag.store import uri_for_resolved

    assert uri_for_resolved(r"\\fileserver\share\lib\index.sqlite").startswith("file:////fileserver/share/lib/")
    assert uri_for_resolved(r"C:\TechRAG\lib\index.sqlite").startswith("file:///C:/TechRAG/lib/")
