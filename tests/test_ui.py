"""The embedded UI in a real (headless) Chromium: citation -> highlighted evidence, zoom alignment, the
highlight toggle and evidence navigation, and that an unverified answer is never shown as verified.
Skipped when Playwright/Chromium is not installed (e.g. the Windows CI build)."""

import glob
import json
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


def _admin(page, tab):
    page.click("#btn-admin")
    page.wait_for_selector("#dlg-admin[open]")
    page.click(f".adm-tab[data-page={tab}]")
    page.wait_for_selector(f"#{tab}:not([hidden])")


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
    assert ui.get_attribute("#tab-source .pv-evlabel", "title"), "what the current highlight is, on hover"


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
    _admin(ui, "adm-library")
    ui.click("#btn-add-folder")
    ui.wait_for_selector("#dlg-folder[open]")
    body = ui.inner_text("#folder-body")
    assert "Haberleşme Arayüzleri" in body and "PCIe" in body and "Genel (ana klasör)" in body
    assert "1 desteklenmeyen" in body and "Toplam 3" in body
    ui.click("#btn-folder-go")
    ui.wait_for_function("() => /bitti/.test(document.querySelector('#job-title').textContent)", timeout=30000)
    ui.wait_for_function("() => document.querySelector('#adm-roots').textContent.includes('Teknik Belgeler')")
    assert "3 belge" in ui.inner_text("#adm-roots"), "Admin shows where the shared files come from"
    ui.click("#dlg-admin [data-close]")
    assert "3 yeni" in ui.inner_text("#job-now")
    ui.wait_for_function("() => document.querySelectorAll('#collections .bucket').length >= 5")
    names = ui.locator("#collections .bucket-name").all_inner_texts()
    assert "Haberleşme Arayüzleri" in names and "Genel (ana klasör)" in names


def test_notes_feature_is_gone_and_saved_notes_are_left_alone(ui):
    saved = '[{"id": 1, "q": "eski soru", "a": "eski cevap", "ts": "dün", "sources": []}]'
    ui.evaluate(f"localStorage.setItem('techrag.notes', {saved!r})")
    ui.reload()
    ui.wait_for_selector("#collections .bucket")
    gone = ui.evaluate("""() => ['[data-tab="notes"]', '#tab-notes', '#notes-list', '#notes-count',
        '#btn-export-notes', '#btn-clear-notes', '.btn-note', '.tabs'].filter((s) => document.querySelector(s))""")
    assert gone == []
    assert ui.inner_text("#viewer-panel .viewer-head h2") == "Kaynak"
    _ask(ui, "DDR5 tRFC değeri nedir?")
    actions = ui.evaluate("() => [...document.querySelectorAll('.msg.assistant .msg-actions button')].map((b) => b.className)")
    assert actions == ["ghost small btn-copy", "ghost small icon btn-fb-up", "ghost small icon btn-fb-down",
                       "ghost small timing"], "Copy, the answer feedback buttons and the time; no note button"
    _cite(ui)  # sources, PDF page and highlight still work
    ui.wait_for_selector("#tab-source .hl.current")
    assert "Not" not in ui.inner_text("#viewer-panel .viewer-head")
    ui.click(".lang[data-lang=en]")
    assert ui.inner_text("#viewer-panel .viewer-head h2") == "Source"
    assert ui.evaluate("localStorage.getItem('techrag.notes')") == saved, "previously saved notes are not deleted"
    footnote_role = ui.evaluate("() => I18N.tr.role_footnote + '/' + I18N.en.role_footnote")
    assert footnote_role == "dipnot/footnote", "PDF footnotes (evidence role) are a different thing and stay"


def test_hidden_window_does_not_poll_and_resumes_when_shown(ui):
    calls = []
    status = {"value": "running"}

    def job(route):
        calls.append(time.time())
        body = ('{"id": "j1", "status": "%s", "messages": ["working"], "report": null, '
                '"progress": {"phase": "index", "done": 1, "total": 3, "file": "a.pdf"}}') % status["value"]
        route.fulfill(status=200, content_type="application/json", body=body)

    ui.route("**/api/jobs/j1", job)
    ui.evaluate("() => { pollJob('j1', 'test'); }")  # not awaited: it runs until the job ends
    ui.wait_for_function("() => document.querySelector('#job-now').textContent.includes('a.pdf')")
    assert ui.evaluate("window.techragHost.setVisible(false)") is False
    ui.wait_for_timeout(1000)          # a request already on its way may still land
    n = len(calls)
    ui.wait_for_timeout(3000)
    assert len(calls) == n, "no polling while the window is hidden"
    status["value"] = "done"            # indexing went on in the backend meanwhile
    ui.evaluate("window.techragHost.setVisible(true)")
    ui.wait_for_function("() => document.querySelector('#job-bar').style.width === '100%'")
    assert len(calls) > n


