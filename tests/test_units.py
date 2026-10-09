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
