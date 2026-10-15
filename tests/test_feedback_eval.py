"""Answer feedback (per user, outside the library), candidate evaluation items, the evaluation run from the app
and the advanced settings."""

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from techrag.evaluation import bundled_questions, load_items, parse_items, summarize
from techrag.feedback import FeedbackStore, candidate_items, export_yaml
from techrag.server import create_app


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


SOURCES = [{"n": 3, "doc_title": "JESD79-5 DDR5", "doc_path": "C:/Users/x/library/sources/ddr/JESD79-5.pdf",
            "revision": "A", "page_start": 6, "page_end": 6, "kind": "passage", "cited": True,
            "text": "a long source text that is not stored"}]


def test_feedback_store_and_candidate_items(tmp_path):
    fs = FeedbackStore(tmp_path / "user")
    up = fs.add({"rating": "up", "question": "DDR5 tRFC 8Gb?", "answer": "tRFC is 295 ns [3].",
                 "verification_status": "supported", "sources": SOURCES})
    fs.add({"rating": "down", "question": "DDR5 VDD?", "answer": "VDD is 1.2 V [1].",
            "correction": {"value": "1.1", "unit": "V", "parameter": "VDD", "document": "JESD79-5", "page": "12"}})
    fs.add({"rating": "down", "question": "Bir şey yanlış", "answer": "x"})
    fs.add({"rating": "down", "question": "PCIe 7 lane margining?", "unanswerable": True})
    with pytest.raises(ValueError):
        fs.add({"rating": "meh", "question": "q"})
    rec = fs.items()[0]
    assert rec["sources"][0]["file"] == "JESD79-5.pdf" and "text" not in rec["sources"][0]
    assert "library" not in json.dumps(fs.items()), "no library path is stored"
    assert fs.summary() == {"total": 4, "up": 1, "down": 3, "with_correction": 1, "candidates": 3}
    cands = {c["question"]: c for c in candidate_items(fs.items())}
    assert cands["DDR5 tRFC 8Gb?"]["keywords"] == ["295 ns"] and cands["DDR5 tRFC 8Gb?"]["pages"] == [6]
    assert cands["DDR5 tRFC 8Gb?"]["document"] == "JESD79-5 DDR5"
    vdd = cands["DDR5 VDD?"]
    assert vdd["facts"] == [{"parameter": ["VDD"], "value": "1.1", "unit": "V"}] and vdd["pages"] == [12]
    assert cands["PCIe 7 lane margining?"]["answerable"] is False
    assert all(c["status"] == "candidate" for c in cands.values())
    assert "Bir şey yanlış" not in cands, "a 👎 without the correct value has nothing to check against"
    text = export_yaml(fs.items())
    assert text.startswith("# Candidate evaluation items") and "NOT a validated benchmark" in text
    items = parse_items(yaml.safe_load(text))
    assert {i.status for i in items} == {"candidate"} and len(items) == 3
    assert fs.delete(up["id"]) and not fs.delete(up["id"]) and fs.summary()["total"] == 3


def test_feedback_api_works_with_a_read_only_library(client, cfg):
    cfg.read_only = True
    before = sorted(p.name for p in cfg.library_dir.rglob("*"))
    r = client.post("/api/feedback", json={"rating": "down", "question": "DDR5 tRFC?", "answer": "350 ns",
                                           "sources": SOURCES, "correction": {"value": "295", "unit": "ns"}})
    assert r.status_code == 200 and r.json()["summary"]["down"] == 1
    assert sorted(p.name for p in cfg.library_dir.rglob("*")) == before, "nothing is written into the library"
    assert (cfg.user_data_dir / "feedback.json").is_file()
    items = client.get("/api/feedback").json()["items"]
    assert items[0]["correction"]["value"] == "295"
    exp = client.get("/api/feedback/export")
    assert exp.status_code == 200 and "fb-" + items[0]["id"] in exp.text and "attachment" in exp.headers["content-disposition"]
    assert client.post("/api/feedback", json={"rating": "x", "question": "q"}).status_code == 400
    assert client.delete(f"/api/feedback/{items[0]['id']}").json()["summary"]["total"] == 0
    assert client.delete("/api/feedback/nope").status_code == 404


