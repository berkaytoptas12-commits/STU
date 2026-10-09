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
            page.wait_for_selector("#collections .bucket")
            yield page
            browser.close()
    finally:
        server.should_exit = True
        th.join(timeout=5)


def _ask(page, q):
    n = page.locator(".msg.assistant").count()
    page.fill("#question", q)
    page.press("#question", "Enter")
    try:
        page.wait_for_selector(f".msg.assistant >> nth={n} >> .msg-actions:not([hidden])", timeout=20000)
    except Exception as exc:
        raise AssertionError(page.inner_text(".messages")) from exc


def _cite(page):
    try:
        page.click(".msg.assistant .body button.cite", timeout=10000)
    except Exception as exc:
        info = page.evaluate("""() => ({body: document.body.className, msgs: document.querySelectorAll('.msg').length,
          html: document.querySelector('.messages').innerHTML.slice(0, 600),
          chat: JSON.stringify(document.querySelector('.chat').getBoundingClientRect())})""")
        raise AssertionError(str(info)) from exc


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
    _cite(ui)
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


# ---------------------------------------------------------------------------- panels

def _sent_scopes(page):
    """Capture the scope of every /api/ask request."""
    sent = []
    page.on("request", lambda r: sent.append(r.post_data_json) if r.url.endswith("/api/ask") else None)
    return sent


def test_bucket_panel_collapse_keeps_scope_and_persists(ui):
    import json as _json

    assert ui.locator("#collections .doc").count() == 0 or not ui.is_visible("#collections .doc"), "buckets start closed"
    counts = ui.locator("#collections .bucket .count").all_inner_texts()
    assert counts == ["2", "1"]
    ui.click("#collections .bucket >> nth=0 >> .bucket-name")
    assert ui.is_visible("#collections .bucket >> nth=0 >> .doc")
    # long names are cut with an ellipsis and carry the full name + location as a tooltip
    name = ui.locator("#collections .doc-name").first
    assert "\n" in name.get_attribute("title") and ui.evaluate(
        "() => getComputedStyle(document.querySelector('#collections .doc-name')).textOverflow") == "ellipsis"
    ui.check("#collections .bucket >> nth=0 >> .doc >> nth=1 >> input")
    assert "1 belge" in ui.inner_text("#scope-line")
    ui.fill("#doc-filter", "i2c")
    assert ui.locator("#collections .bucket").count() == 1
    ui.fill("#doc-filter", "")
    sent = _sent_scopes(ui)
    ui.click("#btn-collapse-left")
    assert ui.is_hidden("#sources-panel")
    _ask(ui, "DDR5 tRFC değeri nedir?")
    assert sent and sent[-1].get("doc_ids") == [2], "collapsing the panel does not change the scope"
    assert ui.evaluate("() => document.querySelector('.chat').getBoundingClientRect().width") > 600
    ui.click("#btn-toggle-left")
    assert ui.is_visible("#sources-panel") and "1 belge" in ui.inner_text("#scope-line")
    ui.click("#btn-collapse-left")
    ui.reload()
    ui.wait_for_selector("#collections .bucket", state="attached")
    assert ui.is_hidden("#sources-panel"), "collapsed state survives a restart"
    ui.click("#btn-toggle-left")
    assert ui.is_visible("#collections .bucket >> nth=0 >> .doc"), "opened buckets survive a restart"
    saved = _json.loads(ui.evaluate("() => localStorage.getItem('techrag.ui')"))
    assert saved["openBuckets"] and saved["leftCollapsed"] is False


def test_bucket_selection_is_sent_as_scope(ui):
    sent = _sent_scopes(ui)
    ui.check("#collections .bucket >> nth=0 >> .bucket-cb")
    assert "ddr" in ui.inner_text("#scope-line")
    _ask(ui, "DDR5 tRFC değeri nedir?")
    assert sent[-1].get("buckets") and not sent[-1].get("doc_ids")


def _viewer_w(page):
    return page.evaluate("() => document.querySelector('#viewer-panel').getBoundingClientRect().width")


