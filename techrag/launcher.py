"""The round launcher icon of the desktop app and its single-instance handling (Windows).

The app starts as a small round icon in the bottom-right corner of the screen. A click opens the chat window, a drag
moves the icon (its place is remembered), and the right-click menu offers "Open chat", "Settings" and "Quit". The icon
stays above normal windows but never takes the keyboard focus.

It is a plain Win32 layered window driven through ctypes on its own thread: per-pixel alpha gives the round shape
smooth edges and makes the transparent corners click-through, and no second browser engine or extra framework is
needed. Its thread is per-monitor DPI aware (v2), so every rectangle below is in physical pixels and the icon is
redrawn at the right size on each monitor. The chat window (pywebview) keeps its own DPI handling unchanged.

Everything that does not touch Win32 - geometry, the click/drag decision, drawing, the state file - is platform
independent and unit tested on any OS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from techrag.config import APP_TITLE, write_text_atomic

log = logging.getLogger("techrag.launcher")

SIZE_DIP = 60        # icon window edge in device-independent pixels (disc + soft shadow)
MARGIN_DIP = 16      # default distance from the work-area edges (bottom-right corner)
DRAG_DIP = 4         # a press that moves farther than this is a drag, not a click
IDM_OPEN, IDM_SETTINGS, IDM_QUIT = 1001, 1002, 1003
# Internal names keep the former "TechRAG" prefix on purpose: an older build still running is detected as the same app.
ACTIVATE_MESSAGE = "TechRAG.Launcher.Activate"

TEXTS = {
    "tr": {
        "open": "Sohbeti aç", "settings": "Ayarlar", "quit": "Uygulamadan çık",
        "hint_title": f"{APP_TITLE} çalışmaya devam ediyor",
        "hint": (f"Pencere gizlendi; {APP_TITLE} ekrandaki mikroçip simgesi olarak çalışmaya devam ediyor. Sohbet, "
                 "seçili bucket'lar, açık PDF ve süren indeksleme olduğu gibi korunur.\n\nSohbeti yeniden açmak için "
                 "simgeye tıklayın; sohbet öndeyken simgeye tıklamak onu yeniden gizler. Uygulamadan tamamen çıkmak "
                 "için simgeye sağ tıklayıp \u201cUygulamadan çık\u201d seçeneğini kullanın."),
        "busy_title": "İndeksleme sürüyor",
        "busy": ("Belge indeksleme devam ediyor. Şimdi çıkarsanız o an yazılmakta olan kayıt tamamlanır; işlenmekte "
                 "olan ve kalan belgeler bir sonraki taramada işlenir. İndeks bozulmaz.\n\nUygulamadan çıkılsın mı?"),
        "running": f"{APP_TITLE} zaten çalışıyor fakat yanıt vermiyor. Biraz bekleyip yeniden deneyin.",
        "note_final": "Cevap hazır", "note_notfound": "Cevap hazır: kaynaklarda bulunamadı",
        "note_clarify": "Netleştirme sorusu var", "note_error": "Cevap verilemedi (hata)",
        "note_click": "Sohbeti açmak için tıklayın.",
    },
    "en": {
        "open": "Open chat", "settings": "Settings", "quit": "Quit",
        "hint_title": f"{APP_TITLE} keeps running",
        "hint": (f"The window was hidden; {APP_TITLE} keeps running as the microchip icon on your screen. The chat, "
                 "the selected buckets, the open PDF and any indexing in progress are kept as they are.\n\nClick the "
                 "icon to open the chat again; clicking it while the chat is in front hides it again. To quit "
                 "completely, right-click the icon and choose \u201cQuit\u201d."),
        "busy_title": "Indexing in progress",
        "busy": ("Documents are still being indexed. If you quit now, the record being written is completed; the "
                 "document in progress and the remaining ones are processed by the next rescan. The index stays "
                 "consistent.\n\nQuit now?"),
        "running": f"{APP_TITLE} is already running but does not respond. Please wait a moment and try again.",
        "note_final": "Answer ready", "note_notfound": "Answer ready: not found in the sources",
        "note_clarify": "A clarifying question is waiting", "note_error": "No answer (error)",
        "note_click": "Click to open the chat.",
    },
}


def text(lang: str, key: str) -> str:
    return (TEXTS.get(lang) or TEXTS["en"]).get(key) or TEXTS["en"][key]


def answer_note(lang: str, info: dict) -> tuple[str, str]:
    """Title and text of the notification for a finished answer: what kind of reply it is and the question."""
    kind, status = info.get("kind"), info.get("status")
    key = {"clarify": "note_clarify", "error": "note_error"}.get(kind) or \
        ("note_notfound" if status == "not_found" else "note_final")
    q = " ".join(str(info.get("question") or "").split())
    if len(q) > 160:
        q = q[:157].rstrip() + "…"
    return text(lang, key), f"{q}\n{text(lang, 'note_click')}" if q else text(lang, "note_click")


# ------------------------------------------------------------------------------------------- geometry
Rect = tuple  # (left, top, right, bottom); right/bottom exclusive, physical pixels


@dataclass
class Monitor:
    name: str
    rect: Rect
    work: Rect          # without the taskbar
    dpi: int = 96
    primary: bool = False

    @property
    def scale(self) -> float:
        return max(int(self.dpi or 96), 48) / 96.0


def icon_px(dpi: int) -> int:
    """Icon window edge in physical pixels at a monitor's DPI (60 DIP: 60 px at 100 %, 90 px at 150 %)."""
    return max(24, round(SIZE_DIP * max(int(dpi or 96), 48) / 96))


def clamp_into(x: int, y: int, size: int, work: Rect) -> tuple[int, int]:
    """Top-left corner that keeps a size x size window completely inside `work`."""
    left, top, right, bottom = work
    x = min(max(int(x), left), max(left, right - size))
    y = min(max(int(y), top), max(top, bottom - size))
    return x, y


def monitor_at(monitors: list[Monitor], x: int, y: int) -> Monitor:
    """The monitor containing the point; for a point outside every monitor (one that was unplugged) the nearest."""
    def dist(m: Monitor) -> int:
        left, top, right, bottom = m.rect
        dx = max(left - x, 0, x - (right - 1))
        dy = max(top - y, 0, y - (bottom - 1))
        return dx * dx + dy * dy

    return min(monitors, key=lambda m: (dist(m), not m.primary))


def primary_monitor(monitors: list[Monitor]) -> Monitor:
    return next((m for m in monitors if m.primary), monitors[0])


def default_anchor() -> dict:
    return {"h": "right", "dx": MARGIN_DIP, "v": "bottom", "dy": MARGIN_DIP}


