"""Theme: the app opens dark unless the user chose light or yellow-black in Ayarlar. The page is served already in the user's theme
(no flash while the script loads); the choice is the user's own, never an organization setting."""
import pytest
from fastapi.testclient import TestClient

from techrag import org
from techrag.config import Config
from techrag.server import create_app


@pytest.fixture()
def client(cfg, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    with TestClient(create_app(cfg, engine, warmup=False, desktop=True)) as c:
        yield c


def test_dark_by_default():
    assert Config().ui.theme == "dark"


def test_the_page_is_served_in_the_users_theme(client):
    assert 'data-theme="dark"' in client.get("/").text
    assert client.get("/api/info").json()["theme"] == "dark"
    engine_before = client.app.state.state["engine"]
    r = client.put("/api/settings", json={"ui": {"theme": "light"}})
    assert r.status_code == 200 and r.json()["settings"]["ui"]["theme"] == "light"
    assert client.app.state.state["engine"] is engine_before, "a theme change does not reload the library"
    assert 'data-theme="light"' in client.get("/").text and 'data-theme="dark"' not in client.get("/").text
    assert client.get("/api/info").json()["theme"] == "light"


def test_yellow_black_is_served_like_the_others(client):
    r = client.put("/api/settings", json={"ui": {"theme": "yellow"}})
    assert r.status_code == 200 and r.json()["settings"]["ui"]["theme"] == "yellow"
    page = client.get("/").text
    assert 'data-theme="yellow"' in page and 'data-theme="dark"' not in page
    assert client.get("/api/info").json()["theme"] == "yellow"


def test_only_the_three_themes(client):
    r = client.put("/api/settings", json={"ui": {"theme": "blue"}})
    assert r.status_code == 400 and "dark, light, yellow" in r.text
    assert 'data-theme="dark"' in client.get("/").text


def test_an_unknown_theme_in_the_file_opens_dark(cfg):
    from techrag.server import ui_theme

    cfg.ui.theme = "blue"
    assert ui_theme(cfg) == "dark"
    for theme in ("dark", "light", "yellow"):
        cfg.ui.theme = theme
        assert ui_theme(cfg) == theme


def test_the_theme_is_never_an_organization_setting(cfg):
    cfg.ui.theme = "light"
    assert "ui" not in org.export(cfg)
