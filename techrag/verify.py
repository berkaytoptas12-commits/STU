"""Answer verification.

Every line of an answer is split into FRAMING (headings, greetings, lead-ins ending in ':', the not-found
sentence) and TECHNICAL claims (sentences, bullets, table rows). Technical claims are checked wherever they
appear - before or after a heading, cited or not, with or without numbers:

1. Deterministic (no LLM): the claim cites existing sources, and every number+unit and bare number in it is
   found (unit-normalised) in the evidence text of what it cites: documents, a calculation whose inputs are
   traceable to sources, or the user's own input. The user's question is never evidence for a documented
   value, and a user value may not be presented as one.
2. Entailment judge (LLM, fresh context: claim + cited evidence only). Only an explicit, well-formed
   "supported" verdict for exactly that claim id counts. Errors, timeouts, malformed JSON, missing or
   duplicate ids and unknown verdicts leave the claim UNVERIFIED - never approved.
3. The final answer is rebuilt from the parsed claims: only supported claims (plus framing) remain;
   unsupported and unverified claims are removed, or flagged when the user asks for that.

Claim status: supported | unsupported | unverified | not_checked (verification or the judge switched off).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

from techrag.prompts import HEADINGS, NOT_FOUND
from techrag.units import find_quantities

SUPPORTED, UNSUPPORTED, UNVERIFIED, NOT_CHECKED = "supported", "unsupported", "unverified", "not_checked"
VERDICTS = ("supported", "partial", "unsupported")
FLAG = "⚠"

CITATION = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
_NUMBER = re.compile(r"(?<![\w.,])(0x[0-9a-fA-F]+|\d+(?:[.,]\d+)*)")
_LIST_MARKER = re.compile(r"^(\s*(?:[-*•]|\d+[.)])\s+)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=\S)")
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|figs?|nos?|approx|min|max|typ|vs|cf|sec|eq|rev|ref|vol|pp|ch|etc|incl|resp|"
                     r"ca|örn|vb|bkz|yy|max|nom)\.|(?<![\w.])(?<!\d )[A-Za-z]\.)$", re.I)
_GREETING = re.compile(r"^(?:merhaba|selam|hello|hi|sure|tabii|elbette|thanks|thank you|teşekkür\w*|umarım|"
                       r"i hope|hope this|kolay gelsin)\b", re.I)
_LEADIN = re.compile(r"^(?:here (?:is|are)|below|the following|aşağıda(?:ki)?|kısaca|özetle|in short|in summary|"
                     r"summary|özet|sonuç olarak|cevap|answer|details|ayrıntılar|kaynaklar|sources)\b", re.I)
# Words that mark a value as the user's own (an assumption/input), not something a standard states.
_USER_MARK = re.compile(r"\b(?:user|you|your|given|assum\w*|suppos\w*|if|for example|e\.g\.|kullanıcı\w*|"
                        r"verdiğiniz|verilen|sizin|siz\w*|varsay\w*|örneğin|eğer|belirttiğiniz|sorudaki|"
                        r"soruda\w*)\b", re.I)


def split_sentences(text: str) -> list[str]:
    """Sentence split that tolerates lowercase symbol starts (tRFC, nCK) and common abbreviations."""
    out: list[str] = []
    for part in _SENT_SPLIT.split(text):
        if out and _ABBREV.search(out[-1]):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


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


def is_framing(sentence: str) -> bool:
    """True for text that states nothing technical (and so needs no evidence)."""
    s = CITATION.sub("", sentence).strip(" *_:-–")
    if not s or is_not_found(s) or sentence.lstrip().startswith("#"):
        return True
    if CITATION.search(sentence) or find_quantities(s) or _bare_numbers(s):
        return False
    words = s.split()
    if sentence.rstrip(" *").endswith(":") or len(words) < 3:
        return True
    return bool((_GREETING.match(s) and len(words) <= 12) or (_LEADIN.match(s) and len(words) <= 8))


# ------------------------------------------------------------------------------------ parsing

@dataclass
class Claim:
    id: int
    text: str
    line: int
    section: str                    # lead | documented | inference (where it was written)
    citations: list[int] = field(default_factory=list)
    kind: str = "fact"              # fact | inference
    status: str = "pending"         # supported | unsupported | unverified | not_checked
    reasons: list[str] = field(default_factory=list)
    deterministic: str = "not_run"  # pass | fail | not_run
    judge: dict = field(default_factory=lambda: {"required": False, "called": False, "completed": False,
                                                 "verdict": "", "reason": "", "error": ""})
    values: list[dict] = field(default_factory=list)   # provenance of each value: document | calculation | user
    evidence: list[dict] = field(default_factory=list)  # locations, filled by the engine
    outcome: str = ""               # kept | flagged | removed

    @property
    def uses_user_input(self) -> bool:
        return any(v["from"] == "user" for v in self.values)

    @property
    def uses_calculation(self) -> bool:
        return any(v["from"] == "calculation" for v in self.values)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(uses_user_input=self.uses_user_input, uses_calculation=self.uses_calculation)
        return d


@dataclass
class Segment:
    text: str
    claim: Optional[int] = None     # claim id; None = framing


@dataclass
class Line:
    kind: str                       # blank | heading | fence | code | table_sep | table_head | table_row | text
    raw: str
    prefix: str = ""
    segments: list[Segment] = field(default_factory=list)


@dataclass
class ParsedAnswer:
    lines: list[Line]
    claims: list[Claim]

    def claim(self, cid: int) -> Optional[Claim]:
        return next((c for c in self.claims if c.id == cid), None)


def _heading_name(line: str, names: Sequence[str]) -> bool:
    s = line.strip().lstrip("#").strip().strip("*").strip().rstrip(":").lower()
    return any(s.startswith(n.lower()[:20]) for n in names)


_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def parse_answer(answer: str) -> ParsedAnswer:
    doc_heads = [h[0] for h in HEADINGS.values()] + ["documented", "kaynaklarda"]
    inf_heads = [h[1] for h in HEADINGS.values()] + ["engineering inference", "mühendislik yorumu"]
    raw_lines = answer.split("\n")
    claims: list[Claim] = []
    lines: list[Line] = []
    section = "lead"
    in_fence = False

    def add_claim(text: str, i: int) -> int:
        kind = "inference" if section == "inference" else "fact"
        claims.append(Claim(len(claims) + 1, text, i, section, parse_citations(text), kind))
        return claims[-1].id

    for i, line in enumerate(raw_lines):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            lines.append(Line("fence", line))
            continue
        if in_fence:
            lines.append(Line("code", line, segments=[Segment(line, add_claim(s, i) if s else None)]))
            continue
        if not s:
            lines.append(Line("blank", line))
            continue
        if s.startswith("#") or (s.startswith("**") and s.endswith("**") and len(s) > 4 and not CITATION.search(s)):
            if _heading_name(s, inf_heads):
                section = "inference"
            elif _heading_name(s, doc_heads):
                section = "documented"
            lines.append(Line("heading", line))
            continue
        if s.startswith("|"):
            if _SEP.match(s):
                lines.append(Line("table_sep", line))
                continue
            nxt = raw_lines[i + 1].strip() if i + 1 < len(raw_lines) else ""
            if _SEP.match(nxt):
                lines.append(Line("table_head", line))
                continue
            cid = None if is_framing(s.replace("|", " ")) else add_claim(s, i)
            lines.append(Line("table_row", line, segments=[Segment(line, cid)]))
            continue
        m = _LIST_MARKER.match(line)
        prefix = m.group(1) if m else re.match(r"^\s*", line).group(0)
        body = line[len(prefix):]
        segs = []
        for sent in split_sentences(body.strip()):
            sent = sent.strip()
            if sent:
                segs.append(Segment(sent, None if is_framing(sent) else add_claim(sent, i)))
        lines.append(Line("text", line, prefix, segs))
    return ParsedAnswer(lines, claims)


def split_claims(answer: str) -> list[Claim]:
    return parse_answer(answer).claims


# ------------------------------------------------------------------------- deterministic check

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


def _evidence(src) -> str:
    return f"{src.header()}\n{src.evidence()}"


def deterministic_check(claims: list[Claim], registry) -> None:
    """Sets status/reasons/values on each claim in place (registry: tools.SourceRegistry). Only what a claim
    CITES counts as its evidence; the user's question is a separate 'user input' source."""
    pools_all = {s.n: _evidence(s) for s in registry.items}
    qty_all = {n: find_quantities(t) for n, t in pools_all.items()}
    for c in claims:
        c.reasons, c.values = [], []
        unverifiable = False
        invalid = [n for n in c.citations if registry.get(n) is None]
        if invalid:
            c.reasons.append("cites non-existent source(s) " + ", ".join(f"[{n}]" for n in invalid))
        cited = [registry.get(n) for n in dict.fromkeys(c.citations) if registry.get(n) is not None]
        if not cited:
            c.reasons.append("no citation: a technical statement must cite the source it comes from"
                             if c.kind == "fact" else
                             "no citation: an engineering inference must cite the sources its premises come from")
        docs = [s for s in cited if s.kind not in ("calc", "user")]
        good_docs = [s for s in docs if s.verified]
        weak_docs = [s for s in docs if not s.verified]
        calcs = [s for s in cited if s.kind == "calc"]
        users = [s for s in cited if s.kind == "user"]
        if docs and not good_docs and not calcs and not users:
            c.reasons.append("cites only unverified extraction(s) " + ", ".join(f"[{s.n}]" for s in weak_docs)
                             + " - not usable as evidence")
            unverifiable = True
        for s in calcs:
            if not s.extra.get("traceable"):
                bad = [i["value"] for i in s.extra.get("inputs", []) if not i.get("sources")]
                c.reasons.append(f"calculation [{s.n}] uses input(s) not found in any source: {', '.join(bad) or '?'}")
        pools = [("document", good_docs), ("calculation", calcs), ("user", users)]
        text = CITATION.sub(" ", c.text)
        quantities = find_quantities(text)
        for q in quantities:
            origin = None
            for name, srcs in pools:
                hit = [s.n for s in srcs if any(q.matches(x) for x in qty_all[s.n])]
                if hit:
                    origin = {"text": q.text.strip(), "from": name, "sources": hit}
                    break
            if origin:
                c.values.append(origin)
                continue
            if any(any(q.matches(x) for x in qty_all[s.n]) for s in weak_docs):
                c.reasons.append(f"value '{q.text.strip()}' appears only in an unverified extraction")
                unverifiable = True
                continue
            if any(s.evidence_text and any(q.matches(x) for x in find_quantities(s.text)) for s in good_docs):
                c.reasons.append(f"value '{q.text.strip()}' appears only in a page-image transcription "
                                 "(unverified extraction), not in the page's text layer")
                unverifiable = True
                continue
            elsewhere = [n for n, qs in qty_all.items() if n not in c.citations and any(q.matches(x) for x in qs)]
            note = f" (it appears in [{elsewhere[0]}]: wrong citation)" if elsewhere else ""
            c.reasons.append(f"value '{q.text.strip()}' not found in the cited sources{note}")
        qty_numbers = {q.number.lstrip("±+-") for q in quantities}
        for num in _bare_numbers(text):
            if num.lstrip("-") in qty_numbers:
                continue
            origin = None
            for name, srcs in pools:
                hit = [s.n for s in srcs if _value_present(num, pools_all[s.n])]
                if hit:
                    origin = {"text": num, "from": name, "sources": hit}
                    break
            if origin:
                c.values.append(origin)
            elif any(_value_present(num, pools_all[s.n]) for s in weak_docs) or \
                    any(s.evidence_text and _value_present(num, s.text) for s in good_docs):
                c.reasons.append(f"number '{num}' appears only in an unverified extraction")
                unverifiable = True
            else:
                c.reasons.append(f"number '{num}' not found in the cited sources")
        if c.kind == "fact" and any(v["from"] == "user" for v in c.values) and not _USER_MARK.search(c.text):
            c.reasons.append("a value from the user's question is presented as documented; say it is the "
                             "user's input/assumption")
        c.deterministic = "fail" if c.reasons else "pass"
        if c.reasons:
            # Only-unverifiable evidence is "cannot be verified", not "contradicted".
            only_unverifiable = unverifiable and all(
                ("unverified extraction" in r) for r in c.reasons)
            c.status = UNVERIFIED if only_unverifiable else UNSUPPORTED
        else:
            c.status = "pending"


