"""A local copy of a read-only shared library's index ("yerel kopya").

Searching an index on a network folder reads it page by page (4 KB at a time) over the network: every new question
asks for other words, so other parts of the keyword index, and a search took minutes instead of seconds. When the
shared library is opened read-only from a network folder, its index.sqlite is copied once to this computer
(<cache>/mirror/<id>/) and everything is searched there; the documents themselves stay on the network (a page is
read only when it is shown).

* The copy is a plain sequential file copy with large reads (the fast way over a network share), checked before it
  is used: the network file must not have changed while it was copied (else it is thrown away and taken again) and
  SQLite's quick_check must pass. A copy is never half-written: it becomes active only when complete.
* The network index is looked at once a minute (its version stamp, size and time - a few bytes): when the library
  was updated there, the new index is copied in the background and the app switches to it; until then the previous
  copy keeps answering. A question already running finishes on the copy it started with.
* Away from the network the copy keeps working (pages of documents cannot be shown then).
* Only for a library opened read-only: whoever writes the library uses the network file itself.
* paths.mirror: auto (network folders only) | on | off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from techrag.config import Config, user_cache_dir, write_text_atomic
from techrag.store import sqlite_ro_uri

log = logging.getLogger("techrag.mirror")

CHECK_SECONDS = 60
CHUNK = 8 * 1024 * 1024
STATE_FILE = "mirror.json"


def is_network(path: Path) -> bool:
    """A UNC path or a mapped network drive (Windows)."""
    s = str(path)
    if s.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    try:
        resolved = str(Path(path).resolve())
        if resolved.startswith("\\\\"):
            return True
        drive = os.path.splitdrive(resolved)[0]
        import ctypes

        return bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4   # DRIVE_REMOTE
    except Exception:
        return False


def wanted(cfg: Config) -> bool:
    mode = (cfg.paths.mirror or "auto").lower()
    if not cfg.read_only or mode == "off":
        return False
    return mode == "on" or is_network(cfg.library_dir)


def mirror_dir(cfg: Config) -> Path:
    base = Path(cfg.paths.cache_dir).expanduser() if cfg.paths.cache_dir else user_cache_dir()
    key = os.path.normcase(str(cfg.db_path.expanduser().resolve()))
    return base / "mirror" / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def source_identity(db: Path) -> str:
    """What says the network index changed: its version stamp (bumped by every change of the library), size and
    modification time. Raises OSError / sqlite3.Error when the network folder cannot be reached."""
    st = db.stat()
    con = sqlite3.connect(sqlite_ro_uri(db), uri=True, timeout=30)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='index_version'").fetchone()
    finally:
        con.close()
    return f"{row[0] if row else ''}|{st.st_size}|{st.st_mtime_ns}"


class Mirror:
    """The copy of one library's index and its state (for the UI)."""

    def __init__(self, cfg: Config):
        self.source = cfg.db_path.expanduser()
        self.dir = mirror_dir(cfg)
        self.lock = threading.Lock()
        self.status: dict = {"state": "network", "progress": 0.0, "size": 0, "synced_at": "", "error": "",
                             "path": ""}
        saved = self._saved()
        if saved and Path(saved.get("file", "")).is_file():
            self.status.update(state="ready", size=saved.get("size", 0), synced_at=saved.get("synced_at", ""),
                               path=saved["file"])

    def _saved(self) -> dict:
        try:
            data = json.loads((self.dir / STATE_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("source") == str(self.source) else {}
        except (OSError, ValueError):
            return {}

    def current(self) -> Optional[Path]:
        """The copy to use, if there is one (also when the network cannot be reached)."""
        p = self.status.get("path")
        return Path(p) if p and Path(p).is_file() else None

    def up_to_date(self) -> Optional[bool]:
        """True / False, or None when the network index cannot be read now."""
        try:
            ident = source_identity(self.source)
        except (OSError, sqlite3.Error) as exc:
            self.status.update(error=f"{exc.__class__.__name__}")
            if self.current():
                self.status["state"] = "offline"
            return None
        return bool(self.current()) and self._saved().get("identity") == ident

    def sync(self, progress: Optional[Callable[[float], None]] = None) -> Optional[Path]:
        """Copy the network index (in the background thread). The new copy's path, or None when it could not be
        taken now (the reason is in status; the previous copy stays in use)."""
        with self.lock:
            self.status.update(state="copying", progress=0.0, error="")
            self.dir.mkdir(parents=True, exist_ok=True)
            for old in self.dir.glob("*.part"):
                old.unlink(missing_ok=True)
            try:
                before = source_identity(self.source)
                size = self.source.stat().st_size
                free = shutil.disk_usage(self.dir).free
                if free < size * 1.1 + 200 * 1024 * 1024:
                    raise OSError(f"not enough disk space ({free // 2**20} MB free, {size // 2**20} MB needed)")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                part = self.dir / f"index-{stamp}.sqlite.part"
                done = 0
                with open(self.source, "rb") as src, open(part, "wb") as dst:
                    while chunk := src.read(CHUNK):
                        dst.write(chunk)
                        done += len(chunk)
                        self.status["progress"] = round(done / max(size, 1), 3)
                        if progress:
                            progress(self.status["progress"])
                if source_identity(self.source) != before or done != size:
                    part.unlink(missing_ok=True)
                    raise RuntimeError("the library changed while it was being copied; it is copied again shortly")
                con = sqlite3.connect(str(part))
                try:
                    ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                finally:
                    con.close()
                if not ok:
                    part.unlink(missing_ok=True)
                    raise RuntimeError("the copied index did not pass the integrity check")
                final = part.with_name(part.name[:-len(".part")])
                os.replace(part, final)
                synced = datetime.now(timezone.utc).isoformat(timespec="seconds")
                write_text_atomic(self.dir / STATE_FILE, json.dumps(
                    {"source": str(self.source), "identity": before, "file": str(final), "size": size,
                     "synced_at": synced}))
                self.status.update(state="ready", progress=1.0, size=size, synced_at=synced, path=str(final))
                return final
            except Exception as exc:
                self.status.update(state="error" if not self.current() else "stale",
                                   error=f"{exc.__class__.__name__}: {exc}"[:300])
                log.warning("library copy not taken: %s", self.status["error"])
                return None

    def cleanup(self) -> None:
        """Older copies (one in use by a running question may be locked on Windows: removed next time)."""
        keep = self.status.get("path")
        for f in self.dir.glob("index-*.sqlite"):
            if str(f) != keep:
                try:
                    f.unlink()
                except OSError:
                    pass


class MirrorManager:
    """Keeps the copy of the current library fresh and tells the app when a new copy is ready."""

    def __init__(self, on_ready: Callable[[Path], None], interval: float = CHECK_SECONDS):
        self.on_ready = on_ready
        self.interval = interval
        self.mirror: Optional[Mirror] = None
        self.cfg: Optional[Config] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def configure(self, cfg: Config) -> Optional[Path]:
        """For a (re)built engine: the local copy to open, or None (use the library itself)."""
        self.cfg = cfg
        if not wanted(cfg):
            self.mirror = None
            return None
        if self.mirror is None or self.mirror.source != cfg.db_path.expanduser():
            self.mirror = Mirror(cfg)
        self._wake.set()
        return self.mirror.current()

    def status(self) -> dict:
        if self.mirror is None:
            mode = self.cfg.paths.mirror if self.cfg else "auto"
            return {"enabled": False, "mode": mode, "state": "off"}
        return {"enabled": True, "mode": self.cfg.paths.mirror if self.cfg else "auto", **self.mirror.status}

    def check(self) -> bool:
        """One round: copy when the network index changed. True when the app switched to a new copy."""
        m = self.mirror
        if m is None:
            return False
        fresh = m.up_to_date()
        if fresh is None or fresh:
            if fresh and m.status.get("state") in ("offline", "stale"):
                m.status.update(state="ready", error="")
            return False
        path = m.sync()
        if path is None or m is not self.mirror:
            return False
        self.on_ready(path)
        m.cleanup()
        return True

    def sync_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="library-mirror", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:   # never let the loop die
                log.warning("library copy check failed: %s", exc)
            self._wake.wait(self.interval)
            self._wake.clear()
