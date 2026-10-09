"""Desktop entry point (TechRAG.exe).

* No arguments: start the FastAPI backend in-process on a random localhost port with a per-launch token
  and show the UI in a native window (Microsoft Edge WebView2 through pywebview). No browser is opened.
* With arguments: behave exactly like the `techrag` CLI (e.g. `TechRAG.exe ingest`, `TechRAG.exe doctor`).
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

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
    """techrag.log next to settings.json (%APPDATA%\\TechRAG by default)."""
    from techrag.settings import settings_path

    d = settings_path().parent
    d.mkdir(parents=True, exist_ok=True)
    return d / "techrag.log"


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

    def save_text(self, filename: str, content: str) -> bool:
        r = self._window.create_file_dialog(_dialog("SAVE"), save_filename=filename)
        path = r if isinstance(r, str) else (r[0] if r else None)
        if not path:
            return False
        Path(path).write_text(content, encoding="utf-8")
        return True


SMOKE_JS = """(function () {
  var b = document.querySelector('.brand');
  return {brand: b ? b.textContent : '', ready: document.readyState,
          bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder),
          collections: !!document.querySelector('#collections')};
})()"""


def _setup_logging(install_hooks: bool) -> Path:
    """Everything (our stages, uvicorn, pywebview, native faults) goes to %APPDATA%\\TechRAG\\techrag.log."""
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
            _message_box("TechRAG", f"{exc_type.__name__}: {exc}\n\nDetails: {path}")

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


def _smoke(window, result_path: str) -> None:
    """CI check of the real window: page loaded, UI rendered, JS<->Python bridge answers; then close."""
    import json

    result: dict = {"ok": False}
    try:
        if not window.events.loaded.wait(120):
            raise TimeoutError("page did not finish loading within 120 s")
        r: dict = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            r = window.evaluate_js(SMOKE_JS) or {}
            if r.get("bridge") and r.get("collections"):
                break
            time.sleep(1)
        result.update(r)
        result["ok"] = bool(r.get("bridge") and "TechRAG" in str(r.get("brand", "")))
    except Exception as exc:
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    log.info("smoke result: %s", result)
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    window.destroy()


def run_gui() -> int:
    from techrag import __version__

    log_path = _setup_logging(install_hooks=getattr(sys, "frozen", False))
    log.info("=== TechRAG %s starting (frozen=%s, python=%s, exe=%s)", __version__, getattr(sys, "frozen", False),
             sys.version.split()[0], sys.executable)
    try:
        log.info("mark-of-the-web removed from %d bundled file(s)", _unblock_bundle())
    except Exception as exc:
        log.warning("could not unblock bundled files: %s", exc)
    import uvicorn

    from techrag.config import load_config, user_config_dir
    from techrag.server import create_app

    cfg = load_config()
    log.info("library: %s", cfg.library_dir)
    token = secrets.token_urlsafe(24)
    cfg.server.api_token = token
    port = _free_port()
    app = create_app(cfg, desktop=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, access_log=False))
    threading.Thread(target=server.run, name="backend", daemon=True).start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        log.error("backend did not start")
        _message_box("TechRAG", f"The local backend did not start. See {log_path}")
        return 1
    log.info("backend listening on 127.0.0.1:%d", port)

    try:
        import webview
    except ImportError:
        _message_box("TechRAG", "pywebview is missing from this build.")
        return 1
    log.info("pywebview %s", _webview_version(webview))
    api = JsApi()
    window = webview.create_window("TechRAG", f"http://127.0.0.1:{port}/?token={token}", js_api=api,
                                   width=1480, height=940, min_size=(960, 620), text_select=True)
    api._window = window
    try:
        window.events.loaded += lambda: log.info("page loaded")
        window.events.closed += lambda: log.info("window closed")
    except Exception:
        pass
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass
    storage = user_config_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    smoke = os.environ.get("TECHRAG_SMOKE")
    try:
        kwargs = {"private_mode": False, "storage_path": str(storage),
                  "debug": bool(os.environ.get("TECHRAG_WEBVIEW_DEBUG"))}
        if os.name == "nt":
            kwargs["gui"] = "edgechromium"  # never fall back to the legacy IE engine
        if smoke:
            kwargs.update(func=_smoke, args=(window, smoke))
        log.info("starting GUI loop (%s)", kwargs.get("gui", "default"))
        webview.start(**kwargs)
    except Exception as exc:
        log.exception("GUI failed")
        _message_box("TechRAG", "Could not open the window. On Windows the Microsoft Edge WebView2 Runtime is "
                                "required (offline installer: 'Evergreen Standalone Installer').\n\n"
                                f"{exc.__class__.__name__}: {exc}\n\nLog: {log_path}")
        return 1
    finally:
        server.should_exit = True
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
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