# ----------------------------------------------------------------------------------- judge

def parse_verdicts(obj, ids: Sequence[int]) -> dict[int, tuple[str, str]]:
    """{claim id: (verdict, reason)}. verdict is one of VERDICTS, or 'missing' / 'invalid' when the judge's
    output does not give exactly one recognised verdict for that id."""
    if not isinstance(obj, dict) or not isinstance(obj.get("verdicts"), list):
        return {i: ("invalid", "judge output is not {\"verdicts\": [...]}") for i in ids}
    seen: dict[int, tuple[str, str]] = {}
    dup: set[int] = set()
    wanted = set(ids)
    for v in obj["verdicts"]:
        if not isinstance(v, dict):
            continue
        raw = v.get("id")
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            cid = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            cid = int(raw.strip())
        else:
            continue
        if cid not in wanted:
            continue
        if cid in seen:
            dup.add(cid)
        seen[cid] = (str(v.get("verdict", "")).strip().lower(), str(v.get("reason", "") or "").strip())
    out = {}
    for i in ids:
        if i in dup:
            out[i] = ("invalid", "several verdicts for this claim")
        elif i not in seen:
            out[i] = ("missing", "no verdict for this claim")
        elif seen[i][0] not in VERDICTS:
            out[i] = ("invalid", f"unrecognised verdict '{seen[i][0]}'")
        else:
            out[i] = seen[i]
    return out