def anchor_for(x: int, y: int, size: int, mon: Monitor) -> dict:
    """How a position is remembered: the monitor and the distance (DIP) to the nearer horizontal and vertical edge of
    its work area - so an icon in the bottom-right corner stays there after a resolution, scaling or taskbar change."""
    left, top, right, bottom = mon.work
    gaps = {"left": x - left, "right": right - (x + size), "top": y - top, "bottom": bottom - (y + size)}
    h = "left" if gaps["left"] <= gaps["right"] else "right"
    v = "top" if gaps["top"] <= gaps["bottom"] else "bottom"
    return {"monitor": mon.name, "h": h, "dx": round(max(0, gaps[h]) / mon.scale, 1),
            "v": v, "dy": round(max(0, gaps[v]) / mon.scale, 1)}


def _num(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and 0 <= f <= 100_000 else default


def place(anchor: Optional[dict], monitors: list[Monitor]) -> tuple[Monitor, int, int, int]:
    """(monitor, x, y, size) for a remembered anchor (bottom-right of the primary work area by default). A monitor
    that is no longer connected falls back to the primary one; the icon always ends up fully inside a work area."""
    a = anchor if isinstance(anchor, dict) else {}
    mon = next((m for m in monitors if a.get("monitor") and m.name == a.get("monitor")), None) or \
        primary_monitor(monitors)
    work = mon.work if mon.work[2] > mon.work[0] and mon.work[3] > mon.work[1] else mon.rect
    size, s = icon_px(mon.dpi), mon.scale
    dx, dy = _num(a.get("dx"), MARGIN_DIP) * s, _num(a.get("dy"), MARGIN_DIP) * s
    x = work[0] + dx if a.get("h") == "left" else work[2] - size - dx
    y = work[1] + dy if a.get("v") == "top" else work[3] - size - dy
    x, y = clamp_into(round(x), round(y), size, work)
    return mon, x, y, size


class PressTracker:
    """Click or drag? A press becomes a drag once the pointer has moved more than `threshold` pixels away from where
    it went down; only a press that never did is a click (so moving the icon never opens the chat)."""

    def __init__(self, threshold: int = DRAG_DIP):
        self.threshold = max(1, int(threshold))
        self.active = False
        self.dragging = False
        self._start = (0, 0)
        self._origin = (0, 0)

    def press(self, point: tuple[int, int], window_pos: tuple[int, int]) -> None:
        self.active, self.dragging = True, False
        self._start, self._origin = tuple(point), tuple(window_pos)

    def move(self, point: tuple[int, int]) -> Optional[tuple[int, int]]:
        """New window position while dragging, else None."""
        if not self.active:
            return None
        dx, dy = point[0] - self._start[0], point[1] - self._start[1]
        if not self.dragging and max(abs(dx), abs(dy)) > self.threshold:
            self.dragging = True
        return (self._origin[0] + dx, self._origin[1] + dy) if self.dragging else None

    def release(self, point: tuple[int, int]) -> Optional[str]:
        """'click', 'drag' or None (no press in progress)."""
        if not self.active:
            return None
        self.move(point)
        kind = "drag" if self.dragging else "click"
        self.active = self.dragging = False
        return kind

    def cancel(self) -> None:
        self.active = self.dragging = False


# ------------------------------------------------------------------------------------------- drawing
def render_icon(size: int, hover: bool = False) -> bytes:
    """Premultiplied BGRA pixels (top-down) of the launcher: a microchip - a rounded square package with four pins
    on each side, a lighter die and a pin-1 dot - with a soft shadow, so it reads on light and dark desktops alike.
    Drawn in code at the monitor's pixel size (nothing is downloaded or scaled), anti-aliased by 4x4 supersampling.
    The corners are fully transparent (click-through); between the pins the alpha is 1/255 - invisible, but the
    whole chip stays one click target."""
    import numpy as np

    ss = 4
    S = float(size)
    coords = (np.arange(size * ss, dtype=np.float32) + 0.5) / ss
    X, Y = np.meshgrid(coords, coords)

    def cover(mask) -> "np.ndarray":
        return mask.astype(np.float32).reshape(size, ss, size, ss).mean(axis=(1, 3))

    def rrect(cx, cy, w, h, r):
        """Signed distance to a rounded rectangle (<= 0 inside)."""
        qx, qy = np.abs(X - cx) - (w / 2 - r), np.abs(Y - cy) - (h / 2 - r)
        return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r

    c = S / 2 - S * 0.01
    body_w, body_r = S * 0.54, S * 0.09
    pin_len, pin_w = S * 0.14, S * 0.07
    edge = body_w / 2
    pins = np.zeros_like(X, dtype=bool)
    for k in range(4):
        off = c + (k - 1.5) * S * 0.12
        for horizontal in (True, False):
            for side in (-1, 1):
                mid = c + side * (edge + pin_len / 2 - S * 0.02)   # tucked a little under the package
                if horizontal:   # pins on the left and right edges
                    pins |= rrect(mid, off, pin_len + S * 0.04, pin_w, pin_w * 0.3) <= 0
                else:            # top and bottom
                    pins |= rrect(off, mid, pin_w, pin_len + S * 0.04, pin_w * 0.3) <= 0
    body = cover(rrect(c, c, body_w, body_w, body_r) <= 0)
    pin = cover(pins)
    die = cover(rrect(c, c, S * 0.26, S * 0.26, S * 0.045) <= 0)
    dot = cover(np.hypot(X - (c - edge + S * 0.10), Y - (c - edge + S * 0.10)) <= S * 0.04)
    reach = edge + pin_len - S * 0.02                      # half-size of the chip with its pins
    hit = cover(rrect(c, c, 2 * reach, 2 * reach, S * 0.10) <= 0)
    sd = rrect(c, c + S * 0.03, body_w, body_w, body_r)   # a soft shadow under the package only
    shadow = (np.clip(1 - sd / (S * 0.08), 0, 1) ** 2).reshape(size, ss, size, ss).mean(axis=(1, 3)) * 0.35

    rows = (np.arange(size, dtype=np.float32) + 0.5)[:, None]
    t = np.clip((rows - (c - edge)) / body_w, 0, 1)[..., None] * np.ones((1, size, 1), np.float32)
    top, bottom = np.array([64, 116, 230], np.float32), np.array([30, 62, 170], np.float32)
    die_top, die_bottom = np.array([132, 172, 255], np.float32), np.array([96, 140, 240], np.float32)
    pin_rgb = np.array([150, 162, 186], np.float32)
    if hover:
        top, bottom, die_top, die_bottom = top + 22, bottom + 22, die_top + 14, die_bottom + 14
    body_rgb = top * (1 - t) + bottom * t
    die_rgb = die_top * (1 - t) + die_bottom * t
    black = np.zeros((size, size, 3), np.float32)

    rgb = np.zeros((size, size, 3), np.float32)   # premultiplied
    alpha = np.zeros((size, size), np.float32)
    for color, a in ((black, np.maximum(shadow, hit / 255.0)), (pin_rgb * np.ones_like(rgb), pin),
                     (body_rgb, body), (die_rgb, die * body), (np.full_like(rgb, 255), dot * body)):
        rgb = color * a[..., None] + rgb * (1 - a[..., None])
        alpha = a + alpha * (1 - a)
    a8 = np.clip(np.round(alpha * 255), 0, 255)
    a8 = np.where(hit > 0.5, np.maximum(a8, 1), a8)   # never 0 inside the click target
    c8 = np.minimum(np.clip(np.round(rgb), 0, 255), a8[..., None])
    return np.dstack([c8[..., 2], c8[..., 1], c8[..., 0], a8]).astype(np.uint8).tobytes()


# ------------------------------------------------------------------------------------------- state
class DesktopState:
    """desktop.json next to settings.json: where the launcher icon sits and which one-time hints were shown. (The chat,
    panel sizes and selections live in the page; nothing here touches the index.)"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data: dict = data if isinstance(data, dict) else {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            try:
                write_text_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))
            except OSError as exc:
                log.warning("could not save %s: %s", self.path, exc)


def instance_key(settings_file: str | Path) -> str:
    """One running instance per Windows session and settings location."""
    p = os.path.normcase(os.path.abspath(str(settings_file)))
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------------------------- Win32
WM_NULL, WM_DESTROY, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION = 0x0000, 0x0002, 0x0010, 0x0011, 0x0016
WM_SETTINGCHANGE, WM_MOUSEACTIVATE, WM_CONTEXTMENU, WM_DISPLAYCHANGE = 0x001A, 0x0021, 0x007B, 0x007E
WM_COMMAND, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0111, 0x0200, 0x0201, 0x0202
WM_CAPTURECHANGED, WM_MOUSELEAVE, WM_DPICHANGED = 0x0215, 0x02A3, 0x02E0
WM_APP_HIDE, WM_APP_SHOW, WM_APP_ZORDER = 0x8000 + 1, 0x8000 + 2, 0x8000 + 3
WM_APP_NOTIFY, WM_APP_TRAY, WM_APP_UNNOTIFY = 0x8000 + 4, 0x8000 + 5, 0x8000 + 6
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION, NOTIFYICON_VERSION_4 = 0, 1, 2, 4, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
NIIF_INFO, NIIF_USER, NIIF_LARGE_ICON, NIIF_RESPECT_QUIET_TIME = 0x01, 0x04, 0x20, 0x80
NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK = 0x0400, 0x0401, 0x0405
FLASHW_ALL, FLASHW_TIMERNOFG, TRAY_ID = 0x03, 0x0C, 1
WM_TIMER, OVERLAP_TIMER, OVERLAP_MS = 0x0113, 1, 400
MK_LBUTTON, MA_NOACTIVATE, SPI_SETWORKAREA = 0x0001, 3, 0x002F
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x00080000, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
GW_HWNDPREV, GA_ROOTOWNER, DWMWA_CLOAKED, WS_EX_TRANSPARENT = 3, 3, 14, 0x00000020
SW_HIDE, SW_SHOWNOACTIVATE, SW_RESTORE = 0, 4, 9
GWL_EXSTYLE, IDC_HAND, LOGPIXELSX = -20, 32649, 88
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
TME_LEAVE, ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x0002, 0x00000002, 0x00, 0x01
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0x0000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SM_SHUTTINGDOWN = 0x2000
ERROR_ALREADY_EXISTS = 183
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
MB_OK, MB_YESNO, MB_ICONQUESTION, MB_ICONINFORMATION = 0x0, 0x4, 0x20, 0x40
MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x10000, 0x40000, 6

_API = None


def win32():
    """ctypes bindings (private WinDLL instances, so that the prototypes set here never change the ones pywebview
    calls through ctypes.windll)."""
    global _API
    if _API is not None:
        return _API
    import ctypes
    from ctypes import wintypes as W

    class Api:
        pass

    a = Api()
    a.ctypes, a.W = ctypes, W
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:
        shcore = None
    LRESULT = ctypes.c_ssize_t
    a.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    a.WINEVENTPROC = ctypes.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND, W.LONG, W.LONG, W.DWORD, W.DWORD)
    a.MONITORENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", a.WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HANDLE),
                    ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE), ("lpszMenuName", W.LPCWSTR),
                    ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HANDLE)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD),
                    ("szDevice", W.WCHAR * 32)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                    ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                    ("biClrImportant", W.DWORD)]

    class TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("dwFlags", W.DWORD), ("hwndTrack", W.HWND), ("dwHoverTime", W.DWORD)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", W.BYTE * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                    ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                    ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                    ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", W.UINT), ("hwnd", W.HWND), ("dwFlags", W.DWORD), ("uCount", W.UINT),
                    ("dwTimeout", W.DWORD)]

    a.NOTIFYICONDATAW, a.FLASHWINFO = NOTIFYICONDATAW, FLASHWINFO
    a.WNDCLASSEXW, a.MONITORINFOEXW, a.BLENDFUNCTION = WNDCLASSEXW, MONITORINFOEXW, BLENDFUNCTION
    a.BITMAPINFOHEADER, a.TRACKMOUSEEVENT = BITMAPINFOHEADER, TRACKMOUSEEVENT
    P = ctypes.POINTER

    def fn(dll, name, restype, *argtypes, optional=False):
        try:
            f = getattr(dll, name)
        except AttributeError:
            if optional:
                return None
            raise
        f.restype, f.argtypes = restype, list(argtypes)
        setattr(a, name, f)
        return f

    H, U, I, B = W.HWND, W.UINT, ctypes.c_int, W.BOOL
    fn(user32, "RegisterClassExW", W.ATOM, P(WNDCLASSEXW))
    fn(user32, "UnregisterClassW", B, W.LPCWSTR, W.HINSTANCE)
    fn(user32, "CreateWindowExW", H, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, I, I, I, I, H, W.HMENU,
       W.HINSTANCE, W.LPVOID)
    fn(user32, "DefWindowProcW", LRESULT, H, U, W.WPARAM, W.LPARAM)
    fn(user32, "DestroyWindow", B, H)
    fn(user32, "ShowWindow", B, H, I)
    fn(user32, "IsWindow", B, H)
    fn(user32, "IsWindowVisible", B, H)
    fn(user32, "IsIconic", B, H)
    fn(user32, "SetWindowPos", B, H, H, I, I, I, I, U)
    fn(user32, "GetWindowRect", B, H, P(W.RECT))
    fn(user32, "ClientToScreen", B, H, P(W.POINT))
    fn(user32, "GetClientRect", B, H, P(W.RECT))
    fn(user32, "SetTimer", ctypes.c_size_t, H, ctypes.c_size_t, U, W.LPVOID)
    fn(user32, "KillTimer", B, H, ctypes.c_size_t)
    fn(user32, "GetCursorPos", B, P(W.POINT))
    fn(user32, "SetCapture", H, H)
    fn(user32, "ReleaseCapture", B)
    fn(user32, "GetMessageW", I, P(W.MSG), H, U, U)
    fn(user32, "TranslateMessage", B, P(W.MSG))
    fn(user32, "DispatchMessageW", LRESULT, P(W.MSG))
    fn(user32, "PostMessageW", B, H, U, W.WPARAM, W.LPARAM)
    fn(user32, "PostQuitMessage", None, I)
    fn(user32, "GetDC", W.HDC, H)
    fn(user32, "ReleaseDC", I, H, W.HDC)
    fn(user32, "UpdateLayeredWindow", B, H, W.HDC, P(W.POINT), P(W.SIZE), W.HDC, P(W.POINT), W.DWORD,
       P(BLENDFUNCTION), W.DWORD)
    fn(user32, "LoadCursorW", W.HANDLE, W.HINSTANCE, W.LPVOID)
    fn(user32, "CreatePopupMenu", W.HMENU)
    fn(user32, "AppendMenuW", B, W.HMENU, U, ctypes.c_size_t, W.LPCWSTR)
    fn(user32, "SetMenuDefaultItem", B, W.HMENU, U, U)
    fn(user32, "TrackPopupMenuEx", I, W.HMENU, U, I, I, H, W.LPVOID)
    fn(user32, "DestroyMenu", B, W.HMENU)
    fn(user32, "SetForegroundWindow", B, H)
    fn(user32, "GetForegroundWindow", H)
    fn(user32, "AllowSetForegroundWindow", B, W.DWORD)
    fn(user32, "TrackMouseEvent", B, P(TRACKMOUSEEVENT))
    fn(user32, "EnumDisplayMonitors", B, W.HDC, W.LPVOID, a.MONITORENUMPROC, W.LPARAM)
    fn(user32, "GetMonitorInfoW", B, W.HMONITOR, P(MONITORINFOEXW))
    fn(user32, "SetWinEventHook", W.HANDLE, W.DWORD, W.DWORD, W.HMODULE, a.WINEVENTPROC, W.DWORD, W.DWORD,
       W.DWORD)
    fn(user32, "UnhookWinEvent", B, W.HANDLE)
    fn(user32, "RegisterWindowMessageW", U, W.LPCWSTR)
    fn(user32, "FindWindowW", H, W.LPCWSTR, W.LPCWSTR)
    fn(user32, "GetWindowThreadProcessId", W.DWORD, H, P(W.DWORD))
    fn(user32, "GetWindowLongW", W.LONG, H, I)
    fn(user32, "GetSystemMetrics", I, I)
    fn(user32, "MessageBoxW", I, H, W.LPCWSTR, W.LPCWSTR, U)
    fn(user32, "GetDpiForWindow", U, H, optional=True)
    fn(user32, "GetWindow", H, H, U)
    fn(user32, "GetAncestor", H, H, U)
    fn(user32, "GetDoubleClickTime", U)
    fn(user32, "LoadIconW", W.HICON, W.HINSTANCE, W.LPVOID)
    fn(user32, "DestroyIcon", B, W.HICON)
    fn(user32, "FlashWindowEx", B, P(FLASHWINFO))
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        shell32 = None
    a.Shell_NotifyIconW = a.ExtractIconExW = None
    if shell32 is not None:
        fn(shell32, "Shell_NotifyIconW", B, W.DWORD, P(NOTIFYICONDATAW), optional=True)
        fn(shell32, "ExtractIconExW", U, W.LPCWSTR, I, P(W.HICON), P(W.HICON), U, optional=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None
    a.DwmGetWindowAttribute = None
    if dwmapi is not None:
        fn(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, H, W.DWORD, W.LPVOID, W.DWORD, optional=True)
    fn(user32, "SetThreadDpiAwarenessContext", W.LPVOID, W.LPVOID, optional=True)
    fn(kernel32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
    fn(kernel32, "CreateMutexW", W.HANDLE, W.LPVOID, B, W.LPCWSTR)
    fn(kernel32, "CloseHandle", B, W.HANDLE)
    fn(gdi32, "CreateCompatibleDC", W.HDC, W.HDC)
    fn(gdi32, "CreateDIBSection", W.HBITMAP, W.HDC, W.LPVOID, U, P(W.LPVOID), W.HANDLE, W.DWORD)
    fn(gdi32, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    fn(gdi32, "DeleteObject", B, W.HGDIOBJ)
    fn(gdi32, "DeleteDC", B, W.HDC)
    fn(gdi32, "GetDeviceCaps", I, W.HDC, I)
    a.GetDpiForMonitor = None
    if shcore is not None:
        fn(shcore, "GetDpiForMonitor", ctypes.c_long, W.HMONITOR, I, P(U), P(U), optional=True)
    a.HWND_TOPMOST, a.HWND_NOTOPMOST = W.HWND(-1), W.HWND(-2)
    _API = a
    return a


def use_physical_pixels() -> bool:
    """Make the calling thread per-monitor DPI aware (v2): its window rectangles are then real pixels on every
    monitor, like the launcher's. Windows 10 1703+; False (and nothing changed) elsewhere."""
    if os.name != "nt":
        return False
    a = win32()
    if a.SetThreadDpiAwarenessContext is None:
        return False
    return bool(a.SetThreadDpiAwarenessContext(a.W.LPVOID(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))


def session_ending() -> bool:
    """True while Windows is logging off / shutting down (the chat window must then really close)."""
    return os.name == "nt" and bool(win32().GetSystemMetrics(SM_SHUTTINGDOWN))


def message_box(title: str, body: str, flags: int = MB_OK | MB_ICONINFORMATION) -> int:
    if os.name != "nt":
        log.info("%s: %s", title, body)
        return 0
    return win32().MessageBoxW(None, body, title, flags | MB_SETFOREGROUND | MB_TOPMOST)


def ask_yes_no(title: str, body: str) -> bool:
    return message_box(title, body, MB_YESNO | MB_ICONQUESTION) == IDYES


def window_state(hwnd) -> dict:
    """Visible / minimized / foreground of any top-level window (for the chat window)."""
    a = win32()
    return {"visible": bool(a.IsWindowVisible(hwnd)), "minimized": bool(a.IsIconic(hwnd)),
            "foreground": (a.GetForegroundWindow() or 0) == hwnd}


def flash_window(hwnd) -> None:
    """Flash a window's taskbar button until it comes to the front (a minimized chat with a new answer)."""
    a = win32()
    info = a.FLASHWINFO()
    info.cbSize = a.ctypes.sizeof(info)
    info.hwnd = hwnd
    info.dwFlags = FLASHW_ALL | FLASHW_TIMERNOFG
    a.FlashWindowEx(a.ctypes.byref(info))


def bring_to_front(hwnd) -> None:
    """Restore a minimized window, raise it above the other windows and make it the active one (allowed: the user
    just clicked our icon)."""
    a = win32()
    if a.IsIconic(hwnd):
        a.ShowWindow(hwnd, SW_RESTORE)
    a.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)   # None = HWND_TOP
    a.SetForegroundWindow(hwnd)


def _rect(hwnd) -> tuple[int, int, int, int]:
    a = win32()
    r = a.W.RECT()
    a.GetWindowRect(hwnd, a.ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _cloaked(hwnd) -> bool:
    """A window on another virtual desktop or a suspended app: 'visible' but not on the screen."""
    a = win32()
    if a.DwmGetWindowAttribute is None:
        return False
    v = a.W.DWORD()
    return a.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, a.ctypes.byref(v), a.ctypes.sizeof(v)) == 0 and bool(v.value)


def in_front(hwnd, ignore=()) -> bool:
    """Is this top-level window visible and in front: the active window (or the owner of the active dialog), or
    not covered by any other normal window? Always-on-top windows (the taskbar, our own icon), click-through
    overlays and hidden or cloaked windows do not count as covering it."""
    a = win32()
    if not hwnd or not a.IsWindowVisible(hwnd) or a.IsIconic(hwnd):
        return False
    fg = a.GetForegroundWindow()
    if fg and (fg == hwnd or a.GetAncestor(fg, GA_ROOTOWNER) == hwnd):
        return True
    left, top, right, bottom = _rect(hwnd)
    skip = {int(h) for h in ignore if h}
    w = a.GetWindow(hwnd, GW_HWNDPREV)
    for _ in range(5000):  # walk up the z-order, from just above the window to the top
        if not w:
            return True
        ex = a.GetWindowLongW(w, GWL_EXSTYLE) & 0xFFFFFFFF
        if int(w) not in skip and a.IsWindowVisible(w) and not a.IsIconic(w) and \
                not ex & (WS_EX_TOPMOST | WS_EX_TRANSPARENT) and not _cloaked(w) and \
                a.GetAncestor(w, GA_ROOTOWNER) != hwnd:
            l2, t2, r2, b2 = _rect(w)
            if min(right, r2) - max(left, l2) > 8 and min(bottom, b2) - max(top, t2) > 8:
                return False
        w = a.GetWindow(w, GW_HWNDPREV)
    return True


def icon_overlap(icon: Sequence[int], client: Sequence[int]) -> Optional[tuple]:
    """The part of a window's client area (left, top, right, bottom in screen pixels) the icon covers, as
    fractions (x0, y0, x1, y1) of the client's width and height - independent of the DPI the page renders at.
    None when they do not overlap."""
    cw, ch = client[2] - client[0], client[3] - client[1]
    if cw <= 0 or ch <= 0:
        return None
    x0, y0 = max(icon[0], client[0]), max(icon[1], client[1])
    x1, y1 = min(icon[2], client[2]), min(icon[3], client[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return tuple(round(v, 4) for v in ((x0 - client[0]) / cw, (y0 - client[1]) / ch,
                                        (x1 - client[0]) / cw, (y1 - client[1]) / ch))


def double_click_seconds() -> float:
    if os.name != "nt":
        return 0.5
    try:
        return max(0.2, min(win32().GetDoubleClickTime() / 1000.0, 1.0))
    except Exception:
        return 0.5


def system_dpi() -> int:
    a = win32()
    hdc = a.GetDC(None)
    try:
        return int(a.GetDeviceCaps(hdc, LOGPIXELSX)) or 96
    finally:
        a.ReleaseDC(None, hdc)


def list_monitors() -> list[Monitor]:
    """All monitors with their work areas, in the coordinates of the calling thread's DPI awareness."""
    a = win32()
    ctypes = a.ctypes
    found: list[Monitor] = []

    def each(hmon, hdc, rect, data):
        info = a.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if a.GetMonitorInfoW(hmon, ctypes.byref(info)):
            dpi = 0
            if a.GetDpiForMonitor is not None:
                dx, dy = a.W.UINT(), a.W.UINT()
                if a.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    dpi = int(dx.value)
            r, w = info.rcMonitor, info.rcWork
            found.append(Monitor(info.szDevice, (r.left, r.top, r.right, r.bottom),
                                 (w.left, w.top, w.right, w.bottom), dpi or system_dpi(), bool(info.dwFlags & 1)))
        return True

    proc = a.MONITORENUMPROC(each)
    a.EnumDisplayMonitors(None, None, proc, 0)
    if not found:
        w, h = a.GetSystemMetrics(0), a.GetSystemMetrics(1)
        found.append(Monitor("", (0, 0, w, h), (0, 0, w, h), system_dpi(), True))
    return found


class SingleInstance:
    """One instance per Windows session and settings location, guarded by a named mutex (Windows releases it even if
    the process dies). A second start of the exe does not start another backend: it asks the running one, through its
    launcher icon, to open the chat window, and exits."""

    def __init__(self, key: str):
        self.key = key
        self._handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        a = win32()
        handle = a.CreateMutexW(None, False, f"Local\\TechRAG.{self.key}")
        err = a.ctypes.get_last_error()
        if not handle:
            log.warning("single-instance mutex unavailable (error %s): starting anyway", err)
            return True
        if err == ERROR_ALREADY_EXISTS:
            a.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            win32().CloseHandle(self._handle)
            self._handle = None

    def notify_running(self, timeout: float = 20.0) -> bool:
        """Ask the running instance to show its chat window (waits while it is still starting up)."""
        if os.name != "nt":
            return False
        a = win32()
        msg = a.RegisterWindowMessageW(ACTIVATE_MESSAGE)
        deadline = time.monotonic() + timeout
        while True:
            hwnd = a.FindWindowW(Launcher.class_name(self.key), None)
            if hwnd:
                pid = a.W.DWORD()
                a.GetWindowThreadProcessId(hwnd, a.ctypes.byref(pid))
                a.AllowSetForegroundWindow(pid.value)  # it may bring its chat window to the front
                return bool(a.PostMessageW(hwnd, msg, 0, 0))
            if time.monotonic() > deadline:
                return False
            time.sleep(0.25)


def _signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def lparam_point(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


class Launcher:
    """The icon window. It only reports what the user did; the callbacks must return quickly (the app queues the
    actual work on its own thread):

    on_click()               - a click (not a drag): open the chat
    on_command(IDM_*)        - a context-menu choice
    on_activate()            - the exe was started a second time
    on_moved(anchor)         - the icon was dragged somewhere else (anchor: see anchor_for)
    on_session_end(final)    - Windows logging off / shutting down: None = asked, True = happening now,
                               False = cancelled by the user
    on_close_request()       - another program asked the app to close (taskkill without /F, an installer)
    on_overlap(area)         - the part of the chat window's client area the icon covers, as fractions
                               (x0, y0, x1, y1) of its width and height, or None (see icon_overlap)

    The icon always stays above normal windows, also above the chat window: it must never vanish behind it
    (the chat page keeps its own controls clear of the area reported by on_overlap).
    """

    def __init__(self, key: str, anchor: Optional[dict], lang: Callable[[], str], on_click: Callable[[], None],
                 on_command: Callable[[int], None], on_activate: Callable[[], None],
                 on_moved: Callable[[dict], None], on_session_end: Optional[Callable[[Optional[bool]], None]] = None,
                 on_close_request: Optional[Callable[[], None]] = None, title: str = APP_TITLE,
                 on_overlap: Optional[Callable[[Optional[tuple]], None]] = None):
        self.key, self.title = key, title
        self.anchor = dict(anchor) if isinstance(anchor, dict) else default_anchor()
        self.lang = lang
        self.on_click, self.on_command, self.on_activate = on_click, on_command, on_activate
        self.on_moved, self.on_session_end = on_moved, on_session_end or (lambda final: None)
        self.on_close_request = on_close_request or (lambda: on_command(IDM_QUIT))
        self.on_overlap = on_overlap or (lambda area: None)
        self._overlap: Optional[tuple] = None
        self._note: Optional[tuple[str, str]] = None   # the notification to show (title, text)
        self._note_lock = threading.Lock()
        self._tray = False                              # our notification-area icon is there
        self._icons: tuple = ()                         # (small, large) app icons for it
        self.notes_shown = 0
        self.note_error = ""
        self._closing = False
        self.hwnd = None
        self.companion = None     # chat window: the page is told which part of it the icon covers
        self.placed: dict = {}
        self.error = ""
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.tracker = PressTracker()
        self._size = icon_px(96)
        self._hover = False
        self._pixels: dict = {}
        self._activate_msg = 0
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def class_name(key: str) -> str:
        return f"TechRAG.Launcher.{key}"

    # -------------------------------------------------------------- called from other threads
    def start(self, timeout: float = 10.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="launcher", daemon=True)
        self._thread.start()
        self.ready.wait(timeout)
        return bool(self.hwnd)

    def post(self, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        hwnd = self.hwnd
        return bool(hwnd) and bool(win32().PostMessageW(hwnd, msg, wparam, lparam))

    def hide(self) -> None:
        self.post(WM_APP_HIDE)

    def show(self) -> None:
        self.post(WM_APP_SHOW)

    def close(self, wait: float = 5.0) -> None:
        self._closing = True  # our own close; a WM_CLOSE from elsewhere is a request to quit the whole app
        if self.post(WM_CLOSE):
            self.closed.wait(wait)

    def notify(self, title: str, body: str) -> bool:
        """Show a Windows notification: a notification-area balloon, which Windows 10/11 shows as a toast (Focus
        assist / quiet hours are respected). A click on it - or on its icon, which stays until the chat is opened -
        opens the chat. Any thread."""
        with self._note_lock:
            self._note = (title, body)
        return self.post(WM_APP_NOTIFY)

    def clear_notification(self) -> None:
        """The chat was opened: the notification icon goes."""
        if self._tray:
            self.post(WM_APP_UNNOTIFY)

    def set_companion(self, hwnd) -> None:
        self.companion = int(hwnd) if hwnd else None
        self.post(WM_APP_ZORDER)  # re-check now: the chat window may already be the active one

    def info(self) -> dict:
        """Placement and window styles (call from a thread made per-monitor aware with use_physical_pixels())."""
        a, hwnd = win32(), self.hwnd
        if not hwnd:
            return {}
        r = a.W.RECT()
        a.GetWindowRect(hwnd, a.ctypes.byref(r))
        ex = a.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        return dict(self.placed, rect=[r.left, r.top, r.right, r.bottom], visible=bool(a.IsWindowVisible(hwnd)),
                    topmost=bool(ex & WS_EX_TOPMOST), noactivate=bool(ex & WS_EX_NOACTIVATE),
                    toolwindow=bool(ex & WS_EX_TOOLWINDOW), layered=bool(ex & WS_EX_LAYERED))

    # -------------------------------------------------------------- launcher thread
    def _run(self) -> None:
        a = win32()
        ctypes = a.ctypes
        try:
            use_physical_pixels()
            self._hinst = a.GetModuleHandleW(None)
            self._wndproc = a.WNDPROC(self._proc)        # keep references: Windows calls these
            self._hook_proc = a.WINEVENTPROC(self._on_foreground)
            wc = a.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = self._hinst
            wc.hCursor = a.LoadCursorW(None, a.W.LPVOID(IDC_HAND))
            wc.lpszClassName = self.class_name(self.key)
            if not a.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
            self._activate_msg = a.RegisterWindowMessageW(ACTIVATE_MESSAGE)
            mon, x, y, size = place(self.anchor, self._monitors())
            self._size = size
            hwnd = a.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                     self.class_name(self.key), self.title, WS_POPUP, x, y, size, size,
                                     None, None, self._hinst, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            self.hwnd = hwnd
            self._render(x, y, size)
            a.ShowWindow(hwnd, SW_SHOWNOACTIVATE)       # visible without taking the focus
            a.SetWindowPos(hwnd, a.HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._placed(mon, x, y, size)
            if self._window_dpi() != mon.dpi:          # created on another monitor than computed: redo
                self._place()
            self._hook = a.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                                           self._hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
            a.SetTimer(hwnd, OVERLAP_TIMER, OVERLAP_MS, None)   # the chat window moves, resizes, maximizes
            log.info("launcher icon at %s (%d px, %s dpi, monitor %s)", (x, y), size, mon.dpi, mon.name)
        except Exception as exc:
            log.exception("launcher icon could not be created")
            self.error = f"{exc.__class__.__name__}: {exc}"
            self.hwnd = None
            self.ready.set()
            self.closed.set()
            return
        self.ready.set()
        msg = a.W.MSG()
        while a.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            a.TranslateMessage(ctypes.byref(msg))
            a.DispatchMessageW(ctypes.byref(msg))
        if getattr(self, "_hook", None):
            a.UnhookWinEvent(self._hook)
        a.UnregisterClassW(self.class_name(self.key), self._hinst)
        self.hwnd = None
        self.closed.set()

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            r = self._handle(hwnd, msg, wparam or 0, lparam or 0)
            if r is not None:
                return r
        except Exception:
            log.exception("launcher message %#x", msg)
        return win32().DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle(self, hwnd, msg: int, wp: int, lp: int) -> Optional[int]:
        a = win32()
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE                          # clicks never activate the icon
        if msg == WM_LBUTTONDOWN:
            a.SetCapture(hwnd)
            self.tracker.threshold = max(1, round(DRAG_DIP * self._window_dpi() / 96))
            self.tracker.press(self._screen_point(hwnd, lp), self._position())
            return 0
        if msg == WM_MOUSEMOVE:
            if self.tracker.active:
                pos = self.tracker.move(self._screen_point(hwnd, lp))
                if pos is not None:
                    self._move(*self._inside_virtual_screen(pos))
            elif not self._hover:
                self._hover = True
                self._render()
                tme = a.TRACKMOUSEEVENT(a.ctypes.sizeof(a.TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                a.TrackMouseEvent(a.ctypes.byref(tme))
            return 0
        if msg == WM_LBUTTONUP:
            kind = self.tracker.release(self._screen_point(hwnd, lp))
            a.ReleaseCapture()
            if kind == "drag":
                self._settle()
            elif kind == "click":
                self._call(self.on_click)
            return 0
        if msg == WM_CAPTURECHANGED:
            if self.tracker.active:                      # e.g. Alt+Tab in the middle of a drag
                dragging = self.tracker.dragging
                self.tracker.cancel()
                if dragging:
                    self._settle()
            return 0
        if msg == WM_MOUSELEAVE:
            self._hover = False
            self._render()
            return 0
        if msg == WM_CONTEXTMENU:
            self._menu(lp)
            return 0
        if msg == WM_COMMAND and (wp & 0xFFFF) in (IDM_OPEN, IDM_SETTINGS, IDM_QUIT):
            self._call(self.on_command, wp & 0xFFFF)
            return 0
        if msg == WM_DPICHANGED:
            r = a.ctypes.cast(lp, a.ctypes.POINTER(a.W.RECT)).contents
            if self.tracker.dragging:                    # moved onto a monitor with another scale factor
                self._render(r.left, r.top, r.right - r.left)
            else:                                        # scale changed in Settings: same corner, new size
                self._place()
            return 0
        if msg == WM_DISPLAYCHANGE or (msg == WM_SETTINGCHANGE and wp == SPI_SETWORKAREA):
            if not self.tracker.active:                  # resolution, monitor or taskbar changed: stay visible
                self._place()
            return 0
        if msg == WM_QUERYENDSESSION:
            self._call(self.on_session_end, None)
            return 1                                     # never block logging off
        if msg == WM_ENDSESSION:
            self._call(self.on_session_end, bool(wp))   # False: the user cancelled logging off
            return 0
        if msg == WM_APP_ZORDER:
            self._on_foreground(None, 0, a.GetForegroundWindow(), 0, 0, 0, 0)
            return 0
        if msg == WM_TIMER and wp == OVERLAP_TIMER:
            self._check_overlap()
            return 0
        if msg == WM_APP_NOTIFY:
            self._show_note(hwnd)
            return 0
        if msg == WM_APP_UNNOTIFY:
            self._remove_note(hwnd)
            return 0
        if msg == WM_APP_TRAY:   # NOTIFYICON_VERSION_4: the event is the low word
            if (lp & 0xFFFF) in (NIN_BALLOONUSERCLICK, NIN_SELECT, NIN_KEYSELECT):
                self._remove_note(hwnd)
                self._call(self.on_command, IDM_OPEN)
            return 0
        if msg == WM_APP_HIDE:
            a.ShowWindow(hwnd, SW_HIDE)
            return 0
        if msg == WM_APP_SHOW:
            a.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            a.SetWindowPos(hwnd, a.HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            return 0
        if msg == WM_CLOSE:
            if self._closing:
                a.DestroyWindow(hwnd)
            else:  # not the app closing its icon: another program wants the app to end - quit it properly
                self._call(self.on_close_request)
            return 0
        if msg == WM_DESTROY:
            a.KillTimer(hwnd, OVERLAP_TIMER)
            self._remove_note(hwnd)
            for icon in self._icons:
                if icon:
                    a.DestroyIcon(icon)
            a.PostQuitMessage(0)
            return 0
        if self._activate_msg and msg == self._activate_msg:
            self._call(self.on_activate)
            return 0
        return None

    def _note_data(self, hwnd):
        a = win32()
        d = a.NOTIFYICONDATAW()
        d.cbSize = a.ctypes.sizeof(d)
        d.hWnd = hwnd
        d.uID = TRAY_ID
        return d

    def _app_icons(self) -> tuple:
        """The exe's own icon (small for the notification area, large for the toast)."""
        if not self._icons:
            a = win32()
            small, large = a.W.HICON(), a.W.HICON()
            if a.ExtractIconExW is not None:
                a.ExtractIconExW(sys.executable, 0, a.ctypes.byref(large), a.ctypes.byref(small), 1)
            fallback = a.LoadIconW(None, a.W.LPVOID(32512))   # IDI_APPLICATION
            self._icons = (small.value or fallback, large.value or small.value or fallback)
        return self._icons

    def _show_note(self, hwnd) -> None:
        a = win32()
        with self._note_lock:
            note, self._note = self._note, None
        if not note:
            return
        if a.Shell_NotifyIconW is None:
            self.note_error = "Shell_NotifyIconW unavailable"
            return
        title, body = note
        small, large = self._app_icons()
        tip = f"{self.title} — {title}"[:127]
        if not self._tray:
            d = self._note_data(hwnd)
            d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            d.uCallbackMessage = WM_APP_TRAY
            d.hIcon = small
            d.szTip = tip
            if not a.Shell_NotifyIconW(NIM_ADD, a.ctypes.byref(d)):
                self.note_error = f"Shell_NotifyIconW(NIM_ADD) failed ({a.ctypes.get_last_error()})"
                log.warning("notification icon could not be added: %s", self.note_error)
                return
            d.uVersion = NOTIFYICON_VERSION_4
            a.Shell_NotifyIconW(NIM_SETVERSION, a.ctypes.byref(d))
            self._tray = True
        d = self._note_data(hwnd)
        d.uFlags = NIF_INFO | NIF_TIP | NIF_SHOWTIP
        d.szTip = tip
        d.szInfoTitle = title[:63]
        d.szInfo = (body or " ")[:255]
        d.hBalloonIcon = large
        d.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON | NIIF_RESPECT_QUIET_TIME
        if a.Shell_NotifyIconW(NIM_MODIFY, a.ctypes.byref(d)):
            self.notes_shown += 1
            self.note_error = ""
        else:
            self.note_error = f"Shell_NotifyIconW(NIM_MODIFY) failed ({a.ctypes.get_last_error()})"
            log.warning("notification could not be shown: %s", self.note_error)

    def _remove_note(self, hwnd) -> None:
        if not self._tray:
            return
        a = win32()
        d = self._note_data(hwnd)
        a.Shell_NotifyIconW(NIM_DELETE, a.ctypes.byref(d))
        self._tray = False

    def _call(self, func, *args) -> None:
        try:
            func(*args)
        except Exception:
            log.exception("launcher callback failed")

    def _screen_point(self, hwnd, lp: int) -> tuple[int, int]:
        a = win32()
        pt = a.W.POINT(_signed16(lp), _signed16(lp >> 16))
        a.ClientToScreen(hwnd, a.ctypes.byref(pt))
        return pt.x, pt.y

    def _position(self) -> tuple[int, int]:
        a = win32()
        r = a.W.RECT()
        a.GetWindowRect(self.hwnd, a.ctypes.byref(r))
        return r.left, r.top

    def _move(self, x: int, y: int) -> None:
        win32().SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def _inside_virtual_screen(self, pos: tuple[int, int]) -> tuple[int, int]:
        a = win32()
        vx, vy = a.GetSystemMetrics(SM_XVIRTUALSCREEN), a.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = a.GetSystemMetrics(SM_CXVIRTUALSCREEN), a.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            return pos
        return clamp_into(pos[0], pos[1], self._size, (vx, vy, vx + vw, vy + vh))

    def _settle(self) -> None:
        """End of a drag: snap fully into the work area of the monitor under the icon's centre and remember it."""
        x, y = self._position()
        mons = self._monitors()
        mon = monitor_at(mons, x + self._size // 2, y + self._size // 2)
        x, y = clamp_into(x, y, self._size, mon.work)
        self._move(x, y)
        self.anchor = anchor_for(x, y, self._size, mon)
        self._placed(mon, x, y, self._size)
        self._call(self.on_moved, dict(self.anchor))

    def _place(self) -> None:
        mon, x, y, size = place(self.anchor, self._monitors())
        if size != self._size:
            self._render(x, y, size)
        else:
            self._move(x, y)
        self._placed(mon, x, y, size)

    def _placed(self, mon: Monitor, x: int, y: int, size: int) -> None:
        self.placed = {"monitor": mon.name, "work": list(mon.work), "dpi": mon.dpi, "x": x, "y": y, "size": size,
                       "size_dip": round(size * 96 / max(mon.dpi, 48), 1), "anchor": dict(self.anchor)}

    def _system_dpi(self) -> int:
        return system_dpi()

    def _monitors(self) -> list[Monitor]:
        return list_monitors()

    def _window_dpi(self) -> int:
        a = win32()
        if self.hwnd and a.GetDpiForWindow is not None:
            d = a.GetDpiForWindow(self.hwnd)
            if d:
                return int(d)
        return self._system_dpi()

    def _render(self, x: Optional[int] = None, y: Optional[int] = None, size: Optional[int] = None) -> None:
        """Draw the icon (per-pixel alpha); with x/y/size also move/resize the window in the same step."""
        a = win32()
        ctypes = a.ctypes
        size = int(size or self._size)
        self._size = size
        key = (size, self._hover)
        if key not in self._pixels:
            self._pixels = {k: v for k, v in self._pixels.items() if k[0] == size}
            self._pixels[key] = render_icon(size, self._hover)
        pixels = self._pixels[key]
        screen = a.GetDC(None)
        mem = a.CreateCompatibleDC(screen)
        bmi = a.BITMAPINFOHEADER(ctypes.sizeof(a.BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = a.W.LPVOID()
        bmp = a.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            if not bmp or not bits:
                raise OSError("CreateDIBSection failed")
            ctypes.memmove(bits, pixels, len(pixels))
            old = a.SelectObject(mem, bmp)
            blend = a.BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            dst = a.W.POINT(int(x), int(y)) if x is not None and y is not None else None
            a.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(dst) if dst is not None else None,
                                  ctypes.byref(a.W.SIZE(size, size)), mem, ctypes.byref(a.W.POINT(0, 0)), 0,
                                  ctypes.byref(blend), ULW_ALPHA)
            a.SelectObject(mem, old)
        finally:
            if bmp:
                a.DeleteObject(bmp)
            a.DeleteDC(mem)
            a.ReleaseDC(None, screen)

    def _menu(self, lp: int) -> None:
        a = win32()
        lang = "tr"
        try:
            lang = self.lang() or "tr"
        except Exception:
            pass
        if lp in (-1, 0xFFFFFFFF):
            pt = a.W.POINT()
            a.GetCursorPos(a.ctypes.byref(pt))
            x, y = pt.x, pt.y
        else:
            x, y = _signed16(lp), _signed16(lp >> 16)
        menu = a.CreatePopupMenu()
        a.AppendMenuW(menu, MF_STRING, IDM_OPEN, text(lang, "open"))
        a.AppendMenuW(menu, MF_STRING, IDM_SETTINGS, text(lang, "settings"))
        a.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        a.AppendMenuW(menu, MF_STRING, IDM_QUIT, text(lang, "quit"))
        a.SetMenuDefaultItem(menu, IDM_OPEN, 0)
        previous = a.GetForegroundWindow()
        a.SetForegroundWindow(self.hwnd)   # a popup menu only closes on an outside click when its owner is active
        cmd = a.TrackPopupMenuEx(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, x, y, self.hwnd, None)
        a.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        a.DestroyMenu(menu)
        if cmd:
            self._call(self.on_command, cmd)
        elif previous and previous != self.hwnd and a.IsWindow(previous):
            a.SetForegroundWindow(previous)  # menu dismissed: give the keyboard focus back

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:
        """Another window became the active one: the icon stays above normal windows (the chat window included -
        it must never vanish behind it), and the chat page learns whether the icon now covers part of it."""
        try:
            if not self.hwnd:
                return
            a = win32()
            a.SetWindowPos(self.hwnd, a.HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._check_overlap()
        except Exception:
            log.exception("launcher z-order")

    def _check_overlap(self) -> None:
        """Tell the app (on_overlap) when the part of the chat window that the icon covers changes."""
        a = win32()
        comp = self.companion
        area = None
        try:
            if comp and self.hwnd and a.IsWindow(comp) and a.IsWindowVisible(comp) and not a.IsIconic(comp) \
                    and a.IsWindowVisible(self.hwnd):
                cr = a.W.RECT()
                a.GetClientRect(comp, a.ctypes.byref(cr))
                origin = a.W.POINT(0, 0)
                a.ClientToScreen(comp, a.ctypes.byref(origin))
                client = (origin.x, origin.y, origin.x + cr.right, origin.y + cr.bottom)
                area = icon_overlap(_rect(self.hwnd), client)
        except Exception:
            log.exception("launcher overlap")
            area = None
        if area != self._overlap:
            self._overlap = area
            self._call(self.on_overlap, area)
