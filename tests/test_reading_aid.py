"""A VLM table that did not match the PDF text cell by cell stays out of the evidence; when search finds it, the
model gets the page's PDF text with that table as a non-citable reading aid, and the score shown is the score of
the text the model actually gets (fake model server)."""

import json

import pymupdf

from conftest import judge_calls
from fakeserver import sources_in
from techrag.prompts import answer_system_prompt
from techrag.tools import READING_AID

PAGE_TEXT = ("Table 2-1 Quokka timing. The quokka delay tQK is 4321 ns at minimum and the quokka hold tQH "
             "is 87 ns at maximum; both apply to the quokka controller.")


def _pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "JEDEC STANDARD DDR4 SDRAM Quokka Supplement", fontsize=14)
    page = doc.new_page()
    page.insert_text((72, 90), "2 Quokka timing", fontsize=14)
    page.insert_textbox(pymupdf.Rect(72, 110, 540, 300), PAGE_TEXT, fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def _vlm(text):
    if "Quokka timing" not in text:
        return {"tables": []}
    # Column names that are not printed on the page: the table cannot be tied to the PDF text, so it is
    # search-only.
    return {"tables": [{"caption": "Table 2-1 Quokka timing", "columns": ["Quokka item", "Lower bound", "Upper bound"],
                        "rows": [["tQK quokka delay", "4321 ns", "-"], ["tQH quokka hold", "-", "87 ns"],
                                 ["tQZ quokka zone", "55 ns", "-"]],
                        "footnotes": [], "parameters": []}]}


def _engine(cfg, fake, make_engine):
    cfg.vision.min_page_score = 3.0
    fake.vlm = _vlm
    _pdf(cfg.sources_dir / "ddr" / "DDR4_Quokka.pdf")
    return make_engine()


def test_unmatched_table_becomes_a_reading_aid_and_is_never_evidence(cfg, fake, make_engine):
    engine = _engine(cfg, fake, make_engine)
    tables = [c for c in engine.store.page_chunks(1, 2, evidence_only=False) if not c.evidence]
    assert tables and "Lower bound" in tables[0].text, "the VLM table is in the index as search-only content"
    res = engine.retrieve("quokka delay lower bound upper bound")
    page = next(p for p in res.passages if "tQK is 4321 ns" in p.text)
    assert page.replaced and "Lower bound" in page.aid and "Lower bound" not in page.text
    assert not any("Lower bound" in p.text for p in res.passages), "the unmatched table is never a passage itself"

    from techrag.tools import SourceRegistry

    src, _ = SourceRegistry().add_passage(page)
    assert READING_AID in src.text and "Lower bound" in src.text
    assert "Lower bound" not in src.evidence() and "tQK is 4321 ns" in src.evidence()


def test_score_shown_is_that_of_the_text_given_to_the_model(cfg, fake, make_engine):
    engine = _engine(cfg, fake, make_engine)
    plan = engine.planner.plan("quokka delay lower bound upper bound", [])
    scope = engine.catalog.resolve_scope(plan)
    ranked, top = engine.retriever._rank(plan, scope, {})
    assert not ranked[0][0].evidence, "the search's best hit is the unmatched VLM table"
    res = engine.retrieve("quokka delay lower bound upper bound")
    page = next(p for p in res.passages if p.replaced)
    assert res.confidence == page.score < top, (res.confidence, top)


def test_values_only_in_the_reading_aid_are_not_documented(cfg, fake, make_engine):
    engine = _engine(cfg, fake, make_engine)
    cfg.answer.regenerate = False

    def answer(msgs, tools):
        src = sources_in(msgs)
        n = next(k for k, t in src.items() if "READING AID" in t)
        return {"content": f"The quokka delay tQK is 4321 ns [{n}]. The quokka zone tQZ is 55 ns [{n}]. "
                           f"The quokka hold upper bound is 99 ns [{n}]."}
    fake.answer = answer
    fin = next(e for e in engine.ask_stream("quokka delay lower bound upper bound") if e["type"] == "final")
    details = fin["verification"]["details"]
    assert details[0]["outcome"] == "kept", details[0]
    assert details[1]["outcome"] == "removed" and details[1]["status"] == "unverified"
    assert details[1]["codes"][0]["code"] == "value_image_only", "55 ns is only in the reading aid"
    assert details[2]["outcome"] == "removed" and details[2]["codes"][0]["code"] == "value_not_found"
    for call in judge_calls(fake):
        assert "READING AID" not in json.dumps(call["body"]), "the judge sees only the PDF text"


def test_answer_contract_explains_marks_and_reading_aids():
    p = answer_system_prompt("tr")
    assert "READING AID" in p and "295⁽1⁾" in p and "10^-12" in p
