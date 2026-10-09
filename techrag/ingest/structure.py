"""Assign every block to a section path (e.g. "4 Physical Layer > 4.2 Logical Sub-block > 4.2.6 LTSSM").

Two strategies:
* PDF outline (bookmarks) when present - most standards ship with one and it is the most reliable.
* Heading regexes (numbered headings, Appendix/Annex, Markdown '#') as a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from techrag.ingest.loaders import TABLE_KIND, Block, LoadedDocument

SECTION_SEP = " > "


@dataclass
class SectionedBlock:
    page: int
    text: str
    kind: str
    section: tuple[str, ...]
    ref: Optional[int] = None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_LEADING_NUM = re.compile(r"^(?:(?:chapter|section|appendix|annex|part|clause)\s+)?(?:[a-z]\s)?(?:\d+\s)*")


def _strip_num(norm: str) -> str:
    return _LEADING_NUM.sub("", norm).strip()


def _heading_matches(line: str, title: str) -> bool:
    nl, nt = _norm(line), _norm(title)
    if not nl or not nt or len(nl) > len(nt) + 30:
        return False  # headings are short lines; long lines are body text that merely starts alike
    if nl.startswith(nt[:60]) or (len(nl) >= 12 and nt.startswith(nl)):
        return True
    sl, st = _strip_num(nl), _strip_num(nt)
    if len(st) < 4 or len(sl) < 4:
        return False
    return sl.startswith(st[:60]) or (len(sl) >= 12 and st.startswith(sl))


def _sections_from_toc(blocks: list[Block], toc: list[tuple[int, str, int]]) -> list[SectionedBlock]:
    entries = sorted(enumerate(toc), key=lambda e: (e[1][2], e[0]))
    entries = [e[1] for e in entries]
    stack: list[tuple[int, str]] = []
    ptr = 0

    def apply(entry):
        level, title, _ = entry
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, re.sub(r"\s+", " ", title).strip()))

    out: list[SectionedBlock] = []
    for b in blocks:
        # Entries whose page is already behind us start at the top of their page; apply them now.
        while ptr < len(entries) and entries[ptr][2] < b.page:
            apply(entries[ptr])
            ptr += 1
        if b.kind != TABLE_KIND:
            lines = [ln for ln in b.text.split("\n") if ln.strip()][:4]
            for line in lines:
                j = ptr
                while j < len(entries) and entries[j][2] == b.page:
                    if _heading_matches(line, entries[j][1]):
                        break
                    j += 1
                if j < len(entries) and entries[j][2] == b.page:
                    for k in range(ptr, j + 1):
                        apply(entries[k])
                    ptr = j + 1
                else:
                    break
        out.append(SectionedBlock(b.page, b.text, b.kind, tuple(t for _, t in stack), b.ref))
    return out


_NUMBERED = re.compile(
    r"^(?P<num>(?:\d{1,2}|[A-Z])(?:\.\d{1,3}){0,7})\.?\s{1,6}(?P<title>[A-Z][^\n]{1,118})$"
)
_KEYWORD = re.compile(
    r"^(?P<kw>Chapter|Section|Appendix|Annex|Attachment|Part)\s+(?P<num>[A-Z0-9]{1,3}(?:\.\d{1,3})*)"
    r"\s*[.:\-–—]?\s*(?P<title>[^\n]{0,118})$",
    re.IGNORECASE,
)
_MD = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+)$")
_UNIT_START = re.compile(
    r"^(?:ns|ps|us|\u00b5s|ms|s|mV|V|mA|A|W|mW|kHz|MHz|GHz|Hz|kbps|Mbps|Gbps|Gb/s|Mb/s|GT/s|ohm|\u03a9|"
    r"pF|nF|uF|dB|mm|mil|bits?|bytes?|UI|%)\b"
)


def detect_heading(line: str, alone: bool) -> tuple[int, str] | None:
    """Return (level, heading text) when the line looks like a section heading."""
    line = line.strip()
    if not line or len(line) > 130:
        return None
    m = _MD.match(line)
    if m:
        return len(m.group("hashes")), m.group("title").strip()
    m = _KEYWORD.match(line)
    if m and alone and not line.endswith("."):
        num = m.group("num")
        return 1 + num.count("."), line
    m = _NUMBERED.match(line)
    if not m:
        return None
    num, title = m.group("num"), m.group("title").strip()
    parts = num.split(".")
    if parts[0].isalpha() and len(parts) == 1:
        return None  # "A Device shall ..." is a sentence, not "Appendix A"
    if title.endswith((".", ",", ";")) or len(title.split()) > 16:
        return None
    digits = sum(c.isdigit() for c in title)
    if digits > 0.3 * len(title):
        return None  # table rows like "3 100 200 ns"
    if len(parts) == 1:
        # Plain "3 Physical Layer": accept only when the line is its own block and the number is small.
        if not alone or int(parts[0]) > 40:
            return None
    if _UNIT_START.match(title):
        return None  # "3.3 V supply", "10 MHz clock"
    return len(parts), f"{num} {title}"


def _sections_from_headings(blocks: list[Block]) -> list[SectionedBlock]:
    stack: list[tuple[int, str]] = []
    out: list[SectionedBlock] = []
    for b in blocks:
        if b.kind != TABLE_KIND:
            first = b.text.split("\n", 1)[0]
            alone = "\n" not in b.text.strip()
            hit = detect_heading(first, alone)
            if hit:
                level, title = hit
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
        out.append(SectionedBlock(b.page, b.text, b.kind, tuple(t for _, t in stack), b.ref))
    return out


def assign_sections(doc: LoadedDocument, blocks: list[Block]) -> list[SectionedBlock]:
    toc = doc.toc
    # A chapter-only outline on a long document is too coarse; numbered headings do better then.
    if len(toc) >= 3 and (max(t[0] for t in toc) >= 2 or len(toc) >= doc.n_pages / 10):
        return _sections_from_toc(blocks, toc)
    return _sections_from_headings(blocks)


def section_label(section: tuple[str, ...], depth: int = 3) -> str:
    if not section:
        return ""
    return SECTION_SEP.join(section[-depth:])