def test_clarifying_question_reply_is_merged_and_cleared(ui):
    sent = _sent_scopes(ui)
    _ask(ui, "tRFC değeri nedir?")
    assert "Netleştirme gerekiyor" in ui.inner_text(".msg.assistant .verify") and \
        "bulunamadı” sonucu değildir" in ui.inner_text(".msg.assistant .verify")
    labels = ui.locator(".msg.assistant .clarify button").all_inner_texts()
    assert any(x.startswith("DDR4") for x in labels) and any(x.startswith("DDR5") for x in labels)
    assert "Hepsini karşılaştır" in labels and "Bilmiyorum" in labels
    assert ui.is_visible("#pending-clarify") and "tRFC değeri nedir?" in ui.inner_text("#pending-text")
    _ask(ui, "DDR4")                    # free text reply in the input box
    assert sent[-1]["clarification"]["question"] == "tRFC değeri nedir?"
    plan = ui.evaluate("() => state.traces[0].plan")
    assert "tRFC değeri nedir? (DDR4)" in plan
    assert ui.is_hidden("#pending-clarify")
    assert "350" in ui.inner_text(".msg.assistant >> nth=1 >> .body")
    _ask(ui, "I2C bus speed?")          # no pending question: sent as it is
    assert sent[-1]["clarification"] is None
    # option buttons answer their own question; "Bilmiyorum" gives a conditional answer with a notice
    _ask(ui, "tRFC değeri nedir?")
    n = ui.locator(".msg.assistant").count()
    ui.locator(".msg.assistant >> nth=-1 >> .clarify button", has_text="Bilmiyorum").click()
    ui.wait_for_selector(f".msg.assistant >> nth={n} >> .msg-actions:not([hidden])", timeout=20000)
    assert "Not:" in ui.inner_text(f".msg.assistant >> nth={n} >> .notice")
    # a new chat drops a pending question (and the conversation's standards)
    ui.click("#btn-new-chat")
    _ask(ui, "tRFC değeri nedir?")
    assert ui.is_visible("#pending-clarify")
    ui.click("#btn-new-chat")
    assert ui.is_hidden("#pending-clarify")


def test_source_panel_shows_the_pdf_page_without_a_text_dump(ui):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    _cite(ui)
    ui.wait_for_selector("#tab-source .hl.current")
    _wait_img(ui)
    assert ui.locator("#tab-source .source-text").count() == 0, "no extracted text under the PDF page"
    title = ui.inner_text("#tab-source h3")
    assert title.startswith("[") and "DDR5" in title, "only the citation number and the document title"
    assert ui.locator("#tab-source .loc-status, #tab-source .source-meta, #tab-source .pv-role").count() == 0, \
        "no verification / location lines above the page"
    tip = ui.get_attribute("#tab-source h3", "title")
    assert "Sayfa" in tip and "rev" in tip, "the details are in the title's tooltip"
    panel = ui.evaluate("() => document.querySelector('#viewer-panel').getBoundingClientRect().height")
    view = ui.evaluate("() => document.querySelector('#tab-source .page-view').getBoundingClientRect().height")
    assert view > 0.75 * panel, (view, panel)   # the freed space goes to the page
    assert ui.is_visible("#tab-source .pv-zin") and ui.is_visible("#tab-source .pv-evnext")
    inside = ui.evaluate("""() => { const b = document.querySelector('#tab-source .pv-bar').getBoundingClientRect();
      const v = document.querySelector('#tab-source .page-view').getBoundingClientRect();
      return b.top > v.top && b.bottom <= v.bottom + 1 && b.left >= v.left && b.right <= v.right; }""")
    assert inside, "the controls are a strip over the bottom of the page"
    bg = ui.evaluate("() => getComputedStyle(document.querySelector('#tab-source .pv-bar')).backgroundColor")
    assert bg.startswith("rgba(") and float(bg.rstrip(")").split(",")[-1]) < 1, f"translucent: {bg}"


def test_non_pdf_source_keeps_its_info_and_an_inspect_option(ui):
    md = ui.evaluate("() => state.docs.find((d) => /\\.md$/.test(d.rel_path || d.path)).id")
    ui.evaluate(f"""() => showSource({{n: 1, kind: 'passage', doc_id: {md}, doc_title: 'I2C Bus Notes',
      section: '3.1 Bus Speeds', page_start: 1, page_end: 1, text: 'Fast-mode supports up to 400 kbit/s.'}}, null, null)""")
    assert "PDF değil" in ui.inner_text("#tab-source .no-page")
    assert ui.locator("#tab-source .page-view").count() == 0, "no empty page frame"
    assert not ui.is_visible("#tab-source .source-excerpt .source-text"), "the text is behind the inspect toggle"
    ui.click("#tab-source .source-excerpt summary")
    assert "400 kbit/s" in ui.inner_text("#tab-source .source-excerpt")


def test_missing_pdf_page_says_so_and_offers_the_text(ui):
    ui.route("**/page/**.png*", lambda route: route.fulfill(status=404, body="page cannot be rendered"))
    pdf = ui.evaluate("() => state.docs.find((d) => /\\.pdf$/.test(d.rel_path || d.path)).id")
    ui.evaluate(f"""() => showSource({{n: 2, kind: 'passage', doc_id: {pdf}, doc_title: 'DDR4', section: '3.2',
      page_start: 6, page_end: 6, text: 'The refresh cycle time tRFC for an 8Gb device is 350 ns.'}}, null, null)""")
    ui.wait_for_selector("#tab-source .pv-fail:not([hidden])")
    assert "görüntülenemiyor" in ui.inner_text("#tab-source .pv-fail")
    assert "350 ns" in ui.inner_text("#tab-source .pv-fail .source-excerpt")
    assert ui.is_hidden("#tab-source .page-view")


