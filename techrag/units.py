"""Engineering quantities: parse "400 ns", "0,4 µs", "8.0 GT/s", "±10%", "1.14 - 1.26 V" and normalise
them to SI so the verifier can tell that "0.4 µs" in an answer equals "400 ns" in a source."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
          "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}

# unit spelling -> (canonical base, factor to base). Prefixable units are marked True.
BASES: dict[str, tuple[str, float, bool]] = {
    "s": ("s", 1.0, True), "sec": ("s", 1.0, False),
    "Hz": ("Hz", 1.0, True),
    "V": ("V", 1.0, True), "A": ("A", 1.0, True), "W": ("W", 1.0, True),
    "Ω": ("ohm", 1.0, True), "ohm": ("ohm", 1.0, True), "ohms": ("ohm", 1.0, True), "Ohm": ("ohm", 1.0, True),
    "Ohms": ("ohm", 1.0, True),
    "F": ("F", 1.0, True), "H": ("H", 1.0, True),
    "bps": ("bit/s", 1.0, True), "b/s": ("bit/s", 1.0, True), "bit/s": ("bit/s", 1.0, True),
    "bits/s": ("bit/s", 1.0, True), "B/s": ("B/s", 1.0, True), "Bps": ("B/s", 1.0, True),
    "T/s": ("T/s", 1.0, True),
    "m": ("m", 1.0, True), "inch": ("m", 0.0254, False), "inches": ("m", 0.0254, False), "mil": ("m", 25.4e-6, False),
    "mils": ("m", 25.4e-6, False), "ft": ("m", 0.3048, False), "feet": ("m", 0.3048, False),
    "dB": ("dB", 1.0, False), "dBm": ("dBm", 1.0, False), "%": ("%", 1.0, False),
    "°C": ("degC", 1.0, False), "degC": ("degC", 1.0, False), "ppm": ("ppm", 1.0, False),
    "UI": ("UI", 1.0, False), "bit": ("bit", 1.0, True), "bits": ("bit", 1.0, True),
    "byte": ("byte", 1.0, True), "bytes": ("byte", 1.0, True), "B": ("byte", 1.0, True),
}

_units_sorted = sorted(BASES, key=len, reverse=True)
_prefixable = [u for u in _units_sorted if BASES[u][2]]
_fixed = [u for u in _units_sorted if not BASES[u][2]]
UNIT_RE = (r"(?:" + "|".join(re.escape(u) for u in _fixed) + r"|(?:[pnuµμmkKMGT])?(?:"
           + "|".join(re.escape(u) for u in _prefixable) + r"))")
NUM_RE = r"[-+±]?\d+(?:[.,]\d+)*"
_QTY = re.compile(
    r"(?<![\w.,])(?P<a>" + NUM_RE + r")(?:\s*(?:–|-|to|ile|~|\.\.\.?)\s*(?P<b>" + NUM_RE + r"))?"
    r"\s?(?P<u>" + UNIT_RE + r")(?![A-Za-z0-9])"
)


@dataclass
class Quantity:
    text: str
    number: str
    unit: str
    base: str
    values: tuple[float, ...]       # SI values; several when the number is ambiguous ("1,200")

    def matches(self, other: "Quantity", rel: float = 1e-6) -> bool:
        if self.base != other.base:
            return False
        return any(_close(a, b, rel) for a in self.values for b in other.values)


def _close(a: float, b: float, rel: float) -> bool:
    return a == b or abs(a - b) <= rel * max(abs(a), abs(b))


def parse_number(s: str) -> list[float]:
    """All plausible readings of a number string: '1,2' -> 1.2 (TR decimal); '1,200' -> 1200 or 1.2."""
    s = s.replace("±", "").replace("+", "").strip()
    neg = s.startswith("-")
    s = s.lstrip("-")
    out: list[float] = []
    if "," in s and "." in s:
        # 1,234.5 (EN) or 1.234,5 (TR)
        out += [_f(s.replace(",", "")), _f(s.replace(".", "").replace(",", "."))]
    elif "," in s:
        out.append(_f(s.replace(",", ".")) if s.count(",") == 1 else None)
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            out.append(_f(s.replace(",", "")))
    elif s.count(".") > 1:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            out.append(_f(s.replace(".", "")))
    else:
        out.append(_f(s))
    vals = [v for v in out if v is not None]
    return [-v for v in vals] if neg else vals


def _f(s: str) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        return None


def unit_info(unit: str) -> Optional[tuple[str, float]]:
    """('ns') -> ('s', 1e-9)."""
    unit = unit.strip()
    if unit in BASES:
        base, factor, _ = BASES[unit]
        return base, factor
    if len(unit) > 1 and unit[0] in PREFIX and unit[1:] in BASES and BASES[unit[1:]][2]:
        base, factor, _ = BASES[unit[1:]]
        return base, factor * PREFIX[unit[0]]
    return None


def find_quantities(text: str) -> list[Quantity]:
    out: list[Quantity] = []
    for m in _QTY.finditer(text):
        info = unit_info(m.group("u"))
        if not info:
            continue
        base, factor = info
        for key in ("a", "b"):
            num = m.group(key)
            if not num:
                continue
            vals = tuple(v * factor for v in parse_number(num))
            if vals:
                out.append(Quantity(m.group(0), num, m.group("u"), base, vals))
    return out


# Units that are meaningful but have no SI conversion (clock cycles); kept as written.
NON_SI_UNITS = {"tck", "nck", "ck", "clk", "clock", "clocks", "cycle", "cycles", "tck(avg)", "tck(abs)"}
_EMPTY_VALUES = {"", "-", "--", "—", "–", "n/a", "na", "none", "x"}
_PLAIN_NUMBER = re.compile(r"^[-+±]?\d+(?:[.,]\d+)*$")
_RANGE = re.compile(r"^[-+±]?\d+(?:[.,]\d+)*\s*(?:-|–|to|~|\.\.\.?)\s*[-+±]?\d+(?:[.,]\d+)*$")


def is_unit_token(text: str) -> bool:
    t = text.strip(" .,;:()[]")
    return bool(t) and (unit_info(t) is not None or t.lower() in NON_SI_UNITS)


def value_kind(value: str) -> str:
    """empty | number | range | expression. 'max(10 ns, 4 tCK)' or '0.5 x VDD' are expressions: they are
    kept verbatim and never reduced to a single number."""
    v = (value or "").strip()
    if v.lower() in _EMPTY_VALUES:
        return "empty"
    if _PLAIN_NUMBER.match(v):
        return "number"
    if _RANGE.match(v):
        return "range"
    return "expression" if re.search(r"\d", v) else "empty"


def number_ambiguous(value: str) -> bool:
    """True when the digits can be read two ways ('1,200' = 1200 or 1.2)."""
    return len({round(x, 12) for x in parse_number(value)}) > 1


def to_si(value: Optional[str], unit: str) -> Optional[float]:
    """SI value of a plain number with a known unit. None for expressions, ranges, ambiguous number formats
    and units without a known SI factor (an unknown unit is never silently treated as SI)."""
    if value is None or value_kind(str(value)) != "number" or number_ambiguous(str(value)):
        return None
    nums = parse_number(str(value))
    if not nums:
        return None
    unit = (unit or "").strip()
    if not unit:
        return nums[0]
    info = unit_info(unit)
    return nums[0] * info[1] if info else None


def base_unit(unit: str) -> str:
    """Canonical base of a known unit ('ns' -> 's'); '' for an unknown unit; clock-cycle units as written."""
    unit = (unit or "").strip()
    info = unit_info(unit)
    if info:
        return info[0]
    return unit if unit.lower() in NON_SI_UNITS else ""


def format_si(value: float, base: str) -> str:
    """Pretty-print an SI value with an engineering prefix: 4e-07 s -> '400 ns'."""
    if base in ("%", "dB", "dBm", "degC", "ppm", "UI") or value == 0 or not math.isfinite(value):
        return f"{value:g} {base}"
    exp = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exp = max(-12, min(12, exp))
    prefix = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}[exp]
    return f"{value / 10 ** exp:.6g} {prefix}{base}"