def test_source_panel_resize_hide_show_and_persist(ui):
    w0 = _viewer_w(ui)
    box = ui.locator("#splitter").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    ui.mouse.move(x, y)
    ui.mouse.down()
    ui.mouse.move(x - 150, y, steps=5)
    assert ui.evaluate("() => document.body.classList.contains('resizing')")
    assert ui.evaluate("() => getComputedStyle(document.body).userSelect") == "none", "no text selection while dragging"
    ui.mouse.up()
    assert abs(_viewer_w(ui) - (w0 + 150)) < 3
    # limits: neither side becomes unusable
    ui.mouse.move(x - 150, y)
    ui.mouse.down()
    ui.mouse.move(5, y, steps=5)
    ui.mouse.up()
    chat = ui.evaluate("() => document.querySelector('.chat').getBoundingClientRect().width")
    assert chat >= 379
    ui.focus("#splitter")
    ui.keyboard.press("End")
    assert abs(_viewer_w(ui) - 300) < 2, "minimum width"
    ui.keyboard.press("ArrowLeft")
    assert abs(_viewer_w(ui) - 320) < 2, "keyboard resizing"
    ui.reload()
    ui.wait_for_selector("#collections .bucket", state="attached")
    assert abs(_viewer_w(ui) - 320) < 2, "width survives a restart"
    ui.click("#btn-close-right")
    assert ui.is_hidden("#viewer-panel") and ui.is_hidden("#splitter")
    ui.reload()
    ui.wait_for_selector("#collections .bucket", state="attached")
    assert ui.is_hidden("#viewer-panel")
    ui.click("#btn-toggle-right")
    assert ui.is_visible("#viewer-panel") and abs(_viewer_w(ui) - 320) < 2


def test_saved_width_is_clamped_on_a_smaller_window(ui):
    ui.evaluate("() => localStorage.setItem('techrag.ui', JSON.stringify({rightW: 1400}))")
    ui.set_viewport_size({"width": 1200, "height": 900})
    ui.reload()
    ui.wait_for_selector("#collections .bucket", state="attached")
    chat = ui.evaluate("() => document.querySelector('.chat').getBoundingClientRect().width")
    assert chat >= 379 and _viewer_w(ui) < 1200 - 236 - 379
    ui.set_viewport_size({"width": 900, "height": 900})  # narrow: panels become drawers, chat keeps the width
    ui.wait_for_timeout(100)
    assert ui.is_hidden("#viewer-panel") and ui.is_hidden("#sources-panel")
    assert ui.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


def test_highlight_alignment_at_different_panel_widths(ui):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    _cite(ui)
    ui.wait_for_selector("#tab-source .hl.current")
    _wait_img(ui)
    a = _rel(ui)
    ui.focus("#splitter")
    ui.keyboard.press("Home")  # widest
    ui.wait_for_timeout(150)
    b = _rel(ui)
    assert b[4] > a[4] + 50
    ui.keyboard.press("End")  # narrowest
    ui.wait_for_timeout(150)
    c = _rel(ui)
    for i in range(4):
        assert abs(a[i] - b[i]) < 0.004 and abs(a[i] - c[i]) < 0.004, (a, b, c)


def test_narrow_window_opens_the_source_as_a_drawer(ui):
    ui.set_viewport_size({"width": 860, "height": 900})
    ui.reload()
    ui.wait_for_selector("#collections .bucket", state="attached")
    _ask(ui, "DDR5 tRFC değeri nedir?")
    _cite(ui)
    ui.wait_for_selector("#tab-source .hl.current")
    assert ui.is_visible("#viewer-panel")
    ui.click("#btn-close-right")
    assert ui.is_hidden("#viewer-panel")


def test_add_document_folder_flow(ui, tmp_path):
    from test_folders import simple_pdf

    root = tmp_path / "Teknik Belgeler"
    simple_pdf(root / "PCIe" / "Base_Specification.pdf", "PCI Express Base Specification 5.0", "LTSSM training.")
    simple_pdf(root / "Haberleşme Arayüzleri" / "Özel Belge.pdf", "I2C bus notes", "Fast-mode is 400 kbit/s.")
    simple_pdf(root / "Kök Belge.pdf", "General notes", "Top level document.")
    (root / "PCIe" / "notlar.xlsx").write_bytes(b"x")
    ui.once("dialog", lambda d: d.accept(str(root)))
    ui.click("#btn-add-folder")
    ui.wait_for_selector("#dlg-folder[open]")
    body = ui.inner_text("#folder-body")
    assert "Haberleşme Arayüzleri" in body and "PCIe" in body and "Genel (ana klasör)" in body
    assert "1 desteklenmeyen" in body and "Toplam 3" in body
    ui.click("#btn-folder-go")
    ui.wait_for_function("() => /bitti/.test(document.querySelector('#job-title').textContent)", timeout=30000)
    assert "3 yeni" in ui.inner_text("#job-now")
    ui.wait_for_function("() => document.querySelectorAll('#collections .bucket').length >= 5")
    names = ui.locator("#collections .bucket-name").all_inner_texts()
    assert "Haberleşme Arayüzleri" in names and "Genel (ana klasör)" in names