def apply_verdict(c: Claim, verdict: str, reason: str) -> None:
    c.judge.update(called=True, verdict=verdict, reason=reason)
    if verdict == "supported":
        c.judge.update(completed=True, error="")
        c.status = SUPPORTED
    elif verdict in ("partial", "unsupported"):
        c.judge.update(completed=True, error="")
        c.status = UNSUPPORTED
        c.reasons.append(f"judge: {verdict}" + (f" - {reason}" if reason else ""))
    else:
        c.judge.update(completed=False, error=reason or verdict)
        c.status = UNVERIFIED


def judge_failed(c: Claim, error: str) -> None:
    c.judge.update(called=True, completed=False, verdict="", error=error)
    c.status = UNVERIFIED


# ------------------------------------------------------------------------------ verification

@dataclass
class Verification:
    claims: list[Claim] = field(default_factory=list)
    not_found: bool = False
    regenerated: bool = False
    removed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    enabled: bool = True             # deterministic checks ran
    judge_enabled: bool = True
    judge_called: bool = False       # at least one judge request was sent
    judge_completed: bool = False    # every claim that needed the judge got a valid verdict
    judge_errors: list[str] = field(default_factory=list)
    parse_failed: bool = False       # an answer with no checkable statements
    clarify: bool = False
    status: str = "pending"

    @property
    def failing(self) -> list[Claim]:
        return [c for c in self.claims if c.status == UNSUPPORTED]

    def count(self, status: str) -> int:
        return sum(1 for c in self.claims if c.status == status)

    def summary(self) -> dict:
        return {
            "status": self.status, "claims": len(self.claims),
            "supported": self.count(SUPPORTED), "unsupported": self.count(UNSUPPORTED),
            "unverified": self.count(UNVERIFIED), "not_checked": self.count(NOT_CHECKED),
            "kept": sum(1 for c in self.claims if c.outcome in ("kept", "flagged")),
            "removed": self.removed, "flagged": self.flagged, "regenerated": self.regenerated,
            "not_found": self.not_found, "parse_failed": self.parse_failed, "clarify": self.clarify,
            "deterministic": {"enabled": self.enabled},
            "judge": {"enabled": self.judge_enabled, "called": self.judge_called,
                      "completed": self.judge_completed, "errors": self.judge_errors[:5]},
            "details": [c.to_dict() for c in self.claims],
        }

    def problems_text(self) -> str:
        return "\n".join(f"- \"{c.text}\" -> {'; '.join(c.reasons)}" for c in self.failing)


