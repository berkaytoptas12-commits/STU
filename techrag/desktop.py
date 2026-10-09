"""Desktop entry point (TechRAG.exe).

* No arguments: start the FastAPI backend in-process on a random localhost port with a per-launch token
  and show the UI in a native window (Microsoft Edge WebView2 through pywebview). No browser is opened.
* With arguments: behave exactly like the `techrag` CLI (e.g. `TechRAG.exe ingest`, `TechRAG.exe doctor`).
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path


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
    from techrag.config import user_config_dir

    d = user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "techrag.log"


def _message_box(title: str, text: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dialog(kind: str):
    """pywebview >= 5 exposes webview.FileDialog.*; older releases had module constants."""
    import webview

    fd = getattr(webview, "FileDialog", None)
    if fd is not None:
        return getattr(fd, kind)
    return getattr(webview, f"{kind}_DIALOG")


class JsApi:
    """Native dialogs exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def pick_folder(self):
        r = self.window.create_file_dialog(_dialog("FOLDER"))
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else None

    def pick_files(self):
        r = self.window.create_file_dialog(
            _dialog("OPEN"), allow_multiple=True,
            file_types=("Documents (*.pdf;*.docx;*.md;*.txt;*.html;*.htm)", "All files (*.*)"))
        return list(r) if r else []

    def save_text(self, filename: str, content: str) -> bool:
        r = self.window.create_file_dialog(_dialog("SAVE"), save_filename=filename)
        path = r if isinstance(r, str) else (r[0] if r else None)
        if not path:
            return False
        Path(path).write_text(content, encoding="utf-8")
        return True


def run_gui() -> int:
    if sys.stdout is None or sys.stderr is None:  # windowed build: no console streams
        sys.stdout = sys.stderr = open(_log_file(), "a", encoding="utf-8", buffering=1)
    import uvicorn

    from techrag.config import load_config, user_config_dir
    from techrag.server import create_app

    cfg = load_config()
    token = secrets.token_urlsafe(24)
    cfg.server.api_token = token
    port = _free_port()
    app = create_app(cfg, desktop=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        _message_box("TechRAG", f"The local backend did not start. See {_log_file()}")
        return 1

    try:
        import webview
    except ImportError:
        _message_box("TechRAG", "pywebview is missing from this build.")
        return 1
    api = JsApi()
    window = webview.create_window("TechRAG", f"http://127.0.0.1:{port}/?token={token}", js_api=api,
                                   width=1480, height=940, min_size=(960, 620), text_select=True)
    api.window = window
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass
    storage = user_config_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        kwargs = {"private_mode": False, "storage_path": str(storage)}
        if os.name == "nt":
            kwargs["gui"] = "edgechromium"  # never fall back to the legacy IE engine
        webview.start(**kwargs)
    except Exception as exc:
        _message_box("TechRAG", "Could not open the window. On Windows the Microsoft Edge WebView2 Runtime is "
                                "required (offline installer: 'Evergreen Standalone Installer').\n\n"
                                f"{exc.__class__.__name__}: {exc}")
        return 1
    finally:
        server.should_exit = True
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
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
