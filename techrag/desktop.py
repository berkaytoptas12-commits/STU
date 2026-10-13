"""Desktop entry point (SayisalTasarimUzmani.exe - "Sayısal Tasarım Uzmanı").

* No arguments: start the FastAPI backend in-process on localhost with a per-launch token. On Windows the app then
  shows a small round launcher icon (techrag.launcher); a click on it opens the chat window - the same UI in a native
  window (Microsoft Edge WebView2 through pywebview). No browser is opened. A second start of the exe brings the
  running one forward instead of starting another backend.
* With arguments: behave exactly like the `techrag` CLI (e.g. `SayisalTasarimUzmani.exe ingest`, `... doctor`).

(`techrag` stays the name of the Python package, CLI and TECHRAG_* variables; users see APP_TITLE.)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from techrag.config import APP_NAME, APP_TITLE

log = logging.getLogger("techrag.desktop")


def _attach_console() -> None:
    """A windowed exe started from cmd/PowerShell with arguments prints into that console."""
    if os.name != "nt":
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except OSError:
            pass


def _log_file() -> Path:
    """SayisalTasarimUzmani.log next to settings.json (%APPDATA%\\SayisalTasarimUzmani, or the existing
    %APPDATA%\\TechRAG folder of an installation from before the rename)."""
    from techrag.settings import settings_path

    d = settings_path().parent
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{APP_NAME}.log"


def _webview_version(webview) -> str:
    version = getattr(webview, "__version__", None)
    if version is None:
        try:
            from webview._version import __version__ as version
        except Exception:
            version = "?"
    return str(version)


def _message_box(title: str, text: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def _backend_socket(key: str) -> socket.socket:
    """The bound localhost socket the backend serves on. Its port is the same at every start (derived from the
    settings location): the page's browser storage - panel layout, language, open buckets - belongs to the page's
    origin, which includes the port, so a new random port at each start forgot all of it. Falls back to any free
    port when that one is taken. Handing uvicorn the bound socket leaves no window for another process to grab it."""
    preferred = 20000 + int(key, 16) % 20000
    for port in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other socket may bind the same port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", port))
            return s
        except OSError:
            s.close()
            if port == 0:
                raise
    raise OSError("no free port")


def _dialog(kind: str):
    """pywebview >= 5 exposes webview.FileDialog.*; older releases had module constants."""
    import webview

    fd = getattr(webview, "FileDialog", None)
    if fd is not None:
        return getattr(fd, kind)
    return getattr(webview, f"{kind}_DIALOG")


class JsApi:
    """Native dialogs exposed to the page as window.pywebview.api.*

    pywebview builds the JS bridge by walking every public attribute of this object recursively. The window
    must therefore live in an underscore attribute: as a public attribute pywebview descends into
    window.native (the WinForms form) and recurses through the .NET object graph forever, which froze the
    app on start-up in v0.1.0. Keep this class to public *methods* only.
    """

    def __init__(self):
        self._window = None

    def pick_folder(self):
        r = self._window.create_file_dialog(_dialog("FOLDER"))
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else None

    def pick_files(self):
        r = self._window.create_file_dialog(
            _dialog("OPEN"), allow_multiple=True,
            file_types=("Documents (*.pdf;*.docx;*.md;*.txt;*.html;*.htm)", "All files (*.*)"))
        return list(r) if r else []


SMOKE_JS = """(function () {
  var b = document.querySelector('.brand');
  return {brand: b ? b.textContent : '', ready: document.readyState,
          bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder),
          collections: !!document.querySelector('#collections'),
          host: !!window.techragHost,
          notes: !!document.querySelector('[data-tab="notes"], #tab-notes, #notes-list, .btn-note')};
})()"""


class DesktopApp:
    """The desktop app while it runs: one backend, one chat window and one launcher icon for the whole session.

    The chat window is created once and from then on only shown and hidden: the conversation, the selected buckets,
    the open PDF page and the panel sizes stay exactly as they were, and indexing (a backend thread) goes on while it
    is hidden. A click on the icon shows it, or hides it again when it is already in front; its close button hides
    it too (explained once); the full exit is the icon's "Quit" or Windows logging off. Launcher callbacks only queue
    work; it runs here on one thread, in order.
    """

    def __init__(self, chat, jobs, lang: Callable[[], str], state, hide_on_close: bool = True,
                 notice: Optional[Callable[[str, str], object]] = None,
                 confirm: Optional[Callable[[str, str], bool]] = None,
                 front: Optional[Callable[[], bool]] = None):
        from techrag import launcher as L

        self._L = L
        self.front = front or self._chat_in_front   # is the (visible) chat window in front of the others?
        self.double_click = L.double_click_seconds()
        self._last_open_click = -1e9
        self.chat, self.jobs, self._lang, self.state = chat, jobs, lang, state
        self.hide_on_close = hide_on_close
        self.notice = notice or L.message_box
        self.confirm = confirm or L.ask_yes_no
        self.launcher = None
        self.quitting = False
        self.session_ending = False   # Windows asked to log off / shut down (may still be cancelled)
        self.visible = not hide_on_close
        self.stats = {"opened": 0, "hidden": 0, "activations": 0, "hints": 0, "toggled_hidden": 0,
                      "double_clicks": 0}
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._q: queue.Queue = queue.Queue()
        self._page_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, name="desktop", daemon=True).start()
        threading.Thread(target=self._page_loop, name="page-calls", daemon=True).start()

    # ------------------------------------------------------------ called from the launcher / pywebview threads
    def lang(self) -> str:
        try:
            return self._lang() or "tr"
        except Exception:
            return "tr"

    def open_chat(self) -> None:
        self._post(self._open, None)

    def toggle_chat(self) -> None:
        """Left click on the icon: hidden -> shown; shown and in front -> hidden; minimized or behind other
        windows -> brought to the front. Clicks are handled one after another on the desktop thread, so fast
        clicks never mix up the state."""
        self._post(self._toggle, time.monotonic())

    def open_settings(self) -> None:
        self._post(self._open, "window.techragHost && window.techragHost.openSettings()")

    def command(self, cmd: int) -> None:
        L = self._L
        if cmd == L.IDM_OPEN:
            self.open_chat()
        elif cmd == L.IDM_SETTINGS:
            self.open_settings()
        elif cmd == L.IDM_QUIT:
            self.request_quit()

    def activate(self) -> None:
        """The exe was started a second time: that one exits, this one shows its chat window."""
        self.stats["activations"] += 1
        self.open_chat()

    def icon_moved(self, anchor: dict) -> None:
        self._post(self.state.set, "launcher", anchor)

    def request_quit(self, confirm: bool = True) -> None:
        self._post(self._quit, confirm)

    def session_end(self, final: Optional[bool]) -> None:
        """Windows logging off / shutting down. None: it asks (the chat window may then close); True: it happens
        now - stop indexing at a safe point right away (Windows ends the process soon after); False: the user
        cancelled it (e.g. to save a file elsewhere) - everything goes on as before."""
        if final is None:
            self.session_ending = True
        elif final:
            self.session_ending = True
            self.quitting = True
            self.shutdown(timeout=5)
        else:
            self.session_ending = False

    def on_chat_closing(self) -> bool:
        """pywebview `closing` handler (GUI thread). Returning False cancels the close: the window is hidden."""
        if self.quitting or not self.hide_on_close or self.session_ending or self._L.session_ending():
            return True  # never block Windows logging off
        self._post(self._hide, True)
        return False

    def idle(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued so far - window actions, then page calls - is done (tests)."""
        done, page = threading.Event(), threading.Event()
        self._post(done.set)
        if not done.wait(timeout):
            return False
        self._page_q.put(page)
        return page.wait(timeout)

    def shutdown(self, timeout: float) -> None:
        """Stop background indexing at a safe point (see Jobs.shutdown); idempotent. Waits at most `timeout`,
        also when another thread is already shutting down (Windows logging off must not wait for a Quit)."""
        if not self._shutdown_lock.acquire(timeout=timeout):
            return
        try:
            if self._shutdown_done:
                return
            t0 = time.time()
            ok = self.jobs.shutdown(timeout)
            self._shutdown_done = True
            log.info("background jobs stopped (%s) in %.1f s", "index writes complete" if ok else
                     "a write did not finish in time; SQLite rolls it back", time.time() - t0)
        finally:
            self._shutdown_lock.release()

    def finish(self) -> None:
        """The GUI loop has ended (quit, Windows logging off or a failure)."""
        self.quitting = True
        self.shutdown(timeout=30)
        if self.launcher is not None:
            self.launcher.close()
        self._q.put((None, ()))
        self._page_q.put(None)

    # ------------------------------------------------------------ desktop thread
    def _post(self, fn, *args) -> None:
        self._q.put((fn, args))

    def _loop(self) -> None:
        while True:
            fn, args = self._q.get()
            if fn is None:
                return
            try:
                fn(*args)
            except Exception:
                log.exception("desktop action %s failed", getattr(fn, "__name__", fn))

    def _chat_in_front(self) -> bool:
        if os.name != "nt":
            return self.visible
        hwnd = self.chat_hwnd()
        if not hwnd:
            return self.visible
        lw = self.launcher
        return self._L.in_front(hwnd, ignore=(getattr(lw, "hwnd", None),))

    def _toggle(self, clicked_at: float) -> None:
        if self.quitting:
            return
        try:
            front = self.visible and self.front()
        except Exception:
            log.exception("could not tell whether the chat window is in front")
            front = False
        if not front:
            self._last_open_click = clicked_at
            self._open(None)
            return
        if clicked_at - self._last_open_click < self.double_click:
            # the second click of a double click that opened the window: it stays open
            self.stats["double_clicks"] += 1
            return
        self.stats["toggled_hidden"] += 1
        self._hide()

    def chat_hwnd(self) -> Optional[int]:
        try:
            return int(self.chat.native.Handle.ToInt64())
        except Exception:
            return None

    def _open(self, script: Optional[str]) -> None:
        if self.quitting:
            return
        if not self.chat.events.shown.wait(120):  # clicked while the window was still being created
            log.error("the chat window was not created")
            return
        hwnd = self.chat_hwnd()
        if hwnd and self.launcher is not None:
            self.launcher.set_companion(hwnd)  # before it becomes active: the icon then steps behind it
        self.chat.show()
        if hwnd and os.name == "nt":
            self._L.bring_to_front(hwnd)  # also restores a minimized window
        self.visible = True
        self.stats["opened"] += 1
        self._page("window.techragHost && window.techragHost.setVisible(true)")
        if script:
            self._page(script)

    def _hide(self, from_close: bool = False) -> None:
        if self.quitting:
            return
        self.chat.hide()
        self.visible = False
        self.stats["hidden"] += 1
        self._page("window.techragHost && window.techragHost.setVisible(false)")
        if from_close and not self.state.get("close_hint_shown"):
            self.state.set("close_hint_shown", True)
            self.stats["hints"] += 1
            lang = self.lang()
            threading.Thread(target=self.notice, args=(self._L.text(lang, "hint_title"), self._L.text(lang, "hint")),
                             name="hint", daemon=True).start()

    def _page(self, script: str) -> None:
        """Tell the page, in order (setVisible(false) must never overtake a later setVisible(true)), on a thread of
        its own: evaluate_js waits until the page is ready, which must not hold up the window actions."""
        self._page_q.put(script)

    def _page_loop(self) -> None:
        while True:
            script = self._page_q.get()
            if script is None:
                return
            if isinstance(script, threading.Event):  # idle() marker
                script.set()
                continue
            try:
                if self.chat.events.loaded.wait(120):
                    self.chat.evaluate_js(script)
            except Exception as exc:
                log.warning("page call failed: %s", exc)

    def _quit(self, confirm: bool) -> None:
        if self._shutdown_done and self.quitting:
            return
        if confirm and self.jobs.running():
            lang = self.lang()
            if not self.confirm(self._L.text(lang, "busy_title"), self._L.text(lang, "busy")):
                return
        log.info("quit requested")
        self.quitting = True
        if self.launcher is not None:
            # Gone at once (not just hidden): a new start of the exe then waits for this process to end and
            # starts afresh, instead of handing over to an app that is shutting down.
            self.launcher.close()
        if self.visible:
            try:
                self.chat.hide()
            except Exception:
                pass
        self.shutdown(timeout=60)
        self.chat.events.shown.wait(120)  # Quit right after start: the window must exist before it is closed
        try:
            self.chat.destroy()  # ends webview.start() in the main thread
        except Exception:
            log.exception("the chat window could not be closed: ending the process")
            _hard_exit(0)