def test_quality_report_dialog(ui):
    _admin(ui, "adm-library")
    ui.click("#btn-quality")
    ui.wait_for_selector("#dlg-quality[open]")
    ui.wait_for_function("() => document.querySelector('#q-run').textContent.includes('işlendi')")
    run = ui.inner_text("#q-run")
    assert "3 işlendi" in run and "payda: VLM'in çıkardığı satırlar" in run and "4/4 doğrulandı (%100)" in run
    assert "cevap doğruluğu ölçüsü değildir" in ui.inner_text("#dlg-quality .hint")
    assert ui.locator("#q-docs .q-doc").count() == 0, "only documents with something to report by default"
    ui.check("#q-all")
    assert ui.locator("#q-docs .q-doc").count() == 3
    assert "PDF değil" in ui.inner_text("#q-docs")
    # a document with a failed VLM page, and one indexed before the report existed
    data = ui.evaluate("() => QUALITY")
    pdfs = [d for d in data["documents"] if d["pdf"]]
    q = pdfs[0]["quality"]
    q.update(status="partial", needs_reprocess=True)
    q["data"]["vlm"]["failed"] = {"6": "ValueError: page 6: the reply was cut off"}
    q["data"]["issues"] = [{"code": "vlm_failed", "stage": "vlm", "page": 6, "cut_off": True,
                            "detail": "ValueError: page 6: the reply was cut off"}]
    pdfs[1]["quality"] = None
    data["retry"] = [{"path": pdfs[0]["path"], "scope": "pages", "pages": [6]}]
    ui.evaluate("(d) => { QUALITY = d; renderQuality(); }", data)
    ui.uncheck("#q-all")
    docs = ui.inner_text("#q-docs")
    assert "Sayfa 6: VLM tablo çıkarımı başarısız" in docs and "vision.max_tokens" in docs
    assert "yeniden işleme gerekli" in docs and "Kalite bilgisi mevcut değil" in docs
    retry = ui.inner_text("#q-retry")
    assert "1 belgede yalnızca başarısız VLM sayfaları (1 sayfa" in retry and "mevcut indeksi silmez" in retry
    assert ui.is_enabled("#btn-q-retry")
    ui.click("#q-docs .goto")
    assert not ui.evaluate("() => document.querySelector('#dlg-quality').open")
    ui.wait_for_function("() => /Sayfa 6/.test((document.querySelector('#tab-source .pv-label') || {}).textContent || '')")
    _wait_img(ui)


def test_removal_reasons_in_turkish_and_citation_repair_note(ui, fake, cfg):
    from fakeserver import sources_in

    cfg.answer.regenerate = False

    def answer(msgs, tools):
        src = sources_in(msgs)
        right = next(k for k in sorted(src) if "295" in src[k])
        wrong = next(k for k in sorted(src) if "295" not in src[k] and k != right)
        return {"content": f"For an 8Gb DDR5 device tRFC is 295 ns [{wrong}]. The wake-up time is 999 ns [{right}]."}
    fake.answer = answer
    _ask(ui, "DDR5 tRFC değeri nedir?")
    ui.click(".msg.assistant .verify details >> nth=0")
    ui.click(".msg.assistant .verify details >> nth=1")
    verify = ui.inner_text(".msg.assistant .verify")
    assert "'999 ns' değeri atıf yapılan kaynaklarda bulunamadı." in verify, verify
    assert "not found in the cited sources" not in verify, "the English reason text is not shown in Turkish"
    assert "atıf düzeltildi" in verify and "1 ifadede atıf düzeltildi" in verify, verify
    ui.click(".lang[data-lang=en]")
    _ask(ui, "DDR5 tRFC value?")
    last = ui.locator(".msg.assistant .verify").last
    last.locator("details").first.click()
    assert "Value '999 ns' was not found in the cited sources." in last.inner_text()


def test_answer_feedback_up_and_correction(ui, cfg):
    import json

    _ask(ui, "DDR5 tRFC değeri nedir?")
    msg = ui.locator(".msg.assistant").last
    assert msg.locator(".btn-fb-up").get_attribute("title") == "Doğru / faydalı"
    msg.locator(".btn-fb-up").click()
    ui.wait_for_function("() => document.querySelector('.msg.assistant .fb-state').textContent.includes('kaydedildi')")
    msg.locator(".btn-fb-down").click()
    ui.wait_for_selector("#dlg-feedback[open]")
    assert "DDR5 tRFC" in ui.inner_text("#fb-question")
    ui.fill("#fb-value", "295")
    ui.fill("#fb-unit", "ns")
    ui.fill("#fb-document", "JESD79-5")
    ui.fill("#fb-page", "6")
    ui.click("#btn-fb-send")
    ui.wait_for_function("() => !document.querySelector('#dlg-feedback').open")
    items = json.loads((cfg.user_data_dir / "feedback.json").read_text(encoding="utf-8"))
    assert [i["rating"] for i in items] == ["up", "down"]
    assert items[1]["correction"]["value"] == "295" and items[1]["correction"]["page"] == 6
    assert items[0]["sources"] and all(s["doc_title"] and s["page_start"] for s in items[0]["sources"])
    assert "text" not in items[0]["sources"][0], "the source text itself is not stored"


def test_evaluation_dialog_runs_a_question_file(ui, tmp_path):
    f = tmp_path / "set.yaml"
    f.write_text("- id: t1\n  question: DDR5 tRFC değeri nedir?\n  document: DDR5\n  pages: [6]\n"
                 "  facts: [{parameter: [tRFC], value: '295', unit: ns}]\n", encoding="utf-8")
    _admin(ui, "adm-eval")
    ui.wait_for_function("() => document.querySelector('#eval-set-bundled').textContent.length > 0")
    assert "Örnek soru seti (" in ui.inner_text("#eval-set-bundled")
    ui.check("input[name=eval-set][value=custom]")
    ui.set_input_files("#eval-file", str(f))
    ui.click("#btn-eval-run")
    ui.wait_for_function("() => document.querySelector('#eval-status').textContent.includes('Tamamlandı')", timeout=30000)
    result = ui.inner_text("#eval-result")
    assert "Doğrulanmış benchmark değil" in result and "1/1" in result and "%100" in result
    assert "ölçülmedi" in result, "a rate with denominator 0 is shown as not measured"
    ui.click("#eval-history summary")
    assert "set.yaml" in ui.inner_text("#eval-reports")


