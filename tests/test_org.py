"""Organization settings ("kurum ayarları"): saved by an admin into a shared folder with a pointer file next to the
exe; read by everyone's app at start as defaults (the user's own settings win; nothing is locked); never the API key
or personal folders; the copy read last time when the folder cannot be reached; asked once at first start."""

import json
import os

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


def _edit_org(share, **changes):
    """The administrator saves the organization settings again with some values changed."""
    data = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    for name, value in changes.items():
        section, key = name.split("__")
        data[section][key] = value
    (share / org.ORG_FILE).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_the_newest_decision_wins(tmp_path, user_settings):
    """A user's own library folder stays theirs until the administrator changes that setting in the organization
    file; then the administrator's new folder applies (and the user can choose again)."""
    share, lib = tmp_path / "share", tmp_path / "ortak"
    share.mkdir()
    lib.mkdir()
    admin = _admin_cfg(tmp_path)
    admin.paths.library_dir = str(lib)
    org.save(share, admin)
    cfg = load_config()
    assert cfg.paths.library_dir == str(lib)
    # the same folder written differently is not a choice of the user's own
    update_settings(cfg, {"paths": {"library_dir": str(lib) + "/"}})
    assert "paths" not in json.loads(user_settings.read_text(encoding="utf-8"))
    cfg = update_settings(cfg, {"paths": {"library_dir": str(tmp_path / "benim")}})
    stored = json.loads(user_settings.read_text(encoding="utf-8"))
    assert stored["_org_base"]["paths.library_dir"] == str(lib)
    assert load_config().paths.library_dir == str(tmp_path / "benim"), "chosen after the organization's value"
    _edit_org(share, llm__model="gemma-yeni")           # another setting changes: the user's folder stays
    cfg = load_config()
    assert cfg.paths.library_dir == str(tmp_path / "benim") and cfg.llm.model == "gemma-yeni"
    assert org.public_status(cfg)["replaced"] == []
    _edit_org(share, paths__library_dir=str(tmp_path / "yeni"))   # the administrator moves the library
    cfg = load_config()
    assert cfg.paths.library_dir == str(tmp_path / "yeni"), "the administrator decided later"
    assert org.public_status(cfg)["replaced"] == ["paths.library_dir"]
    stored = json.loads(user_settings.read_text(encoding="utf-8"))
    assert "paths" not in stored and stored["_org_replaced"]["paths.library_dir"]["value"] == str(tmp_path / "benim")
    cfg = load_config()
    assert cfg.paths.library_dir == str(tmp_path / "yeni") and org.public_status(cfg)["replaced"] == []
    update_settings(cfg, {"paths": {"library_dir": str(tmp_path / "yine-benim")}})   # the user chooses again
    assert load_config().paths.library_dir == str(tmp_path / "yine-benim")
    assert "paths.library_dir" not in json.loads(user_settings.read_text(encoding="utf-8")).get("_org_replaced", {})