def _setup_logging(install_hooks: bool) -> Path:
    """Everything (our stages, uvicorn, pywebview, native faults) goes to the log file next to settings.json."""
    import faulthandler
    import logging

    path = _log_file()
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            path.write_text("", encoding="utf-8")
    except OSError:
        pass
    fh = open(path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or sys.stderr is None:  # windowed build: no console streams
        sys.stdout = sys.stderr = fh
    handler = logging.StreamHandler(fh)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"))
    for name in ("techrag", "webview", "pywebview", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    if install_hooks:
        faulthandler.enable(file=fh, all_threads=True)  # native crashes still leave a Python traceback

        def excepthook(exc_type, exc, tb):
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
            _message_box(APP_TITLE, f"{exc_type.__name__}: {exc}\n\nDetails: {path}")

        def thread_hook(args):
            log.error("thread %s crashed", getattr(args.thread, "name", "?"),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        sys.excepthook = excepthook
        threading.excepthook = thread_hook
    return path


def _unblock_bundle() -> int:
    """Remove the Mark-of-the-Web (Zone.Identifier stream) from the bundled DLLs.

    Files extracted from a zip that a browser downloaded inherit it, and .NET Framework then refuses to load
    those assemblies (HRESULT 0x80131515) - which breaks pythonnet and the WebView2 bridge."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return 0
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    n = 0
    for p in base.rglob("*"):
        if p.suffix.lower() in (".dll", ".pyd", ".exe"):
            try:
                os.remove(f"{p}:Zone.Identifier")
                n += 1
            except OSError:
                pass
    return n


def _wait(pred: Callable[[], object], timeout: float, step: float = 0.2):
    deadline = time.time() + timeout
    while True:
        try:
            v = pred()
        except Exception:
            v = None
        if v or time.time() > deadline:
            return v
        time.sleep(step)


def _smoke(desk: DesktopApp, result_path: str) -> None:
    """CI check of the packaged app with real windows: the launcher icon comes first; a drag moves it without opening
    the chat; a click opens the chat (UI, JS<->Python bridge, no notes UI); a click on the icon hides the chat when it
    is in front, shows it again with the page kept, restores it when minimized and raises it when another window
    covers it; the close button only hides it and the page is kept; a second start of the exe hands over instead of
    starting a backend; the icon's menu opens the settings and finally quits the app (the CI step checks that the
    process then ends by itself)."""
    chat = desk.chat
    r: dict = {"ok": False, "checks": {}}
    checks = r["checks"]

    def check(name: str, cond) -> None:
        checks[name] = bool(cond)

    def page(timeout: float = 120) -> dict:
        res = _wait(lambda: (lambda v: v if v and v.get("bridge") and v.get("collections") else None)(
            chat.evaluate_js(SMOKE_JS)), timeout, 1.0)
        return res or (chat.evaluate_js(SMOKE_JS) or {})

    hint_before = bool(desk.state.get("close_hint_shown"))  # a previous run (the other build) already showed it
    r["saved_position_at_start"] = desk.state.get("launcher")
    try:
        lw = desk.launcher
        if os.name != "nt" or lw is None:  # no launcher on this platform: the window opened directly
            check("loaded", chat.events.loaded.wait(120))
            r["page"] = pg = page()
            check("chat_ui", pg.get("bridge") and APP_TITLE in str(pg.get("brand", "")))
            check("notes_removed", pg.get("notes") is False)
        else:
            from techrag import launcher as L

            L.use_physical_pixels()
            a = L.win32()
            check("launcher_created", lw.ready.wait(30) and lw.hwnd)
            info = lw.info()
            r["launcher"] = info
            check("launcher_visible", info.get("visible"))
            check("launcher_topmost", info.get("topmost"))
            check("launcher_never_activates", info.get("noactivate") and info.get("toolwindow"))
            check("launcher_per_pixel_alpha", info.get("layered"))
            check("launcher_size_56_64_dip", 56 <= float(info.get("size_dip", 0)) <= 64)
            rect, work = info["rect"], info["work"]
            check("launcher_inside_work_area", work[0] <= rect[0] and work[1] <= rect[1] and rect[2] <= work[2]
                  and rect[3] <= work[3])
            check("chat_window_created", chat.events.shown.wait(120))
            hwnd = desk.chat_hwnd()
            check("chat_hidden_at_start", hwnd and not L.window_state(hwnd)["visible"])

            def post(msg: int, x: int, y: int) -> None:
                a.PostMessageW(lw.hwnd, msg, 0 if msg == L.WM_LBUTTONUP else L.MK_LBUTTON, L.lparam_point(x, y))

            c = (rect[2] - rect[0]) // 2
            post(L.WM_LBUTTONDOWN, c, c)
            post(L.WM_MOUSEMOVE, c - 30, c - 20)
            post(L.WM_MOUSEMOVE, c - 90, c - 70)
            post(L.WM_LBUTTONUP, c, c)
            time.sleep(1.5)
            moved = lw.info()
            r["after_drag"] = moved
            check("drag_moves_icon", moved["rect"][:2] != rect[:2])
            check("drag_does_not_open_chat", not L.window_state(hwnd)["visible"] and desk.stats["opened"] == 0)
            check("drag_position_saved", _wait(lambda: (desk.state.get("launcher") or {}).get("h"), 5))

            def click() -> None:
                cc = (moved["rect"][2] - moved["rect"][0]) // 2
                post(L.WM_LBUTTONDOWN, cc, cc)
                post(L.WM_LBUTTONUP, cc, cc)

            click()
            check("click_opens_chat", _wait(lambda: L.window_state(hwnd)["visible"], 30))
            r["page"] = pg = page()
            check("chat_ui", pg.get("bridge") and APP_TITLE in str(pg.get("brand", "")) and pg.get("host"))
            check("notes_removed", pg.get("notes") is False)
            chat.evaluate_js("window.__smoke = 'kept'; document.querySelector('#question').value = 'draft'; 1")

            def pause() -> None:  # the next click is a new click, not the second half of a double click
                time.sleep(desk.double_click + 0.4)

            def front() -> bool:
                return bool(L.in_front(hwnd, ignore=(lw.hwnd,)))

            # the icon toggles: in front -> hidden (not quit), hidden -> shown, minimized/covered -> to the front
            r["front_after_open"] = front()
            check("opened_chat_is_in_front", _wait(front, 10))
            pause()
            click()
            check("click_hides_chat_in_front", _wait(lambda: not L.window_state(hwnd)["visible"], 30))
            time.sleep(0.5)
            check("hiding_is_not_quitting", lw.hwnd and not desk.quitting and desk.stats["toggled_hidden"] == 1)
            pause()
            click()
            check("click_shows_hidden_chat", _wait(lambda: L.window_state(hwnd)["visible"], 30))
            check("page_state_kept_after_toggle", _wait(lambda: chat.evaluate_js(
                "[window.__smoke, document.querySelector('#question').value]") == ["kept", "draft"], 10))
            a.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            _wait(lambda: L.window_state(hwnd)["minimized"], 10)
            pause()
            click()
            check("click_restores_minimized_chat", _wait(
                lambda: (lambda st: st["visible"] and not st["minimized"])(L.window_state(hwnd)), 30))
            cover = _cover_window(L._rect(hwnd))
            try:
                check("covered_chat_is_not_in_front", cover and _wait(lambda: not front(), 10))
                hidden_before = desk.stats["toggled_hidden"]
                pause()
                click()
                check("click_raises_covered_chat", _wait(front, 30) and L.window_state(hwnd)["visible"]
                      and desk.stats["toggled_hidden"] == hidden_before)
            finally:
                if cover:
                    a.PostMessageW(cover, L.WM_CLOSE, 0, 0)
            a.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the window's close button
            check("close_hides_chat", _wait(lambda: not L.window_state(hwnd)["visible"], 30))
            time.sleep(1.0)
            check("app_keeps_running_after_close", lw.hwnd and not desk.quitting)
            check("close_hint_only_the_first_time", desk.stats["hints"] == (0 if hint_before else 1))
            click()
            check("click_reopens_chat", _wait(lambda: L.window_state(hwnd)["visible"], 30))
            check("page_state_kept", chat.evaluate_js(
                "[window.__smoke, document.querySelector('#question').value]") == ["kept", "draft"])
            if getattr(sys, "frozen", False):
                env = {k: v for k, v in os.environ.items() if k != "TECHRAG_SMOKE"}
                env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # a fresh start of the exe, as from Explorer
                before, t0 = desk.stats["activations"], time.time()
                code = subprocess.run([sys.executable], env=env, timeout=180).returncode
                r["second_instance"] = {"exit_code": code, "seconds": round(time.time() - t0, 1)}
                check("second_instance_exits", code == 0)
                check("second_instance_hands_over", _wait(lambda: desk.stats["activations"] > before, 20))
            a.PostMessageW(lw.hwnd, L.WM_COMMAND, L.IDM_SETTINGS, 0)
            check("menu_opens_settings", _wait(
                lambda: chat.evaluate_js("document.querySelector('#dlg-settings').open") is True, 30))
            chat.evaluate_js("document.querySelector('#dlg-settings').close(); 1")
            r["stats"] = dict(desk.stats)
        r["ok"] = bool(checks) and all(checks.values())
    except Exception as exc:
        r["error"] = f"{exc.__class__.__name__}: {exc}"
    log.info("smoke result: %s", r)
    Path(result_path).write_text(json.dumps(r, indent=1), encoding="utf-8")
    if desk.launcher is not None and desk.launcher.hwnd:  # full exit through the icon's menu, as a user would
        from techrag import launcher as L

        desk.launcher.post(L.WM_COMMAND, L.IDM_QUIT)
    else:
        desk.request_quit(confirm=False)


def _cover_window(rect) -> Optional[int]:
    """Smoke test: an ordinary window of 'another program' over the chat window, active and on top, with its own
    message loop (a window nobody answers would be ghosted by Windows). WM_CLOSE ends it."""
    from techrag import launcher as L

    ready, out = threading.Event(), {}

    def run() -> None:
        a = L.win32()
        x0, y0, x1, y1 = rect
        hwnd = a.CreateWindowExW(0, "STATIC", "smoke cover", 0x00CF0000 | 0x10000000, x0 + 20, y0 + 20,
                                 max(200, x1 - x0 - 40), max(200, y1 - y0 - 40), None, None,
                                 a.GetModuleHandleW(None), None)
        out["hwnd"] = hwnd
        if hwnd:
            L.bring_to_front(hwnd)
        ready.set()
        msg = a.W.MSG()
        while hwnd and a.IsWindow(hwnd) and a.GetMessageW(a.ctypes.byref(msg), None, 0, 0) > 0:
            a.TranslateMessage(a.ctypes.byref(msg))
            a.DispatchMessageW(a.ctypes.byref(msg))

    threading.Thread(target=run, name="smoke-cover", daemon=True).start()
    ready.wait(10)
    return out.get("hwnd")


def _smoke_exit(result_path: str, clean: bool) -> None:
    try:
        r = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = {"ok": False, "error": "no smoke result"}
    r["exit"] = "clean" if clean else "error"
    Path(result_path).write_text(json.dumps(r, indent=1), encoding="utf-8")


def _set_process_dpi_aware() -> None:
    """The system DPI awareness pywebview sets for its window - set before any window exists (the launcher icon is
    created first), so the chat window looks exactly as before. The icon's own thread is per-monitor aware."""
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _chat_geometry(state) -> tuple[int, int, int, int, Optional[int], Optional[int]]:
    """Chat window (width, height, min width, min height, x, y) in logical pixels. 1480 x 940 leaves room for the
    PDF panel; smaller when the screen is. On Windows it is centred on the work area of the monitor that shows the
    launcher icon - the size and the position come from the same monitor (by default Windows would centre it on
    the monitor under the mouse, which may be a smaller one)."""
    w, h, x, y = 1480, 940, None, None
    if os.name == "nt":
        try:
            from techrag.launcher import list_monitors, primary_monitor, system_dpi

            mons = list_monitors()  # this thread is system DPI aware, like the chat window
            name = (state.get("launcher") or {}).get("monitor")
            mon = next((m for m in mons if name and m.name == name), None) or primary_monitor(mons)
            scale = max(system_dpi(), 48) / 96
            left, top, right, bottom = mon.work
            ww, wh = (right - left) / scale, (bottom - top) / scale
            w, h = min(w, int(ww * 0.94)), min(h, int(wh * 0.92))
            x, y = int(left / scale + (ww - w) / 2), int(top / scale + (wh - h) / 2)
        except Exception:
            log.exception("could not size the chat window for its monitor")
    return w, h, min(960, w), min(620, h), x, y


def _launcher_supported() -> bool:
    return os.name == "nt" and not os.environ.get("TECHRAG_NO_LAUNCHER")


def _make_launcher(key: str, desk: DesktopApp, state):
    from techrag.launcher import Launcher

    return Launcher(key, state.get("launcher"), desk.lang, on_click=desk.toggle_chat, on_command=desk.command,
                    on_activate=desk.activate, on_moved=desk.icon_moved, on_session_end=desk.session_end,
                    on_close_request=lambda: desk.request_quit(confirm=False))


def run_gui() -> int:
    from techrag import __version__
    from techrag.launcher import SingleInstance, instance_key
    from techrag.settings import settings_path

    log_path = _setup_logging(install_hooks=getattr(sys, "frozen", False))
    log.info("=== %s %s starting (frozen=%s, python=%s, exe=%s)", APP_TITLE, __version__, getattr(sys, "frozen", False),
             sys.version.split()[0], sys.executable)
    try:
        log.info("mark-of-the-web removed from %d bundled file(s)", _unblock_bundle())
    except Exception as exc:
        log.warning("could not unblock bundled files: %s", exc)
    if os.name == "nt":
        _set_process_dpi_aware()
    key = instance_key(settings_path())
    guard = SingleInstance(key)
    deadline = time.monotonic() + 90  # longer than a Quit may take to stop indexing
    while not guard.acquire():  # another instance runs (or is still starting / just closing)
        if guard.notify_running(timeout=0.5):
            log.info("already running in this session: asked it to open its chat window")
            return 0
        if time.monotonic() > deadline:
            from techrag.launcher import text

            _message_box(APP_TITLE, text("tr", "running") + "\n\n" + text("en", "running"))
            return 0
    try:
        return _run_gui(key, log_path)
    finally:
        guard.release()


def _run_gui(key: str, log_path: Path) -> int:
    import uvicorn

    from techrag.config import load_config, user_config_dir
    from techrag.launcher import DesktopState
    from techrag.server import create_app

    cfg = load_config()
    log.info("library: %s", cfg.library_dir)
    token = secrets.token_urlsafe(24)
    cfg.server.api_token = token
    sock = _backend_socket(key)
    port = sock.getsockname()[1]
    app = create_app(cfg, desktop=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, access_log=False))
    backend = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="backend", daemon=True)
    backend.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        log.error("backend did not start")
        _message_box(APP_TITLE, f"The local backend did not start. See {log_path}")
        return 1
    log.info("backend listening on 127.0.0.1:%d", port)

    try:
        import webview
    except ImportError:
        _message_box(APP_TITLE, "pywebview is missing from this build.")
        return 1
    log.info("pywebview %s", _webview_version(webview))
    use_launcher = _launcher_supported()
    state = DesktopState(log_path.parent / "desktop.json")
    api = JsApi()
    width, height, min_w, min_h, x, y = _chat_geometry(state)
    chat = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/?token={token}", js_api=api,
                                 width=width, height=height, x=x, y=y, min_size=(min_w, min_h), text_select=True,
                                 hidden=use_launcher)  # created once, in the background: the icon shows it
    api._window = chat
    desk = DesktopApp(chat, app.state.jobs, lambda: app.state.state["cfg"].ui.language, state,
                      hide_on_close=use_launcher)
    chat.events.closing += desk.on_chat_closing
    try:
        chat.events.loaded += lambda: log.info("page loaded")
        chat.events.closed += lambda: log.info("chat window closed")
    except Exception:
        pass
    if use_launcher:
        launcher = _make_launcher(key, desk, state)
        desk.launcher = launcher
        if not launcher.start():
            log.error("launcher icon unavailable (%s): opening the chat window directly", launcher.error)
            desk.launcher = None
            desk.hide_on_close = False
            desk.open_chat()
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass
    storage = user_config_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    smoke = os.environ.get("TECHRAG_SMOKE")
    clean = False
    try:
        kwargs = {"private_mode": False, "storage_path": str(storage),
                  "debug": bool(os.environ.get("TECHRAG_WEBVIEW_DEBUG"))}
        if os.name == "nt":
            kwargs["gui"] = "edgechromium"  # never fall back to the legacy IE engine
        if smoke:
            kwargs.update(func=_smoke, args=(desk, smoke))
        log.info("starting GUI loop (%s, launcher=%s)", kwargs.get("gui", "default"), desk.launcher is not None)
        webview.start(**kwargs)
        clean = True
    except Exception as exc:
        log.exception("GUI failed")
        _message_box(APP_TITLE, "Could not open the window. On Windows the Microsoft Edge WebView2 Runtime is "
                                "required (offline installer: 'Evergreen Standalone Installer').\n\n"
                                f"{exc.__class__.__name__}: {exc}\n\nLog: {log_path}")
        return 1
    finally:
        desk.finish()
        server.should_exit = True
        backend.join(5)
        if smoke:
            _smoke_exit(smoke, clean)
    log.info("exit")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] != "--gui":
        if getattr(sys, "frozen", False):
            _attach_console()
        if argv[0] == "selftest":
            from techrag.selftest import main as selftest

            return selftest(argv[1:])
        from techrag.cli import main as cli_main

        return cli_main(argv)
    code = run_gui()
    _hard_exit(code)
    return code


def _hard_exit(code: int) -> None:
    """End the GUI process now. A normal interpreter exit waits until every worker of the indexing thread pools
    (VLM page extraction, embedding batches) has finished its queued model calls - possibly long after the user
    chose Quit, with the icon gone but the process still running. Index writes are already stopped at that point
    (Jobs.shutdown); the log is flushed first."""
    import logging as _logging

    try:
        _logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            if stream:
                stream.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