def test_advanced_settings_are_shown_and_saved(ui):
    ui.click("#btn-settings")
    ui.wait_for_selector("#dlg-settings[open]")
    assert ui.is_visible("#set-api-key") and ui.locator(".svc input[type=password]").count() == 0, \
        "one shared API key, none per service"
    cards = ui.evaluate("() => [...document.querySelectorAll('#dlg-settings .svc')].map((c) => c.dataset.svc)")
    assert cards == ["llm"], "only the chat model in Settings"
    assert ui.locator("#dlg-settings #set-context, #dlg-settings #set-tls-ca, #dlg-settings #set-thinking").count() == 0
    assert ui.is_checked("#set-notify")
    ui.uncheck("#set-notify")
    ui.click("#btn-save-settings")
    ui.wait_for_function("() => document.querySelector('#settings-status').textContent.length > 0")
    ui.wait_for_function("() => !document.querySelector('#dlg-settings').open")
    _admin(ui, "adm-settings")
    ui.wait_for_function("() => document.querySelectorAll('#adm-svc-grid .svc').length === 3")
    cards = ui.evaluate("() => [...document.querySelectorAll('#adm-svc-grid .svc')].map((c) => c.dataset.svc)")
    assert cards == ["vision", "embedding", "reranker"]
    assert ui.input_value("#set-context") == "16000" and ui.input_value("#set-judge-batch") == "8"
    assert ui.is_checked("#set-repair-cites") and ui.input_value("#set-follow-refs") == "3"
    ui.wait_for_function("() => document.querySelector('#ctx-info').textContent.length > 0")
    assert ui.input_value("#set-tool-rounds") == "4" and ui.is_visible("#set-tls-ca") and ui.is_visible("#set-thinking")
    ui.fill("#set-judge-batch", "4")
    ui.fill("#set-follow-refs", "2")
    ui.fill("#set-tool-rounds", "2")
    ui.click("#btn-save-admin-settings")
    ui.wait_for_function("() => document.querySelector('#adm-settings-status').textContent.length > 0")
    s = ui.evaluate("async () => (await (await fetch('/api/settings')).json())")
    assert s["answer"]["judge_batch"] == 4 and s["retrieval"]["follow_references"] == 2
    assert "api_key" not in s["llm"] and s["auth"]["api_key"] == ""
    assert s["llm"]["max_tool_rounds"] == 2 and s["ui"]["notify"] is False

def test_evaluation_results_show_each_answer(ui, tmp_path):
    f = tmp_path / "two.yaml"
    f.write_text("- id: t1\n  kind: value\n  question: DDR5 tRFC değeri nedir?\n"
                 "  facts: [{parameter: [tRFC], value: '295', unit: ns}]\n  note: refresh table\n"
                 "- id: t2\n  kind: value\n  question: DDR5 tRFC değeri nedir?\n"
                 "  facts: [{parameter: [tRFC], value: '999', unit: ns}]\n", encoding="utf-8")
    _admin(ui, "adm-eval")
    ui.check("input[name=eval-set][value=custom]")
    ui.set_input_files("#eval-file", str(f))
    ui.click("#btn-eval-run")
    ui.wait_for_selector(".ev-item >> nth=1", timeout=30000)
    items = ui.locator(".ev-item")
    assert "Doğru" in items.nth(0).inner_text() and "Yanlış" in items.nth(1).inner_text()
    items.nth(0).locator("summary").click()
    body = items.nth(0).inner_text()
    assert "Beklenen" in body and "tRFC = 295 ns" in body and "refresh table" in body
    assert "295 ns" in items.nth(0).locator(".ev-answer").inner_text() and "Atıf yapılan kaynaklar" in body
    ui.check(".ev-only-problems")
    assert items.nth(0).is_hidden() and items.nth(1).is_visible()
    ui.uncheck(".ev-only-problems")
    n = ui.locator(".msg.assistant").count()
    items.nth(0).locator(".ev-ask").click()
    ui.wait_for_selector(f".msg.assistant >> nth={n} >> .msg-actions:not([hidden])", timeout=20000)
    assert not ui.evaluate("document.querySelector('#dlg-admin').open")


_SEND_CLEAR = """() => {
  const a = window.techragHost.iconArea(), r = document.querySelector('#btn-send').getBoundingClientRect();
  const W = innerWidth, H = innerHeight;
  return !(a[0] * W < r.right && a[2] * W > r.left && a[1] * H < r.bottom && a[3] * H > r.top);
}"""


def test_send_button_moves_out_from_under_the_launcher_icon(ui):
    """The launcher icon stays on top of the chat window (also a maximized one); the desktop app tells the page
    which part of the window it covers and the send row moves left of it."""
    l, t, r, b, w, h = ui.evaluate("""() => { const r = document.querySelector('#btn-send').getBoundingClientRect();
      return [r.left, r.top, r.right, r.bottom, innerWidth, innerHeight]; }""")
    area = [(r - 30) / w, (t - 10) / h, (r + 30) / w, (b + 20) / h]   # a 60 px icon over the send button
    assert ui.evaluate(f"window.techragHost.setIconArea({json.dumps(area)})") is True
    assert ui.evaluate(_SEND_CLEAR) is True, "the send button is no longer under the icon"
    assert ui.evaluate("document.querySelector('#question').getBoundingClientRect().width") > 200
    assert "icon-over-composer" in ui.evaluate("document.body.className")
    ui.click("#btn-toggle-right")                                        # the chat column changes width
    ui.wait_for_function(_SEND_CLEAR)
    ui.click("#btn-toggle-right")
    ui.wait_for_function(_SEND_CLEAR)
    ui.evaluate("window.techragHost.setIconArea(null)")                 # the icon moved away
    assert ui.evaluate("document.querySelector('#composer').style.paddingRight") == ""
    assert "icon-over-composer" not in ui.evaluate("document.body.className")
    q = ui.evaluate("""() => { const r = document.querySelector('#question').getBoundingClientRect();
      return [r.left + r.width / 2, r.top, innerWidth, innerHeight]; }""")
    mid = [(q[0] - 30) / q[2], q[1] / q[3], (q[0] + 30) / q[2], (q[1] + 60) / q[3]]
    ui.evaluate(f"window.techragHost.setIconArea({json.dumps(mid)})")   # dragged onto the middle of the box
    assert ui.evaluate("document.querySelector('#composer').style.paddingRight") == "", "layout left alone"


