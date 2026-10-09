"""Post-hoc answer checks: citation validity, citation coverage and numeric grounding.

Wrong numbers are the most damaging failure for standards questions (a timing, a voltage, a bit
position). Every number in the answer is looked up in the retrieved sources; values that do not
appear there are reported so the user (or a self-correction pass) can catch them.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Sequence

from techrag.prompts import NOT_FOUND

CITATION = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
_NUMBER = re.compile(r"(?<![\w.,])(0x[0-9a-fA-F]+|\d+(?:[.,]\d+)*)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]\s*)?\d+[.)]\s+", re.M)
_THOUSANDS = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")


@dataclass
class Verification:
    citations_used: list[int] = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    uncited_sentences: int = 0
    checked_sentences: int = 0
    not_found: bool = False

    @property
    def needs_fix(self) -> bool:
        return bool(self.invalid_citations or self.unsupported_numbers)

    @property
    def status(self) -> str:
        if self.not_found:
            return "not_found"
        if self.needs_fix:
            return "warning"
        if self.checked_sentences and not self.citations_used:
            return "warning"
        return "ok"

    def problems(self) -> list[str]:
        out = []
        if self.invalid_citations:
            out.append("citations to non-existent sources: " + ", ".join(f"[{n}]" for n in self.invalid_citations))
        if self.unsupported_numbers:
            out.append("values not found in any source: " + ", ".join(self.unsupported_numbers))
        if self.checked_sentences and not self.citations_used:
            out.append("the answer has no [n] citations")
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status
        d["problems"] = self.problems()
        return d


def parse_citations(text: str) -> list[int]:
    nums: list[int] = []
    for m in CITATION.finditer(text):
        for part in re.split(r"\s*,\s*", m.group(1)):
            if re.match(r"^\d+\s*[–-]\s*\d+$", part):
                a, b = (int(x) for x in re.split(r"\s*[–-]\s*", part))
                if 0 < b - a < 50:
                    nums.extend(range(a, b + 1))
                    continue
            if part.strip().isdigit():
                nums.append(int(part))
    return nums


def _variants(num: str) -> set[str]:
    out = {num}
    if _THOUSANDS.match(num):
        out.add(re.sub(r"[.,]", "", num))
    if "," in num and "." not in num:
        out.add(num.replace(",", "."))  # Turkish decimal comma
    if "." in num and "," not in num:
        out.add(num.replace(".", ","))
    return out


def _present(num: str, haystack: str) -> bool:
    for v in _variants(num):
        pat = r"(?<!\d)(?<!\d[.,])" + re.escape(v) + r"(?!\d)(?![.,]\d)"
        if re.search(pat, haystack, flags=re.I):
            return True
    return False


def extract_numbers(answer: str) -> list[str]:
    text = CITATION.sub(" ", answer)
    text = _LIST_MARKER.sub(" ", text)
    found = []
    for m in _NUMBER.finditer(text):
        n = m.group(1).rstrip(".,")
        if not n:
            continue
        if n.isdigit() and len(n) == 1:
            continue  # small counts ("2 lanes") are too often spelled differently to check reliably
        found.append(n)
    return list(dict.fromkeys(found))


def verify_answer(answer: str, passages: Sequence, question: str = "") -> Verification:
    v = Verification()
    norm = re.sub(r"\s+", " ", answer).strip().lower()
    v.not_found = any(phrase.lower().rstrip(".") in norm for phrase in NOT_FOUND.values())

    cited = parse_citations(answer)
    valid = {p.number for p in passages}
    v.citations_used = sorted(set(n for n in cited if n in valid))
    v.invalid_citations = sorted(set(n for n in cited if n not in valid))

    haystack = "\n".join(
        f"{p.doc_title}\n{p.section}\n{p.page_start}-{p.page_end}\n{p.text}" for p in passages
    ) + "\n" + question
    v.unsupported_numbers = [n for n in extract_numbers(answer) if not _present(n, haystack)]

    for line in answer.split("\n"):
        s = line.strip()
        if not s or s.startswith(("|", "#")) or s.endswith(":"):
            continue
        for sent in re.split(r"(?<=[.!?])\s+", s):
            if len(sent.split()) < 8:
                continue
            v.checked_sentences += 1
            if not CITATION.search(sent):
                v.uncited_sentences += 1
    return v
