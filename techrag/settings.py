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
SECTIONS = ("auth", "llm", "llm_fast", "vision", "embedding", "reranker", "paths", "ui", "answer", "retrieval",
            "tls", "usage", "org")
SERVICE_SECTIONS = ("llm", "vision", "embedding", "reranker")   # the chat model's key first (see _migrate_keys)
MASK = "••••••••"
# Per own setting the organization could set: the organization's value when the user chose theirs (None: it set
# none then). A later, different organization value is a newer decision and replaces the user's (follow_org).
BASE_KEY = "_org_base"
# Own values the organization's newer decision replaced: kept here (not applied), for reference.
REPLACED_KEY = "_org_replaced"
# Clocks of this computer and of the server holding the organization file may differ by this much (seconds).
CLOCK_SLACK = 300


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
    ui = raw.get("ui")
    if isinstance(ui, dict) and ui.get("theme") == "yellow":
        ui["theme"] = "dark"   # 0.15.1-0.15.2's third theme (yellow-black) became the dark theme
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
    from techrag import org

    stored = load_user_settings()
    _migrate_keys(stored)
    base = stored[BASE_KEY] if isinstance(stored.get(BASE_KEY), dict) else {}
    replaced = stored[REPLACED_KEY] if isinstance(stored.get(REPLACED_KEY), dict) else {}

    def keep(section: str, k: str, v: Any, holder: dict) -> None:
        """Only what differs from the organization settings is kept as the user's own (a setting left as the
        organization has it follows the organization file when that changes later), together with the
        organization's value at this moment: a later, different one replaces it (follow_org)."""
        name, org_value = org.setting_name(section, k), org.value(section, k)
        replaced.pop(name, None)
        if org_value is not None and org.same(section, k, v, org_value):
            holder.pop(k, None)
            base.pop(name, None)
            return
        holder[k] = v
        if org.sets(section, k):
            base[name] = org_value

    applied: dict = {}
    for section, values in (patch or {}).items():
        if section == "read_only":
            applied["read_only"] = bool(values)
            keep("read_only", "read_only", applied["read_only"], stored)
            continue
        if section not in SECTIONS or not isinstance(values, dict):
            continue
        dest, now = stored.setdefault(section, {}), applied.setdefault(section, {})
        for k, v in values.items():
            if k in SECRET_KEYS and (v == MASK or v is None or section != "auth"):
                continue
            now[k] = v
            keep(section, k, v, dest)
            if k in SECRET_KEYS:
                _UNREADABLE.pop((section, k), None)  # explicitly replaced (or cleared) by the user
        if not dest:
            stored.pop(section, None)
    for key, value in ((BASE_KEY, base), (REPLACED_KEY, replaced)):
        if value:
            stored[key] = value
        else:
            stored.pop(key, None)
    # Only what changed goes into the running config: environment variables and command-line options (e.g.
    # --library, TECHRAG_READ_ONLY) keep winning, as they did at start-up.
    probe = cfg.copy()
    apply_dict(probe, applied, strict=False)  # validates types before persisting
    check_ranges(probe, applied)
    _apply_env(probe)
    probe.share_api_key(adopt_legacy=False)
    save_user_settings(stored)
    return probe


