"""Desktop app lifecycle with stand-ins for pywebview and the Win32 launcher icon (no display needed): one backend,
the icon first, a click opens the chat, closing only hides it, a second start hands over, the full exit stops
indexing safely. The real icon window is tested on Windows in test_launcher.py and by the CI GUI smoke test."""

import json
import sys
import threading
import types

import httpx
import pytest
from fastapi import HTTPException

from techrag import desktop
from techrag.ingest.pipeline import IngestStopped
from techrag import launcher as L


class FakeEvent:
    """pywebview.Event semantics: handlers via +=, set() returns True when a handler returned False (cancel)."""

    def __init__(self):
        self.items, self._ev = [], threading.Event()

    def __iadd__(self, fn):
        self.items.append(fn)
        return self

    def set(self, *args):
        results = [fn() for fn in self.items]
        self._ev.set()
        return any(r is False for r in results)

    def wait(self, timeout=None):
        return self._ev.wait(timeout)

    def is_set(self):
        return self._ev.is_set()


class FakeWindow:
    def __init__(self, url, **kw):
        self.url, self.kw = url, kw
        self.events = types.SimpleNamespace(**{n: FakeEvent() for n in ("shown", "loaded", "closing", "closed")})
        self.visible = not kw.get("hidden")
        self.calls, self.scripts = [], []
        self.destroyed = threading.Event()

    def show(self):
        self.calls.append("show")
        self.visible = True

    def hide(self):
        self.calls.append("hide")
        self.visible = False

    def user_closes(self) -> bool:
        """The close button: True if the window really closed."""
        cancelled = self.events.closing.set()
        if not cancelled:
            self.visible = False
            self.destroyed.set()
        return not cancelled

    def destroy(self):
        self.calls.append("destroy")
        if self.user_closes() is False:
            raise AssertionError("destroy() must not be cancelled")

    def evaluate_js(self, script):
        self.scripts.append(script)
        return True

    def create_file_dialog(self, kind, **kw):
        return ["C:/picked"]


class FakeLauncher:
    instances: list = []

    def __init__(self, key, desk, state):
        self.key, self.desk, self.anchor = key, desk, state.get("launcher")
        self.calls = []
        FakeLauncher.instances.append(self)

    def start(self):
        self.calls.append("start")
        return True

    def hide(self):
        self.calls.append("hide")

    def close(self):
        self.calls.append("close")

    def set_companion(self, hwnd):
        pass

    def notify(self, title, body):
        self.calls.append(("notify", title, body))
        return True

    def clear_notification(self):   # the real one does nothing without a notification
        self.cleared = getattr(self, "cleared", 0) + 1

    # what the user does with the icon (the real one calls on_click = DesktopApp.toggle_chat)
    def click(self):
        self.desk.toggle_chat()

    def menu(self, cmd):
        self.desk.command(cmd)


class FakeGuard:
    running = False
    log: list = []

    def __init__(self, key):
        self.key = key

    def acquire(self):
        FakeGuard.log.append(("acquire", self.key))
        return not FakeGuard.running

    def release(self):
        FakeGuard.log.append(("release", self.key))

    def notify_running(self, timeout=20.0):
        FakeGuard.log.append(("notify", self.key))
        return True


