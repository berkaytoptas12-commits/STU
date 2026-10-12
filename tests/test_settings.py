"""settings.json: unreadable files, keys that cannot be decrypted, environment variables."""

import json

from techrag import settings as S


def test_settings_with_bom_and_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("TECHRAG_SETTINGS", str(path))
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"llm": {"model": "m1"}}).encode())
    assert S.load_user_settings()["llm"]["model"] == "m1", "a BOM (Notepad, PowerShell 5) is fine"
    path.write_text('{"llm": {"model": "m1", ', encoding="utf-8")
    assert S.load_user_settings() == {}
    assert (tmp_path / "settings.json.unreadable").read_text(encoding="utf-8").startswith('{"llm"'), \
        "a copy is kept before the next save replaces it"


def test_key_that_cannot_be_decrypted_is_kept(tmp_path, monkeypatch, cfg):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("TECHRAG_SETTINGS", str(path))
    locked = {"dpapi": "AAAA"}  # written by another Windows account: not decryptable here
    path.write_text(json.dumps({"llm": {"api_key": locked, "model": "m"}}), encoding="utf-8")
    assert S.load_user_settings()["llm"]["api_key"] == ""
    S.update_settings(cfg, {"ui": {"language": "en"}})
    assert json.loads(path.read_text(encoding="utf-8"))["llm"]["api_key"] == locked, "not overwritten with ''"
    S.update_settings(cfg, {"llm": {"api_key": "new-key"}})
    assert json.loads(path.read_text(encoding="utf-8"))["llm"]["api_key"] != locked, "a new key replaces it"


def test_unknown_environment_variables_do_not_break_start_up(monkeypatch, tmp_path):
    from techrag.config import load_config

    monkeypatch.setenv("TECHRAG_NO_USER_SETTINGS", "1")
    monkeypatch.setenv("TECHRAG_LIBRARY_DIR", str(tmp_path))       # a property, not a setting
    monkeypatch.setenv("TECHRAG_PATHS__LIBRARY_DIR", str(tmp_path / "lib"))
    cfg = load_config()
    assert cfg.paths.library_dir == str(tmp_path / "lib")
