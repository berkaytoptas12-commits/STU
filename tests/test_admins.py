"""Admin rights (0.15.7): the organization settings name the users who may use Admin ("admins:" in kurum-ayarlari.yaml).
Names match without the domain part and letter case; no list means everyone (as before); only the organization file
sets the list; the server refuses Admin's operations to the others while the Settings window, asking and the user's
own things stay open; an admin cannot save a list that locks themselves out."""

import pytest
import yaml
from fastapi.testclient import TestClient

from techrag import admins, org
from techrag.config import Config, load_config
from techrag.server import create_app
from techrag.settings import settings_path


def _as(monkeypatch, name: str) -> None:
    monkeypatch.setattr(admins, "current", lambda: name)


def _share_with(tmp_path, names):
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    c = Config()
    c.llm.base_url, c.llm.model = "http://gpu01:8000/v1", "chat-model"
    org.save(share, c, admins=names)
    return share


# ---------------------------------------------------------------------------------------------- names

def test_names_match_without_domain_and_letter_case():
    assert admins.normalize("ŞİRKET\\Ahmet.YILMAZ") == "ahmet.yilmaz"
    assert admins.normalize("  ayse.demir@sirket.local ") == "ayse.demir"
    assert admins.normalize("İSMAİL") == admins.normalize("ismail")
    assert admins.parse_list("a\nb, A ;c\r\n\n") == ["a", "b", "c"], "one per line or separated, no duplicates"
    assert admins.parse_list(["Ali", "ali", "", None, "ŞİRKET\\ali"]) == ["Ali"]
    assert admins.parse_list(None) == [] and admins.parse_list("") == []


def test_no_list_means_everyone(monkeypatch):
    _as(monkeypatch, "anyone")
    assert admins.admin_list() == [] and admins.is_admin()


def test_listed_and_unlisted_users(monkeypatch):
    names = ["ŞİRKET\\Ahmet", "ayse@sirket.local"]
    for who, ok in (("ahmet", True), ("AHMET", True), ("SIRKET2\\ahmet", True), ("ayse", True), ("veli", False),
                    ("", False)):
        _as(monkeypatch, who)
        assert admins.is_admin(names=names) is ok, who


def test_the_current_account_is_the_operating_systems():
    name = admins.account_name()
    assert name and name == admins.account_name(), "asked once, the same every time"


# ---------------------------------------------------------------------------------------------- where the list comes from

@pytest.fixture()
def user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "me" / "settings.json"))
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    return tmp_path / "me" / "settings.json"


def test_only_the_organization_file_sets_the_list(tmp_path, user_settings, monkeypatch):
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text('{"admins": ["me"]}', encoding="utf-8")
    load_config()
    assert admins.admin_list() == [], "a user's own settings cannot make them an admin"
    share = _share_with(tmp_path, ["boss", "Ali"])
    org._reset()
    monkeypatch.setenv("TECHRAG_ORG_SETTINGS", str(share))
    load_config()                                              # another user's app reads the organization file
    assert admins.admin_list() == ["boss", "Ali"]
    _as(monkeypatch, "me")
    assert not admins.is_admin()
    _as(monkeypatch, "ali")
    assert admins.is_admin()


def test_the_copy_read_last_keeps_the_list_when_the_folder_is_away(tmp_path, user_settings, monkeypatch):
    share = _share_with(tmp_path, ["boss"])
    org._reset()
    monkeypatch.setenv("TECHRAG_ORG_SETTINGS", str(share))
    load_config()
    assert (settings_path().parent / org.CACHE_FILE).exists()
    (share / org.ORG_FILE).unlink()                            # the network folder cannot be reached
    org._reset()
    load_config()
    assert org.STATUS["cached"] and admins.admin_list() == ["boss"], "the lock does not open when the folder is away"


def test_an_edited_file_with_an_empty_list_means_everyone(tmp_path, user_settings, monkeypatch):
    share = _share_with(tmp_path, [])
    data = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    assert "admins" not in data
    (share / org.ORG_FILE).write_text(yaml.safe_dump(dict(data, admins="")), encoding="utf-8")
    org._reset()
    monkeypatch.setenv("TECHRAG_ORG_SETTINGS", str(share))
    load_config()
    _as(monkeypatch, "anyone")
    assert admins.admin_list() == [] and admins.is_admin()


