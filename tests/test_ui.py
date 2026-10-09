"""The embedded UI in a real (headless) Chromium: citation -> highlighted evidence, zoom alignment, the
highlight toggle and evidence navigation, and that an unverified answer is never shown as verified.
Skipped when Playwright/Chromium is not installed (e.g. the Windows CI build)."""

import glob
import os
import socket
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def ui(cfg, engine, tmp_path, monkeypatch):
    import uvicorn

    from techrag.server import create_app

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, engine, warmup=False, desktop=False), host="127.0.0.1",
                                           port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as p:
            browser = None
            candidates = [None, os.environ.get("TECHRAG_CHROMIUM"), "/opt/pw-browsers/chromium",
                          *glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")]
            for exe in candidates:
                try:
                    browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
                    break
                except Exception:
                    continue
            if browser is None:  # no browser in this environment
                pytest.skip("chromium not available")
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_selector("#collections .doc")
            yield page
            browser.close()
    finally:
        server.should_exit = True
        th.join(timeout=5)


def _ask(page, q):
    page.fill("#question", q)
    page.press("#question", "Enter")
    page.wait_for_selector(".msg.assistant .msg-actions:not([hidden])", timeout=20000)


def _rel(page):
    """Current highlight position as fractions of the page image."""
    return page.evaluate("""() => {
      const img = document.querySelector('#tab-source .page-wrap img').getBoundingClientRect();
      const hl = document.querySelector('#tab-source .hl.current').getBoundingClientRect();
      return [(hl.left - img.left) / img.width, (hl.top - img.top) / img.height,
              hl.width / img.width, hl.height / img.height, img.width];
    }""")


def _wait_img(page):
    page.wait_for_function("() => { const i = document.querySelector('#tab-source .page-wrap img'); return i && i.complete && i.naturalWidth > 0; }")


def test_citation_click_highlights_evidence_and_zoom_keeps_alignment(ui):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    assert "✓" in ui.inner_text(".msg.assistant .verify")
    ui.click(".msg.assistant .body button.cite")
    ui.wait_for_selector("#tab-source .hl.current")
    _wait_img(ui)
    a = _rel(ui)
    ui.click("#tab-source .pv-zin")
    ui.click("#tab-source .pv-zin")
    _wait_img(ui)
    ui.wait_for_timeout(200)
    b = _rel(ui)
    assert b[4] > 1.8 * a[4], "zoomed in"
    for i in range(4):
        assert abs(a[i] - b[i]) < 0.004, (a, b)
    # toggle and navigation
    ui.uncheck("#tab-source .pv-hl")
    assert ui.is_hidden("#tab-source .hl-layer")
    ui.check("#tab-source .pv-hl")
    label = ui.inner_text("#tab-source .pv-evlabel")
    assert label.startswith("1/")
    total = int(label.split("/")[1])
    if total > 1:
        ui.click("#tab-source .pv-evnext")
        assert ui.inner_text("#tab-source .pv-evlabel").startswith("2/")
    status = ui.inner_text("#tab-source .loc-status")
    assert "destekleniyor" in status and ("işaretlendi" in status)


def test_unverified_answer_is_not_shown_as_verified(ui, fake):
    import httpx

    fake.judge = lambda msgs: httpx.Response(500, json={"error": "down"})
    _ask(ui, "DDR5 tRFC değeri nedir?")
    verify = ui.inner_text(".msg.assistant .verify")
    body = ui.inner_text(".msg.assistant .body")
    assert "✓" not in verify and "tamamlanamadı" in verify
    assert "295" not in body
