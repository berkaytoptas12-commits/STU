"""Organization settings ("kurum ayarları"): the defaults every user of the app gets, kept in one file on a shared
folder (usually the shared library's folder), so that the exe can be handed to others without setting it up again.

* Saved from Admin › Ayarlar › "Kurum ayarlarını kaydet": the current model servers and models, answering and
  advanced settings, connection security, the shared library folder (and whether others open it read-only) and the
  usage log folder go into <folder>/kurum-ayarlari.yaml. Never the API key (each user enters it once; it is stored
  encrypted for that user) and never personal folders.
* Found, in this order: the TECHRAG_ORG_SETTINGS environment variable (IT can set it), a one-line pointer file next
  to the exe (SayisalTasarimUzmani.kurum.txt, written when the settings are saved, handed out with the exe), or the
  folder the user gave when the app asked once at first start.
* Applied as defaults: built-in defaults < organization settings < the user's own settings < environment. Nothing is
  locked; a user's own settings file keeps only what differs from the organization settings, so a later change of
  the organization file reaches everyone who did not change that setting themselves.
* The newest decision wins: a user's own value is kept with the organization's value at the time it was chosen.
  When the organization file later sets that setting to something else, the organization's new value replaces the
  user's (the user can change it again afterwards). Own values saved before this was recorded (older versions):
  when the user's settings file was written after the organization file was last saved, they are kept; otherwise
  the organization's value applies. How the user opens the library (read-only or not) is always kept. See
  techrag.settings.follow_org.
* When the folder cannot be reached (a laptop away from the network) the copy read last time is used.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from techrag.config import Config, write_text_atomic

log = logging.getLogger("techrag.org")

ORG_FILE = "kurum-ayarlari.yaml"
POINTER_FILE = "SayisalTasarimUzmani.kurum.txt"
CACHE_FILE = "kurum-ayarlari.cache.yaml"

# What the organization file holds: whole sections, minus the keys listed (secrets and per-user paths).
EXPORT = {"llm": ("api_key",), "llm_fast": (), "vision": ("api_key",), "embedding": ("api_key",),
          "reranker": ("api_key",), "answer": (), "retrieval": (), "tls": (), "usage": ()}
EXPORT_PATHS = ("library_dir",)

STATUS: dict = {"path": "", "source": "", "loaded": False, "cached": False, "error": "", "values": {},
                "replaced": [], "mtime": None}


def _reset() -> None:
    STATUS.update(path="", source="", loaded=False, cached=False, error="", values={}, replaced=[], mtime=None)


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def sets(section: str, key: str) -> bool:
    """Whether the organization file can set this setting at all."""
    if section == "read_only":
        return True
    if section == "paths":
        return key in EXPORT_PATHS
    return section in EXPORT and key not in EXPORT[section]


def setting_name(section: str, key: str) -> str:
    return "read_only" if section == "read_only" else f"{section}.{key}"


def same(section: str, key: str, a: Any, b: Any) -> bool:
    """Whether two values of a setting are the same; the library folder compared as a path (the same folder written
    differently, e.g. with a trailing separator or in other letter case on Windows)."""
    if a == b:
        return True
    if section == "paths" and key == "library_dir" and a and b:
        norm = [os.path.normcase(os.path.normpath(str(x).strip())) for x in (a, b)]
        if norm[0] == norm[1]:
            return True
        from techrag.config import same_path

        return same_path(str(a).strip(), str(b).strip())
    return False


def pointer_path() -> Optional[Path]:
    """The pointer file next to the exe (TECHRAG_ORG_POINTER overrides it, for tests and odd installs)."""
    override = os.environ.get("TECHRAG_ORG_POINTER")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / POINTER_FILE
    return None   # running from source: no exe to sit next to


def org_file(where: str | Path) -> Path:
    """A folder (the file inside it) or the file itself."""
    p = Path(str(where).strip().strip('"')).expanduser()
    return p if p.suffix.lower() in (".yaml", ".yml") else p / ORG_FILE


def locate(user: dict) -> tuple[Optional[Path], str]:
    env = os.environ.get("TECHRAG_ORG_SETTINGS", "").strip()
    if env:
        return org_file(env), "env"
    ptr = pointer_path()
    if ptr is not None:
        try:
            if ptr.is_file():
                line = next((x.strip() for x in ptr.read_text(encoding="utf-8-sig").splitlines()
                             if x.strip() and not x.lstrip().startswith("#")), "")
                if line:
                    return org_file(line), "pointer"
        except OSError as exc:
            log.warning("pointer file %s cannot be read: %s", ptr, exc.__class__.__name__)
    own = (user.get("org") or {}).get("path") if isinstance(user.get("org"), dict) else ""
    if own:
        return org_file(own), "user"
    return None, ""


def _cache_path() -> Path:
    from techrag.settings import settings_path   # next to the user's settings.json

    return settings_path().parent / CACHE_FILE


def _clean(data: Any) -> dict:
    """Only what the organization file may set (an edited file cannot smuggle in a key or a personal path)."""
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for section, drop in EXPORT.items():
        if isinstance(data.get(section), dict):
            out[section] = {k: v for k, v in data[section].items() if k not in drop}
    if isinstance(data.get("paths"), dict):
        paths = {k: v for k, v in data["paths"].items() if k in EXPORT_PATHS}
        if paths:
            out["paths"] = paths
    if isinstance(data.get("read_only"), bool):
        out["read_only"] = data["read_only"]
    from techrag.config import upgrade_timeouts

    return upgrade_timeouts(out)   # an organization file exported before 0.15.4 says timeout: 300


def load(user: dict) -> dict:
    """The organization settings to apply (also recorded in STATUS for the UI); {} when there are none."""
    _reset()
    path, source = locate(user)
    if path is None:
        return {}
    STATUS.update(path=str(path), source=source)
    try:
        values = _clean(yaml.safe_load(path.read_text(encoding="utf-8-sig")))
        STATUS.update(loaded=True, values=values, mtime=_mtime(path))   # mtime: when it was last saved
        try:   # for the days the folder cannot be reached
            write_text_atomic(_cache_path(), yaml.safe_dump({"_source": str(path), "_mtime": STATUS["mtime"],
                                                             **values}, allow_unicode=True, sort_keys=False))
        except OSError:
            pass
        return values
    except FileNotFoundError:
        STATUS["error"] = "not_found"
    except (OSError, yaml.YAMLError) as exc:
        STATUS["error"] = exc.__class__.__name__
    try:
        cached = yaml.safe_load(_cache_path().read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("_source") == str(path):
            values = _clean(cached)
            mtime = cached.get("_mtime")
            STATUS.update(loaded=True, cached=True, values=values,
                          mtime=float(mtime) if isinstance(mtime, (int, float)) else None)
            log.warning("organization settings %s not reachable (%s); the copy read last time is used", path,
                        STATUS["error"])
            return values
    except (OSError, yaml.YAMLError):
        pass
    log.warning("organization settings %s not available (%s)", path, STATUS["error"])
    return {}


def value(section: str, key: str) -> Any:
    """The organization's value of a setting (None when it sets none)."""
    if section == "read_only":
        return STATUS["values"].get("read_only")
    sec = STATUS["values"].get(section)
    return sec.get(key) if isinstance(sec, dict) else None