def test_stop_button_stops_a_slow_answer(ui, fake):
    """While the model works the send button becomes Stop; Stop cancels the answer (nothing unverified is shown as
    an answer) and the send button comes back."""
    from techrag import askctx

    def slow(msgs, tools):
        ctx = askctx.current()
        for _ in range(750):
            if ctx is not None and ctx.stopped:
                break
            time.sleep(0.02)
        return {"content": "tRFC is 295 ns [1]."}

    fake.answer = slow
    ui.fill("#question", "DDR5 tRFC?")
    ui.press("#question", "Enter")
    ui.wait_for_selector("#btn-stop:not([hidden]):not([disabled])")
    assert ui.is_hidden("#btn-send") and ui.inner_text("#btn-stop") == "Durdur"
    ui.wait_for_selector(".msg.assistant .elapsed:not([hidden])")
    assert "Geçen süre" in ui.inner_text(".msg.assistant .elapsed")
    ui.click("#btn-stop")
    ui.wait_for_selector(".msg.assistant .notice.stopped", timeout=10000)
    ui.wait_for_selector("#btn-send:not([hidden]):not([disabled])")
    assert ui.is_hidden("#btn-stop") and ui.is_hidden(".msg.assistant .msg-actions")
    assert ui.is_hidden(".msg.assistant .elapsed")


def test_answer_shows_where_its_time_went(ui):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    btn = ui.locator(".msg.assistant .timing")
    assert btn.is_visible() and btn.inner_text().startswith("Süre:")
    btn.click()
    text = ui.inner_text(".msg.assistant .timing-detail")
    for part in ("Soru çözümleme", "Arama", "Cevap yazma", "Doğrulama", "Toplam", "Model çağrıları", "cevap:",
                 "denetçi:"):
        assert part in text, (part, text)
    btn.click()
    assert ui.is_hidden(".msg.assistant .timing-detail")


def test_thinking_and_searches_are_in_admin_not_in_the_chat(ui):
    assert ui.locator("#btn-library").count() == 0 and ui.locator("#btn-eval").count() == 0, \
        "library and evaluation live in Admin"
    _ask(ui, "DDR5 tRFC değeri nedir?")
    msg = ui.locator(".msg.assistant").last
    assert msg.locator(".reasoning, .tool-log, .plan").count() == 0, "the chat shows no thinking or search log"
    assert "295" in msg.locator(".body").inner_text()
    _admin(ui, "adm-trace")
    trace = ui.locator("#trace-list .trace").first
    assert "DDR5 tRFC değeri nedir?" in trace.locator("summary").first.inner_text()
    assert trace.locator(".trace-thinking-box").count() == 1 and not trace.locator(".trace-thinking-box").get_attribute(
        "open"), "thinking is collapsed until opened"
    search = trace.locator(".trace-search-box")
    search.locator("summary").click()
    text = search.inner_text()
    assert "DDR5" in text and "atıf yapıldı" in text, "the sources the model got, cited ones marked"
    assert "search" in text or "Aramalar" in text


def test_personal_files_are_chosen_from_the_computer(ui, tmp_path, cfg):
    groups = ui.evaluate("() => [...document.querySelectorAll('.src-group')].map((h) => h.textContent)")
    assert groups == ["Ortak", "Kişisel"]
    assert str(cfg.personal_dir) in ui.inner_text("#personal-dir")
    assert "Henüz kişisel dosya yok" in ui.inner_text("#personal-collections")
    f = tmp_path / "picked" / "notlarim.md"
    f.parent.mkdir()
    f.write_text("# Notlarım\n\nThe ZQX7 probe ground spring must be shorter than 5 mm.\n", encoding="utf-8")
    ui.set_input_files("#personal-file-input", str(f))
    ui.wait_for_selector("#personal-collections .bucket", timeout=30000)
    assert ui.inner_text("#personal-collections .bucket-name") == "Kişisel"
    assert (cfg.personal_dir / "notlarim.md").exists(), "saved into the personal folder"
    assert ui.locator("#collections .bucket-name", has_text="Kişisel").count() == 0, "not mixed into the shared list"
    _ask(ui, "ZQX7 probe ground spring")
    titles = ui.evaluate("() => state.traces[0].sources.map((s) => s.doc_title)")
    assert titles and "notlar" in titles[0].lower(), f"the personal document is searched with the shared ones: {titles}"


def test_usage_log_tab(ui, tmp_path):
    _admin(ui, "adm-usage")
    ui.wait_for_function("() => document.querySelector('#usage-me').textContent.length > 0")
    folder = tmp_path / "ortak" / "loglar"
    ui.fill("#usage-dir", str(folder))
    ui.click("#btn-usage-save")
    ui.wait_for_function("() => document.querySelector('#usage-status').textContent === 'Kaydedildi.'")
    ui.click("#dlg-admin [data-close]")
    _ask(ui, "DDR5 tRFC değeri nedir?")
    ui.wait_for_function("async () => (await (await fetch('/api/usage')).json()).users.length === 1")
    _admin(ui, "adm-usage")
    ui.wait_for_selector("#usage-table table")
    row = ui.inner_text("#usage-table tbody tr")
    assert row.split("\t")[1].strip() == "1", row
    assert list(folder.glob("*.csv")), "one CSV per user in the chosen folder"


