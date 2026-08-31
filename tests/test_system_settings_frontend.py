from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.main import FRONTEND_ROUTES, create_app

ROOT = Path(__file__).parents[1]


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["DATABASE_URL"],
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    return TestClient(create_app(settings))


def _install_react_build(monkeypatch, tmp_path) -> None:
    build_dir = tmp_path / "react_static"
    build_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Settings,
        "react_static_dir",
        property(lambda _: build_dir),
    )


def test_settings_is_a_first_class_route_and_credentials_alias_still_resolves(
    monkeypatch,
    tmp_path,
) -> None:
    assert "/settings" in FRONTEND_ROUTES
    assert "/credentials" not in FRONTEND_ROUTES
    _install_react_build(monkeypatch, tmp_path)

    client = _client()
    with client:
        settings_page = client.get("/settings")
        credentials_redirect = client.get("/credentials", follow_redirects=False)
        credentials_page = client.get("/credentials")

    assert settings_page.status_code == 200
    assert credentials_redirect.status_code == 308
    assert credentials_redirect.headers["location"] == "/settings"
    assert [response.status_code for response in credentials_page.history] == [308]
    assert credentials_page.url.path == "/settings"
    assert credentials_page.content == settings_page.content


def test_react_system_settings_owns_binance_credentials_and_ai_models() -> None:
    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    page = (ROOT / "web" / "src" / "pages" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )

    assert '{ key: "settings"' in app
    assert 'page === "settings"' in app
    assert "SettingsPage user={user}" in app
    assert "API CREDENTIALS" in page
    assert 'name="api_key"' in page
    assert 'name="api_secret"' in page
    assert "deleteCredentials" in page
    assert "AI MODEL PROVIDERS" in page
