"""Launcher icon: placement on one or more monitors and DPI scales, click-vs-drag, drawing, state file, and (on
Windows only) the real icon window."""

import json
import os
import threading
import time

import numpy as np
import pytest

from techrag import launcher as L
from techrag.launcher import (DesktopState, Monitor, PressTracker, anchor_for, clamp_into, icon_px, monitor_at, place,
                              render_icon)

FHD = Monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), 96, True)


def inside(x, y, size, work):
    return work[0] <= x and work[1] <= y and x + size <= work[2] and y + size <= work[3]


def test_default_place_is_bottom_right_of_the_work_area():
    mon, x, y, size = place(None, [FHD])
    assert size == 60 and (x, y) == (1920 - 60 - 16, 1040 - 60 - 16), "above the taskbar, 16 DIP margin"
    hidpi = Monitor("D1", (0, 0, 2880, 1620), (0, 0, 2880, 1560), 144, True)
    _, x, y, size = place(None, [hidpi])
    assert size == 90 == icon_px(144) and (x, y) == (2880 - 90 - 24, 1560 - 90 - 24), "60 DIP at 150 %"
    assert 56 <= L.SIZE_DIP <= 64


def test_taskbar_on_the_left_or_top_is_respected():
    left_bar = Monitor("D1", (0, 0, 1920, 1080), (60, 0, 1920, 1080), 96, True)
    top_bar = Monitor("D1", (0, 0, 1920, 1080), (0, 40, 1920, 1080), 96, True)
    for mon in (left_bar, top_bar):
        _, x, y, size = place({"h": "left", "dx": 0, "v": "top", "dy": 0}, [mon])
        assert (x, y) == mon.work[:2] and inside(x, y, size, mon.work)


def test_remembered_corner_survives_resolution_and_scale_changes():
    mon, x, y, size = place(None, [FHD])
    a = anchor_for(x - 300, y - 200, size, mon)   # dragged somewhere near the bottom-right
    assert a == {"monitor": FHD.name, "h": "right", "dx": 316.0, "v": "bottom", "dy": 216.0}
    small = Monitor(FHD.name, (0, 0, 1280, 720), (0, 0, 1280, 680), 96, True)
    _, x2, y2, s2 = place(a, [small])
    assert (1280 - x2 - s2, 680 - y2 - s2) == (316, 216), "same distance to the bottom-right corner"
    scaled = Monitor(FHD.name, (0, 0, 1920, 1080), (0, 0, 1920, 1040), 120, True)
    _, x3, y3, s3 = place(a, [scaled])
    assert s3 == 75 and (1920 - x3 - s3, 1040 - y3 - s3) == (395, 270), "DIP distances scale with the monitor"


def test_unplugged_monitor_falls_back_to_the_primary_one():
    second = Monitor("\\\\.\\DISPLAY2", (1920, -200, 3840, 880), (1920, -200, 3840, 880), 144, False)
    _, x, y, size = place({"h": "left", "dx": 100, "v": "top", "dy": 50}, [FHD])
    a = anchor_for(1920 + 150, -200 + 75, 90, second)
    assert a["monitor"] == second.name
    mon, x, y, size = place(a, [FHD])                 # DISPLAY2 is gone
    assert mon is FHD and inside(x, y, size, FHD.work)
    mon, x, y, size = place(a, [FHD, second])         # and back again
    assert mon is second and (x, y) == (2070, -125) and size == 90


def test_points_and_windows_are_kept_on_screen():
    assert clamp_into(5000, -300, 60, FHD.work) == (1860, 0)
    assert clamp_into(-40, 2000, 60, FHD.work) == (0, 980)
    left = Monitor("L", (-1280, 0, 0, 1024), (-1280, 0, 0, 984), 96, False)
    assert monitor_at([FHD, left], -10, 500) is left
    assert monitor_at([FHD, left], 2500, 500) is FHD, "outside every monitor: the nearest"
    assert monitor_at([FHD, left], -2000, 2000) is left
    for bad in ({"dx": "x", "dy": float("nan")}, {"dx": 1e12, "dy": -5}, "garbage", {"monitor": 3}):
        _, x, y, size = place(bad, [FHD])
        assert inside(x, y, size, FHD.work)


def test_press_tracker_tells_a_click_from_a_drag():
    t = PressTracker(threshold=4)
    t.press((100, 100), (50, 50))
    assert t.move((103, 97)) is None and t.release((102, 101)) == "click", "a little jitter is still a click"
    t.press((100, 100), (50, 50))
    assert t.move((110, 100)) == (60, 50) and t.dragging
    assert t.move((101, 99)) == (51, 49), "once dragging, coming back does not make it a click"
    assert t.release((101, 99)) == "drag" and not t.active
    assert t.release((0, 0)) is None and t.move((5, 5)) is None
    t.press((0, 0), (0, 0))
    t.cancel()
    assert t.release((50, 50)) is None


