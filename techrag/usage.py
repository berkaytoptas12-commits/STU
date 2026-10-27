"""Usage log: who asked what and how many tokens it took.

One CSV file per user (``<log folder>/<username>.csv``) in the folder the admin chose - a network folder is fine:
every computer only ever appends to its own user's file, so several users never write the same file. The files are
for Excel: UTF-8 with BOM, ';' between fields. A line per question: time, Windows user name, computer, the question,
how it ended, duration and token counts. The answer text is not written. When the folder cannot be written (a
network share offline) the line goes to a local file in the user's settings folder instead, and the admin sees the
error.
"""

from __future__ import annotations

import csv
import getpass
import io
import logging
import os
import re
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("techrag.usage")

COLUMNS = ["zaman", "kullanici", "bilgisayar", "soru", "sonuc", "dogrulama", "sure_sn", "istem_token",
           "uretilen_token", "dusunme_token", "toplam_token", "model_cagrisi", "token_tahmini", "model"]
DELIMITER = ";"
_LOCK = threading.Lock()


def current_user() -> str:
    """The Windows (or OS) user name of whoever runs the app."""
    try:
        return getpass.getuser() or "bilinmiyor"
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "bilinmiyor"


def current_machine() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or ""


def _file_name(user: str) -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", user).strip(" .") or "kullanici") + ".csv"


def _row_text(row: dict) -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter=DELIMITER, lineterminator="\r\n").writerow([row.get(c, "") for c in COLUMNS])
    return buf.getvalue()


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("﻿" + _row_text({c: c for c in COLUMNS}))
        fh.write(text)


class UsageLog:
    def __init__(self, log_dir: Path, fallback_dir: Path, user: Optional[str] = None,
                 machine: Optional[str] = None):
        self.dir = Path(log_dir)
        self.fallback_dir = Path(fallback_dir)
        self.user = user or current_user()
        self.machine = machine if machine is not None else current_machine()
        self.last_error = ""

    @property
    def path(self) -> Path:
        return self.dir / _file_name(self.user)

    def record(self, question: str, outcome: str, verification: str = "", seconds: float = 0.0,
               tokens: Optional[dict] = None, model: str = "", when: Optional[datetime] = None) -> Path:
        """Append one question. Never raises: the answer must not fail because of the log."""
        t = tokens or {}
        row = {"zaman": (when or datetime.now().astimezone()).isoformat(timespec="seconds"), "kullanici": self.user,
               "bilgisayar": self.machine, "soru": " ".join(str(question).split()), "sonuc": outcome,
               "dogrulama": verification, "sure_sn": int(round(seconds)), "istem_token": t.get("prompt", 0),
               "uretilen_token": t.get("completion", 0), "dusunme_token": t.get("reasoning", 0),
               "toplam_token": t.get("prompt", 0) + t.get("completion", 0), "model_cagrisi": t.get("calls", 0),
               "token_tahmini": "evet" if t.get("estimated") else "hayir", "model": model}
        text = _row_text(row)
        with _LOCK:
            try:
                _append(self.path, text)
                self.last_error = ""
                return self.path
            except OSError as exc:
                self.last_error = f"{exc.__class__.__name__}: {self.dir}"
                log.warning("usage log folder not writable (%s); writing locally", exc.__class__.__name__)
                fallback = self.fallback_dir / _file_name(self.user)
                try:
                    _append(fallback, text)
                except OSError:
                    pass
                return fallback

    def summary(self, max_bytes: int = 20_000_000) -> dict:
        """Per user: questions, tokens and the last question time, read from every CSV in the folder."""
        users: list[dict] = []
        errors: list[str] = []
        try:
            files = sorted(p for p in self.dir.glob("*.csv") if p.is_file())
        except OSError as exc:
            return {"users": [], "errors": [f"{exc.__class__.__name__}: {self.dir}"]}
        for p in files:
            try:
                if p.stat().st_size > max_bytes:
                    errors.append(f"{p.name}: too large to summarize")
                    continue
                with open(p, encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.DictReader(fh, delimiter=DELIMITER))
            except (OSError, csv.Error, UnicodeDecodeError) as exc:
                errors.append(f"{p.name}: {exc.__class__.__name__}")
                continue

            def num(r: dict, k: str) -> int:
                try:
                    return int(r.get(k) or 0)
                except ValueError:
                    return 0

            users.append({"user": rows[-1].get("kullanici") or p.stem if rows else p.stem, "file": p.name,
                          "questions": len(rows), "tokens": sum(num(r, "toplam_token") for r in rows),
                          "completion_tokens": sum(num(r, "uretilen_token") for r in rows),
                          "last": rows[-1].get("zaman", "") if rows else ""})
        users.sort(key=lambda u: u["last"], reverse=True)
        return {"users": users, "errors": errors}


def token_totals(calls_summary: dict) -> dict:
    """askctx.AskContext.summary() -> totals for one question."""
    out = {"prompt": 0, "completion": 0, "reasoning": 0, "calls": 0, "estimated": False}
    for s in (calls_summary or {}).values():
        out["prompt"] += s.get("prompt_tokens", 0)
        out["completion"] += s.get("completion_tokens", 0)
        out["reasoning"] += s.get("reasoning_tokens", 0)
        out["calls"] += s.get("calls", 0)
        out["estimated"] = out["estimated"] or bool(s.get("estimated"))
    return out
