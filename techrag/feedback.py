"""Answer feedback: 👍 / 👎 with an optional correction (correct value, unit, document, page), kept per user in the
user data folder - never in the library, so it also works with a shared read-only library.

Each record holds the question, the answer as shown, its verification status and the cited sources (document
title, file name, revision, pages): no server address, API key or library path. Feedback can be exported as
candidate evaluation items (status: candidate) - they become a benchmark only after an engineer checks them
against the documents.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from techrag import __version__
from techrag.config import write_text_atomic
from techrag.units import find_quantities
from techrag.verify import CITATION

RATINGS = ("up", "down")
_LIMITS = {"question": 4000, "answer": 20000, "value": 200, "unit": 40, "parameter": 200, "document": 300,
           "note": 2000}
_lock = threading.Lock()


def _text(v, limit: int) -> str:
    return str(v or "").strip()[:limit]


def _page(v) -> Optional[int]:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def _source(s: dict) -> dict:
    return {"n": s.get("n"), "doc_title": _text(s.get("doc_title"), 300),
            "file": Path(str(s.get("doc_path") or "")).name[:300], "revision": _text(s.get("revision"), 40),
            "page_start": _page(s.get("page_start")), "page_end": _page(s.get("page_end")),
            "kind": _text(s.get("kind"), 20)}


class FeedbackStore:
    def __init__(self, folder: Path):
        self.path = Path(folder) / "feedback.json"

    def items(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.path, json.dumps(items, ensure_ascii=False, indent=1))

    def add(self, data: dict) -> dict:
        rating = str(data.get("rating") or "")
        if rating not in RATINGS:
            raise ValueError("rating must be 'up' or 'down'")
        question = _text(data.get("question"), _LIMITS["question"])
        if not question:
            raise ValueError("question is required")
        corr = data.get("correction") or {}
        record = {
            "id": uuid.uuid4().hex[:12],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rating": rating, "question": question,
            "answer": _text(data.get("answer"), _LIMITS["answer"]),
            "verification_status": _text(data.get("verification_status"), 40),
            "sources": [_source(s) for s in (data.get("sources") or []) if isinstance(s, dict)][:30],
            "correction": {k: _text(corr.get(k), _LIMITS[k]) for k in ("value", "unit", "parameter", "document",
                                                                          "note")},
            "unanswerable": bool(data.get("unanswerable")),
            "app_version": __version__,
        }
        record["correction"]["page"] = _page(corr.get("page"))
        with _lock:
            items = self.items()
            items.append(record)
            self._save(items)
        return record

    def delete(self, fid: str) -> bool:
        with _lock:
            items = self.items()
            kept = [d for d in items if d.get("id") != fid]
            if len(kept) == len(items):
                return False
            self._save(kept)
        return True

    def summary(self) -> dict:
        items = self.items()
        return {"total": len(items), "up": sum(1 for d in items if d.get("rating") == "up"),
                "down": sum(1 for d in items if d.get("rating") == "down"),
                "with_correction": sum(1 for d in items if (d.get("correction") or {}).get("value")),
                "candidates": len(candidate_items(items))}


# ------------------------------------------------------------------------------ export

def _answer_values(answer: str) -> list[str]:
    """The values with units an approved answer states (they become the item's expected keywords)."""
    seen = []
    for q in find_quantities(CITATION.sub(" ", answer or "")):
        t = re.sub(r"\s+", " ", q.text.strip())
        if t not in seen:
            seen.append(t)
    return seen[:6]


def candidate_item(d: dict) -> Optional[dict]:
    """One feedback record as an evaluation item (status: candidate), or None when it says nothing checkable."""
    corr = d.get("correction") or {}
    item: dict = {"id": f"fb-{d.get('id', '')}", "status": "candidate", "question": d.get("question", ""),
                  "source": "feedback", "rating": d.get("rating")}
    cited = [s for s in d.get("sources") or [] if s.get("doc_title") or s.get("file")]
    if d.get("unanswerable"):
        item["answerable"] = False
        return item
    value = (corr.get("value") or "").strip()
    if value:
        if corr.get("parameter"):
            fact = {"parameter": [corr["parameter"]], "value": value}
            if corr.get("unit"):
                fact["unit"] = corr["unit"]
            item["facts"] = [fact]
        else:
            item["keywords"] = [f"{value} {corr['unit']}".strip() if corr.get("unit") else value]
    elif d.get("rating") == "up":
        values = _answer_values(d.get("answer", ""))
        if not values:
            return None
        item["keywords"] = values
    else:
        return None     # a 👎 without the correct value: nothing to check against
    doc = (corr.get("document") or "").strip()
    if not doc and d.get("rating") == "up" and cited:
        doc = cited[0].get("doc_title") or cited[0].get("file") or ""
    if doc:
        item["document"] = doc
    if corr.get("page"):
        item["pages"] = [int(corr["page"])]
    elif d.get("rating") == "up" and cited and cited[0].get("page_start"):
        first = cited[0]
        item["pages"] = list(range(first["page_start"], (first.get("page_end") or first["page_start"]) + 1))[:5]
    if corr.get("note"):
        item["note"] = corr["note"]
    return item


def candidate_items(items: list[dict]) -> list[dict]:
    return [c for c in (candidate_item(d) for d in items) if c]


def export_yaml(items: list[dict]) -> str:
    cands = candidate_items(items)
    head = ("# Candidate evaluation items from answer feedback - NOT a validated benchmark.\n"
            "# An engineer should check each one against the document (revision, physical page, value, unit,\n"
            "# conditions) and set `status: expert_verified` before it counts as a benchmark item.\n")
    return head + (yaml.safe_dump(cands, allow_unicode=True, sort_keys=False, width=110) if cands else "[]\n")
