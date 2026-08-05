from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.main import FRONTEND_ROUTES, create_app

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    ),
}


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


def _assert_security_headers(response) -> None:
    for name, expected in SECURITY_HEADERS.items():
        assert response.headers[name] == expected


@pytest.mark.parametrize("path", FRONTEND_ROUTES)
def test_explicit_frontend_route_serves_the_same_index(path: str) -> None:
    client = _client()
    with client:
        index = client.get("/")
        page = client.get(path)

    assert index.status_code == 200
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.content == index.content
    _assert_security_headers(page)


def test_legacy_credentials_route_redirects_to_settings_without_shadowing_api() -> None:
    client = _client()
    with client:
        legacy_page = client.get("/credentials", follow_redirects=False)
        settings_page = client.get("/settings")
        credentials_api = client.put(
            "/api/v2/me/binance-credentials",
            json={"api_key": "A" * 64, "api_secret": "B" * 64},
            follow_redirects=False,
        )

    assert legacy_page.status_code == 308
    assert legacy_page.headers["location"] == "/settings"
    _assert_security_headers(legacy_page)
    assert settings_page.status_code == 200
    assert settings_page.headers["content-type"].startswith("text/html")
    assert credentials_api.status_code == 401
    assert "location" not in credentials_api.headers


def test_assets_and_api_routes_are_not_shadowed_by_frontend_routes() -> None:
    client = _client()
    with client:
        asset = client.get("/assets/app.js")
        docs = client.get("/api/docs")
        api_missing = client.get("/api/v2/route-that-does-not-exist")

    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert 'const TAB_USER_ID_KEY = "quantdesk.tab-user-id"' in asset.text
    assert 'headers.set("X-QuantDesk-User-ID", authenticatedUserId)' in asset.text
    assert "String(actualUser.id) !== authenticatedUserId" in asset.text
    assert "if (refreshAccessPromise) return refreshAccessPromise" in asset.text
    assert "rejectChangedIdentity(actualUser)" in asset.text
    live_asset = client.get("/assets/live.js")
    assert live_asset.status_code == 200
    assert "REAL FUNDS" in live_asset.text
    assert "与模拟盘完全相同" in live_asset.text
    assert "live-symbols" not in live_asset.text
    assert "BTCUSDT" not in live_asset.text
    assert 'id="live-delete"' in live_asset.text
    assert 'id="live-rename"' in live_asset.text
    assert 'account.last_error_code === "risk_review_required"' in live_asset.text
    assert "历史/人工仓位沿用原保护，新开仓暂停，需人工复核" in live_asset.text
    paper_asset = client.get("/assets/paper.js")
    assert paper_asset.status_code == 200
    assert 'id="paper-delete"' in paper_asset.text
    assert 'id="paper-rename"' in paper_asset.text
    assert "syncCountKnown && Number(syncedTradfiSymbols) === 0" in paper_asset.text
    assert "Number(data.account?.synced_tradfi_symbols || 0) === 0" not in paper_asset.text
    assert 'href="/live" data-panel-target="live"' in client.get("/").text
    assert docs.status_code == 200
    assert api_missing.status_code == 404
    assert api_missing.json() == {"detail": "Not Found"}
    _assert_security_headers(asset)
    _assert_security_headers(api_missing)


@pytest.mark.parametrize(
    "path",
    (
        "/unknown",
        "/monitor/details",
        "/login/reset-password",
        "/api",
        "/assets/does-not-exist.js",
    ),
)
def test_unknown_paths_remain_404(path: str) -> None:
    client = _client()
    with client:
        response = client.get(path)

    assert response.status_code == 404
    _assert_security_headers(response)


def test_frontend_routes_are_not_added_to_openapi() -> None:
    client = _client()
    with client:
        paths = client.get("/openapi.json").json()["paths"]

    assert all(path not in paths for path in ("/", *FRONTEND_ROUTES, "/credentials"))
