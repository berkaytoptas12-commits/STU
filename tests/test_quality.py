"""Indexing quality report: per-run and per-document records, VLM page accounting, targeted retry, failures that
keep the existing index, schema migration and no secrets in the report (synthetic PDFs + fake model server)."""

import json
import re
import sqlite3
from pathlib import Path

import pymupdf
import pytest

from conftest import _draw_table
from techrag.ingest.quality import scrub

VLM_SYSTEM = "extract tables"


def tables_pdf(path: Path, pages: int = 3) -> Path:
    """A cover page and `pages` pages with one ruled timing table each (table i on PDF page i + 1)."""
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_text((72, 90), "JEDEC STANDARD DDR4 SDRAM Timing Supplement", fontsize=14)
    cover.insert_text((72, 120), "Revision C", fontsize=11)
    for i in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 90), f"{i} Timing group {i}", fontsize=14)
        page.insert_text((72, 130), f"Table {i}-1 Timing parameters", fontsize=11)
        _draw_table(page, 150, [["Parameter", "Min", "Max", "Unit"], [f"tX{i}", f"{100 + i}", "-", "ns"],
                                [f"tY{i}", f"{200 + i}", "-", "ns"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def vlm_reply(fail_pages=(), empty_pages=()):
    def reply(text):
        page = int(re.search(r"Page (\d+)\.", text).group(1))
        if page in fail_pages:
            return ["not the expected JSON object"]          # an unreadable reply: the page failed
        i = page - 1
        if page in empty_pages or f"tX{i}" not in text:
            return {"tables": []}
        return {"tables": [{
            "caption": f"Table {i}-1 Timing parameters", "columns": ["Parameter", "Min", "Max", "Unit"],
            "rows": [[f"tX{i}", f"{100 + i}", "-", "ns"], [f"tY{i}", f"{200 + i}", "-", "ns"]], "footnotes": [],
            "parameters": [
                {"parameter": f"X time {i}", "symbol": f"tX{i}", "min": f"{100 + i}", "typ": "", "max": "",
                 "unit": "ns", "conditions": "", "notes": ""},
                {"parameter": f"Y time {i}", "symbol": f"tY{i}", "min": f"{200 + i}", "typ": "", "max": "",
                 "unit": "ns", "conditions": "", "notes": ""}]}]}
    return reply


def vlm_pages(fake, since=0) -> list[int]:
    out = []
    for c in fake.calls[since:]:
        msgs = c["body"].get("messages") or [{}]
        if VLM_SYSTEM in str(msgs[0].get("content", "")):
            text = " ".join(p.get("text", "") for p in msgs[-1]["content"] if isinstance(p, dict))
            out.append(int(re.search(r"Page (\d+)\.", text).group(1)))
    return sorted(out)


@pytest.fixture()
def qlib(cfg, registry, clients, fake):
    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store

    cfg.vision.min_page_score = 3.0
    tables_pdf(cfg.sources_dir / "ddr" / "DDR4_Timing.pdf")
    store = Store(cfg.db_path)

    def ingestor(vision=True, **kw):
        return Ingestor(cfg, store, clients["embedder"], registry, llm=clients["llm"],
                        vision=clients["vision"] if vision else None, **kw)
    return store, ingestor


KEY = "ddr/DDR4_Timing.pdf"


def test_vlm_failure_is_neither_no_table_nor_success(qlib, fake):
    store, ingestor = qlib
    fake.vlm = vlm_reply(fail_pages={3}, empty_pages={4})
    report = ingestor().run()
    assert report.partial == [KEY] and not report.failed
    q = store.quality()[KEY]
    v = q["data"]["vlm"]
    assert v["enabled"] and v["selected"] == [2, 3, 4], v
    assert v["with_tables"] == [2] and v["no_table"] == [4], "examined without a table"
    assert list(v["failed"]) == ["3"] and 3 not in v["no_table"], "a failed page is not 'no table'"
    assert v["filtered_out"] == 1, "the cover page stayed below the table filter"
    assert q["status"] == "partial" and q["needs_reprocess"]
    issue = next(i for i in q["data"]["issues"] if i["code"] == "vlm_failed")
    assert issue["page"] == 3 and issue["stage"] == "vlm" and "JSON" in issue["detail"]
    run = store.ingest_runs()[0]
    assert run["status"] == "done" and run["kind"] == "sync"
    s = run["summary"]
    assert s["partial"] == 1 and s["vlm"]["failed"] == 1 and s["vlm"]["no_table"] == 1 and s["vlm"]["with_tables"] == 1
    assert s["tables"]["rows"] == 2 and s["tables"]["rows_verified"] == 2
    assert store.document_by_path(KEY).n_chunks > 0, "the document is usable meanwhile"


def test_retry_repeats_only_the_failed_pages_and_keeps_the_rest(qlib, fake):
    store, ingestor = qlib
    fake.vlm = vlm_reply(fail_pages={3})
    ingestor().run()
    before = store.document_by_path(KEY)
    n = len(fake.calls)
    from techrag.ingest.pipeline import retry_plan

    assert retry_plan(store) == [{"path": KEY, "scope": "pages", "pages": [3]}]
    fake.vlm = vlm_reply()
    report = ingestor().retry_failed()
    assert vlm_pages(fake, n) == [3], "pages answered before come from the cache"
    assert report.retry[KEY]["scope"] == "pages" and report.retry[KEY]["result"] == "ok"
    q = store.quality()[KEY]
    assert q["status"] == "ok" and not q["needs_reprocess"] and q["data"]["retry"]["pages"] == [3]
    assert q["data"]["vlm"]["with_tables"] == [2, 3, 4] and q["data"]["vlm"]["cached"] == 2
    emb = q["data"]["embedding"]
    assert emb["reused"] > 0 and emb["embedded"] < emb["reused"] + emb["embedded"], "unchanged chunks not re-sent"
    rows = {r.symbol for r in store.search_parameters("tX3 tX2 tX1", 20, None, verified_only=True)}
    assert {"tX1", "tX2", "tX3"} <= rows, "the earlier tables are still there, the retried one is added"
    assert store.ingest_runs()[0]["kind"] == "retry"
    assert store.document_by_path(KEY).n_chunks >= before.n_chunks


def test_failed_retry_keeps_the_usable_index(qlib, fake, clients, monkeypatch):
    store, ingestor = qlib
    fake.vlm = vlm_reply(fail_pages={3})
    ingestor().run()
    before = store.document_by_path(KEY)
    chunks_before = store.page_chunks(before.id, 2)

    def down(texts):
        raise RuntimeError("POST http://10.20.30.40:8000/v1/embeddings: connection refused "
                           "(Authorization: Bearer sk-secret1234567890abcdef)")
    monkeypatch.setattr(clients["embedder"], "embed_documents", down)
    fake.vlm = vlm_reply()
    report = ingestor().retry_failed()
    assert report.retry[KEY]["result"] == "failed" and KEY in report.failed
    after = store.document_by_path(KEY)
    assert after.id == before.id and after.sha256 == before.sha256, "the document rows were not touched"
    assert [c.text for c in store.page_chunks(after.id, 2)] == [c.text for c in chunks_before]
    q = store.quality()[KEY]
    assert q["status"] == "failed" and q["stage"] == "embedding" and q["index_kept"] and q["last_ok_at"]
    assert q["data"]["previous"]["vlm"]["with_tables"] == [2, 4], "what the kept index holds is still shown"
    text = json.dumps(q) + json.dumps(store.ingest_runs()[0]) + json.dumps(report.to_dict())
    for secret in ("10.20.30.40", "sk-secret", "Bearer sk", "http://"):
        assert secret not in text, secret


def test_a_retry_that_is_worse_is_not_written(qlib, fake, cfg):
    store, ingestor = qlib
    fake.vlm = vlm_reply(fail_pages={3})
    ingestor().run()
    before = store.document_by_path(KEY)
    # the cached pages are lost and the VLM fails everywhere now
    import shutil

    shutil.rmtree(cfg.vlm_cache_dir / before.sha256[:24])
    fake.vlm = vlm_reply(fail_pages={2, 3, 4})
    report = ingestor().retry_failed()
    assert report.retry[KEY]["result"] == "kept_existing"
    after = store.document_by_path(KEY)
    assert after.id == before.id, "the better existing index stays"
    assert store.quality()[KEY]["data"]["vlm"]["failed"] == {"3": store.quality()[KEY]["data"]["vlm"]["failed"]["3"]}


def test_vlm_switched_off_is_reported_as_such(qlib, fake):
    store, ingestor = qlib
    report = ingestor(vision=False).run()
    q = store.quality()[KEY]
    assert q["data"]["vlm"] == {"enabled": False, "off_reason": "disabled"} and q["status"] == "ok"
    assert any(i["code"] == "vlm_disabled" for i in q["data"]["issues"])
    assert store.ingest_runs()[0]["summary"]["vlm"]["enabled"] is False
    assert report.quality["vlm"]["selected"] == 0 and not vlm_pages(fake)


def test_fatal_failures_name_their_stage(qlib, cfg, fake):
    store, ingestor = qlib
    fake.vlm = vlm_reply()
    (cfg.sources_dir / "ddr" / "broken.pdf").write_bytes(b"%PDF-1.7 not really a pdf")
    report = ingestor().run()
    assert "ddr/broken.pdf" in report.failed
    q = store.quality()["ddr/broken.pdf"]
    assert q["status"] == "failed" and q["stage"] == "parse" and not q["index_kept"] and q["needs_reprocess"]
    assert store.ingest_runs()[0]["summary"]["stage_failures"]["parse"] == 1
    assert store.ingest_runs()[0]["status"] == "done_with_errors"
    # fixed by the user: the retry processes the whole document (there is no earlier index to build on)
    tables_pdf(cfg.sources_dir / "ddr" / "broken.pdf", pages=1)
    from techrag.ingest.pipeline import retry_plan

    assert {"path": "ddr/broken.pdf", "scope": "document", "pages": []} in retry_plan(store)
    report = ingestor().retry_failed(["ddr/broken.pdf"])
    assert report.added == ["ddr/broken.pdf"] and report.retry["ddr/broken.pdf"]["result"] == "ok"
    assert store.quality()["ddr/broken.pdf"]["status"] == "ok"


def test_unchanged_documents_keep_their_record_and_runs_are_listed(qlib, fake):
    store, ingestor = qlib
    fake.vlm = vlm_reply()
    ingestor().run()
    first = store.quality()[KEY]["processed_at"]
    report = ingestor().run()
    assert report.skipped == [KEY] and store.quality()[KEY]["processed_at"] == first
    runs = store.ingest_runs()
    assert len(runs) == 2 and runs[0]["summary"]["skipped_unchanged"] == 1 and runs[0]["summary"]["processed"] == 0


def test_schema_v4_library_is_upgraded_without_touching_its_documents(qlib, cfg, fake):
    from techrag.store import Store

    store, ingestor = qlib
    fake.vlm = vlm_reply()
    ingestor().run()
    con = sqlite3.connect(cfg.db_path)  # what a v0.5 library looks like
    con.execute("DROP TABLE doc_quality")
    con.execute("DROP TABLE ingest_runs")
    con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # a shared (read-only) library is one file
    con.close()
    ro = Store(cfg.db_path, read_only=True)
    assert ro.quality() is None and ro.ingest_runs() == [], "an old read-only library: 'not available', no guess"
    docs_before = [(d.id, d.sha256, d.n_chunks) for d in ro.documents()]
    up = Store(cfg.db_path)
    assert up.get_meta("schema_version") == "5" and up.quality() == {}
    assert [(d.id, d.sha256, d.n_chunks) for d in up.documents()] == docs_before
    assert up.search_parameters("tX1", 5, None, verified_only=True), "the index still answers"


def test_scrub_removes_addresses_and_secrets():
    raw = ("ConnectError: https://llm.corp.local:8443/v1/chat failed for 192.168.1.20:8000 with api_key=abc123 "
           "and token: tok_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0; host vllm-box:8000 Bearer eyJhbGciOi.sig")
    out = scrub(raw)
    for bad in ("llm.corp.local", "192.168.1.20", "abc123", "tok_live", "vllm-box", "eyJhbGciOi", "https://"):
        assert bad not in out, (bad, out)
    assert "ConnectError" in out


def test_quality_api_and_retry_job(cfg, engine, tmp_path, monkeypatch, fake):
    from fastapi.testclient import TestClient

    from techrag.server import create_app

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    cfg.llm.api_key = "sk-very-secret-key-123456"
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        r = c.get("/api/quality").json()
        assert r["available"] and r["runs"][0]["summary"]["processed"] == 3 and r["vlm_enabled"]
        docs = {d["path"]: d for d in r["documents"]}
        assert docs["ddr/JESD79-5_DDR5_Mini.pdf"]["quality"]["status"] == "ok"
        assert docs["i2c/UM10204_i2c_notes.md"]["quality"]["data"]["vlm"]["off_reason"] == "not_pdf"
        text = json.dumps(r)
        assert "sk-very-secret" not in text and cfg.llm.base_url not in text
        # a legacy document (indexed before the report existed) has no record: nothing is guessed
        with sqlite3.connect(cfg.db_path) as con:
            con.execute("DELETE FROM doc_quality WHERE path='ddr/JESD79-4C_DDR4_Mini.pdf'")
        r = c.get("/api/quality").json()
        assert next(d for d in r["documents"] if d["path"] == "ddr/JESD79-4C_DDR4_Mini.pdf")["quality"] is None
        assert r["retry"] == []
        job = c.post("/api/quality/retry", json={}).json()["job"]
        import time

        for _ in range(100):
            j = c.get(f"/api/jobs/{job}").json()
            if j["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert j["status"] == "done" and j["report"]["retry"] == {}


def test_cli_quality_and_retry(qlib, cfg, fake, capsys, monkeypatch):
    from types import SimpleNamespace

    from techrag import cli

    store, ingestor = qlib
    fake.vlm = vlm_reply(fail_pages={3})
    ingestor().run()
    cli.cmd_quality(cfg, SimpleNamespace(all=False))
    out = capsys.readouterr().out
    assert "PARTIAL ddr/DDR4_Timing.pdf" in out and "vlm_failed p.3" in out and "denominator" in out
    fake.vlm = vlm_reply()
    monkeypatch.setattr(cli, "_ingestor", lambda c, a: (None, ingestor()))
    assert cli.cmd_retry(cfg, SimpleNamespace(path=[])) == 0
    assert "retry ddr/DDR4_Timing.pdf: pages [3] -> ok" in capsys.readouterr().out


def test_runs_that_did_not_finish_are_listed_as_such(qlib, cfg, fake, monkeypatch):
    from techrag.ingest import pipeline
    from techrag.store import Store

    store, ingestor = qlib
    ing = ingestor()

    def broken(*a, **kw):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(pipeline, "scan_root", broken)
    with pytest.raises(RuntimeError):
        ing.run()                        # the run ends with an error (what the server's job then reports)
    assert store.ingest_runs()[0]["status"] == "running"
    ing.abort_run("failed")
    monkeypatch.undo()
    assert store.ingest_runs()[0]["status"] == "failed"
    ing2 = ingestor()
    fake.vlm = vlm_reply()
    ing2._begin_run(__import__("techrag.ingest.pipeline", fromlist=["IngestReport"]).IngestReport())
    Store(cfg.db_path)                   # the app was closed meanwhile and is started again
    assert store.ingest_runs()[0]["status"] == "interrupted"
