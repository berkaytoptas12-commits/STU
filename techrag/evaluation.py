"""Gold-set evaluation. Retrieval and answers are measured separately, and the system's own verification
result is never reported as accuracy.

Item (YAML list):

    - id: ddr5-trfc-8gb
      status: draft              # draft | expert_verified - only expert-verified items make a benchmark
      question: "DDR5 8Gb için tRFC nedir?"
      answerable: true           # false: the loaded documents do not answer it; abstaining is correct
      document: "JESD79-5"       # substring of the expected source document's title or path
      revision: "A"              # optional
      pages: [6]                 # physical page index(es) (1-based) holding the answer
      facts:                     # each must be stated correctly in one statement of the answer
        - parameter: [tRFC, tRFC1]   # name/symbol (alternatives) that the statement names
          value: "295"
          unit: ns
          qualifier: min         # optional: min | typ | max
          conditions: [8Gb]      # optional: words the statement must contain
      keywords: [["8b/10b"]]     # optional non-numeric expectations (inner list = alternatives)

    Legacy items with only `expected: [...]` are still read; they get a weaker substring score that is
    reported separately ("legacy_contains_rate") and never as accuracy.

A statement such as "the limit is not 100 ns but 999 ns" does NOT count as stating 100 ns: a negation next to
the expected value, or another value with the same unit for the same parameter, makes the fact wrong.

Reported: recall@k / MRR (retrieval), answered and abstention rates, fact accuracy of answered questions,
wrong-answer rate, correct-abstention rate on unanswerable items, source/page/evidence-location accuracy,
and - separately, labelled as such - the internal verification pass rate.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from techrag.units import find_quantities, parse_number
from techrag.verify import CITATION

_NEGATION = re.compile(r"\b(?:not|değil|degil|instead of|yerine|rather than|incorrect|yanlış|yanlis|wrong|hatalı)\b",
                       re.I)
_CONDITION = re.compile(r"\b\d+\s?[GMK]b\b|\bDDR\d-\d+\b|\b[\w+]+-mode\b|\bGen\s?\d\b", re.I)
_QUALIFIERS = {"min": ("min", "minimum", "en az", "asgari"), "max": ("max", "maximum", "en fazla", "azami"),
               "typ": ("typ", "typical", "tipik", "nominal")}


@dataclass
class Fact:
    parameter: list[str]
    value: str
    unit: str = ""
    qualifier: str = ""
    conditions: list[str] = field(default_factory=list)


@dataclass
class EvalItem:
    id: str
    question: str
    answerable: bool = True
    status: str = "draft"
    document: Optional[str] = None
    revision: Optional[str] = None
    pages: list[int] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    keywords: list = field(default_factory=list)
    expected: list = field(default_factory=list)     # legacy substring expectations
    domain: Optional[str] = None

    @property
    def expected_doc(self) -> Optional[str]:
        return self.document

    @property
    def expected_pages(self) -> list[int]:
        return self.pages


@dataclass
class EvalResult:
    id: str
    question: str
    status: str
    answerable: bool
    first_hit_rank: Optional[int]            # 1-based rank of the first relevant retrieved passage
    answered: Optional[bool] = None          # the final answer contains at least one technical statement
    correct: Optional[bool] = None           # answerable: all facts/keywords right; unanswerable: abstained
    wrong: Optional[bool] = None             # answered, but wrong (or answered an unanswerable question)
    facts: list = field(default_factory=list)
    source_correct: Optional[bool] = None
    page_correct: Optional[bool] = None
    location_correct: Optional[bool] = None
    internal_pass_rate: Optional[float] = None
    verification_status: str = ""
    legacy_contains: Optional[bool] = None
    seconds: float = 0.0
    answer: str = ""
    sources: list = field(default_factory=list)
    error: Optional[str] = None


def _list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def load_items(path: str | Path) -> list[EvalItem]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or []
    items = []
    for i, r in enumerate(raw):
        facts = [Fact([str(x) for x in _list(f.get("parameter"))], str(f.get("value", "")), str(f.get("unit", "") or ""),
                      str(f.get("qualifier", "") or "").lower(), [str(x) for x in _list(f.get("conditions"))])
                 for f in r.get("facts") or []]
        pages = r.get("pages") or r.get("expected_pages") or ([r["expected_page"]] if r.get("expected_page") else [])
        exp = r.get("expected") or []
        items.append(EvalItem(
            id=str(r.get("id", i)), question=r["question"], answerable=bool(r.get("answerable", True)),
            status=str(r.get("status", "draft")), document=r.get("document") or r.get("expected_doc"),
            revision=r.get("revision"), pages=[int(p) for p in pages], facts=facts,
            keywords=_list(r.get("keywords")), expected=[exp] if isinstance(exp, str) else exp,
            domain=r.get("domain")))
    return items


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace(" ", " ").replace(" ", " "))


def contains_all(text: str, expected: list) -> bool:
    t = _norm(text)
    for entry in expected:
        alts = [_norm(str(a)) for a in (entry if isinstance(entry, list) else [entry])]
        alts += [a.replace(".", ",") for a in alts if re.search(r"\d\.\d", a)]
        if not any(a in t for a in alts):
            return False
    return True


def _mentions(text: str, names: list[str]) -> bool:
    low = text.lower()
    return any(re.search(r"(?<![\w])" + re.escape(n.lower()) + r"(?![\w])", low) for n in names if n)


def judge_fact(fact: Fact, statement: str) -> str:
    """'correct' | 'conflict' (the statement gives this parameter a different/negated value) | 'absent'."""
    t = CITATION.sub(" ", statement)
    if not _mentions(t, fact.parameter):
        return "absent"
    if fact.conditions and not all(_norm(c).replace(" ", "") in _norm(t).replace(" ", "") for c in fact.conditions):
        return "absent"
    exp = find_quantities(f"{fact.value} {fact.unit}") if fact.unit else []
    qs = find_quantities(t)
    if exp:
        hits = [q for q in qs if q.matches(exp[0])]
        others = [q for q in qs if q.base == exp[0].base and not q.matches(exp[0])]
    else:
        want = set(parse_number(fact.value))
        nums = re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])", t)
        hits = [n for n in nums if want & set(parse_number(n))]
        others = []
    if not hits:
        return "conflict" if (qs or re.search(r"\d", t)) else "absent"
    vtxt = re.escape(fact.value.replace(".", "<D>")).replace("<D>", "[.,]")
    for m in re.finditer(vtxt, t):
        window = t[max(0, m.start() - 30): m.end() + 30]
        if _NEGATION.search(window):
            return "conflict"
    if others and len(set(x.lower() for x in _CONDITION.findall(t))) < 2:
        return "conflict"
    if fact.qualifier:
        own = _QUALIFIERS.get(fact.qualifier, ())
        other = [w for k, ws in _QUALIFIERS.items() if k != fact.qualifier for w in ws]
        if not _mentions(t, list(own)) and _mentions(t, other):
            return "conflict"
    return "correct"


def is_hit(item: EvalItem, doc_text: str, page_start: int, page_end: int, text: str) -> bool:
    if item.document and item.document.lower() not in doc_text.lower():
        return False
    if item.pages:
        return any(page_start <= p <= page_end for p in item.pages)
    if item.facts:
        return contains_all(text, [f.value for f in item.facts])
    return contains_all(text, item.expected) if item.expected else bool(item.document)


def _doc_match(item: EvalItem, s: dict) -> bool:
    if not item.document:
        return False
    if item.document.lower() not in f"{s.get('doc_title', '')} {s.get('doc_path', '')}".lower():
        return False
    return not item.revision or str(s.get("revision", "")).lower() == str(item.revision).lower()


def score_answer(item: EvalItem, ans: dict) -> dict:
    v = ans.get("verification") or {}
    claims = [c for c in v.get("details", []) if c.get("outcome") in ("kept", "flagged")]
    answered = bool(claims)
    out: dict = {"answered": answered, "verification_status": v.get("status", "")}
    if v.get("claims"):
        out["internal_pass_rate"] = round(v.get("supported", 0) / v["claims"], 3)
    if not item.answerable:
        out.update(correct=not answered, wrong=answered)
        return out
    facts = []
    for f in item.facts:
        res = [judge_fact(f, c["text"]) for c in claims]
        verdict = "conflict" if "conflict" in res else ("correct" if "correct" in res else "absent")
        facts.append({"parameter": f.parameter[0] if f.parameter else "", "value": f.value, "unit": f.unit,
                      "result": verdict})
    text = "\n".join(c["text"] for c in claims)
    kw_ok = contains_all(text, item.keywords) if item.keywords else True
    has_expect = bool(item.facts or item.keywords)
    correct = answered and has_expect and kw_ok and all(f["result"] == "correct" for f in facts)
    out.update(facts=facts, correct=correct if has_expect else None,
               wrong=(answered and not correct) if has_expect else None)
    sources = [s for s in ans.get("sources", []) if s.get("cited")]
    if item.document:
        out["source_correct"] = any(_doc_match(item, s) for s in sources)
        if item.pages:
            out["page_correct"] = any(_doc_match(item, s) and any(s["page_start"] <= p <= s["page_end"]
                                                                  for p in item.pages) for s in sources)
            regions = [(r, c) for c in claims for e in c.get("evidence", []) for r in e.get("regions", [])]
            vals = [f.value for f in item.facts]
            out["location_correct"] = any(
                r["page"] in item.pages and (not vals or any(val in (r.get("quote") or "") for val in vals))
                for r, c in regions)
    if item.expected:
        out["legacy_contains"] = contains_all(ans.get("answer", ""), item.expected)
    return out


def run_eval(engine, items: list[EvalItem], retrieval_only: bool = False,
             progress: Optional[Callable[[str], None]] = None) -> dict:
    progress = progress or (lambda m: None)
    results: list[EvalResult] = []
    for n, item in enumerate(items, 1):
        t = time.time()
        try:
            rr = engine.retrieve(item.question)
            rank = next((i for i, p in enumerate(rr.passages, 1)
                         if is_hit(item, f"{p.doc_title} {p.doc_path}", p.page_start, p.page_end, p.text)), None)
            res = EvalResult(item.id, item.question, item.status, item.answerable, rank,
                             sources=[f"{p.doc_title} | {p.section} | p.{p.page_start}" for p in rr.passages])
            if not retrieval_only:
                ans = engine.ask(item.question)
                res.answer = ans.get("answer", "")
                for k, val in score_answer(item, ans).items():
                    setattr(res, k, val)
        except Exception as exc:
            res = EvalResult(item.id, item.question, item.status, item.answerable, None,
                             error=f"{exc.__class__.__name__}: {exc}")
        res.seconds = round(time.time() - t, 2)
        results.append(res)
        progress(_line(n, len(items), res))
    return {"summary": summarize(results, retrieval_only), "results": [asdict(r) for r in results],
            "timestamp": datetime.now().isoformat(timespec="seconds")}


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def summarize(results: list[EvalResult], retrieval_only: bool = False) -> dict:
    ok = [r for r in results if r.error is None]
    retr = [r for r in ok if r.answerable]

    def recall(k: int) -> Optional[float]:
        return _rate(sum(1 for r in retr if r.first_hit_rank and r.first_hit_rank <= k), len(retr))

    expert = sum(1 for r in results if r.status == "expert_verified")
    s: dict = {
        "n": len(results), "errors": len(results) - len(ok),
        "answerable": sum(1 for r in ok if r.answerable), "unanswerable": sum(1 for r in ok if not r.answerable),
        "expert_verified_items": expert,
        "benchmark": bool(results) and expert == len(results),
        "note": ("All items are expert-verified against the loaded documents." if results and expert == len(results)
                 else "Not a validated benchmark: draft items' expected values are not checked against your "
                      "documents by an expert, so these numbers are indicative only."),
        "recall@1": recall(1), "recall@3": recall(3), "recall@5": recall(5), "recall@8": recall(8),
        "mrr": round(sum(1 / r.first_hit_rank if r.first_hit_rank else 0.0 for r in retr) / len(retr), 3) if retr else None,
    }
    if retrieval_only:
        return s
    ans = [r for r in ok if r.answered is not None]
    answered = [r for r in ans if r.answered]
    a_ok = [r for r in ans if r.answerable and r.correct is not None]
    a_answered = [r for r in a_ok if r.answered]
    un = [r for r in ans if not r.answerable]
    s.update({
        "answered_rate": _rate(len(answered), len(ans)),
        "abstention_rate": _rate(len(ans) - len(answered), len(ans)),
        "fact_accuracy_of_answered": _rate(sum(1 for r in a_answered if r.correct), len(a_answered)),
        "fact_accuracy_of_all_answerable": _rate(sum(1 for r in a_ok if r.correct), len(a_ok)),
        "wrong_answer_rate_of_answered": _rate(sum(1 for r in answered if r.wrong), len(answered)),
        "wrong_answer_rate_of_all": _rate(sum(1 for r in ans if r.wrong), len(ans)),
        "correct_abstention_rate": _rate(sum(1 for r in un if not r.answered), len(un)),
        "unnecessary_abstention_rate": _rate(sum(1 for r in a_ok if not r.answered), len(a_ok)),
        "source_accuracy": _rate(sum(1 for r in a_answered if r.source_correct), sum(1 for r in a_answered if r.source_correct is not None)),
        "page_accuracy": _rate(sum(1 for r in a_answered if r.page_correct), sum(1 for r in a_answered if r.page_correct is not None)),
        "evidence_location_accuracy": _rate(sum(1 for r in a_answered if r.location_correct),
                                            sum(1 for r in a_answered if r.location_correct is not None)),
        "internal_verification_pass_rate": _mean([r.internal_pass_rate for r in ans]),
        "internal_verification_note": "share of statements the system's own checks accepted; not an independent "
                                      "accuracy or faithfulness measure",
        "legacy_contains_rate": _rate(sum(1 for r in ans if r.legacy_contains), sum(1 for r in ans if r.legacy_contains is not None)),
        "avg_seconds": round(sum(r.seconds for r in ok) / len(ok), 2) if ok else None,
    })
    return s


def _line(n: int, total: int, r: EvalResult) -> str:
    if r.error:
        return f"[{n}/{total}] {r.id:<14} ERROR {r.error}"
    if r.answered is None:
        return f"[{n}/{total}] {r.id:<14} hit@{r.first_hit_rank or '-'} {r.seconds:.1f}s"
    res = "-" if r.correct is None else ("OK" if r.correct else "WRONG" if r.wrong else "MISS")
    act = "answered" if r.answered else "abstained"
    return f"[{n}/{total}] {r.id:<14} hit@{r.first_hit_rank or '-'} {act:<9} {res:<5} {r.verification_status} {r.seconds:.1f}s"


def save_report(report: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
