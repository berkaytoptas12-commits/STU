"""Desktop launcher wiring with a stand-in for pywebview (no display needed)."""

import sys
import types

import httpx

from techrag import desktop


def test_gui_starts_backend_with_token_and_opens_window(monkeypatch, tmp_path):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("TECHRAG_PATHS__LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("TECHRAG_NO_USER_SETTINGS", "1")
    seen = {}

    fake = types.ModuleType("webview")
    fake.settings = {}
    fake.FileDialog = types.SimpleNamespace(OPEN=10, FOLDER=20, SAVE=30)

    class Window:
        def __init__(self, url):
            self.url = url

        def create_file_dialog(self, kind, **kw):
            return ["C:/picked"] if kind != 30 else "C:/out.md"

    def create_window(title, url, js_api=None, **kw):
        seen["url"], seen["api"] = url, js_api
        return Window(url)

    def start(**kw):
        seen["start"] = kw
        url = seen["url"]
        base, token = url.split("/?token=")
        seen["with_token"] = httpx.get(f"{base}/api/info", headers={"Authorization": f"Bearer {token}"}).status_code
        seen["without_token"] = httpx.get(f"{base}/api/info").status_code
        seen["ui"] = httpx.get(base + "/").status_code

    fake.create_window, fake.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert desktop.run_gui() == 0
    assert seen["url"].startswith("http://127.0.0.1:") and "?token=" in seen["url"]
    assert seen["with_token"] == 200 and seen["without_token"] == 401 and seen["ui"] == 200
    assert seen["start"]["private_mode"] is False
    assert seen["api"].pick_folder() == "C:/picked"
    out = tmp_path / "notes.md"
    seen["api"]._window.create_file_dialog = lambda kind, **kw: str(out)
    assert seen["api"].save_text("notes.md", "# hi") and out.read_text() == "# hi"


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
    assert sorted(exposed) == ["pick_files", "pick_folder", "save_text"]
    public_data = [n for n in dir(api) if not n.startswith("_") and not callable(getattr(api, n))]
    assert public_data == [], "JsApi must not have public data attributes (pywebview walks them)"

    class Old(desktop.JsApi):  # the v0.1.0 layout reproduces the runaway walk
        def __init__(self):
            super().__init__()
            self.window = Window()

    import pytest

    with pytest.raises(RecursionError):
        _pywebview_walk(Old())
