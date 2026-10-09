"""Regression evaluation over a YAML question set.

Each item:
    id: i2c-01
    domain: i2c                     # optional, informational
    question: "I2C Fast-mode'da maksimum SCL frekansı nedir?"
    expected: ["400"]               # every entry must appear in the answer; an entry may be a list of
                                    # alternatives, e.g. [["400 kHz", "400 kbit/s"]]
    expected_doc: "UM10204"         # optional: substring of a source document title/path

Metrics: context recall (expected values present in retrieved passages), source hit (expected_doc
retrieved), answer accuracy (expected values present in the answer), grounding (verification ok).
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

from techrag.engine import RAGEngine


@dataclass
class EvalItem:
    id: str
    question: str
    expected: list = field(default_factory=list)
    expected_doc: Optional[str] = None
    domain: Optional[str] = None


@dataclass
class EvalResult:
    id: str
    question: str
    context_recall: Optional[bool]
    source_hit: Optional[bool]
    answer_correct: Optional[bool]
    grounded: Optional[bool]
    not_found: Optional[bool]
    seconds: float
    answer: str = ""
    sources: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    error: Optional[str] = None


def load_items(path: str | Path) -> list[EvalItem]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or []
    items = []
    for i, r in enumerate(raw):
        exp = r.get("expected") or []
        if isinstance(exp, str):
            exp = [exp]
        items.append(EvalItem(id=str(r.get("id", i)), question=r["question"], expected=exp,
                              expected_doc=r.get("expected_doc"), domain=r.get("domain")))
    return items


def _norm(s: str) -> str:
    s = s.lower().replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", s)


def _contains(text: str, expected: list) -> bool:
    t = _norm(text)
    for entry in expected:
        alts = entry if isinstance(entry, list) else [entry]
        alts = [_norm(str(a)) for a in alts]
        # Accept Turkish decimal commas for numeric alternatives ("1,2 V" == "1.2 V").
        alts += [a.replace(".", ",") for a in alts if re.search(r"\d\.\d", a)]
        if not any(a in t for a in alts):
            return False
    return True


def run_eval(engine: RAGEngine, items: list[EvalItem], retrieval_only: bool = False,
             progress: Optional[Callable[[str], None]] = None) -> dict:
    progress = progress or (lambda m: None)
    results: list[EvalResult] = []
    for n, item in enumerate(items, 1):
        t = time.time()
        try:
            if retrieval_only:
                rr = engine.retrieve(item.question)
                passages, answer, verification = rr.passages, "", None
            else:
                ans = engine.ask(item.question)
                passages, answer, verification = ans.passages, ans.answer, ans.verification
            context = "\n".join(p.text for p in passages)
            docs = " ".join(f"{p.doc_title} {p.doc_path}" for p in passages).lower()
            res = EvalResult(
                id=item.id, question=item.question,
                context_recall=_contains(context, item.expected) if item.expected else None,
                source_hit=(item.expected_doc.lower() in docs) if item.expected_doc else None,
                answer_correct=None if retrieval_only or not item.expected else _contains(answer, item.expected),
                grounded=None if verification is None else verification.status == "ok",
                not_found=None if verification is None else verification.not_found,
                seconds=round(time.time() - t, 2), answer=answer,
                sources=[f"[{p.number}] {p.doc_title} | {p.section} | p.{p.page_start}" for p in passages],
                problems=verification.problems() if verification else [],
            )
        except Exception as exc:
            res = EvalResult(item.id, item.question, None, None, None, None, None,
                             round(time.time() - t, 2), error=f"{exc.__class__.__name__}: {exc}")
        results.append(res)
        progress(_line(n, len(items), res))

    def rate(attr: str) -> Optional[float]:
        vals = [getattr(r, attr) for r in results if getattr(r, attr) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "n": len(results),
        "context_recall": rate("context_recall"),
        "source_hit": rate("source_hit"),
        "answer_accuracy": rate("answer_correct"),
        "grounded_rate": rate("grounded"),
        "not_found_rate": rate("not_found"),
        "errors": sum(1 for r in results if r.error),
        "avg_seconds": round(sum(r.seconds for r in results) / max(len(results), 1), 2),
    }
    return {"summary": summary, "results": [asdict(r) for r in results],
            "timestamp": datetime.now().isoformat(timespec="seconds")}


def _flag(v: Optional[bool]) -> str:
    return "-" if v is None else ("OK" if v else "XX")


def _line(n: int, total: int, r: EvalResult) -> str:
    if r.error:
        return f"[{n}/{total}] {r.id:<14} ERROR {r.error}"
    return (f"[{n}/{total}] {r.id:<14} ctx={_flag(r.context_recall)} doc={_flag(r.source_hit)} "
            f"ans={_flag(r.answer_correct)} grounded={_flag(r.grounded)} {r.seconds:.1f}s")


def save_report(report: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