def _wait(client, job, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{job}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.1)
    raise AssertionError("evaluation did not finish")


CUSTOM = """
- id: t1
  status: expert_verified
  question: DDR5 tRFC değeri nedir?
  document: DDR5
  pages: [6]
  facts: [{parameter: [tRFC], value: "295", unit: ns}]
- id: t2
  question: ARINC 429 kelime uzunluğu nedir?
  answerable: false
"""


def test_evaluation_runs_in_the_background_and_saves_a_report(client, cfg):
    info = client.get("/api/eval").json()
    assert info["sets"]["bundled"] == len(load_items(bundled_questions())) >= 30 and info["reports"] == []
    r = client.post("/api/eval/run", json={"set": "custom", "yaml": CUSTOM, "name": "mine.yaml"}).json()
    assert r["total"] == 2
    job = _wait(client, r["job"])
    assert job["status"] == "done", job["messages"]
    s = job["report"]["summary"]
    assert s["n"] == 2 and s["counts"]["recall@5"] == [1, 1] and s["counts"]["correct_abstention_rate"][1] == 1
    assert s["counts"]["fact_accuracy_of_answered"] == [1, 1], s
    reports = client.get("/api/eval").json()["reports"]
    assert len(reports) == 1 and reports[0]["set"] == "mine.yaml"
    full = client.get(f"/api/eval/reports/{reports[0]['name']}").json()
    assert len(full["results"]) == 2 and full["set"] == "mine.yaml"
    assert (cfg.user_data_dir / "eval_reports" / reports[0]["name"]).is_file()
    assert not list(cfg.library_dir.rglob("eval_*.json")), "reports are not written into the library"
    assert client.get("/api/eval/reports/..%2Fsettings.json").status_code == 404
    assert client.post("/api/eval/run", json={"set": "custom", "yaml": "{not: a list}"}).status_code == 400
    assert client.post("/api/eval/run", json={"set": "feedback"}).status_code == 400, "no feedback yet: empty set"


def test_evaluation_can_be_stopped_and_report_errors_are_scrubbed(client, engine, monkeypatch):
    calls = {"n": 0}

    def slow_ask(q, *a, **kw):
        calls["n"] += 1
        time.sleep(0.3)
        raise RuntimeError("connection to http://10.1.2.3:8000/v1 failed, api_key=sk-abcdefgh12345678")
    monkeypatch.setattr(engine, "ask", slow_ask)
    r = client.post("/api/eval/run", json={"set": "custom", "yaml": CUSTOM}).json()
    job = _wait(client, r["job"])
    assert job["status"] == "done"
    rep = client.get(f"/api/eval/reports/{job['report']['name']}").json()
    text = json.dumps(rep)
    assert "10.1.2.3" not in text and "sk-abcdefgh" not in text and "<server>" in text
    assert rep["summary"]["errors"] == 2
    r = client.post("/api/eval/run", json={"set": "bundled"}).json()
    assert client.post("/api/eval/run", json={"set": "bundled"}).status_code == 409, "one evaluation at a time"
    client.post(f"/api/jobs/{r['job']}/cancel")
    job = _wait(client, r["job"])
    assert job["status"] == "cancelled" and calls["n"] < 10
    assert len(client.get("/api/eval").json()["reports"]) == 1, "a stopped run saves no report"


def test_summary_rates_carry_their_denominators():
    from techrag.evaluation import EvalResult

    res = [EvalResult("a", "q", "draft", True, 1, answered=True, correct=True, wrong=False),
           EvalResult("b", "q", "draft", True, None, answered=False, correct=False, wrong=False),
           EvalResult("c", "q", "draft", False, None, answered=False, correct=True, wrong=False)]
    s = summarize(res)
    assert s["counts"]["recall@1"] == [1, 2] and s["recall@1"] == 0.5
    assert s["counts"]["fact_accuracy_of_answered"] == [1, 1]
    assert s["counts"]["source_accuracy"] == [0, 0] and s["source_accuracy"] is None, "not measured, not 0"


