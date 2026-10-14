"""Following cross-references: a retrieved passage that says "see Table 4-12" or "Section 5.3" brings that table /
section of the same document along (bounded, inside the scope)."""

import pymupdf

from conftest import _draw_table
from techrag.retrieval import Passage

OVERVIEW = ("The quokka controller refresh overview. The quokka refresh timing values are listed in Table 4-12. "
            "The exit procedure is described in Section 5.3.")


def _xref_pdf(path, table_label="4-12"):
    sections = [(1, "1 Overview", 2), (1, "4 Timing", 3), (2, "4.1 Refresh timing tables", 3),
                (1, "5 Self refresh", 4), (2, "5.3 Exit procedure", 4)]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "JEDEC STANDARD DDR4 SDRAM Supplement", fontsize=14)
    page = doc.new_page()
    page.insert_text((72, 90), "1 Overview", fontsize=14)
    page.insert_textbox(pymupdf.Rect(72, 120, 540, 300), OVERVIEW, fontsize=11)
    page = doc.new_page()
    page.insert_text((72, 90), "4 Timing", fontsize=14)
    page.insert_text((72, 130), "4.1 Refresh timing tables", fontsize=12)
    page.insert_text((72, 170), f"Table {table_label} Refresh timing", fontsize=11)
    _draw_table(page, 190, [["Parameter", "Min", "Max", "Unit"], ["tRFC", "312", "-", "ns"],
                            ["tREFI", "-", "7.8", "us"]])
    page = doc.new_page()
    page.insert_text((72, 90), "5 Self refresh", fontsize=14)
    page.insert_text((72, 130), "5.3 Exit procedure", fontsize=12)
    page.insert_textbox(pymupdf.Rect(72, 150, 540, 300), "After self refresh exit the controller waits tXS = 321 ns "
                        "before it issues a valid command.", fontsize=11)
    doc.set_toc([[lvl, title, p] for lvl, title, p in sections])
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def _refs(res):
    return {p.referenced_as: p for p in res.passages if p.referenced_as}


def test_table_and_section_references_are_followed(cfg, make_engine):
    cfg.retrieval.final_top_k = 1
    cfg.retrieval.section_expand_tokens = 0
    cfg.retrieval.neighbor_window = 0
    _xref_pdf(cfg.sources_dir / "ddr" / "DDR4_Xref.pdf")
    engine = make_engine()
    res = engine.retrieve("quokka refresh overview")
    assert any("quokka" in p.text and not p.referenced_as for p in res.passages)
    refs = _refs(res)
    assert set(refs) == {"Table 4-12", "Section 5.3"}, [(p.referenced_as, p.text[:60]) for p in res.passages]
    assert "312" in refs["Table 4-12"].text and refs["Table 4-12"].page_start == 3
    assert "321 ns" in refs["Section 5.3"].text and refs["Section 5.3"].page_start == 4
    top = max(p.score for p in res.passages if not p.referenced_as)
    assert all(p.score < top for p in refs.values()), "a reference never outranks the passage it came from"


def test_references_can_be_switched_off(cfg, make_engine):
    cfg.retrieval.final_top_k = 1
    cfg.retrieval.section_expand_tokens = 0
    cfg.retrieval.neighbor_window = 0
    cfg.retrieval.follow_references = 0
    _xref_pdf(cfg.sources_dir / "ddr" / "DDR4_Xref.pdf")
    res = make_engine().retrieve("quokka refresh overview")
    assert not _refs(res)


def test_only_the_same_document_is_searched_for_the_reference(cfg, make_engine):
    """The table is in another document: nothing is added (no reference leaves its document)."""
    cfg.retrieval.final_top_k = 1
    cfg.retrieval.section_expand_tokens = 0
    cfg.retrieval.neighbor_window = 0
    _xref_pdf(cfg.sources_dir / "ddr" / "DDR4_Xref.pdf", table_label="9-1")
    _xref_pdf(cfg.sources_dir / "ddr" / "DDR4_Other.pdf", table_label="4-12")
    engine = make_engine()
    res = engine.retrieve("quokka refresh overview")
    first = next(p for p in res.passages if not p.referenced_as)
    refs = _refs(res)
    assert all(p.doc_id == first.doc_id for p in refs.values())
    if "Table 4-12" in refs:
        assert "Table 4-12" in refs["Table 4-12"].text


def test_the_reference_makes_room_by_dropping_the_lowest_ranked_passage(engine, monkeypatch):
    r = engine.retriever

    def passage(score, text, ref=""):
        return Passage(1, "d", "d.pdf", "ddr", "s", 1, 1, "text", text, score, [int(score * 100)], [], referenced_as=ref)

    top, low, mid = passage(0.9, "x " * 400), passage(0.1, "y " * 400), passage(0.5, "z " * 400)
    ref = passage(0.8, "t " * 400, "Table 1-1")
    monkeypatch.setattr(r, "follow_references", lambda ps: [(0, ref)])
    out = r.with_references([top, mid, low], budget=3 * 530)
    assert out == [top, mid, ref], [p.score for p in out]
    out = r.with_references([top, mid, low], budget=1000)
    assert out == [top, mid, low], "a reference that cannot fit even after making room drops nothing"
    out = r.with_references([top, mid, low], budget=3 * 520 + 520)
    assert out == [top, mid, low, ref], "room left: nothing is dropped"
