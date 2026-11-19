"""Usage log: who asked what and how many tokens it took.

One CSV file per user (``<log folder>/<username>.csv``) in the folder the admin chose - a network folder is fine:
every computer only ever appends to its own user's file, so several users never write the same file. The files are
for Excel: UTF-8 with BOM, ';' between fields. A line per question: time, Windows user name, computer, the question,
how it ended, duration and the tokens it took. The answer text is not written. When the folder cannot be written (a
network share offline) the line goes to a local file in the user's settings folder instead, and the admin sees the
error.

The tokens of a question (toplam_token) are what the models generated for it, thinking included: the same total as
under the answer's "Süre" breakdown. Up to 0.15.5 a line also held the prompt, generated and thinking tokens and
toplam_token was prompt + generated. Such a file is converted once, the next time its user asks: a copy is kept next
to it (<name>.csv.yedek - not *.csv, so it is not counted again) and the file is rewritten in one step, each old
line's toplam_token becoming its generated tokens. While the file is open elsewhere (Excel) the conversion waits:
the line is added in the file's old layout and the conversion is tried again with the next question.
"""

from __future__ import annotations

import csv
import getpass
import io
import logging
import os
import re
import shutil
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("techrag.usage")

COLUMNS = ["zaman", "kullanici", "bilgisayar", "soru", "sonuc", "dogrulama", "sure_sn", "toplam_token",
           "model_cagrisi", "token_tahmini", "model"]
# The layout up to 0.15.5 (toplam_token = prompt + generated there); its generated tokens are this layout's total.
OLD_COLUMNS = ["zaman", "kullanici", "bilgisayar", "soru", "sonuc", "dogrulama", "sure_sn", "istem_token",
               "uretilen_token", "dusunme_token", "toplam_token", "model_cagrisi", "token_tahmini", "model"]
BACKUP_SUFFIX = ".yedek"
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


def _row_text(row: dict, columns: list[str] = COLUMNS) -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter=DELIMITER, lineterminator="\r\n").writerow([row.get(c, "") for c in columns])
    return buf.getvalue()


def _header(path: Path) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh, delimiter=DELIMITER), [])


def _row_tokens(row: dict) -> int:
    """The tokens a question took (generated, thinking included), in either layout."""
    for key in ("uretilen_token", "toplam_token") if "uretilen_token" in row else ("toplam_token",):
        try:
            return int(row.get(key) or 0)
        except ValueError:
            continue
    return 0


def _convert(path: Path) -> bool:
    """Rewrite a file of the old layout in the current one, keeping a copy next to it. False (file left as it was)
    when it cannot be done now, e.g. the file is open in Excel."""
    tmp = path.with_name(path.name + ".tmp")
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():   # an earlier copy is never overwritten
        backup = path.with_name(f"{path.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{BACKUP_SUFFIX}")
    made: list[Path] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=DELIMITER))
        text = "\ufeff" + _row_text({c: c for c in COLUMNS}) + "".join(
            _row_text(dict(r, toplam_token=_row_tokens(r))) for r in rows)
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            made.append(tmp)
            fh.write(text)
        shutil.copy2(path, backup)
        made.append(backup)
        os.replace(tmp, path)
        log.info("usage log %s converted to the current layout (copy: %s)", path.name, backup.name)
        return True
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        log.warning("usage log %s not converted yet (%s); trying again with the next question", path.name,
                    exc.__class__.__name__)
        for leftover in made:   # this attempt's copy and temporary file: the next attempt makes them again
            try:
                leftover.unlink()
            except OSError:
                pass
        return False


def _append(path: Path, row: dict, old_row: dict) -> None:
    """Add a question's line. A file of the old layout is converted first; when it cannot be now, the line goes in
    the file's old layout (old_row), so its columns never shift."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    columns, values = COLUMNS, row
    if not new and _header(path) == OLD_COLUMNS and not _convert(path):
        columns, values = OLD_COLUMNS, old_row
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("\ufeff" + _row_text({c: c for c in COLUMNS}))
        fh.write(_row_text(values, columns))


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
               "dogrulama": verification, "sure_sn": int(round(seconds)), "toplam_token": t.get("completion", 0),
               "model_cagrisi": t.get("calls", 0), "token_tahmini": "evet" if t.get("estimated") else "hayir",
               "model": model}
        # the same line in the layout up to 0.15.5, for a file that cannot be converted yet
        old_row = dict(row, istem_token=t.get("prompt", 0), uretilen_token=t.get("completion", 0),
                       dusunme_token=t.get("reasoning", 0), toplam_token=t.get("prompt", 0) + t.get("completion", 0))
        with _LOCK:
            try:
                _append(self.path, row, old_row)
                self.last_error = ""
                return self.path
            except OSError as exc:
                self.last_error = f"{exc.__class__.__name__}: {self.dir}"
                log.warning("usage log folder not writable (%s); writing locally", exc.__class__.__name__)
                fallback = self.fallback_dir / _file_name(self.user)
                try:
                    _append(fallback, row, old_row)
                except OSError:
                    pass
                return fallback

    def summary(self, max_bytes: int = 20_000_000) -> dict:
        """Per user: questions, tokens (generated, thinking included - as under "Süre"), the average per question
        and the last question time, read from every CSV in the folder (either layout)."""
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

            tokens = sum(_row_tokens(r) for r in rows)
            users.append({"user": rows[-1].get("kullanici") or p.stem if rows else p.stem, "file": p.name,
                          "questions": len(rows), "tokens": tokens,
                          "avg_tokens": round(tokens / len(rows)) if rows else 0,
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
