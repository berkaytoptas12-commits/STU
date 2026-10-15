"""Question history: the questions asked in the chat and the answers shown, kept per user in the user data folder
(never in the library) so they can be read again in the Evaluation window.

A record holds the question, the final answer as shown, its verification status and counts, the statements
verification removed (with their reason codes), the cited sources (document title, revision, section, pages)
and the time taken - no source texts, library path, server address or key. At most MAX_ITEMS records are
kept (the oldest go first). Switched off with answer.keep_history.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from techrag import __version__
from techrag.config import write_text_atomic

MAX_ITEMS = 500
_lock = threading.Lock()


def _page(v) -> Optional[int]:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def record_from_final(question: str, final: dict) -> dict:
    """The history record of one answered question (the engine's 'final' event)."""
    v = final.get("verification") or {}
    sources = []
    for s in final.get("sources") or []:
        if not s.get("cited"):
            continue
        sources.append({"n": s.get("n"), "kind": str(s.get("kind") or "")[:20],
                        "doc_title": str(s.get("doc_title") or "")[:300],
                        "revision": str(s.get("revision") or "")[:40],
                        "section": str(s.get("section") or "").split(" > ")[-1][:200],
                        "page_start": _page(s.get("page_start")), "page_end": _page(s.get("page_end")),
                        "page_label": str(s.get("page_label") or "")[:20]})
    removed = [{"text": str(c.get("text") or "")[:1000], "status": c.get("status", ""),
                "reasons": [str(r)[:300] for r in (c.get("reasons") or [])][:6],
                "codes": [k for k in (c.get("codes") or []) if isinstance(k, dict)][:6]}
               for c in v.get("details") or [] if c.get("outcome") == "removed"][:20]
    return {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": str(question or "")[:4000],
        "merged_question": str(final.get("merged_question") or "")[:4000],
        "answer": str(final.get("answer") or "")[:30000],
        "notice": str(final.get("notice") or "")[:1000],
        "status": v.get("status", ""),
        "counts": {k: v.get(k) for k in ("claims", "supported", "unsupported", "unverified", "kept")
                   if v.get(k) is not None},
        "regenerated": bool(v.get("regenerated")), "citation_fixes": v.get("citation_fixes", 0),
        "judge_completed": bool((v.get("judge") or {}).get("completed")),
        "removed": removed, "sources": sources,
        "scope": [str(e) for e in ((final.get("scope") or {}).get("entities") or [])][:10],
        "seconds": (final.get("timings") or {}).get("total"),
        "app_version": __version__,
    }


class HistoryStore:
    def __init__(self, folder: Path):
        self.path = Path(folder) / "history.json"

    def items(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.path, json.dumps(items, ensure_ascii=False, indent=1))

    def add(self, record: dict) -> dict:
        with _lock:
            items = self.items()
            items.append(record)
            self._save(items[-MAX_ITEMS:])
        return record

    def delete(self, hid: str) -> bool:
        with _lock:
            items = self.items()
            kept = [d for d in items if d.get("id") != hid]
            if len(kept) == len(items):
                return False
            self._save(kept)
        return True

    def clear(self) -> int:
        with _lock:
            n = len(self.items())
            if n:
                self._save([])
        return n

    def search(self, query: str = "", limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """Newest first; query matches the question, the answer and the cited document titles."""
        items = list(reversed(self.items()))
        q = (query or "").strip().lower()
        if q:
            items = [d for d in items
                     if q in d.get("question", "").lower() or q in d.get("answer", "").lower()
                     or any(q in s.get("doc_title", "").lower() for s in d.get("sources", []))]
        return items[offset:offset + limit], len(items)
