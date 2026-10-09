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
    fake.FOLDER_DIALOG, fake.OPEN_DIALOG, fake.SAVE_DIALOG = 20, 10, 30

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
    seen["api"].window.create_file_dialog = lambda kind, **kw: str(out)
    assert seen["api"].save_text("notes.md", "# hi") and out.read_text() == "# hi"


def test_arguments_run_the_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TECHRAG_NO_USER_SETTINGS", "1")
    assert desktop.main(["--library", str(tmp_path / "lib"), "docs"]) in (0, 1)
    assert desktop.main(["selftest"]) == 0
    assert '"ok": true' in capsys.readouterr().out