@pytest.fixture
def gui(monkeypatch, tmp_path):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("TECHRAG_PATHS__LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("TECHRAG_NO_USER_SETTINGS", "1")
    monkeypatch.delenv("TECHRAG_SMOKE", raising=False)
    FakeLauncher.instances = []
    FakeGuard.running, FakeGuard.log = False, []
    monkeypatch.setattr(L, "SingleInstance", FakeGuard)
    monkeypatch.setattr(desktop, "_launcher_supported", lambda: True)
    monkeypatch.setattr(desktop, "_make_launcher", lambda key, desk, state: FakeLauncher(key, desk, state))
    notices, answers = [], []
    real_init = desktop.DesktopApp.__init__

    def init(self, *a, **kw):
        kw["notice"] = lambda title, body: notices.append((title, body))
        kw["confirm"] = lambda title, body: answers.pop(0)
        real_init(self, *a, **kw)
        seen["desk"] = self

    monkeypatch.setattr(desktop.DesktopApp, "__init__", init)
    seen: dict = {"windows": [], "servers": 0}
    import uvicorn

    real_server = uvicorn.Server

    def server(*a, **kw):
        seen["servers"] += 1
        return real_server(*a, **kw)

    monkeypatch.setattr(uvicorn, "Server", server)
    fake = types.ModuleType("webview")
    fake.settings = {}
    fake.FileDialog = types.SimpleNamespace(OPEN=10, FOLDER=20, SAVE=30)

    def create_window(title, url, js_api=None, **kw):
        w = FakeWindow(url, **kw)
        js_api._window = w
        seen["windows"].append(w)
        seen["api"] = js_api
        return w

    fake.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", fake)
    return types.SimpleNamespace(fake=fake, seen=seen, notices=notices, answers=answers, path=tmp_path)


def _run(gui, scenario):
    """webview.start(): the GUI loop runs until the chat window is destroyed; the scenario plays the user."""
    errors = []

    def start(**kw):
        gui.seen["start"] = kw
        w = gui.seen["windows"][0]
        w.events.shown.set()
        w.events.loaded.set()

        def play():
            try:
                scenario(w, FakeLauncher.instances[0], gui.seen["desk"])
            except BaseException as exc:  # reported after the loop
                errors.append(exc)
                w.destroyed.set()

        t = threading.Thread(target=play)
        t.start()
        assert w.destroyed.wait(30), "the GUI loop never ended"
        t.join(10)

    gui.fake.start = start
    code = desktop.run_gui()
    if errors:
        raise errors[0]
    return code


def test_icon_first_then_click_opens_the_one_chat_window(gui):
    def scenario(w, icon, desk):
        assert icon.calls == ["start"], "the launcher icon is shown before the GUI loop"
        assert w.kw["hidden"] is True and not w.visible, "the chat window starts hidden"
        base, token = w.url.split("/?token=")
        assert httpx.get(f"{base}/api/info", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert httpx.get(f"{base}/api/info").status_code == 401
        for _ in range(3):  # a double (triple) click opens it once: the same window, the same backend
            icon.click()
        assert desk.idle()
        assert w.visible and w.calls == ["show"] and desk.stats["opened"] == 1 and desk.stats["double_clicks"] == 2
        assert any("setVisible(true)" in s for s in w.scripts)
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0
    w = gui.seen["windows"][0]
    assert len(gui.seen["windows"]) == 1 and gui.seen["servers"] == 1
    assert gui.seen["start"]["private_mode"] is False
    assert gui.seen["api"].pick_folder() == "C:/picked"
    assert w.calls[-2:] == ["hide", "destroy"]
    icon_calls = FakeLauncher.instances[0].calls
    assert icon_calls[:2] == ["start", "close"], "the icon goes at once: a new start then waits for this one to end"
    assert [x[0] for x in FakeGuard.log] == ["acquire", "release"]


def test_click_toggles_the_chat_window(gui):
    def scenario(w, icon, desk):
        desk.double_click = 0.0
        front = {"value": True}
        desk.front = lambda: front["value"]
        icon.click()                                   # hidden -> shown
        assert desk.idle() and w.visible and w.calls == ["show"]
        chat_state = "window.__chat = 'kept'"
        w.evaluate_js(chat_state)
        job = desk.jobs.create("ingest")
        job["status"] = "running"
        icon.click()                                   # shown and in front -> hidden
        assert desk.idle() and not w.visible and w.calls == ["show", "hide"] and desk.stats["toggled_hidden"] == 1
        assert not desk.quitting and desk.jobs.running(), "hiding is not quitting: indexing goes on"
        assert "destroy" not in w.calls and any("setVisible(false)" in s for s in w.scripts)
        icon.click()                                   # hidden -> shown again, the same page
        assert desk.idle() and w.visible and w.calls == ["show", "hide", "show"]
        front["value"] = False                         # minimized or behind another window
        icon.click()
        assert desk.idle() and w.visible and w.calls[-1] == "show", "brought to the front, not hidden"
        front["value"] = True
        icon.menu(L.IDM_OPEN)                          # the menu's "Open chat" never hides it
        assert desk.idle() and w.visible
        for _ in range(25):                            # many fast clicks: one window, a consistent state
            icon.click()
        assert desk.idle() and len(gui.seen["windows"]) == 1
        assert w.visible == (w.calls[-1] == "show") == desk.visible
        job["status"] = "done"
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0


def test_page_learns_which_part_the_icon_covers(gui):
    """The icon stays on top of the chat window; the page is told which part of the window it covers (fractions),
    also again on every opening of the window, so it can keep the send button out from under it."""
    def scenario(w, icon, desk):
        desk.double_click = 0.0
        desk.front = lambda: True
        icon.click()
        assert desk.idle()
        desk.icon_overlap((0.9, 0.92, 1.0, 1.0))         # the real icon reports this from its own thread
        assert desk.idle() and w.scripts[-1].endswith("setIconArea([0.9, 0.92, 1.0, 1.0])")
        icon.click()                                       # hidden: the icon covers nothing of it
        assert desk.idle()
        desk.icon_overlap(None)
        assert desk.idle() and w.scripts[-1].endswith("setIconArea(null)")
        desk.icon_overlap((0.5, 0.5, 0.6, 0.6))
        icon.click()
        assert desk.idle() and w.visible
        shown = max(i for i, s in enumerate(w.scripts) if "setVisible(true)" in s)
        assert w.scripts[shown + 1].endswith("setIconArea([0.5, 0.5, 0.6, 0.6])"), "sent again after showing"
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0


def test_answer_ready_notifies_only_when_the_chat_is_out_of_view(gui):
    """A finished answer: a Windows notification when the chat is hidden (behind the icon), minimized or behind other
    windows - never while it is in front; off in Settings means off. Opening the chat removes the notification."""
    def scenario(w, icon, desk):
        desk.double_click = 0.0
        front = {"value": True}
        desk.front = lambda: front["value"]
        info = {"kind": "final", "question": "DDR5 tRFC değeri nedir?", "status": "supported"}

        def notes():
            assert desk.idle()
            return [c for c in icon.calls if isinstance(c, tuple) and c[0] == "notify"]

        desk.answer_ready(info)                        # hidden at start
        assert len(notes()) == 1 and notes()[0][1] == "Cevap hazır" and "DDR5 tRFC" in notes()[0][2]
        icon.click()                                   # the user opens the chat
        assert desk.idle() and w.visible and getattr(icon, "cleared", 0) >= 1
        desk.answer_ready(info)                        # in front: the user sees it
        assert len(notes()) == 1
        front["value"] = False                         # minimized or behind another program
        desk.answer_ready(dict(info, kind="clarify"))
        assert len(notes()) == 2 and notes()[1][1] == "Netleştirme sorusu var"
        desk.notify_enabled = lambda: False            # Settings: off
        desk.answer_ready(info)
        assert len(notes()) == 2 and desk.stats["notified"] == 2
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0


def test_answer_note_texts():
    title, body = L.answer_note("tr", {"kind": "final", "status": "not_found", "question": "  çok   uzun " * 40})
    assert title == "Cevap hazır: kaynaklarda bulunamadı" and len(body.split("\n")[0]) <= 160
    assert body.endswith("Sohbeti açmak için tıklayın.")
    assert L.answer_note("en", {"kind": "error", "question": "x"})[0] == "No answer (error)"


def test_double_click_opens_and_keeps_the_window_open(gui):
    def scenario(w, icon, desk):
        desk.front = lambda: desk.visible
        desk.double_click = 30.0                       # every click below is "the second click"
        icon.click()
        icon.click()
        assert desk.idle() and w.visible and w.calls == ["show"] and desk.stats["double_clicks"] == 1
        desk.double_click = 0.0
        icon.click()                                   # a later single click hides it
        assert desk.idle() and not w.visible
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0


def test_the_real_launcher_click_is_the_toggle(monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, *a, **kw):
            seen.update(kw)

    monkeypatch.setattr(L, "Launcher", Spy)
    desk = types.SimpleNamespace(lang=lambda: "tr", toggle_chat=object(), open_chat=object(), command=None,
                                 activate=None, icon_moved=None, session_end=None, request_quit=None,
                                 icon_overlap=object())
    desktop._make_launcher("k", desk, L.DesktopState("/nonexistent/desktop.json"))
    assert seen["on_click"] is desk.toggle_chat and seen["on_activate"] is desk.activate
    assert seen["on_overlap"] is desk.icon_overlap


def test_close_button_hides_and_keeps_the_page(gui):
    def scenario(w, icon, desk):
        icon.click()
        assert desk.idle()
        assert w.user_closes() is False, "the close button does not end the app"
        assert desk.idle() and not w.visible and not desk.quitting
        assert any("setVisible(false)" in s for s in w.scripts), "the page stops polling while hidden"
        assert len(gui.notices) == 1 and "simge" in gui.notices[0][1], "explained once (TR by default)"
        icon.click()
        assert desk.idle() and w.visible
        assert w.user_closes() is False and desk.idle()
        assert len(gui.notices) == 1, "only the first time"
        assert "destroy" not in w.calls, "hide/show never re-creates or reloads the page"
        icon.menu(L.IDM_SETTINGS)
        assert desk.idle() and w.visible
        assert any("openSettings" in s for s in w.scripts)
        desk.icon_moved({"monitor": "D1", "h": "left", "dx": 40, "v": "top", "dy": 10})
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0
    st = json.loads((gui.path / "desktop.json").read_text(encoding="utf-8"))
    assert st["close_hint_shown"] is True and st["launcher"]["h"] == "left"


def test_quit_while_indexing_asks_and_stops_at_a_safe_point(gui):
    def scenario(w, icon, desk):
        jobs = desk.jobs
        job = jobs.create("ingest")
        job["status"] = "running"
        gui.answers.extend([False, True])
        icon.menu(L.IDM_QUIT)
        assert desk.idle() and not desk.quitting and not jobs.gate.stop.is_set(), "'No' keeps the app running"
        icon.menu(L.IDM_QUIT)
        assert w.destroyed.wait(10)
        assert jobs.gate.stop.is_set(), "jobs were told to stop"
        with pytest.raises(HTTPException):
            jobs.create("ingest")       # nothing new starts while closing
        with pytest.raises(IngestStopped):
            jobs.gate.check()           # a running job stops before its next document
        assert jobs.gate._lock.locked(), "the write lock is kept: no index write starts after the exit"

    assert _run(gui, scenario) == 0
    assert not gui.answers


def test_session_end_lets_the_window_close(gui):
    def scenario(w, icon, desk):
        icon.click()
        assert desk.idle()
        desk.session_end(None)            # Windows asks to log off
        assert w.user_closes() is True, "Windows logging off is never blocked"

    assert _run(gui, scenario) == 0


def test_cancelled_logoff_leaves_the_app_working(gui):
    def scenario(w, icon, desk):
        desk.session_end(None)
        desk.session_end(False)           # the user cancelled logging off
        assert not desk.quitting
        icon.click()
        assert desk.idle() and w.visible, "the icon still opens the chat"
        assert w.user_closes() is False and desk.idle() and not w.visible, "close still only hides"
        icon.menu(L.IDM_QUIT)

    assert _run(gui, scenario) == 0


def test_close_request_from_another_program_quits(gui):
    def scenario(w, icon, desk):
        job = desk.jobs.create("ingest")
        job["status"] = "running"
        desk.request_quit(confirm=False)  # what the launcher reports for a foreign WM_CLOSE
        assert w.destroyed.wait(10) and desk.jobs.gate.stop.is_set()

    assert _run(gui, scenario) == 0
    assert not gui.answers, "no confirmation asked"


def test_second_start_hands_over_without_a_backend(gui, monkeypatch):
    FakeGuard.running = True
    monkeypatch.setattr(desktop, "_run_gui", lambda *a: (_ for _ in ()).throw(AssertionError("second backend")))
    assert desktop.run_gui() == 0
    assert [x[0] for x in FakeGuard.log] == ["acquire", "notify"]
    assert gui.seen["servers"] == 0 and not gui.seen["windows"]


def test_without_a_launcher_the_window_opens_and_closes_normally(gui, monkeypatch):
    monkeypatch.setattr(desktop, "_launcher_supported", lambda: False)

    def scenario(w, icon, desk):
        raise AssertionError("no launcher expected")

    def start(**kw):
        w = gui.seen["windows"][0]
        assert w.kw["hidden"] is False and w.visible and not FakeLauncher.instances
        assert w.user_closes() is True

    gui.fake.start = start
    assert desktop.run_gui() == 0


def test_arguments_run_the_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TECHRAG_NO_USER_SETTINGS", "1")
    assert desktop.main(["--library", str(tmp_path / "lib"), "docs"]) in (0, 1)
    assert desktop.main(["selftest"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def _pywebview_walk(obj, base="", functions=None, seen=None, depth=0):
    """Same traversal rules as webview.util.inject_pywebview.get_functions (pywebview 6): every public
    attribute is visited, methods are exposed, other objects are recursed into."""
    import inspect

    functions = {} if functions is None else functions
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return functions
    seen.add(id(obj))
    if depth > 50:
        raise RecursionError(f"walked into {base}")
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name)
        full = f"{base}.{name}" if base else name
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            functions[full] = attr
        elif inspect.isclass(attr) or (not callable(attr) and hasattr(attr, "__module__")):
            _pywebview_walk(attr, full, functions, seen, depth + 1)
    return functions


def test_js_api_bridge_does_not_descend_into_the_window():
    """Regression for v0.1.0: the window was a public JsApi attribute, so pywebview walked into the WinForms
    form (window.native.AccessibilityObject.Bounds.Empty.Empty...) and the app hung on start-up."""

    class Rect:  # like System.Drawing.Rectangle: every .Empty access returns a new object
        @property
        def Empty(self):
            return Rect()

    class Native:
        Bounds = property(lambda self: Rect())

    class Window:
        native = Native()

        def create_file_dialog(self, *a, **kw):
            return None

    api = desktop.JsApi()
    api._window = Window()
    exposed = _pywebview_walk(api)
    assert sorted(exposed) == ["pick_files", "pick_folder"], "notes export (save_text) is gone"
    public_data = [n for n in dir(api) if not n.startswith("_") and not callable(getattr(api, n))]
    assert public_data == [], "JsApi must not have public data attributes (pywebview walks them)"

    class Old(desktop.JsApi):  # the v0.1.0 layout reproduces the runaway walk
        def __init__(self):
            super().__init__()
            self.window = Window()

    with pytest.raises(RecursionError):
        _pywebview_walk(Old())


def test_backend_port_is_the_same_at_every_start():
    """The page's browser storage (panel layout, language, open buckets) belongs to its origin, port included."""
    key = L.instance_key("/somewhere/settings.json")
    first = desktop._backend_socket(key)
    port = first.getsockname()[1]
    first.close()
    second = desktop._backend_socket(key)
    assert second.getsockname()[1] == port, "same origin after a restart"
    taken = desktop._backend_socket(key)
    assert taken.getsockname()[1] != port, "another free port while it is in use"
    second.close()
    taken.close()


def test_former_app_folders_keep_being_used(tmp_path):
    from techrag.config import app_dir

    assert app_dir(tmp_path).name == "SayisalTasarimUzmani", "new installations"
    (tmp_path / "TechRAG").mkdir()
    assert app_dir(tmp_path).name == "TechRAG", "settings, keys and library stay where they were"
    (tmp_path / "SayisalTasarimUzmani").mkdir()
    assert app_dir(tmp_path).name == "SayisalTasarimUzmani"
    assert app_dir(tmp_path, lower=True).name == "sayisaltasarimuzmani"
