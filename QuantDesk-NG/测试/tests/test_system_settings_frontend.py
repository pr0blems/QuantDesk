from __future__ import annotations

import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.main import FRONTEND_ROUTES, create_app


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


def test_settings_is_a_first_class_route_and_legacy_credentials_still_resolves() -> None:
    assert "/settings" in FRONTEND_ROUTES
    assert "/credentials" not in FRONTEND_ROUTES

    client = _client()
    with client:
        settings_page = client.get("/settings")
        legacy_redirect = client.get("/credentials", follow_redirects=False)
        legacy_page = client.get("/credentials")

    assert settings_page.status_code == 200
    assert legacy_redirect.status_code == 308
    assert legacy_redirect.headers["location"] == "/settings"
    assert [response.status_code for response in legacy_page.history] == [308]
    assert legacy_page.url.path == "/settings"
    assert legacy_page.content == settings_page.content


def test_system_settings_navigation_owns_the_binance_credential_form() -> None:
    client = _client()
    with client:
        page = client.get("/")
        script = client.get("/assets/app.js")

    assert page.status_code == 200
    assert script.status_code == 200

    html = page.text
    security_heading = html.index("<p>安全与管理</p>")
    settings_navigation = html.index('href="/settings" data-panel-target="settings"')
    risk_navigation = html.index('href="/risk" data-panel-target="risk"')
    assert security_heading < settings_navigation < risk_navigation
    assert ">系统设置</a>" in html[settings_navigation:risk_navigation]
    assert 'href="/credentials"' not in html
    assert 'data-panel-target="credentials"' not in html

    settings_panel = html.index('data-panel="settings"')
    credential_form = html.index('id="credential-form"')
    credential_message = html.index('id="dashboard-message"')
    assert settings_panel < credential_form < credential_message
    assert 'name="api_key"' in html[credential_form:credential_message]
    assert 'name="api_secret"' in html[credential_form:credential_message]
    assert 'id="delete-credentials"' in html[credential_form:credential_message]
    assert "Binance API 凭据" in html[settings_panel:credential_message]
    assert 'data-open-panel="credentials"' not in html
    assert 'data-open-panel="settings"' in html

    assert 'settings: "/settings"' in script.text
    assert 'credentials: "/credentials"' not in script.text