def test_chat_history_keeps_and_reopens_conversations(ui, cfg):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    ui.wait_for_function("async () => (await (await fetch('/api/chats')).json()).chats.length === 1")
    ui.click(".side-tab[data-side=side-chats]")
    ui.wait_for_selector("#chat-list .chat-item")
    assert ui.is_hidden("#collections") and "DDR5 tRFC değeri nedir?" in ui.inner_text("#chat-list .chat-item")
    assert "1 soru" in ui.inner_text("#chat-list .chat-meta")
    assert ui.locator("#chat-list .chat-item.active").count() == 1, "the open conversation is marked"
    ui.click("#btn-chat-new")
    assert ui.locator(".msg.assistant").count() == 0
    _ask(ui, "I2C bus speed?")
    ui.wait_for_function("() => document.querySelectorAll('#chat-list .chat-item').length === 2")
    ui.locator("#chat-list .chat-item", has_text="DDR5 tRFC").click()
    # the I2C chat on screen also has one answer: wait for the reopened conversation itself
    ui.wait_for_function("""() => document.querySelectorAll('.msg.assistant').length === 1
      && (document.querySelector('.msg.user') || {}).textContent === 'DDR5 tRFC değeri nedir?'""")
    msg = ui.locator(".msg.assistant").first
    assert "295" in msg.locator(".body").inner_text() and msg.locator(".msg-actions").is_visible()
    assert "DDR5 tRFC değeri nedir?" in ui.inner_text(".msg.user")
    _cite(ui)                               # citations and PDF highlights work in a reopened conversation
    ui.wait_for_selector("#tab-source .hl.current")
    ui.once("dialog", lambda d: d.accept())
    ui.locator("#chat-list .chat-item", has_text="I2C").locator(".chat-del").click()
    ui.wait_for_function("() => document.querySelectorAll('#chat-list .chat-item').length === 1")
    assert len(list((cfg.user_data_dir / "chats").glob("*.json"))) == 2, "one conversation file + the index"
    ui.click(".side-tab[data-side=side-src]")
    assert ui.is_visible("#collections")


def test_shared_and_personal_lists_are_resizable(ui):
    def heights():
        return ui.evaluate("""() => ['#src-shared', '#src-personal'].map((s) =>
          document.querySelector(s).getBoundingClientRect().height)""")
    a, b = heights()
    assert abs(a - b) < 30, (a, b)   # split in the middle by default
    bar = ui.locator("#src-splitter").bounding_box()
    x, y = bar["x"] + bar["width"] / 2, bar["y"] + bar["height"] / 2
    ui.mouse.move(x, y)
    ui.mouse.down()
    ui.mouse.move(x, y + 120, steps=5)
    ui.mouse.up()
    a2, b2 = heights()
    assert a2 > a + 80 and b2 < b - 80, (a2, b2)
    ui.reload()
    ui.wait_for_selector("#collections .bucket")
    a3, _ = heights()
    assert abs(a3 - a2) < 15, "kept across restarts"
    ui.focus("#src-splitter")
    ui.keyboard.press("Enter")
    a4, b4 = heights()
    assert abs(a4 - b4) < 30, "Enter puts it back in the middle"
    ui.keyboard.press("ArrowUp")
    assert heights()[0] < a4 - 10


def test_a_personal_document_is_removed_from_the_list(ui, tmp_path, cfg):
    f = tmp_path / "picked" / "silinecek.md"
    f.parent.mkdir()
    f.write_text("# Silinecek\n\nThe QWX5 rail is 0.8 V.\n", encoding="utf-8")
    ui.set_input_files("#personal-file-input", str(f))
    ui.wait_for_selector("#personal-collections .bucket", timeout=30000)
    ui.click("#personal-collections .bucket-name")
    assert ui.locator("#collections .doc-del").count() == 0, "shared documents have no remove button"
    ui.once("dialog", lambda d: d.accept())
    ui.click("#personal-collections .doc-del")
    ui.wait_for_function("() => !document.querySelector('#personal-collections .bucket')")
    assert "kaldırıldı" in ui.inner_text("#personal-status")
    assert not (cfg.personal_dir / "silinecek.md").exists() and f.exists(), "the picked original stays"


_POS = """() => { const m = document.querySelector('#messages'); const els = m.querySelectorAll('.msg.assistant');
  const el = els[els.length - 1];
  return {delta: el.getBoundingClientRect().top - m.getBoundingClientRect().top, top: m.scrollTop,
          max: m.scrollHeight - m.clientHeight}; }"""


def test_sources_come_with_the_answer_and_the_view_starts_at_it(ui, fake):
    import re

    gate = threading.Event()
    base = fake._answer

    def answer(msgs, tools):
        gate.wait(20)
        out = base(msgs, tools)
        m = re.search(r"is (\d+) ns \[(\d+)\]", out.get("content") or "")
        if m:   # a long answer: longer than the chat area
            line = f"- The refresh cycle time tRFC for an 8Gb device is {m[1]} ns [{m[2]}]."
            out = {"content": f"tRFC for an 8Gb device is {m[1]} ns [{m[2]}].\n\n### Documented\n" + "\n".join([line] * 40)}
        return out

    fake.answer = answer
    ui.set_viewport_size({"width": 1500, "height": 700})
    gate.set()
    _ask(ui, "DDR5 tRFC değeri nedir?")   # an earlier answer above
    gate.clear()
    ui.fill("#question", "DDR4 tRFC değeri nedir?")
    ui.press("#question", "Enter")
    ui.wait_for_function("() => state.traces.length === 2 && state.traces[0].sources.length > 0")
    assert ui.locator(".msg.assistant >> nth=1 >> .cite-chip").count() == 0, "no sources while the answer is written"
    gate.set()
    ui.wait_for_selector(".msg.assistant >> nth=1 >> .msg-actions:not([hidden])", timeout=20000)
    assert ui.locator(".msg.assistant >> nth=1 >> .cite-chip").count() > 0, "the sources come with the answer"
    ui.wait_for_function(f"() => {{ const p = ({_POS})(); return Math.abs(p.delta - 8) < 3; }}")
    pos = ui.evaluate(_POS)
    assert pos["top"] < pos["max"] - 50, f"the start of the answer, not the end of the chat: {pos}"
    # a reopened conversation opens at the start of its last answer too
    ui.click(".side-tab[data-side=side-chats]")
    ui.wait_for_selector("#chat-list .chat-item")
    ui.click("#btn-chat-new")
    ui.locator("#chat-list .chat-item").first.click()
    ui.wait_for_function("() => document.querySelectorAll('.msg.assistant').length === 2")
    ui.wait_for_function(f"() => {{ const p = ({_POS})(); return Math.abs(p.delta - 8) < 3 && p.top < p.max - 50; }}")