def test_advanced_settings_and_context_info(client, fake, engine, monkeypatch):
    monkeypatch.setattr(engine.llm.api, "model_entries", lambda: [{"id": "qwen-test", "max_model_len": 8192}])
    engine.llm._context_probed = False
    c = client.get("/api/settings/context").json()
    assert c == {"setting": 16000, "server": 8192, "used": 8192}
    s = client.put("/api/settings", json={"llm": {"context_tokens": 32768}, "answer": {"judge_batch": 4,
                                          "repair_citations": False}, "retrieval": {"follow_references": 0},
                                          "vision": {"min_page_score": 6.5, "max_pages_per_doc": 200}}).json()
    st = s["settings"]
    assert st["llm"]["context_tokens"] == 32768 and st["answer"]["judge_batch"] == 4
    assert st["answer"]["repair_citations"] is False and st["retrieval"]["follow_references"] == 0
    assert st["vision"]["min_page_score"] == 6.5 and st["vision"]["max_pages_per_doc"] == 200
    for bad in ({"answer": {"judge_batch": 0}}, {"llm": {"context_tokens": 100}},
                {"retrieval": {"follow_references": 99}}, {"vision": {"min_page_score": -1}}):
        assert client.put("/api/settings", json=bad).status_code == 400, bad
    assert client.get("/api/settings").json()["answer"]["judge_batch"] == 4, "a rejected value changes nothing"


def test_clarify_items_and_per_question_details():
    from techrag.evaluation import EvalItem, item_expectation, score_answer

    item = EvalItem("c1", "DDR4 tRFC?", clarify=True, kind="condition", note="depends on density")
    asked = {"answer": "Which density?", "verification": {"status": "clarify", "details": []}, "sources": []}
    assert score_answer(item, asked) == {"answered": False, "verification_status": "clarify", "clarified": True,
                                         "correct": True, "wrong": False}
    plain = EvalItem("p1", "DDR4 VDD?")
    out = score_answer(plain, asked)
    assert out["clarified"] and not out["answered"] and out.get("correct") is None
    exp = item_expectation(item)
    assert exp["clarify"] and exp["kind"] == "condition" and exp["note"] == "depends on density"


def test_report_keeps_expected_answer_sources_and_removed_statements(client):
    yaml_text = """
- id: v1
  kind: value
  question: DDR5 tRFC değeri nedir?
  facts: [{parameter: [tRFC], value: "295", unit: ns}]
  note: check the refresh table
"""
    r = client.post("/api/eval/run", json={"set": "custom", "yaml": yaml_text, "name": "one.yaml"}).json()
    job = _wait(client, r["job"])
    rep = client.get(f"/api/eval/reports/{job['report']['name']}").json()
    res = rep["results"][0]
    assert res["expected"]["facts"][0]["value"] == "295" and res["expected"]["note"] == "check the refresh table"
    assert "295" in res["answer"] and res["cited"] and " — s. " in res["cited"][0]
    assert res["removed"] == [] and res["clarified"] is False
    assert "clarification_rate" in rep["summary"]["counts"]


def test_bundled_question_set_fits_the_library_and_downloads(client):
    from collections import Counter

    items = load_items(bundled_questions())
    assert len(items) == 30 and len({i.id for i in items}) == 30
    kinds = Counter(i.kind for i in items)
    assert set(kinds) == {"value", "design", "condition", "comparison", "unanswerable"}
    assert sum(i.clarify for i in items) == kinds["condition"] and any(not i.answerable for i in items)
    assert {i.id.split("-")[0] for i in items} >= {"a429", "a818", "come", "ddr", "dp", "eth", "gen", "i2c",
                                                   "lpddr4", "pcie", "rs422", "usb"}
    assert all(i.status == "draft" for i in items), "nobody checked them against the documents yet"
    r = client.get("/api/eval/questions")
    assert r.status_code == 200 and "a818-01" in r.text and "attachment" in r.headers["content-disposition"]
