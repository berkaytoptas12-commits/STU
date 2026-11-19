"""The shared API key through the organization settings: written into the organization file when the admin ticks it
(plain text there), used by every user without entering it; a user's own key stays on their computer and can go back
to the organization's; a later organization key replaces the users' own; no user file and no local copy holds the
organization's key in plain text. Only made-up keys are used here."""

import json
import os

import pytest
import yaml

from techrag import org
from techrag.config import Config, load_config
from techrag.settings import load_user_settings, public_settings, settings_path, update_settings

ORG_KEY = "org-key-AAAA-1111"
ORG_KEY_2 = "org-key-BBBB-2222"
OWN_KEY = "own-key-CCCC-3333"


@pytest.fixture()
def user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "me" / "settings.json"))
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    return tmp_path / "me" / "settings.json"


def _admin(key: str = ORG_KEY) -> Config:
    c = Config()
    c.llm.base_url, c.llm.model = "http://gpu01:8000/v1", "chat-model"
    c.auth.api_key = key
    c.share_api_key(adopt_legacy=False)
    return c


def _share(tmp_path, key: str = ORG_KEY, include_key: bool = True):
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    out = org.save(share, _admin(key), include_key=include_key)
    org._reset()
    return share, out


def _user_file_text(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_the_admin_writes_the_key_and_every_user_gets_it(tmp_path, user_settings):
    share, out = _share(tmp_path)
    text = (share / org.ORG_FILE).read_text(encoding="utf-8")
    assert out["key_included"] and yaml.safe_load(text)["auth"] == {"api_key": ORG_KEY}
    assert "düz metin" in text, "the file says the key is in plain text"
    cfg = load_config()                                   # another user: nothing of their own
    assert cfg.auth.api_key == cfg.llm.api_key == cfg.embedding.api_key == ORG_KEY
    st = org.public_status(cfg)
    assert not st["needs_key"] and st["org_key"]
    pub = public_settings(cfg)
    assert pub["auth"]["api_key"] != ORG_KEY and pub["auth"]["source"] == "org" and pub["auth"]["org_key"]


def test_without_the_tick_no_key_is_written(tmp_path, user_settings):
    share, out = _share(tmp_path, include_key=False)
    text = (share / org.ORG_FILE).read_text(encoding="utf-8")
    assert not out["key_included"] and ORG_KEY not in text and "auth" not in yaml.safe_load(text)
    cfg = load_config()
    assert cfg.auth.api_key == "" and org.public_status(cfg)["needs_key"]


def test_a_users_own_key_stays_on_their_computer_and_can_go_back(tmp_path, user_settings):
    share, _ = _share(tmp_path)
    cfg = update_settings(load_config(), {"auth": {"api_key": OWN_KEY}})
    assert cfg.auth.api_key == OWN_KEY and public_settings(cfg)["auth"]["source"] == "own"
    assert ORG_KEY not in _user_file_text(user_settings), "the organization's key is never in the user's file"
    assert yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))["auth"]["api_key"] == ORG_KEY, \
        "the organization file is not changed"
    cfg = load_config()                                   # the next start: the own key still applies
    assert cfg.auth.api_key == OWN_KEY
    cfg = update_settings(cfg, {"auth": {"use_org_key": True}})   # "Kurum anahtarına dön"
    assert cfg.auth.api_key == ORG_KEY and public_settings(cfg)["auth"]["source"] == "org"
    assert "auth" not in load_user_settings(), "no own key left"
    assert load_config().auth.api_key == ORG_KEY


def test_an_emptied_key_falls_back_to_the_organizations(tmp_path, user_settings):
    _share(tmp_path)
    cfg = update_settings(load_config(), {"auth": {"api_key": OWN_KEY}})
    cfg = update_settings(cfg, {"auth": {"api_key": ""}})
    assert cfg.auth.api_key == ORG_KEY and "auth" not in load_user_settings()


def test_a_later_organization_key_replaces_the_users_own(tmp_path, user_settings):
    share, _ = _share(tmp_path)
    update_settings(load_config(), {"auth": {"api_key": OWN_KEY}})
    _share(tmp_path, key=ORG_KEY_2)                       # the admin changes the key afterwards
    cfg = load_config()
    assert cfg.auth.api_key == ORG_KEY_2, "the newest decision wins"
    assert "auth.api_key" in org.STATUS["replaced"]
    text = _user_file_text(user_settings)
    assert ORG_KEY not in text and ORG_KEY_2 not in text and OWN_KEY not in text, \
        "neither key is kept in plain text (the replaced own key is not kept at all)"
    replaced = json.loads(text)["_org_replaced"]["auth.api_key"]
    assert replaced["value"] is None and replaced["organization"].startswith("sha256:")


def test_a_key_set_while_the_organization_had_it_stays_the_users(tmp_path, user_settings):
    _share(tmp_path)
    update_settings(load_config(), {"auth": {"api_key": OWN_KEY}})
    base = json.loads(_user_file_text(user_settings))["_org_base"]["auth.api_key"]
    assert base.startswith("sha256:") and ORG_KEY not in base, "a fingerprint, not the key"
    assert load_config().auth.api_key == OWN_KEY, "the organization did not change: the user's choice is newer"


def test_the_local_copy_keeps_the_key_encrypted_and_serves_it_when_the_folder_is_away(tmp_path, user_settings):
    share, _ = _share(tmp_path)
    load_config()
    cache = settings_path().parent / org.CACHE_FILE
    text = cache.read_text(encoding="utf-8")
    if os.name == "nt":
        assert ORG_KEY not in text and "dpapi" in text, "encrypted for this Windows user"
    else:
        assert (cache.stat().st_mode & 0o777) == 0o600, "readable by its owner only"
    (share / org.ORG_FILE).unlink()                       # the network folder cannot be reached
    cfg = load_config()
    assert org.STATUS["cached"] and cfg.auth.api_key == ORG_KEY
