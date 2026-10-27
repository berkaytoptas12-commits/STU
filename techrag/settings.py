"""Per-user settings written by the desktop Settings dialog (%APPDATA%/SayisalTasarimUzmani/settings.json; an
existing %APPDATA%/TechRAG folder from before the rename keeps being used).

One API key is used for every model server (section "auth"); keys saved per service by older versions are moved
there on the next save. API keys are encrypted with Windows DPAPI (bound to the Windows user account) when
available; on other platforms the file is created with owner-only permissions.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from techrag.config import Config, _apply_env, apply_dict, user_config_dir

log = logging.getLogger("techrag.settings")

SECRET_KEYS = ("api_key",)
SECTIONS = ("auth", "llm", "vision", "embedding", "reranker", "paths", "ui", "answer", "retrieval", "tls", "usage")
SERVICE_SECTIONS = ("llm", "vision", "embedding", "reranker")   # the chat model's key first (see _migrate_keys)
MASK = "••••••••"


def settings_path() -> Path:
    override = os.environ.get("TECHRAG_SETTINGS")
    return Path(override) if override else user_config_dir() / "settings.json"


# ------------------------------------------------------------------------------ DPAPI

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_secret(value: str) -> Any:
    if not value:
        return ""
    if os.name == "nt":
        try:
            return {"dpapi": base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")}
        except Exception as exc:  # never fall back to writing the key in plain text
            raise RuntimeError("the API key could not be encrypted with Windows DPAPI and was not saved") from exc
    return value


# Keys that could not be decrypted (another Windows account, a reset password): kept as stored, so that saving
# any other setting does not overwrite them with "".
_UNREADABLE: dict[tuple[str, str], Any] = {}


def decrypt_secret(value: Any, where: tuple[str, str] = ("", "")) -> str:
    if isinstance(value, dict) and "dpapi" in value:
        try:
            return _dpapi(base64.b64decode(value["dpapi"]), False).decode("utf-8")
        except Exception:
            _UNREADABLE[where] = value
            log.warning("the stored %s key cannot be decrypted by this Windows account; it is kept unchanged",
                        where[0] or "API")
            return ""
    return value if isinstance(value, str) else ""


# ------------------------------------------------------------------------------ load/save

def load_user_settings() -> dict:
    path = settings_path()
    _UNREADABLE.clear()  # describes the file as read now
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))  # Notepad / PowerShell 5 write a BOM
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        # Keep a copy before the next save replaces it - it holds the endpoints and keys.
        backup = path.with_name(path.name + ".unreadable")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        log.warning("settings file %s cannot be read (%s); a copy is kept as %s", path, exc.__class__.__name__,
                    backup.name)
        return {}
    for name, section in raw.items():
        if isinstance(section, dict):
            for k in SECRET_KEYS:
                if k in section:
                    section[k] = decrypt_secret(section[k], (name, k))
    return raw


def save_user_settings(data: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = json.loads(json.dumps(data))
    for name, section in out.items():
        if isinstance(section, dict):
            for k in SECRET_KEYS:
                if k in section:
                    kept = _UNREADABLE.get((name, k))
                    section[k] = kept if (kept is not None and not section[k]) else encrypt_secret(section[k])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def public_settings(cfg: Config) -> dict:
    """Editable settings for the UI, the shared key masked (services have no key of their own)."""
    d = cfg.to_dict()
    out = {s: d[s] for s in SECTIONS}
    out["auth"]["api_key"] = MASK if cfg.auth.api_key else ""
    for s in SERVICE_SECTIONS:
        out[s].pop("api_key", None)
    out["read_only"] = cfg.read_only
    return out


def _migrate_keys(stored: dict) -> None:
    """Settings saved before the shared key: the first per-service key (the chat model's first) becomes the shared
    key and the per-service keys are removed. A key this Windows account cannot decrypt moves as it is stored."""
    legacy = [(n, stored[n]) for n in SERVICE_SECTIONS if isinstance(stored.get(n), dict) and "api_key" in stored[n]]
    if not legacy:
        return
    auth = stored["auth"] if isinstance(stored.get("auth"), dict) else {}
    if not auth.get("api_key") and ("auth", "api_key") not in _UNREADABLE:
        for name, section in legacy:
            if section.get("api_key"):
                auth["api_key"] = section["api_key"]
                break
            if (name, "api_key") in _UNREADABLE:
                _UNREADABLE[("auth", "api_key")] = _UNREADABLE[(name, "api_key")]
                auth["api_key"] = ""
                break
    for name, section in legacy:
        section.pop("api_key", None)
        _UNREADABLE.pop((name, "api_key"), None)
    stored["auth"] = auth


def update_settings(cfg: Config, patch: dict) -> Config:
    """Merge a settings patch from the UI into the stored user settings and return a new Config.
    A masked or missing api_key keeps the stored one; a key given for a single service is ignored (one shared key,
    section "auth")."""
    stored = load_user_settings()
    _migrate_keys(stored)
    applied: dict = {}
    for section, values in (patch or {}).items():
        if section == "read_only":
            stored["read_only"] = applied["read_only"] = bool(values)
            continue
        if section not in SECTIONS or not isinstance(values, dict):
            continue
        dest, now = stored.setdefault(section, {}), applied.setdefault(section, {})
        for k, v in values.items():
            if k in SECRET_KEYS and (v == MASK or v is None or section != "auth"):
                continue
            dest[k] = now[k] = v
            if k in SECRET_KEYS:
                _UNREADABLE.pop((section, k), None)  # explicitly replaced (or cleared) by the user
    # Only what changed goes into the running config: environment variables and command-line options (e.g.
    # --library, TECHRAG_READ_ONLY) keep winning, as they did at start-up.
    probe = cfg.copy()
    apply_dict(probe, applied, strict=False)  # validates types before persisting
    check_ranges(probe, applied)
    _apply_env(probe)
    probe.share_api_key(adopt_legacy=False)
    save_user_settings(stored)
    return probe


# Advanced settings the UI edits: (section, key) -> (lowest, highest)
RANGES = {("llm", "context_tokens"): (2048, 1_048_576), ("vision", "min_page_score"): (0.0, 50.0),
          ("vision", "max_pages_per_doc"): (0, 100_000), ("answer", "judge_batch"): (1, 32),
          ("retrieval", "follow_references"): (0, 10), ("retrieval", "reference_tokens"): (100, 8000),
          ("llm", "max_tool_rounds"): (0, 10)}


def check_ranges(cfg: Config, changed: dict) -> None:
    """Range check of the advanced values this update changes (a value set elsewhere, e.g. in a YAML file,
    does not block an unrelated change)."""
    for (section, key), (lo, hi) in RANGES.items():
        if key not in (changed.get(section) or {}):
            continue
        v = getattr(getattr(cfg, section), key)
        if not lo <= v <= hi:
            raise ValueError(f"{section}.{key} must be between {lo} and {hi} (got {v})")
