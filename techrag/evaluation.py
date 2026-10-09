"""Gold-set evaluation: retrieval and generation are measured separately.

Item (YAML list):
    id: ddr-01
    question: "DDR4 SDRAM'in VDD besleme gerilimi nedir?"
    expected: [["1.2", "1,2"]]          # every entry must appear in the answer; inner lists = alternatives
    expected_doc: "JESD79-4"            # optional: substring of the source document title/path
    expected_pages: [21]                # optional: the page(s) that hold the answer

Retrieval: recall@k (k = 1, 3, 5, final_top_k) -- a passage is a hit when it comes from expected_doc and
covers an expected page (or, without pages, contains the expected values).
Generation: answer accuracy, faithfulness (supported / checked claims), removed-claim rate, not-found rate.
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


@dataclass
class EvalItem:
    id: str
    question: str
    expected: list = field(default_factory=list)
    expected_doc: Optional[str] = None
    expected_pages: list[int] = field(default_factory=list)
    domain: Optional[str] = None


@dataclass
class EvalResult:
    id: str
    question: str
    first_hit_rank: Optional[int]          # 1-based rank of the first relevant passage
    answer_correct: Optional[bool] = None
    faithfulness: Optional[float] = None
    removed: int = 0
    not_found: Optional[bool] = None
    seconds: float = 0.0
    answer: str = ""
    sources: list = field(default_factory=list)
    error: Optional[str] = None


def load_items(path: str | Path) -> list[EvalItem]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or []
    items = []
    for i, r in enumerate(raw):
        exp = r.get("expected") or []
        pages = r.get("expected_pages") or ([r["expected_page"]] if r.get("expected_page") else [])
        items.append(EvalItem(str(r.get("id", i)), r["question"], [exp] if isinstance(exp, str) else exp,
                              r.get("expected_doc"), [int(p) for p in pages], r.get("domain")))
    return items


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace(" ", " ").replace(" ", " "))


def contains_all(text: str, expected: list) -> bool:
    t = _norm(text)
    for entry in expected:
        alts = [_norm(str(a)) for a in (entry if isinstance(entry, list) else [entry])]
        alts += [a.replace(".", ",") for a in alts if re.search(r"\d\.\d", a)]
        if not any(a in t for a in alts):
            return False
    return True


def is_hit(item: EvalItem, doc_text: str, page_start: int, page_end: int, text: str) -> bool:
    if item.expected_doc and item.expected_doc.lower() not in doc_text.lower():
        return False
    if item.expected_pages:
        return any(page_start <= p <= page_end for p in item.expected_pages)
    return contains_all(text, item.expected) if item.expected else bool(item.expected_doc)


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
            res = EvalResult(item.id, item.question, rank,
                             sources=[f"{p.doc_title} | {p.section} | p.{p.page_start}" for p in rr.passages])
            if not retrieval_only:
                ans = engine.ask(item.question)
                v = ans.get("verification") or {}
                res.answer = ans.get("answer", "")
                res.answer_correct = contains_all(res.answer, item.expected) if item.expected else None
                res.faithfulness = (v["supported"] / v["checked"]) if v.get("checked") else None
                res.removed = len(v.get("removed", []))
                res.not_found = bool(v.get("not_found"))
        except Exception as exc:
            res = EvalResult(item.id, item.question, None, error=f"{exc.__class__.__name__}: {exc}")
        res.seconds = round(time.time() - t, 2)
        results.append(res)
        progress(_line(n, len(items), res))

    def recall(k: int) -> Optional[float]:
        rel = [r for r in results if r.error is None]
        return round(sum(1 for r in rel if r.first_hit_rank and r.first_hit_rank <= k) / len(rel), 3) if rel else None

    def mean(vals) -> Optional[float]:
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "n": len(results),
        "recall@1": recall(1), "recall@3": recall(3), "recall@5": recall(5), "recall@8": recall(8),
        "mrr": mean([1 / r.first_hit_rank if r.first_hit_rank else 0.0 for r in results if r.error is None]),
        "answer_accuracy": mean([float(r.answer_correct) for r in results if r.answer_correct is not None]),
        "faithfulness": mean([r.faithfulness for r in results]),
        "removed_claims_per_answer": mean([float(r.removed) for r in results if r.not_found is not None]),
        "not_found_rate": mean([float(r.not_found) for r in results if r.not_found is not None]),
        "errors": sum(1 for r in results if r.error),
        "avg_seconds": mean([r.seconds for r in results]),
    }
    return {"summary": summary, "results": [asdict(r) for r in results],
            "timestamp": datetime.now().isoformat(timespec="seconds")}


def _line(n: int, total: int, r: EvalResult) -> str:
    if r.error:
        return f"[{n}/{total}] {r.id:<12} ERROR {r.error}"
    ans = "-" if r.answer_correct is None else ("OK" if r.answer_correct else "XX")
    faith = "-" if r.faithfulness is None else f"{r.faithfulness:.2f}"
    return f"[{n}/{total}] {r.id:<12} hit@{r.first_hit_rank or '-'} ans={ans} faith={faith} {r.seconds:.1f}s"


def save_report(report: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
