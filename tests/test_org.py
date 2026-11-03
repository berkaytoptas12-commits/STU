"""Organization settings ("kurum ayarları"): saved by an admin into a shared folder with a pointer file next to the
exe; read by everyone's app at start as defaults (the user's own settings win; nothing is locked); never the API key
or personal folders; the copy read last time when the folder cannot be reached; asked once at first start."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from techrag import org
from techrag.config import Config, load_config
from techrag.server import create_app
from techrag.settings import load_user_settings, update_settings


@pytest.fixture()
def user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "me" / "settings.json"))
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    return tmp_path / "me" / "settings.json"


def _admin_cfg(tmp_path) -> Config:
    c = Config()
    c.auth.api_key = "sk-secret-123"
    for svc, model in (("llm", "gemma-4-31B-it"), ("embedding", "qwen3-emb"), ("reranker", "qwen3-rr")):
        getattr(c, svc).base_url = f"http://gpu01:800{len(svc)}/v1"
        getattr(c, svc).model = model
        getattr(c, svc).api_key = "sk-secret-123"
    c.llm.thinking_control = "reasoning_effort"
    c.paths.library_dir = "\\\\sunucu\\paylasim\\library"
    c.paths.personal_dir = str(tmp_path / "admin-personal")
    c.usage.log_dir = "\\\\sunucu\\paylasim\\gunluk"
    return c


def test_saved_settings_hold_no_key_and_no_personal_folder(tmp_path, user_settings):
    share = tmp_path / "share"
    share.mkdir()
    out = org.save(share, _admin_cfg(tmp_path))
    text = (share / org.ORG_FILE).read_text(encoding="utf-8")
    assert "sk-secret" not in text and "api_key" not in text and "admin-personal" not in text
    data = yaml.safe_load(text)
    assert data["llm"]["model"] == "gemma-4-31B-it" and data["llm"]["thinking_control"] == "reasoning_effort"
    assert data["read_only"] is True, "others open the shared library read-only by default"
    assert data["usage"]["log_dir"] == "\\\\sunucu\\paylasim\\gunluk"
    ptr = tmp_path / "exe" / org.POINTER_FILE
    assert out["pointer"] == str(ptr) and ptr.read_text(encoding="utf-8").strip() == str(share)
    assert out["warnings"] == [] or out["warnings"] == ["library_local"]


def test_another_user_gets_them_as_defaults_and_keeps_own_changes(tmp_path, user_settings):
    share = tmp_path / "share"
    share.mkdir()
    org.save(share, _admin_cfg(tmp_path))
    org._reset()
    cfg = load_config()                      # a new user: nothing of their own yet
    assert org.STATUS["source"] == "pointer" and org.STATUS["loaded"]
    assert cfg.llm.model == "gemma-4-31B-it" and cfg.embedding.base_url.startswith("http://gpu01")
    assert cfg.read_only is True and cfg.auth.api_key == "", "the key is never shared"
    assert org.public_status(cfg)["needs_key"] and not org.public_status(cfg)["ask"]
    # the user enters the key and saves the card as it is shown: only what differs from the organization is kept
    update_settings(cfg, {"auth": {"api_key": "sk-mine"}, "llm": {"base_url": cfg.llm.base_url, "model": "gemma-4-31B-it"},
                          "answer": {"judge_batch": 4}})
    stored = json.loads(user_settings.read_text(encoding="utf-8"))
    assert "llm" not in stored, "values equal to the organization's are not the user's own"
    assert stored["answer"] == {"judge_batch": 4} and stored["auth"]["api_key"]
    # the organization changes the model later: the user follows; their own change stays
    data = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    data["llm"]["model"] = "gemma-4-31B-it-v2"
    (share / org.ORG_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config()
    assert cfg.llm.model == "gemma-4-31B-it-v2" and cfg.answer.judge_batch == 4 and cfg.auth.api_key == "sk-mine"


def test_the_copy_read_last_is_used_when_the_folder_is_away(tmp_path, user_settings):
    share = tmp_path / "share"
    share.mkdir()
    org.save(share, _admin_cfg(tmp_path))
    load_config()
    (share / org.ORG_FILE).unlink()          # the network folder cannot be reached
    cfg = load_config()
    assert org.STATUS["cached"] and cfg.llm.model == "gemma-4-31B-it"
    st = org.public_status(cfg)
    assert st["loaded"] and st["cached"] and st["error"] == "not_found"


def test_an_edited_file_cannot_set_a_key_or_personal_paths(tmp_path, user_settings):
    share = tmp_path / "share"
    share.mkdir()
    (share / org.ORG_FILE).write_text(yaml.safe_dump({
        "auth": {"api_key": "stolen"}, "llm": {"model": "m", "api_key": "x"}, "server": {"api_token": "t"},
        "paths": {"library_dir": "\\\\s\\lib", "personal_dir": "C:\\x", "user_data_dir": "C:\\y"}}), encoding="utf-8")
    (tmp_path / "exe").mkdir()
    (tmp_path / "exe" / org.POINTER_FILE).write_text(f"# kurum\n{share}\n", encoding="utf-8")
    cfg = load_config()
    assert cfg.llm.model == "m" and cfg.auth.api_key == "" and cfg.llm.api_key == "" and cfg.server.api_token == ""
    assert cfg.paths.library_dir == "\\\\s\\lib" and cfg.paths.personal_dir != "C:\\x"


def test_the_environment_variable_and_the_first_start_answer(tmp_path, user_settings, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    org.save(share, _admin_cfg(tmp_path))
    (tmp_path / "exe" / org.POINTER_FILE).unlink()
    org._reset()
    cfg = load_config()
    assert not org.STATUS["path"] and org.public_status(cfg)["ask"], "a fresh install with nothing found is asked"
    update_settings(cfg, {"org": {"path": str(share), "asked": True}})
    assert load_config().llm.model == "gemma-4-31B-it" and org.STATUS["source"] == "user"
    monkeypatch.setenv("TECHRAG_ORG_SETTINGS", str(share / org.ORG_FILE))
    load_config()
    assert org.STATUS["source"] == "env"


# ---------------------------------------------------------------------------------------------- API

@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def test_admin_saves_and_a_first_start_uses_them(client, cfg, tmp_path):
    share = tmp_path / "paylasim"
    share.mkdir()
    assert client.post("/api/org/save", json={"path": str(tmp_path / "yok")}).status_code == 400
    r = client.post("/api/org/save", json={"path": str(share), "others_read_only": False}).json()
    assert r["file"] == str(share / org.ORG_FILE) and r["pointer"]
    assert yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))["read_only"] is False
    info = client.get("/api/info").json()["org"]
    assert info["loaded"] and info["source"] == "saved" and not info["ask"]
    assert load_user_settings()["org"]["path"] == str(share)
    # another computer, first start (no pointer file next to its exe): a wrong folder is refused, the right one used
    (tmp_path / "exe" / org.POINTER_FILE).unlink()
    org._reset()
    assert client.post("/api/org/use", json={"path": str(tmp_path)}).status_code == 400
    r = client.post("/api/org/use", json={"path": str(share)}).json()
    assert r["ok"] and r["org"]["loaded"] and r["org"]["source"] == "user"
    r = client.post("/api/org/use", json={"path": ""}).json()
    assert r["ok"] and load_user_settings()["org"]["asked"] is True