def export(cfg: Config, others_read_only: bool = True) -> dict:
    d = cfg.to_dict()
    out = {section: {k: v for k, v in d[section].items() if k not in drop} for section, drop in EXPORT.items()}
    out["paths"] = {k: d["paths"][k] for k in EXPORT_PATHS}
    out["paths"]["library_dir"] = str(cfg.library_dir.resolve()) if cfg.paths.library_dir else ""
    out["read_only"] = bool(others_read_only)
    return out


HEADER = """# Sayısal Tasarım Uzmanı - kurum ayarları ({when})
# Bu dosyadaki değerler her kullanıcı için varsayılandır; kullanıcı kendi bilgisayarında değiştirebilir.
# API anahtarı ve kişisel klasörler burada tutulmaz. Admin › Ayarlar › "Kurum ayarlarını kaydet" ile yazılır.
"""


def save(folder: str | Path, cfg: Config, others_read_only: bool = True) -> dict:
    """Write the organization file into folder (atomically: others never read half a file) and the pointer file
    next to the exe. Returns where both went; the pointer's error when it could not be written (e.g. the exe sits
    in a read-only folder) - the settings file is written either way."""
    target = org_file(folder)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"folder not found: {target.parent}")
    data = export(cfg, others_read_only)
    text = HEADER.format(when=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")) + \
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    write_text_atomic(target, text)
    out = {"file": str(target), "pointer": "", "pointer_error": "", "warnings": []}
    lib = data["paths"]["library_dir"]
    if lib and not lib.startswith(("\\\\", "//")):
        out["warnings"].append("library_local")
    ptr = pointer_path()
    if ptr is None:
        out["pointer_error"] = "no_exe"
    else:
        try:
            write_text_atomic(ptr, f"{target.parent}\n")
            out["pointer"] = str(ptr)
        except OSError as exc:
            out["pointer_error"] = exc.__class__.__name__
    STATUS.update(path=str(target), source="saved", loaded=True, cached=False, error="", values=_clean(data),
                  replaced=[], mtime=_mtime(target))
    return out


def public_status(cfg: Config) -> dict:
    """For the UI: where the organization settings come from, and whether to ask for them (first start, nothing
    found, nothing set up) or for the API key (organization settings in place, no key yet)."""
    unconfigured = not cfg.llm.model   # a fresh install: no chat model chosen yet
    return {"path": STATUS["path"], "source": STATUS["source"], "loaded": STATUS["loaded"],
            "cached": STATUS["cached"], "error": STATUS["error"], "pointer": str(pointer_path() or ""),
            "ask": not STATUS["path"] and not cfg.org.asked and unconfigured,
            "needs_key": STATUS["loaded"] and not cfg.auth.api_key,
            "replaced": list(STATUS.get("replaced") or [])}