def test_icon_is_round_with_transparent_corners():
    for size in (60, 90, 120):
        px = np.frombuffer(render_icon(size), np.uint8).reshape(size, size, 4)
        assert px[0, 0, 3] == px[0, -1, 3] == px[-1, 0, 3] == px[-1, -1, 3] == 0, "corners click-through"
        assert px[size // 2, size // 5, 3] == 255, "opaque disc"
        assert (px[..., :3] <= px[..., 3:4]).all(), "premultiplied alpha"
        edge = px[size // 2, :, 3]
        assert ((edge > 0) & (edge < 255)).any(), "anti-aliased edge"
    assert render_icon(60, hover=True) != render_icon(60)


def test_desktop_state_file(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text("{broken", encoding="utf-8")
    st = DesktopState(path)
    assert st.get("launcher") is None
    st.set("launcher", {"h": "left", "dx": 1, "v": "top", "dy": 2})
    st.set("close_hint_shown", True)
    again = DesktopState(path)
    assert again.get("launcher")["h"] == "left" and again.get("close_hint_shown") is True
    assert json.loads(path.read_text(encoding="utf-8"))["close_hint_shown"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_instance_key_and_texts():
    assert L.instance_key("C:/a/settings.json") == L.instance_key("C:/a/settings.json")
    assert L.instance_key("C:/a/settings.json") != L.instance_key("C:/b/settings.json")
    assert set(L.TEXTS["tr"]) == set(L.TEXTS["en"])
    assert L.text("tr", "quit") == "Uygulamadan çık" and L.text("tr", "open") == "Sohbeti aç"
    assert L.text("tr", "settings") == "Ayarlar" and L.text("de", "quit") == "Quit"
    assert all("Sayısal Tasarım Uzmanı" in L.text(lang, "hint") for lang in ("tr", "en"))


def test_single_instance_is_a_no_op_off_windows():
    if os.name == "nt":
        pytest.skip("Windows: covered by the real-window tests")
    s = L.SingleInstance("k")
    assert s.acquire() and L.SingleInstance("k").acquire() and not s.notify_running(0.1)
    assert L.use_physical_pixels() is False and L.session_ending() is False


# ----------------------------------------------------------------------- Windows: the real icon window
win = pytest.mark.skipif(os.name != "nt", reason="Win32 launcher window")


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@pytest.fixture
def real_launcher():
    events = {"click": 0, "commands": [], "activate": 0, "moved": [], "session": [], "close_request": 0}
    lock = threading.Lock()

    def bump(k):
        with lock:
            events[k] += 1

    key = f"test{os.getpid()}"
    ln = L.Launcher(key, None, lambda: "tr", on_click=lambda: bump("click"),
                    on_command=lambda c: events["commands"].append(c), on_activate=lambda: bump("activate"),
                    on_moved=lambda a: events["moved"].append(a),
                    on_session_end=lambda final: events["session"].append(final),
                    on_close_request=lambda: bump("close_request"))
    assert ln.start(), ln.error
    L.use_physical_pixels()
    yield ln, events, key
    ln.close()


def _post(ln, msg, x, y):
    L.win32().PostMessageW(ln.hwnd, msg, 0 if msg == L.WM_LBUTTONUP else L.MK_LBUTTON, L.lparam_point(x, y))


@win
def test_real_icon_window(real_launcher):
    ln, events, key = real_launcher
    info = ln.info()
    assert info["visible"] and info["topmost"] and info["noactivate"] and info["toolwindow"] and info["layered"]
    assert 56 <= info["size_dip"] <= 64
    r, w = info["rect"], info["work"]
    assert w[0] <= r[0] and w[1] <= r[1] and r[2] <= w[2] and r[3] <= w[3]
    assert L.win32().GetForegroundWindow() != ln.hwnd, "showing the icon does not take the focus"

    c = (r[2] - r[0]) // 2
    _post(ln, L.WM_LBUTTONDOWN, c, c)
    _post(ln, L.WM_LBUTTONUP, c + 1, c)
    assert _wait(lambda: events["click"] == 1)

    _post(ln, L.WM_LBUTTONDOWN, c, c)
    _post(ln, L.WM_MOUSEMOVE, c - 40, c - 30)
    _post(ln, L.WM_LBUTTONUP, c, c)
    assert _wait(lambda: len(events["moved"]) == 1)
    assert events["click"] == 1, "a drag is not a click"
    moved = ln.info()
    assert moved["rect"][:2] != r[:2] and events["moved"][0]["h"] in ("left", "right")

    L.win32().PostMessageW(ln.hwnd, L.WM_COMMAND, L.IDM_SETTINGS, 0)
    assert _wait(lambda: events["commands"] == [L.IDM_SETTINGS])


@win
def test_real_single_instance_hands_over(real_launcher):
    ln, events, key = real_launcher
    first = L.SingleInstance(key)
    assert first.acquire()
    second = L.SingleInstance(key)
    assert not second.acquire(), "a second start is detected"
    assert second.notify_running(timeout=5)
    assert _wait(lambda: events["activate"] == 1), "the running instance is asked to open its chat"
    first.release()
    third = L.SingleInstance(key)
    assert third.acquire(), "released when the first one ends"
    third.release()


@win
def test_real_icon_closes_cleanly():
    ln = L.Launcher(f"close{os.getpid()}", None, lambda: "en", lambda: None, lambda c: None, lambda: None,
                    lambda a: None)
    assert ln.start(), ln.error
    hwnd = ln.hwnd
    ln.close()
    assert ln.closed.is_set() and ln.hwnd is None and not L.win32().IsWindow(hwnd)


@win
def test_real_icon_reports_foreign_close_and_logoff(real_launcher):
    ln, events, key = real_launcher
    a = L.win32()
    a.PostMessageW(ln.hwnd, L.WM_CLOSE, 0, 0)          # e.g. taskkill without /F
    assert _wait(lambda: events["close_request"] == 1)
    assert a.IsWindow(ln.hwnd), "the icon is not destroyed by someone else's WM_CLOSE: the app quits properly"
    a.PostMessageW(ln.hwnd, L.WM_QUERYENDSESSION, 0, 0)
    a.PostMessageW(ln.hwnd, L.WM_ENDSESSION, 0, 0)     # logging off cancelled
    assert _wait(lambda: events["session"] == [None, False])
