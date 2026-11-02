"""Removing a file the user can get back: the Windows Recycle Bin, or a "silinenler" folder.

Used when a personal document is removed. On a local fixed drive under Windows the file goes to the Recycle Bin
(SHFileOperation with undo; if Windows could only delete it for good it asks first instead of doing it silently).
Anywhere else - a network drive, a drive without a Recycle Bin, another OS - the file is moved into the given
fallback folder. A file is never deleted for good without being asked.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("techrag.trash")

RECYCLE_BIN, FOLDER = "recycle_bin", "folder"


def _fixed_drive(path: Path) -> bool:
    import ctypes

    drive = os.path.splitdrive(str(path.resolve()))[0]
    if not drive or drive.startswith("\\\\"):
        return False   # UNC path: a network share has no Recycle Bin
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3   # DRIVE_FIXED


def _recycle(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 0x3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x4, 0x10, 0x40, 0x400, 0x4000
    files = ctypes.create_unicode_buffer(str(path.resolve()) + "\0")   # a list of paths ends with two NULs
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(files, wintypes.LPCWSTR), None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                         False, None, None)
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0 or op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperation failed ({res:#x})")


def move_to_trash(path: Path, fallback_dir: Path) -> tuple[str, Path]:
    """(RECYCLE_BIN, path) or (FOLDER, where the file now is)."""
    path = Path(path)
    if os.name == "nt":
        try:
            if _fixed_drive(path):
                _recycle(path)
                if not path.exists():
                    return RECYCLE_BIN, path
        except Exception as exc:   # the folder below still keeps the file recoverable
            log.warning("Recycle Bin not available for %s (%s); moving it to %s", path.name, exc.__class__.__name__,
                        fallback_dir)
    fallback_dir.mkdir(parents=True, exist_ok=True)
    dest = fallback_dir / path.name
    n = 2
    while dest.exists():
        dest = fallback_dir / f"{path.stem} ({n}){path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return FOLDER, dest
