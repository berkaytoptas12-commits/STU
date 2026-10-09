"""Per-user settings written by the desktop Settings dialog (%APPDATA%/TechRAG/settings.json).

API keys are encrypted with Windows DPAPI (bound to the Windows user account) when available; on
other platforms the file is created with owner-only permissions.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from techrag.config import Config, apply_dict, user_config_dir

SECRET_KEYS = ("api_key",)
SECTIONS = ("llm", "vision", "embedding", "reranker", "paths", "ui", "answer", "retrieval")
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
        except Exception:
            pass
    return value


def decrypt_secret(value: Any) -> str:
    if isinstance(value, dict) and "dpapi" in value:
        try:
            return _dpapi(base64.b64decode(value["dpapi"]), False).decode("utf-8")
        except Exception:
            return ""
    return value if isinstance(value, str) else ""


# ------------------------------------------------------------------------------ load/save

def load_user_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for section in raw.values():
        if isinstance(section, dict):
            for k in SECRET_KEYS:
                if k in section:
                    section[k] = decrypt_secret(section[k])
    return raw


def save_user_settings(data: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = json.loads(json.dumps(data))
    for section in out.values():
        if isinstance(section, dict):
            for k in SECRET_KEYS:
                if k in section:
                    section[k] = encrypt_secret(section[k])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def public_settings(cfg: Config) -> dict:
    """Editable settings for the UI, secrets masked."""
    d = cfg.to_dict()
    out = {s: d[s] for s in SECTIONS}
    for s in ("llm", "vision", "embedding", "reranker"):
        out[s]["api_key"] = MASK if out[s].get("api_key") else ""
    out["read_only"] = cfg.read_only
    return out


def update_settings(cfg: Config, patch: dict) -> Config:
    """Merge a settings patch from the UI into the stored user settings and return a new Config.
    A masked or missing api_key keeps the stored one."""
    stored = load_user_settings()
    for section, values in (patch or {}).items():
        if section == "read_only":
            stored["read_only"] = bool(values)
            continue
        if section not in SECTIONS or not isinstance(values, dict):
            continue
        dest = stored.setdefault(section, {})
        for k, v in values.items():
            if k in SECRET_KEYS and (v == MASK or v is None):
                continue
            dest[k] = v
    probe = cfg.copy()
    apply_dict(probe, stored, strict=False)  # validates types before persisting
    save_user_settings(stored)
    return probe
