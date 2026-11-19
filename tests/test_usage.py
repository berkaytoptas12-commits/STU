"""Usage log: one CSV per user (Excel: UTF-8 BOM, ';'), a line per question with tokens and no answer text."""

import csv
import json
import time

import pytest
from fastapi.testclient import TestClient

from techrag.server import create_app
from techrag import usage as U
from techrag.usage import COLUMNS, OLD_COLUMNS, UsageLog, token_totals


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def test_lines_are_appended_per_user_for_excel(tmp_path):
    log = UsageLog(tmp_path / "logs", tmp_path / "local", user="ayse", machine="PC-1")
    log.record("DDR5 tRFC; 8Gb\nnedir?", "cevaplandi", "supported", 12.4,
               {"prompt": 1000, "completion": 200, "reasoning": 50, "calls": 3})
    log.record("I2C hızı?", "hata", tokens={"completion": 10, "estimated": True})
    raw = (tmp_path / "logs" / "ayse.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.decode("utf-8-sig").splitlines()[0] == ";".join(COLUMNS)
    rows = _rows(tmp_path / "logs" / "ayse.csv")
    assert len(rows) == 2 and rows[0]["soru"] == "DDR5 tRFC; 8Gb nedir?", "one line per question, ';' quoted"
    assert rows[0]["kullanici"] == "ayse" and rows[0]["bilgisayar"] == "PC-1" and rows[0]["sure_sn"] == "12"
    assert rows[0]["toplam_token"] == "200", "the tokens the models generated (thinking included), as under Süre"
    assert "istem_token" not in rows[0] and "uretilen_token" not in rows[0] and "dusunme_token" not in rows[0]
    assert rows[0]["model_cagrisi"] == "3"
    assert rows[1]["sonuc"] == "hata" and rows[1]["token_tahmini"] == "evet"
    UsageLog(tmp_path / "logs", tmp_path / "local", user="mehmet").record("q", "cevaplandi", tokens={"prompt": 5})
    s = log.summary()
    assert [u["user"] for u in s["users"]][-1] in ("ayse", "mehmet") and len(s["users"]) == 2
    ayse = next(u for u in s["users"] if u["user"] == "ayse")
    assert ayse["questions"] == 2 and ayse["tokens"] == 210 and ayse["avg_tokens"] == 105 and not s["errors"]


def test_an_unreachable_folder_falls_back_to_a_local_file(tmp_path):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x", encoding="utf-8")
    log = UsageLog(blocker / "logs", tmp_path / "local", user="ayse")
    path = log.record("soru", "cevaplandi")
    assert path == tmp_path / "local" / "ayse.csv" and _rows(path)[0]["soru"] == "soru"
    assert log.last_error, "the admin is told the folder could not be written"


def test_token_totals_sum_every_stage():
    t = token_totals({"answer": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 40, "reasoning_tokens": 10},
                      "judge": {"calls": 1, "prompt_tokens": 50, "completion_tokens": 5, "estimated": True}})
    assert t == {"prompt": 150, "completion": 45, "reasoning": 10, "calls": 3, "estimated": True}


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def test_each_question_is_logged_for_the_current_user(client, cfg, tmp_path, monkeypatch):
    import techrag.server as server

    folder = tmp_path / "share" / "logs"
    r = client.put("/api/settings", json={"usage": {"log_dir": str(folder)}}).json()
    assert r["settings"]["usage"]["log_dir"] == str(folder)
    with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC değeri nedir?"}) as resp:
        events = [json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:")]
    answer = events[-1]["answer"]
    info = None
    for _ in range(100):  # the line is written when the answer thread ends
        info = client.get("/api/usage").json()
        if info["users"]:
            break
        time.sleep(0.05)
    assert info["dir"] == str(folder) and info["user"] == server.current_user()
    rows = _rows(folder / f"{info['user']}.csv")
    assert len(rows) == 1 and rows[0]["soru"] == "DDR5 tRFC değeri nedir?" and rows[0]["sonuc"] == "cevaplandi"
    assert int(rows[0]["toplam_token"]) > 0 and int(rows[0]["model_cagrisi"]) > 0
    assert answer[:40] not in (folder / f"{info['user']}.csv").read_text(encoding="utf-8-sig"), "no answer text"
    assert info["users"][0]["questions"] == 1


def test_logging_can_be_switched_off(client, cfg, tmp_path):
    client.put("/api/settings", json={"usage": {"enabled": False, "log_dir": str(tmp_path / "off")}})
    client.post("/api/ask", json={"question": "DDR5 tRFC değeri nedir?", "stream": False})
    assert not (tmp_path / "off").exists()


def test_a_slow_log_does_not_turn_a_finished_answer_into_a_stopped_one(client, cfg, tmp_path, monkeypatch):
    """Closing the stream after the last event sets ctx.stopped: while the line is being written (a slow network
    folder) that must not count as the user stopping the answer - for the log nor for the desktop notification."""
    from techrag import usage

    real = usage.UsageLog.record

    def slow(self, *a, **kw):
        time.sleep(0.5)
        return real(self, *a, **kw)

    monkeypatch.setattr(usage.UsageLog, "record", slow)
    seen = []
    client.app.state.on_answer = seen.append
    client.app.state.state["cfg"].usage.log_dir = str(tmp_path / "slow")
    with client.stream("POST", "/api/ask", json={"question": "DDR5 tRFC değeri nedir?"}) as resp:
        assert [json.loads(x[5:]) for x in resp.iter_lines() if x.startswith("data:")][-1]["type"] == "final"
    for _ in range(100):
        if seen:
            break
        time.sleep(0.05)
    assert seen and seen[0]["kind"] == "final", "the desktop is told the answer is ready"
    rows = _rows(next((tmp_path / "slow").glob("*.csv")))
    assert rows[0]["sonuc"] == "cevaplandi"


def _old_file(path, rows):
    """A file as written up to 0.15.5: toplam_token = prompt + generated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [";".join(OLD_COLUMNS)] + [";".join(str(r.get(c, "")) for c in OLD_COLUMNS) for r in rows]
    path.write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))


OLD_ROWS = [{"zaman": "2026-11-01T09:00:00+03:00", "kullanici": "ayse", "soru": "eski 1", "sonuc": "cevaplandi",
             "istem_token": 40000, "uretilen_token": 3000, "dusunme_token": 2000, "toplam_token": 43000,
             "model_cagrisi": 6, "token_tahmini": "hayir"},
            {"zaman": "2026-11-02T09:00:00+03:00", "kullanici": "ayse", "soru": "eski 2", "sonuc": "cevaplandi",
             "istem_token": 60000, "uretilen_token": 5000, "dusunme_token": 4000, "toplam_token": 65000,
             "model_cagrisi": 8, "token_tahmini": "hayir"}]


def test_an_old_file_is_converted_once_with_a_copy_kept(tmp_path):
    path = tmp_path / "logs" / "ayse.csv"
    _old_file(path, OLD_ROWS)
    before = path.read_bytes()
    log = UsageLog(tmp_path / "logs", tmp_path / "local", user="ayse")
    assert log.summary()["users"][0]["tokens"] == 8000, "an old file already counts its generated tokens"
    log.record("yeni", "cevaplandi", tokens={"prompt": 50000, "completion": 6000, "reasoning": 4000, "calls": 7})
    assert (tmp_path / "logs" / "ayse.csv.yedek").read_bytes() == before, "the old file is kept as it was"
    raw = path.read_bytes().decode("utf-8-sig")
    assert raw.splitlines()[0] == ";".join(COLUMNS)
    rows = _rows(path)
    assert [r["soru"] for r in rows] == ["eski 1", "eski 2", "yeni"]
    assert [r["toplam_token"] for r in rows] == ["3000", "5000", "6000"], "every line in the new measure"
    assert rows[0]["model_cagrisi"] == "6" and rows[0]["zaman"] == "2026-11-01T09:00:00+03:00"
    s = log.summary()
    assert [u["file"] for u in s["users"]] == ["ayse.csv"], "the copy is not counted as another user"
    assert s["users"][0]["tokens"] == 14000 and s["users"][0]["avg_tokens"] == 4667
    log.record("bir daha", "cevaplandi", tokens={"completion": 1000})
    assert len(list((tmp_path / "logs").glob("*.yedek"))) == 1, "converted once"


def test_a_file_open_elsewhere_keeps_its_layout_until_it_can_be_converted(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "ayse.csv"
    _old_file(path, OLD_ROWS)
    log = UsageLog(tmp_path / "logs", tmp_path / "local", user="ayse")
    real_replace = U.os.replace

    def busy(src, dst):
        raise PermissionError("the file is open in Excel")

    monkeypatch.setattr(U.os, "replace", busy)
    log.record("excel açıkken", "cevaplandi", tokens={"prompt": 50000, "completion": 6000, "reasoning": 4000})
    rows = _rows(path)
    assert path.read_bytes().decode("utf-8-sig").splitlines()[0] == ";".join(OLD_COLUMNS), "not converted yet"
    assert rows[-1]["soru"] == "excel açıkken" and rows[-1]["uretilen_token"] == "6000" \
        and rows[-1]["istem_token"] == "50000", "the line is in the file's old layout: no shifted columns"
    assert not list((tmp_path / "logs").glob("*.tmp")), "no half-written file left behind"
    log.record("excel hâlâ açık", "cevaplandi", tokens={"completion": 1000})
    assert not list((tmp_path / "logs").glob("*.yedek")), "no copy piles up while the file stays open"
    assert log.summary()["users"][0]["tokens"] == 15000, "old-layout lines count their generated tokens"
    monkeypatch.setattr(U.os, "replace", real_replace)
    log.record("sonra", "cevaplandi", tokens={"completion": 2000})
    rows = _rows(path)
    assert path.read_bytes().decode("utf-8-sig").splitlines()[0] == ";".join(COLUMNS)
    assert [r["toplam_token"] for r in rows] == ["3000", "5000", "6000", "1000", "2000"]
    assert len(list((tmp_path / "logs").glob("*.yedek"))) == 1, "one copy, of the file as it was"
