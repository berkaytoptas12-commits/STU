"""Model servers may stay silent for 15 minutes before a request fails (queue, reading the prompt, a pause in a
streamed answer); connecting stays short. Settings saved by an earlier version with the old 5-minute default take
the new one; any other value someone chose stays."""

import json

import pytest
import yaml

from techrag import org
from techrag.api import make_http_client
from techrag.config import LEGACY_TIMEOUT, REQUEST_TIMEOUT, Config, load_config
from techrag.settings import load_user_settings


@pytest.fixture()
def user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "me" / "settings.json"))
    monkeypatch.delenv("TECHRAG_NO_USER_SETTINGS", raising=False)
    monkeypatch.delenv("TECHRAG_ORG_SETTINGS", raising=False)
    org._reset()
    return tmp_path / "me" / "settings.json"


def test_every_model_server_waits_15_minutes_and_connecting_stays_short():
    assert REQUEST_TIMEOUT == 15 * 60
    c = Config()
    c.llm_fast.model = "fast-model"
    for svc in (c.llm, c.chat_config("fast"), c.vision_service(), c.embedding, c.reranker):
        assert svc.timeout == REQUEST_TIMEOUT
        t = make_http_client(svc).timeout
        assert (t.read, t.write, t.pool) == (REQUEST_TIMEOUT,) * 3
        assert t.connect == 10.0, "an unreachable server is still reported at once"


def test_an_organization_file_of_an_earlier_version_gets_the_new_wait(tmp_path, user_settings, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    old = Config()
    for svc in (old.llm, old.vision, old.embedding, old.reranker):
        svc.timeout = LEGACY_TIMEOUT          # what every export before 0.15.4 wrote
    old.reranker.timeout = 600.0              # a value an admin chose
    org.save(share, old)
    saved = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    assert saved["llm"]["timeout"] == LEGACY_TIMEOUT, "the file really holds the old default"
    monkeypatch.setenv("TECHRAG_ORG_SETTINGS", str(share / org.ORG_FILE))
    cfg = load_config()
    assert org.STATUS["loaded"]
    assert (cfg.llm.timeout, cfg.vision.timeout, cfg.embedding.timeout) == (REQUEST_TIMEOUT,) * 3
    assert cfg.reranker.timeout == 600.0, "a chosen value stays"


def test_own_settings_of_an_earlier_version_get_the_new_wait(user_settings):
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(json.dumps({"llm": {"timeout": LEGACY_TIMEOUT, "model": "m"},
                                         "embedding": {"timeout": 120.0}}), encoding="utf-8")
    stored = load_user_settings()
    assert stored["llm"] == {"model": "m"} and stored["embedding"] == {"timeout": 120.0}
    cfg = load_config()
    assert cfg.llm.timeout == REQUEST_TIMEOUT and cfg.embedding.timeout == 120.0


def test_new_organization_files_carry_the_new_wait(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    org.save(share, Config())
    saved = yaml.safe_load((share / org.ORG_FILE).read_text(encoding="utf-8"))
    assert all(saved[s]["timeout"] == REQUEST_TIMEOUT for s in ("llm", "vision", "embedding", "reranker"))