# ---------------------------------------------------------------------------------------------- the server

@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


ADMIN_ONLY = [
    ("get", "/api/usage", None), ("post", "/api/usage/open", {}), ("get", "/api/folders", None),
    ("post", "/api/rescan", {}), ("get", "/api/quality", None), ("post", "/api/documents/purge-missing", {}),
    ("get", "/api/library/removed", None), ("get", "/api/feedback", None), ("get", "/api/eval", None),
    ("post", "/api/library/publish", {}), ("post", "/api/ingest", {}),
    ("post", "/api/org/save", {"path": "x"}),
]


def test_the_server_refuses_admins_operations_to_the_others(client, tmp_path, monkeypatch):
    _as(monkeypatch, "boss")
    _share_with(tmp_path, ["boss"])
    _as(monkeypatch, "veli")
    info = client.get("/api/info").json()
    assert info["user"] == "veli" and info["admin"] is False and info["admins_set"] is True
    assert info["org"]["admins"] == ["boss"]
    for method, path, body in ADMIN_ONLY:
        r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["detail"] == "admin_required", path
    # the Settings window, the language and the user's own things stay open
    for patch in ({"ui": {"theme": "light"}}, {"ui": {"language": "en"}}, {"llm": {"model": "gemma"}},
                  {"llm_fast": {"model": "gemma-fast", "base_url": "http://gpu02:8000/v1"}},
                  {"auth": {"api_key": "made-up-key-1"}}):
        assert client.put("/api/settings", json=patch).status_code == 200, patch
    for patch in ({"answer": {"judge_batch": 3}}, {"llm": {"temperature": 0.5}}, {"paths": {"library_dir": "x"}},
                  {"usage": {"log_dir": "x"}}, {"tls": {"use_system_store": False}}, {"embedding": {"model": "e"}},
                  {"ui": {"theme": "dark"}, "answer": {"judge_batch": 3}}):
        r = client.put("/api/settings", json=patch)
        assert r.status_code == 403 and r.json()["detail"] == "admin_required", patch
    assert client.get("/api/settings").status_code == 200 and client.get("/api/documents").status_code == 200
    assert client.get("/api/chats").status_code == 200 and client.get("/api/personal/status").status_code == 200


def test_an_admin_and_everyone_without_a_list_keep_admin(client, tmp_path, monkeypatch):
    _as(monkeypatch, "anyone")
    info = client.get("/api/info").json()
    assert info["admin"] is True and info["admins_set"] is False
    assert client.get("/api/usage").status_code == 200
    _share_with(tmp_path, ["ŞİRKET\\Boss"])
    _as(monkeypatch, "BOSS")
    assert client.get("/api/info").json()["admin"] is True
    assert client.get("/api/usage").status_code == 200
    assert client.put("/api/settings", json={"answer": {"judge_batch": 3}}).status_code == 200


def test_an_admin_cannot_save_a_list_without_themselves(client, tmp_path, monkeypatch):
    share = tmp_path / "paylasim"
    share.mkdir()
    _as(monkeypatch, "boss")
    r = client.post("/api/org/save", json={"path": str(share), "admins": ["ali", "veli"]})
    assert r.status_code == 400 and r.json()["detail"] == "self_not_listed:boss"
    assert not (share / org.ORG_FILE).exists(), "nothing written"
    r = client.post("/api/org/save", json={"path": str(share), "admins": ["Boss@sirket.local", "ali", "ALI"]})
    assert r.status_code == 200
    assert yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))["admins"] == ["Boss@sirket.local", "ali"]
    assert client.get("/api/info").json()["org"]["admins"] == ["Boss@sirket.local", "ali"], "applies at once"
    _as(monkeypatch, "veli")
    assert client.get("/api/info").json()["admin"] is False
    _as(monkeypatch, "Ali")
    assert client.get("/api/info").json()["admin"] is True
    r = client.post("/api/org/save", json={"path": str(share), "admins": []})    # back to everyone
    assert r.status_code == 200 and "admins" not in yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    _as(monkeypatch, "veli")
    assert client.get("/api/info").json()["admin"] is True
