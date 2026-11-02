"""Chat history: each user's conversations, kept on their own computer.

One JSON file per conversation in ``<user settings folder>/chats`` plus a small index for the list. A conversation is
what the page needs to show it again: every question with its final answer event (answer, citations, sources,
verification and the evidence locations for the PDF highlights). Never written into the (shared) library or the
usage log; deleted one by one or all at once from the "Sohbetler" tab.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from techrag.config import write_text_atomic

log = logging.getLogger("techrag.chats")

_ID = re.compile(r"^[0-9a-f]{8,40}$")
_LOCK = threading.Lock()
MAX_TURNS = 500


def valid_id(cid: str) -> bool:
    return bool(_ID.match(cid or ""))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")   # orders two saves in one second


class ChatStore:
    def __init__(self, folder: Path):
        self.dir = Path(folder)

    @property
    def _index_path(self) -> Path:
        return self.dir / "index.json"

    def _path(self, cid: str) -> Path:
        if not valid_id(cid):
            raise ValueError("invalid chat id")
        return self.dir / f"{cid}.json"

    @staticmethod
    def _entry(chat: dict) -> dict:
        return {"id": chat["id"], "title": chat.get("title", ""), "created": chat.get("created", ""),
                "updated": chat.get("updated", ""), "turns": len(chat.get("turns") or [])}

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict) and valid_id(e.get("id", ""))]
        except (OSError, ValueError):
            pass
        return self._rebuild_index()

    def _rebuild_index(self) -> list[dict]:
        """The list from the conversation files themselves (no index yet, or an unreadable one)."""
        out = []
        for p in self.dir.glob("*.json") if self.dir.is_dir() else ():
            if p.name == "index.json" or not valid_id(p.stem):
                continue
            try:
                chat = json.loads(p.read_text(encoding="utf-8"))
                out.append(self._entry(dict(chat, id=p.stem)))
            except (OSError, ValueError):
                log.warning("unreadable chat file %s skipped", p.name)
        return out

    def _write_index(self, entries: list[dict]) -> None:
        entries.sort(key=lambda e: e.get("updated", ""), reverse=True)
        write_text_atomic(self._index_path, json.dumps(entries, ensure_ascii=False))

    def list(self) -> list[dict]:
        with _LOCK:
            return sorted(self._read_index(), key=lambda e: e.get("updated", ""), reverse=True)

    def get(self, cid: str) -> Optional[dict]:
        try:
            return json.loads(self._path(cid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, cid: str, chat: dict) -> dict:
        turns = chat.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("a conversation needs at least one question")
        if len(turns) > MAX_TURNS:
            raise ValueError(f"at most {MAX_TURNS} questions per conversation")
        out = {"id": cid, "title": str(chat.get("title") or "")[:200], "created": str(chat.get("created") or _now()),
               "updated": _now(), "turns": turns}
        path = self._path(cid)
        with _LOCK:
            self.dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps(out, ensure_ascii=False))
            entries = [e for e in self._read_index() if e["id"] != cid]
            entries.append(self._entry(out))
            self._write_index(entries)
        return self._entry(out)

    def delete(self, cid: str) -> bool:
        path = self._path(cid)
        with _LOCK:
            existed = path.exists()
            path.unlink(missing_ok=True)
            entries = self._read_index()
            if any(e["id"] == cid for e in entries) or existed:
                self._write_index([e for e in entries if e["id"] != cid])
        return existed

    def clear(self) -> int:
        with _LOCK:
            n = 0
            for p in self.dir.glob("*.json") if self.dir.is_dir() else ():
                if p.name != "index.json" and valid_id(p.stem):
                    p.unlink(missing_ok=True)
                    n += 1
            if self.dir.is_dir():
                self._write_index([])
        return n
