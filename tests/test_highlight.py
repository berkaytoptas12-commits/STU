"""Evidence highlights for Turkish answers to English standards: technical terms carry a claim without numbers,
a value written in another unit (1250 ps for 1.25 ns) finds its cell, and values printed in a table's header row
(speed bins) are found there. Two equally good places still give no highlight - nothing is guessed.

Synthetic PDFs; these check the mechanics, not how often a real model's answers can be located."""

from test_tables_evidence import _locator, one_page

from techrag.evidence import _content, support_units
from techrag.tools import Source

BODY = ("The device supports on-die termination (ODT) for the DQ, DQS and DM signals. "
        "The termination resistance RTT is selected through mode register MR1.")
ROWS = [["Speed Bin", "DDR3-800", "DDR3-1333", "DDR3-1600", "Unit"],
        ["tCK(avg) min", "2.5", "1.5", "1.25", "ns"],
        ["tRP min", "15", "13.5", "1.5", "ns"]]


def _md(rows):
    head, *rest = rows
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in rest])


def _setup():
    _, g = one_page(ROWS, body=BODY)
    src = Source(1, "passage", BODY.replace(". ", ".\n") + "\n\n" + _md(ROWS), doc_id=1, page_start=1, page_end=1,
                 entities=["DDR3"])
    return g, src


def _inside(region, box, g):
    r = g.to_display(box.rect())
    cy = (r[1] + r[3]) / 2
    return any(rc[1] <= cy <= rc[3] and rc[0] <= r[2] and r[0] <= rc[2] for rc in region["rects"])


def _same_line(region, box, g):
    r = g.to_display(box.rect())
    cy = (r[1] + r[3]) / 2
    return any(rc[1] <= cy <= rc[3] for rc in region["rects"])


def test_turkish_letters_stay_whole_words():
    assert _content("Sonlandırma direnci üzerinden seçilir") == {"sonlandirma", "direnci", "uzerinden", "secilir"}


def test_a_turkish_claim_without_numbers_is_found_by_its_terms():
    g, src = _setup()
    rec = _locator({1: g}).locate("Sonlandırma direnci RTT, MR1 mod yazmacı üzerinden seçilir [1].", src)
    assert rec["status"] == "located", rec
    assert rec["quotes"] == ["The termination resistance RTT is selected through mode register MR1."], \
        "the sentence about RTT and MR1, not the ODT sentence"
    assert any(_inside(r, g.find_phrase("MR1")[0], g) for r in rec["regions"])


def test_two_equally_good_sentences_give_no_highlight():
    body = "RTT is set by MR1. The RTT value is also listed in MR2."
    _, g = one_page([], body=body)
    src = Source(1, "passage", body.replace(". ", ".\n"), doc_id=1, page_start=1, page_end=1)
    rec = _locator({1: g}).locate("RTT değeri tabloda verilir [1].", src)
    assert rec["status"] == "not_located" and rec["regions"] == []


def test_a_value_in_another_unit_finds_its_cell():
    g, src = _setup()
    rec = _locator({1: g}).locate("DDR3-1600 için tCK(avg) en az 1250 ps'dir [1].", src)
    assert rec["status"] == "located", rec
    value = [r for r in rec["regions"] if r["role"] == "value"]
    assert value and _inside(value[0], g.find_phrase("1.25")[0], g), "the 1.25 ns cell"
    assert any(r["role"] == "header" and "1600" in r["quote"] for r in rec["regions"])


def test_header_row_values_are_found():
    g, src = _setup()
    units = support_units("DDR3 veri hızları 800 ile 1600 MT/s arasındadır [1].", src.text, ignore=["DDR3"])
    assert [u["kind"] for u in units] == ["header"] and set(units[0]["numbers"]) == {"800", "1600"}
    rec = _locator({1: g}).locate("DDR3 veri hızları 800 ile 1600 MT/s arasındadır [1].", src)
    assert rec["status"] == "located"
    heads = [r for r in rec["regions"] if r["role"] == "header"]
    assert {r["quote"] for r in heads} == {"DDR3-800", "DDR3-1600"}
    assert _inside(heads[0], g.find_phrase(heads[0]["quote"])[0], g)


def test_a_repeated_number_is_highlighted_in_the_row_the_claim_names():
    g, src = _setup()
    for claim, label in (("Ön şarj süresi tRP en az 1.5 ns olmalıdır [1].", "tRP"),
                         ("Saat periyodu tCK(avg) en az 1.5 ns olmalıdır [1].", "tCK(avg)")):
        rec = _locator({1: g}).locate(claim, src)
        value = [r for r in rec["regions"] if r["role"] == "value"]
        assert value and all(_same_line(v, g.find_phrase(label, label=True)[0], g) for v in value), claim
