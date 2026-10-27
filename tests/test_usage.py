"""Usage log: one CSV per user (Excel: UTF-8 BOM, ';'), a line per question with tokens and no answer text."""

import csv
import json
import time

import pytest
from fastapi.testclient import TestClient

from techrag.server import create_app
from techrag.usage import COLUMNS, UsageLog, token_totals


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
    assert rows[0]["toplam_token"] == "1200" and rows[0]["dusunme_token"] == "50" and rows[0]["model_cagrisi"] == "3"
    assert rows[1]["sonuc"] == "hata" and rows[1]["token_tahmini"] == "evet"
    UsageLog(tmp_path / "logs", tmp_path / "local", user="mehmet").record("q", "cevaplandi", tokens={"prompt": 5})
    s = log.summary()
    assert [u["user"] for u in s["users"]][-1] in ("ayse", "mehmet") and len(s["users"]) == 2
    ayse = next(u for u in s["users"] if u["user"] == "ayse")
    assert ayse["questions"] == 2 and ayse["tokens"] == 1210 and not s["errors"]


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
