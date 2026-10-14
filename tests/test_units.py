from techrag.units import find_quantities, format_si, parse_number


def q(text):
    return find_quantities(text)[0]


def test_parse_and_normalise():
    assert q("350 ns").values == (350e-9,) or abs(q("350 ns").values[0] - 350e-9) < 1e-18
    assert q("0,35 µs").matches(q("350 ns")), "Turkish decimal comma + prefix conversion"
    assert q("8.0 GT/s").base == "T/s"
    assert q("400 kbit/s").matches(q("0.4 Mb/s"))
    assert q("100 Ω").matches(q("0.1 kohm"))
    assert q("4000 ft").base == "m"
    assert not q("1.2 V").matches(q("1.25 V"))


def test_ranges_and_negative():
    vals = [x.values[0] for x in find_quantities("VDD 1.14 – 1.26 V, -40 °C to 85 °C")]
    assert vals == [1.14, 1.26, -40.0, 85.0]


def test_not_quantities():
    assert find_quantities("3 states and 128b/130b encoding in DDR4") == []


def test_parse_number_ambiguity():
    assert set(parse_number("1,200")) == {1.2, 1200.0}
    assert parse_number("1.234,5") == [1.2345, 1234.5] or 1234.5 in parse_number("1.234,5")
    assert format_si(3.5e-7, "s") == "350 ns"


def test_signs_and_separators_are_kept_apart():
    from techrag.geometry import norm_tokens
    from techrag.units import to_si, value_kind

    assert norm_tokens("-40") == norm_tokens("(−40 °C)")[:1] == ["-40"] and norm_tokens("40") == ["40"]
    assert norm_tokens("DDR4-3200") == ["ddr4", "3200"] and norm_tokens("1-5 ns") == ["1", "5", "ns"]
    assert norm_tokens("295ns") == ["295", "ns"], "a unit printed next to the number"
    assert norm_tokens("1,200") != norm_tokens("1.200"), "1,200 may mean 1200: never equal to 1.2"
    assert norm_tokens("1,2") == norm_tokens("1.2") and norm_tokens("0,125") == ["0.125"]
    q = find_quantities("−40 °C to 85 °C; 1–5 ns; –3.3 V")
    assert [v for x in q for v in x.values] == [-40.0, 85.0, 1e-9, 5e-9, -3.3]
    assert value_kind("−40") == "number" and to_si("−40", "V") == -40.0


def test_table_values_with_empty_cells_and_footnote_marks():
    from techrag.units import find_quantities, table_quantities

    def q(text):
        return [x.text for x in table_quantities(text)]
    assert q("| tRFC | 295 | - | ns |") == ["295 ns"], "an empty Max cell does not cut the value from its unit"
    assert q("| tREFI | - | 7.8 | us |") == ["7.8 us"]
    assert q("| tRFC | 295⁽1⁾ | - | ns |") == ["295 ns"], "295 with footnote 1"
    assert q("| tRFC | 2951 | - | ns |") == ["2951 ns"], "a different number stays different"
    assert q("| tXS | 5 | n/a | tCK |") == [], "clock cycles have no SI unit"
    assert q("| tA | 5 | - |\n| tB | - | ns |") == [], "a row label in between cuts the run"
    assert [x.text for x in find_quantities("tRFC is 295⁽1⁾ ns")] == ["295 ns"]