def test_strip_says_why_a_citation_has_no_highlight(ui, fake):
    fake.answer = lambda msgs, tools: {"content": "Bu bölüm yenileme işlemini anlatır [1]."}
    _ask(ui, "DDR5 yenileme nasıl çalışır?")
    _cite(ui)
    _wait_img(ui)
    assert ui.locator("#tab-source .hl").count() == 0
    note = ui.locator("#tab-source .pv-bar .pv-note")
    assert note.is_visible() and "Konum eşleştirilemedi" in note.inner_text()
    assert "kesin konum eşleştirilemedi" in note.get_attribute("title")
    fake.answer = None
    _ask(ui, "DDR5 tRFC değeri nedir?")
    ui.locator(".msg.assistant >> nth=1 >> .body button.cite").first.click()
    ui.wait_for_selector("#tab-source .hl.current")
    assert ui.locator("#tab-source .pv-note").count() == 0, "no note when the evidence is highlighted"


def test_the_start_screen_has_no_example_questions(ui):
    assert ui.is_visible("#empty-state") and "Standartlara sorun" in ui.inner_text("#empty-state")
    assert ui.locator("#empty-state button, #examples, .example").count() == 0


def test_shared_and_personal_lists_collapse(ui):
    def pane_heights():
        return ui.evaluate("""() => ['#src-shared', '#src-personal'].map((s) =>
          document.querySelector(s).getBoundingClientRect().height)""")
    a, b = pane_heights()
    ui.click(".src-toggle[data-pane=shared]")
    assert ui.is_hidden("#collections") and ui.is_hidden("#src-splitter")
    assert ui.get_attribute(".src-toggle[data-pane=shared]", "aria-expanded") == "false"
    a2, b2 = pane_heights()
    assert a2 < 40 and b2 > b + a - 60, "the open list takes the room of the collapsed one"
    assert ui.evaluate("() => document.querySelector('.src-toggle[data-pane=shared]').textContent") == "Ortak"
    ui.reload()
    ui.wait_for_selector("#personal-box", state="attached")
    assert ui.is_hidden("#collections"), "kept across restarts"
    ui.click(".src-toggle[data-pane=personal]")
    assert ui.is_hidden("#personal-box") and ui.is_hidden("#personal-collections")
    ui.click(".src-toggle[data-pane=shared]")
    ui.click(".src-toggle[data-pane=personal]")
    ui.wait_for_selector("#collections .bucket")
    assert ui.is_visible("#src-splitter") and ui.is_visible("#personal-box")
    a3, b3 = pane_heights()
    assert abs(a3 - a) < 15 and abs(b3 - b) < 15, "both open: the bar divides the room as before"


def test_admin_removes_and_restores_a_shared_document(ui, cfg):
    files = {p: p.read_bytes() for p in cfg.sources_dir.rglob("*") if p.is_file()}
    count = ui.locator("#collections .bucket .count").evaluate_all("(els) => els.reduce((a, e) => a + +e.textContent, 0)")
    _admin(ui, "adm-library")
    ui.click("#adm-docs-box summary")
    ui.wait_for_selector("#adm-docs .adm-doc-del")
    assert "dosyası silinmez" in ui.inner_text("#adm-docs-box .hint")
    ui.fill("#adm-doc-filter", "DDR5")
    assert ui.locator("#adm-docs li").count() == 1
    name = ui.inner_text("#adm-docs .adm-doc-name")
    ui.once("dialog", lambda d: d.accept())
    ui.click("#adm-docs .adm-doc-del")
    ui.wait_for_function("(n) => document.querySelector('#adm-docs-status').textContent.includes(n)", arg=name)
    ui.wait_for_selector("#adm-removed .adm-restore")
    assert name in ui.inner_text("#adm-removed")
    after = ui.locator("#collections .bucket .count").evaluate_all("(els) => els.reduce((a, e) => a + +e.textContent, 0)")
    assert after == count - 1
    assert {p: p.read_bytes() for p in files} == files, "no file deleted or changed"
    ui.click("#adm-removed .adm-restore")
    ui.wait_for_function(f"() => [...document.querySelectorAll('#collections .bucket .count')].reduce((a, e) => a + +e.textContent, 0) === {count}",
                         timeout=30000)
    ui.wait_for_function("() => document.querySelector('#adm-removed').textContent.includes('Kaldırılan belge yok')")


def test_a_folder_becomes_one_personal_bucket(ui, tmp_path):
    picked = tmp_path / "Proje Notları"
    (picked / "alt").mkdir(parents=True)
    (picked / "a.md").write_text("# A\n\nThe QQA4 rail is 0.9 V.\n", encoding="utf-8")
    (picked / "alt" / "b.md").write_text("# B\n\nThe QQB4 rail is 0.8 V.\n", encoding="utf-8")
    ui.once("dialog", lambda d: d.accept(str(picked)))
    ui.click("#btn-personal-folder")
    ui.wait_for_selector("#personal-collections .bucket .link-ic", state="attached", timeout=10000)  # listed at once
    # indexed: one bucket with both documents (sub-folders included)
    ui.wait_for_function("""() => { const c = document.querySelector('#personal-collections .bucket:not(.linked-empty) .count');
      return c && c.textContent === '2'; }""", timeout=30000)
    assert ui.locator("#personal-collections .bucket").count() == 1
    assert ui.inner_text("#personal-collections .bucket-name") == "Proje Notları"
    assert str(picked) in ui.get_attribute("#personal-collections .link-ic", "title")
    ui.once("dialog", lambda d: d.accept())
    ui.click("#personal-collections .bucket-unlink")
    ui.wait_for_function("() => !document.querySelector('#personal-collections .bucket')")
    assert "dosyalara dokunulmadı" in ui.inner_text("#personal-status")
    assert (picked / "a.md").exists() and (picked / "alt" / "b.md").exists()