def test_settings_saved_by_an_older_version_give_way_to_the_organization(tmp_path, user_settings):
    """An install set up before the organization settings (or before this version): its own values were saved with
    nothing to tell when; the organization's values apply - the API key, personal folders and how this user opens
    the library stay theirs."""
    share = tmp_path / "share"
    share.mkdir()
    admin = _admin_cfg(tmp_path)
    admin.paths.library_dir = str(tmp_path / "ortak")
    org.save(share, admin)
    old = {"auth": {"api_key": "sk-mine"}, "llm": {"model": "eski-model", "base_url": "http://eski:8000/v1"},
           "paths": {"library_dir": "\\\\eski\\library", "personal_dir": str(tmp_path / "kisisel")},
           "answer": {"judge_batch": 3}, "read_only": False, "ui": {"language": "tr"}}
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text(json.dumps(old), encoding="utf-8")
    day_before = (share / org.ORG_FILE).stat().st_mtime - 86400
    os.utime(user_settings, (day_before, day_before))   # set up before the administrator's last save
    org._reset()
    cfg = load_config()
    assert cfg.paths.library_dir == str(tmp_path / "ortak") and cfg.llm.model == "gemma-4-31B-it"
    assert cfg.llm.base_url == "http://gpu01:8003/v1" and cfg.answer.judge_batch == Config().answer.judge_batch
    assert cfg.auth.api_key == "sk-mine" and cfg.paths.personal_dir == str(tmp_path / "kisisel")
    assert cfg.read_only is False, "how this user opens the library stays theirs"
    assert sorted(org.public_status(cfg)["replaced"]) == ["answer.judge_batch", "llm.base_url", "llm.model",
                                                         "paths.library_dir"]
    stored = load_user_settings()
    assert stored["auth"]["api_key"] == "sk-mine" and stored["paths"] == {"personal_dir": str(tmp_path / "kisisel")}
    assert stored["read_only"] is False and stored["_org_base"] == {"read_only": True}
    assert stored["_org_replaced"]["llm.model"]["value"] == "eski-model"
    again = load_config()                 # nothing more to do the next time
    assert org.public_status(again)["replaced"] == [] and again.paths.library_dir == cfg.paths.library_dir
    # the organization's folder cannot be read and no copy was read before: the user's settings are left alone
    org._reset()
    (tmp_path / "exe" / org.POINTER_FILE).write_text(str(tmp_path / "yok"), encoding="utf-8")
    assert load_user_settings() == stored and load_config().read_only is False


def test_own_settings_of_an_older_version_changed_after_the_last_save_stay(tmp_path, user_settings):
    """The administrator's own computer: saving the organization settings writes the user's settings file right
    after; what the administrator changed since then (with an older version) stays theirs on updating."""
    share = tmp_path / "share"
    share.mkdir()
    admin = _admin_cfg(tmp_path)
    admin.paths.library_dir = str(tmp_path / "ortak")
    org.save(share, admin)
    saved = (share / org.ORG_FILE).stat().st_mtime
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text(json.dumps({"llm": {"thinking_control": "chat_template"}, "read_only": False}),
                             encoding="utf-8")
    os.utime(user_settings, (saved + 3600, saved + 3600))      # changed an hour after the organization's save
    org._reset()
    cfg = load_config()
    assert cfg.llm.thinking_control == "chat_template" and org.public_status(cfg)["replaced"] == []
    stored = load_user_settings()
    assert stored["_org_base"] == {"llm.thinking_control": "reasoning_effort", "read_only": True}
    # from now on the usual rule: the administrator changes that setting later -> theirs applies
    _edit_org(share, llm__thinking_control="none")
    cfg = load_config()
    assert cfg.llm.thinking_control == "none" and org.public_status(cfg)["replaced"] == ["llm.thinking_control"]


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


def test_the_admin_keeps_opening_the_library_their_own_way(client, cfg, tmp_path, monkeypatch):
    """"Others open it read-only" is for the others: after a restart the admin's own library is not read-only."""
    share = tmp_path / "paylasim"
    share.mkdir()
    r = client.post("/api/org/save", json={"path": str(share)}).json()
    assert yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))["read_only"] is True
    assert r["file"] and load_user_settings()["read_only"] is False
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    org._reset()
    restarted = load_config()
    assert restarted.read_only is False and org.STATUS["loaded"]
    # the admin moves the library and saves the organization settings again: after a restart the admin opens the
    # new folder, still not read-only, and nothing of theirs was replaced
    new = tmp_path / "yeni-kutuphane"
    assert not client.post("/api/library/open", json={"path": str(new), "read_only": False}).json()["error"]
    client.post("/api/org/save", json={"path": str(share)})
    assert yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))["paths"]["library_dir"] == str(new)
    org._reset()
    restarted = load_config()
    assert restarted.paths.library_dir == str(new) and restarted.read_only is False
    assert org.STATUS["replaced"] == []
    assert client.get("/api/info").json()["org"]["replaced"] == []
