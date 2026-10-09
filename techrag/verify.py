"""Answer verification.

1. Split the answer into claims (sentences, bullets, table rows) and note whether each sits in the
   "documented" part or the "engineering inference" part.
2. Deterministic check (no LLM): citations exist; every number+unit matches (after SI normalisation) a
   quantity in the CITED sources; bare numbers occur in the cited sources. Inference claims may use any
   source or calculation.
3. Entailment judge (LLM, fresh context: claim + cited source text only) for documented claims that
   passed step 2.
4. Failing claims are regenerated once by the caller, then stripped or flagged here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.prompts import NOT_FOUND, HEADINGS
from techrag.units import find_quantities

CITATION = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
_NUMBER = re.compile(r"(?<![\w.,])(0x[0-9a-fA-F]+|\d+(?:[.,]\d+)*)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=\S)")
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|figs?|nos?|approx|min|max|typ|vs|cf|sec|eq|rev|ref|vol|pp|ch|etc|incl|resp|"
                     r"ca|örn|vb|bkz|yy|max|nom)\.|(?<![\w.])(?<!\d )[A-Za-z]\.)$", re.I)


def split_sentences(text: str) -> list[str]:
    """Sentence split that tolerates lowercase symbol starts (tRFC, nCK) and common abbreviations."""
    out: list[str] = []
    for part in _SENT_SPLIT.split(text):
        if out and _ABBREV.search(out[-1]):
            out[-1] += " " + part
        else:
            out.append(part)
    return out
FLAG = "⚠"


@dataclass
class Claim:
    id: int
    text: str
    line: int
    section: str              # documented | inference | lead
    citations: list[int] = field(default_factory=list)
    status: str = "pending"   # ok | fail | skipped
    reasons: list[str] = field(default_factory=list)
    judge: str = ""

    @property
    def effective_section(self) -> str:
        """A cited lead sentence is checked like documented text; an uncited one like inference."""
        if self.section == "lead":
            return "documented" if self.citations else "inference"
        return self.section

    @property
    def checkable(self) -> bool:
        return self.effective_section != "inference" or bool(find_quantities(self.text) or _bare_numbers(self.text))

    def to_dict(self) -> dict:
        return asdict(self)


def parse_citations(text: str) -> list[int]:
    nums: list[int] = []
    for m in CITATION.finditer(text):
        for part in re.split(r"\s*,\s*", m.group(1)):
            r = re.match(r"^(\d+)\s*[–-]\s*(\d+)$", part)
            if r and 0 < int(r.group(2)) - int(r.group(1)) < 50:
                nums.extend(range(int(r.group(1)), int(r.group(2)) + 1))
            elif part.strip().isdigit():
                nums.append(int(part))
    return nums


def _bare_numbers(text: str) -> list[str]:
    text = CITATION.sub(" ", text)
    text = _LIST_MARKER.sub(" ", text)
    out = []
    for m in _NUMBER.finditer(text):
        n = m.group(1).rstrip(".,")
        if n and not (n.isdigit() and len(n) == 1):
            out.append(n)
    return out


def is_not_found(answer: str) -> bool:
    norm = re.sub(r"\s+", " ", answer).lower()
    return any(p.lower().rstrip(".") in norm for p in NOT_FOUND.values())


def _is_heading(line: str, names: Sequence[str]) -> Optional[str]:
    s = line.strip().lstrip("#").strip().strip("*").strip().rstrip(":").lower()
    for n in names:
        if s.startswith(n.lower()[:20]):
            return n
    return None


def split_claims(answer: str) -> list[Claim]:
    doc_heads = [h[0] for h in HEADINGS.values()] + ["documented", "kaynaklarda"]
    inf_heads = [h[1] for h in HEADINGS.values()] + ["engineering inference", "mühendislik yorumu"]
    claims: list[Claim] = []
    section = "lead"
    table_cites: list[int] = []
    for i, line in enumerate(answer.split("\n")):
        s = line.strip()
        if not s:
            table_cites = []
            continue
        if s.startswith("#") or (s.startswith("**") and s.endswith("**")):
            if _is_heading(s, inf_heads):
                section = "inference"
            elif _is_heading(s, doc_heads):
                section = "documented"
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            cites = parse_citations(s)
            if not cites and not (find_quantities(s) or _bare_numbers(s)):
                table_cites = table_cites or []
                continue  # header row
            claims.append(Claim(len(claims) + 1, s, i, section if section != "lead" else "documented",
                                cites or list(table_cites)))
            if cites:
                table_cites = cites
            continue
        body = _LIST_MARKER.sub("", s)
        for sent in split_sentences(body):
            sent = sent.strip()
            if len(sent.split()) < 3 and not find_quantities(sent):
                continue
            if sent.endswith(":") and not CITATION.search(sent):
                continue
            claims.append(Claim(len(claims) + 1, sent, i, section, parse_citations(sent)))
    return claims


def _value_present(num: str, hay: str) -> bool:
    variants = {num}
    if "," in num and "." not in num:
        variants.add(num.replace(",", "."))
    if "." in num and "," not in num:
        variants.add(num.replace(".", ","))
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", num):
        variants.add(re.sub(r"[.,]", "", num))
    for v in variants:
        if re.search(r"(?<!\d)(?<!\d[.,])" + re.escape(v) + r"(?!\d)(?![.,]\d)", hay, re.I):
            return True
    return False


def deterministic_check(claims: list[Claim], registry, question: str = "") -> None:
    """Sets status/reasons on each claim (in place). registry: tools.SourceRegistry."""
    all_text = registry.all_text() + "\n" + question
    all_qty = find_quantities(all_text)
    for c in claims:
        c.reasons = []
        if not c.checkable:
            c.status = "skipped"
            continue
        invalid = [n for n in c.citations if registry.get(n) is None]
        if invalid:
            c.reasons.append("cites non-existent source(s) " + ", ".join(f"[{n}]" for n in invalid))
        valid = [n for n in c.citations if registry.get(n) is not None]
        has_facts = bool(find_quantities(c.text) or _bare_numbers(c.text)) or len(c.text.split()) >= 6
        sec = c.effective_section
        if sec != "inference" and not valid and has_facts:
            c.reasons.append("no citation")
        if sec == "inference":
            hay, qty_pool = all_text, all_qty
        else:
            hay = registry.text_of(valid) + "\n" + question
            qty_pool = find_quantities(hay)
        for q in find_quantities(CITATION.sub(" ", c.text)):
            if not any(q.matches(s) for s in qty_pool):
                where = ""
                if sec != "inference" and any(q.matches(s) for s in all_qty):
                    where = " (it appears in another source: wrong citation)"
                c.reasons.append(f"value '{q.text.strip()}' not found in the cited sources{where}")
        qty_numbers = {q.number.lstrip("±+") for q in find_quantities(c.text)}
        for n in _bare_numbers(c.text):
            if n in qty_numbers or n.lstrip("-") in qty_numbers:
                continue
            if not _value_present(n, hay):
                c.reasons.append(f"number '{n}' not found in the cited sources")
        c.status = "fail" if c.reasons else "ok"


@dataclass
class Verification:
    claims: list[Claim] = field(default_factory=list)
    not_found: bool = False
    regenerated: bool = False
    removed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    judge_used: bool = False

    @property
    def failing(self) -> list[Claim]:
        return [c for c in self.claims if c.status == "fail"]

    @property
    def status(self) -> str:
        if self.not_found and not [c for c in self.claims if c.section != "lead" and c.status == "ok"]:
            return "not_found"
        if self.failing or self.flagged:
            return "warning"
        if self.removed:
            return "corrected"
        return "ok"

    def summary(self) -> dict:
        checked = [c for c in self.claims if c.status in ("ok", "fail")]
        return {"status": self.status, "claims": len(self.claims), "checked": len(checked),
                "supported": sum(c.status == "ok" for c in checked), "failed": len(self.failing),
                "removed": self.removed, "flagged": self.flagged, "regenerated": self.regenerated,
                "judge_used": self.judge_used, "not_found": self.not_found,
                "details": [c.to_dict() for c in self.claims]}

    def problems_text(self) -> str:
        return "\n".join(f"- \"{c.text}\" -> {'; '.join(c.reasons)}" for c in self.failing)


def apply_failures(answer: str, claims: list[Claim], mode: str = "strip") -> tuple[str, list[str]]:
    """Remove (or flag) failing claims in the answer text. Returns (new answer, affected claim texts)."""
    failing = [c for c in claims if c.status == "fail"]
    if not failing:
        return answer, []
    lines = answer.split("\n")
    affected = []
    for c in failing:
        line = lines[c.line] if c.line < len(lines) and lines[c.line] is not None else ""
        if not line or c.text not in line:
            continue
        affected.append(c.text)
        if mode == "flag":
            lines[c.line] = line.replace(c.text, f"{c.text} {FLAG}", 1)
            continue
        if line.strip().startswith("|"):
            lines[c.line] = None  # type: ignore[call-overload]
            continue
        new = line.replace(c.text, "", 1)
        if not _LIST_MARKER.sub("", new).strip(" .;,"):
            lines[c.line] = None  # type: ignore[call-overload]
        else:
            lines[c.line] = re.sub(r"\s{2,}", " ", new).rstrip()
    out = "\n".join(l for l in lines if l is not None)
    return re.sub(r"\n{3,}", "\n\n", out).strip(), affected
