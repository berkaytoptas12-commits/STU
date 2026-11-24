"""Who may use Admin: the user names in the organization settings ("admins:" in kurum-ayarlari.yaml).

* The user name is the Windows account the app runs under, asked from Windows (GetUserNameW) - not the USERNAME
  environment variable, which anyone can set before starting the app.
* Names are compared without a domain part ("ŞİRKET\\ahmet" and "ahmet@sirket.local" are "ahmet") and without regard
  to letter case. A local account of the same name on another computer counts too (the chosen way of matching).
* The list comes only from the organization file (or the copy of it read last time, when its folder cannot be
  reached): a user's own settings, config.yaml or an environment variable cannot set it.
* No list - no organization settings, or none set there - means everyone, as before 0.15.7: rolling out a version
  never locks anyone out; the lock starts when an admin saves the organization settings with names in the list.

This keeps people from changing shared things by accident or without permission. The app runs on the user's own
computer under their own account, so it is not a wall against someone who sets out to get around it: what protects
the shared library and the organization file is who may write to their folders.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Optional

_ACCOUNT: Optional[str] = None


def account_name() -> str:
    """The account this process runs under, as the operating system reports it (cached: it cannot change)."""
    global _ACCOUNT
    if _ACCOUNT is None:
        name = ""
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                size = wintypes.DWORD(257)
                buf = ctypes.create_unicode_buffer(size.value)
                if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
                    name = buf.value
            except Exception:
                name = ""
        else:
            try:
                import pwd

                name = pwd.getpwuid(os.getuid()).pw_name
            except Exception:
                name = ""
        if not name:   # the API failed: the name the usage log uses
            from techrag.usage import current_user

            name = current_user()
        _ACCOUNT = name
    return _ACCOUNT


def current() -> str:
    """The user name Admin rights are checked for (tests replace this)."""
    return account_name()


def normalize(name: Any) -> str:
    """A user name as it is compared: no domain part, no letter case."""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    s = s.rsplit("\\", 1)[-1].split("@", 1)[0].strip()
    return s.replace("İ", "I").lower()


def parse_list(value: Any) -> list[str]:
    """The names of a list (or of text with one name per line, or separated by commas), as written, without
    duplicates or empty entries."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[\r\n,;]+", str(value or ""))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, (str, int)):
            continue
        text = str(item).strip()
        key = normalize(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def admin_list() -> list[str]:
    """The names in the organization settings loaded now ([] when it sets none)."""
    from techrag import org

    return parse_list((org.STATUS.get("values") or {}).get("admins"))


def is_admin(user: Optional[str] = None, names: Optional[list[str]] = None) -> bool:
    """Whether a user (the current one by default) may use Admin: everyone while the list is empty."""
    names = admin_list() if names is None else names
    if not names:
        return True
    who = normalize(current() if user is None else user)
    return bool(who) and who in {normalize(n) for n in names}