def test_a_judge_cut_off_by_thinking_is_explained(ui, fake):
    def judge(msgs):
        import httpx

        events = [{"choices": [{"delta": {"reasoning_content": "Let me think. " * 60}}]},
                  {"choices": [{"delta": {}, "finish_reason": "length"}]}]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    fake.judge = judge
    _ask(ui, "DDR5 tRFC değeri nedir?")
    box = ui.inner_text(".msg.assistant .verify")
    assert "düşünmeye harcadı" in box and "JudgeCutOff" not in box, box


def test_contents_sidebar_left_of_the_pdf(ui):
    ui.click("#collections .bucket-name >> nth=0")
    ui.click("#collections .doc .doc-name >> nth=0")
    ui.wait_for_selector("#tab-source .toc-side .toc a")
    _wait_img(ui)
    side = ui.evaluate("""() => { const s = document.querySelector('#tab-source .toc-side').getBoundingClientRect();
      const p = document.querySelector('#tab-source .page-view').getBoundingClientRect(); return [s.left, s.right, p.left, s.width]; }""")
    assert side[1] <= side[2], "the contents sit to the left of the page"
    assert ui.locator("#tab-source h3", has_text="İçindekiler").count() == 0, "no list under the page any more"
    # an entry turns to its page
    target = ui.get_attribute("#tab-source .toc-side .toc a >> nth=-1", "data-page")
    ui.click("#tab-source .toc-side .toc a >> nth=-1")
    ui.wait_for_function("(p) => document.querySelector('#tab-source .pv-label').textContent.startsWith('Sayfa ' + p)", arg=target)
    # the width is dragged on the bar and kept
    bar = ui.locator("#tab-source .toc-split").bounding_box()
    ui.mouse.move(bar["x"] + bar["width"] / 2, bar["y"] + bar["height"] / 2)
    ui.mouse.down()
    ui.mouse.move(bar["x"] + 80, bar["y"] + bar["height"] / 2, steps=5)
    ui.mouse.up()
    w = ui.evaluate("() => document.querySelector('#tab-source .toc-side').getBoundingClientRect().width")
    shell = ui.evaluate("() => document.querySelector('#tab-source .pv-shell').getBoundingClientRect().width")
    assert w > side[3] + 20 and w <= 0.6 * shell + 1, ("wider, but the page keeps most of the panel", w, side, shell)
    # closed from the strip, and still closed after a restart
    ui.click("#tab-source .pv-toc")
    assert ui.is_hidden("#tab-source .toc-side") and ui.is_hidden("#tab-source .toc-split")
    ui.reload()
    ui.wait_for_selector("#collections .bucket")
    if not ui.locator("#collections .doc .doc-name").first.is_visible():   # the open buckets are kept too
        ui.click("#collections .bucket-name >> nth=0")
    ui.click("#collections .doc .doc-name >> nth=0")
    ui.wait_for_selector("#tab-source .pv-toc:not([hidden])")
    assert ui.is_hidden("#tab-source .toc-side")
    ui.click("#tab-source .pv-toc")
    assert abs(ui.evaluate("() => document.querySelector('#tab-source .toc-side').getBoundingClientRect().width") - w) < 3


def test_contents_in_the_source_view_start_closed(ui):
    _ask(ui, "DDR5 tRFC değeri nedir?")
    _cite(ui)
    ui.wait_for_selector("#tab-source .pv-toc:not([hidden])")
    assert ui.is_hidden("#tab-source .toc-side"), "a citation opens on its highlighted page"
    ui.wait_for_selector("#tab-source .hl.current")
    ui.click("#tab-source .pv-toc")
    assert ui.is_visible("#tab-source .toc-side .toc a")


def test_first_start_asks_for_the_organization_settings(ui, cfg, tmp_path):
    import yaml

    from techrag import org

    share = tmp_path / "paylasim"
    share.mkdir()
    data = org.export(cfg)
    (share / org.ORG_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    model = cfg.llm.model
    cfg.llm.model = ""          # a fresh install: nothing set up, nothing found
    ui.reload()
    ui.wait_for_selector("#dlg-org[open]")
    ui.fill("#org-ask-path", str(tmp_path))
    ui.click("#btn-org-use")
    ui.wait_for_function("() => document.querySelector('#org-ask-status').textContent.includes('bulunamadı')")
    ui.fill("#org-ask-path", str(share))
    ui.click("#btn-org-use")
    ui.wait_for_selector("#dlg-org:not([open])", state="attached")
    ui.wait_for_selector("#dlg-settings[open]")   # the organization settings came in: only the key is asked for
    assert "API anahtarınızı" in ui.inner_text("#settings-org-hint")
    assert ui.evaluate("() => document.activeElement.id") == "set-api-key"
    assert client_model(ui) == model


def client_model(ui):
    return ui.evaluate("async () => (await (await fetch('/api/settings')).json()).llm.model")


def test_admin_saves_the_organization_settings(ui, tmp_path):
    from techrag import org

    share = tmp_path / "ortak"
    share.mkdir()
    _admin(ui, "adm-settings")
    ui.wait_for_function("() => document.querySelector('#org-status').textContent.includes('Kurum ayarı kullanılmıyor')")
    ui.fill("#org-path", str(share))
    ui.click("#btn-org-save")
    ui.wait_for_function("() => document.querySelector('#org-save-status').textContent.includes('Kaydedildi')")
    status = ui.inner_text("#org-save-status")
    assert org.POINTER_FILE in status and "dağıtırken" in status
    assert (share / org.ORG_FILE).exists() and (tmp_path / "exe" / org.POINTER_FILE).exists()
    ui.wait_for_function("(p) => document.querySelector('#org-status').textContent.includes(p)", arg=str(share))