def render(parsed: ParsedAnswer, decide: Callable[[Claim], str]) -> str:
    """Rebuild the answer from its parsed lines. decide(claim) -> 'keep' | 'flag' | 'drop'. Lines whose
    claims are all dropped disappear, then empty tables, lead-ins and headings."""
    out: list[tuple[str, str]] = []  # (kind, text)
    for ln in parsed.lines:
        if ln.kind in ("blank", "heading", "fence", "table_sep", "table_head"):
            out.append((ln.kind, ln.raw))
            continue
        has_claim = any(s.claim is not None for s in ln.segments)
        parts, kept = [], False
        for seg in ln.segments:
            c = parsed.claim(seg.claim) if seg.claim is not None else None
            if c is None:
                parts.append(seg.text)
                continue
            d = decide(c)
            if d == "keep":
                parts.append(seg.text)
                kept = True
            elif d == "flag":
                parts.append(f"{seg.text} {FLAG}" if ln.kind != "table_row" else seg.text.rstrip().rstrip("|")
                             + f" {FLAG} |")
                kept = True
        if has_claim and not kept:
            continue
        if ln.kind in ("table_row", "code"):
            out.append((ln.kind, parts[0] if parts else ln.raw))
        else:
            out.append(("text", (ln.prefix + " ".join(parts)).rstrip()))
    out = _drop_empty_structures(out)
    text = "\n".join(t for _, t in out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _drop_empty_structures(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    def next_item(i: int) -> Optional[tuple[str, str]]:
        return next(((k, t) for k, t in items[i + 1:] if k != "blank"), None)

    def level(t: str) -> int:
        return (len(t) - len(t.lstrip().lstrip("#"))) if t.lstrip().startswith("#") else 9

    changed = True
    while changed:
        changed = False
        res: list[tuple[str, str]] = []
        skip = set()
        for i, (kind, text) in enumerate(items):
            if i in skip:
                continue
            nxt = next_item(i)
            drop = False
            if kind == "table_head":
                after = [k for k, _ in items[i + 1:] if k != "blank"][:2]
                drop = after[:2] != ["table_sep", "table_row"]
            elif kind == "table_sep":
                prev = [k for k, _ in res if k != "blank"]
                drop = not prev or prev[-1] != "table_head"
            elif kind == "fence" and nxt and nxt[0] == "fence":
                skip.add(next(j for j in range(i + 1, len(items)) if items[j][0] == "fence"))
                drop = True
            elif kind == "text" and text.rstrip(" *").endswith(":"):
                drop = nxt is None or nxt[0] == "heading" or (nxt[0] == "text" and nxt[1].rstrip(" *").endswith(":"))
            elif kind == "heading":
                drop = nxt is None or (nxt[0] == "heading" and level(nxt[1]) <= level(text))
            if drop:
                changed = True
                continue
            res.append((kind, text))
        items = res
    return items