def follow_org(user: dict, persist: bool = True) -> dict:
    """The user's own settings to apply over the organization settings just loaded: the newest decision wins.

    * An own value equal to the organization's is not the user's own: it is dropped (the setting follows the
      organization file from now on).
    * An own value chosen while the organization had the value it has now is kept (the user's choice is newer).
    * When the organization file sets that setting to something else now, the administrator decided after the
      user: the organization's value replaces the user's. The user's value is moved aside (REPLACED_KEY), never
      applied again; the user can change the setting again (a new own value).
    * Own values saved before the organization's value was recorded with them (older versions): when the user's
      settings file was written after the organization file was last saved (e.g. the administrator's own computer:
      saving the organization settings writes it right after), they are kept and the organization's value is
      recorded with them; otherwise the organization decided later and its value applies. How this user opens the
      library (read_only) is always kept.
    Nothing changes when the organization settings could not be read (no file, no copy read before). The settings
    file is rewritten only when something changed; the replaced names are in org.STATUS["replaced"] for the UI."""
    from datetime import datetime, timezone

    from techrag import org

    org.STATUS["replaced"] = []
    if not org.STATUS.get("loaded") or not isinstance(user, dict):
        return user
    base = user[BASE_KEY] if isinstance(user.get(BASE_KEY), dict) else {}
    moved = user[REPLACED_KEY] if isinstance(user.get(REPLACED_KEY), dict) else {}
    try:
        written = settings_path().stat().st_mtime
    except OSError:
        written = None
    org_saved = org.STATUS.get("mtime")
    written_later = written is not None and org_saved is not None and written > org_saved - CLOCK_SLACK
    items = [("read_only", "read_only", user)] if "read_only" in user else []
    items += [(s, k, user[s]) for s in SECTIONS if isinstance(user.get(s), dict) for k in list(user[s])]
    replaced: list[str] = []
    emptied: set[str] = set()
    changed = False
    for section, k, holder in items:
        if not org.sets(section, k):
            continue
        org_value = org.value(section, k)
        if org_value is None:
            continue
        name, own = org.setting_name(section, k), holder[k]
        if org.same(section, k, own, org_value):
            holder.pop(k)
            base.pop(name, None)
            emptied.add(section)
            changed = True
            continue
        if name in base:
            if org.same(section, k, base[name], org_value):
                continue                    # chosen while the organization had this value: the user's is newer
        elif section == "read_only" or written_later:
            base[name] = org_value          # how this user opens the library / chosen after the organization's save
            changed = True
            continue
        holder.pop(k)
        base.pop(name, None)
        moved[name] = {"value": own, "organization": org_value,
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        replaced.append(name)
        emptied.add(section)
        changed = True
    for section in emptied:
        if section != "read_only" and user.get(section) == {}:
            user.pop(section)
    for key, value in ((BASE_KEY, base), (REPLACED_KEY, moved)):
        if value:
            user[key] = value
        else:
            user.pop(key, None)
    org.STATUS["replaced"] = replaced
    if replaced:
        log.info("the organization settings changed after this user's own choice; they now apply to: %s",
                 ", ".join(replaced))
    if changed and persist:
        try:
            save_user_settings(user)
        except OSError as exc:
            log.warning("user settings could not be updated: %s", exc.__class__.__name__)
    return user


# Advanced settings the UI edits: (section, key) -> (lowest, highest)
RANGES = {("llm", "context_tokens"): (2048, 1_048_576), ("vision", "min_page_score"): (0.0, 50.0),
          ("vision", "max_pages_per_doc"): (0, 100_000), ("answer", "judge_batch"): (1, 32),
          ("retrieval", "follow_references"): (0, 10), ("retrieval", "reference_tokens"): (100, 8000),
          ("llm", "max_tool_rounds"): (0, 10), ("retrieval", "final_top_k"): (1, 30),
          ("retrieval", "bm25_top_k"): (5, 500), ("retrieval", "dense_top_k"): (5, 500),
          ("reranker", "candidates"): (5, 300)}


# Settings with a fixed set of values: (section, key) -> allowed values
CHOICES = {("ui", "theme"): ("dark", "light")}


def check_ranges(cfg: Config, changed: dict) -> None:
    """Range check of the advanced values this update changes (a value set elsewhere, e.g. in a YAML file,
    does not block an unrelated change)."""
    for (section, key), allowed in CHOICES.items():
        if key in (changed.get(section) or {}) and getattr(getattr(cfg, section), key) not in allowed:
            raise ValueError(f"{section}.{key} must be one of {', '.join(allowed)}")
    for (section, key), (lo, hi) in RANGES.items():
        if key not in (changed.get(section) or {}):
            continue
        v = getattr(getattr(cfg, section), key)
        if not lo <= v <= hi:
            raise ValueError(f"{section}.{key} must be between {lo} and {hi} (got {v})")
    # the reranker orders only its candidates: fewer of them than passages given to the model would cap top k
    if ("candidates" in (changed.get("reranker") or {}) or "final_top_k" in (changed.get("retrieval") or {})) \
            and cfg.reranker.candidates < cfg.retrieval.final_top_k:
        raise ValueError(f"reranker.candidates ({cfg.reranker.candidates}) must not be smaller than "
                         f"retrieval.final_top_k ({cfg.retrieval.final_top_k})")
