from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import binance_client
from quantdesk_v2.config import Settings
from quantdesk_v2.database import build_engine, get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import Base

FIXED_TIME_MS = 1_735_689_600_123
API_KEY = "K" * 64
API_SECRET = "S" * 64
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def build_test_client() -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = create_app(settings)

    def override_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def register_and_login(client: TestClient, *, configure: bool) -> dict[str, str]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": "binance-user", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v2/auth/login",
        json={
            "username": "binance-user",
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    if configure:
        saved = client.put(
            "/api/v2/me/binance-credentials",
            headers=headers,
            json={
                "api_key": API_KEY,
                "api_secret": API_SECRET,
                # This is user intent, not proof that Binance granted TRADE permission.
                "permissions": ["READ", "TRADE"],
            },
        )
        assert saved.status_code == 200
    return headers


def response(status: int, payload: object) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def assert_signed_request(url: str, headers: dict[str, str]) -> None:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, strict_parsing=True)
    assert query["recvWindow"] == ["5000"]
    assert query["timestamp"] == [str(FIXED_TIME_MS)]
    signed_payload = f"recvWindow=5000&timestamp={FIXED_TIME_MS}"
    expected_signature = hmac.new(
        API_SECRET.encode(), signed_payload.encode(), hashlib.sha256
    ).hexdigest()
    assert query["signature"] == [expected_signature]
    assert headers["X-MBX-APIKEY"] == API_KEY
    assert API_KEY not in url
    assert API_SECRET not in url


def test_portfolio_margin_summary_is_signed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        assert timeout == 4.0
        assert_signed_request(url, headers)
        calls.append(urlsplit(url).path)
        if calls[-1] == "/papi/v1/account":
            return response(
                200,
                {
                    "accountEquity": "100.25",
                    "actualEquity": "99.75",
                    "totalAvailableBalance": "80.50",
                    "accountStatus": "NORMAL",
                },
            )
        if calls[-1] == "/papi/v1/balance":
            return response(
                200,
                [
                    {
                        "asset": "USDT",
                        "totalWalletBalance": "95.00",
                        "umWalletBalance": "90.00",
                        "umUnrealizedPNL": "4.75",
                        "updateTime": 1_735_689_599_000,
                    }
                ],
            )
        raise AssertionError(f"unexpected Binance path: {calls[-1]}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client()
    with client:
        headers = register_and_login(client, configure=True)
        result = client.get("/api/v2/me/binance-account", headers=headers)

    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert result.json() == {
        "configured": True,
        "connected": True,
        "can_trade": None,
        "account_type": "PORTFOLIO_MARGIN",
        "wallet_balance": 100.25,
        "available_balance": 80.5,
        "unrealized_pnl": 4.75,
        "currency": "USD",
        "updated_at": "2024-12-31T23:59:59Z",
        "error_category": None,
    }
    assert calls == ["/papi/v1/account", "/papi/v1/balance"]
    assert API_KEY not in result.text
    assert API_SECRET not in result.text


def test_credential_control_characters_are_rejected() -> None:
    client = build_test_client()
    with client:
        headers = register_and_login(client, configure=False)
        result = client.put(
            "/api/v2/me/binance-credentials",
            headers=headers,
            json={
                "api_key": f"{'K' * 32}\rINJECTED",
                "api_secret": API_SECRET,
                "permissions": ["READ"],
            },
        )

    assert result.status_code == 422


def test_not_configured_returns_safe_state_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transport(*_: object) -> tuple[int, bytes]:
        raise AssertionError("network must not be called without credentials")

    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client()
    with client:
        headers = register_and_login(client, configure=False)
        result = client.get("/api/v2/me/binance-account", headers=headers)

    assert result.status_code == 200
    payload = result.json()
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert payload["can_trade"] is None
    assert payload["account_type"] is None
    assert payload["error_category"] == "not_configured"


def test_papi_minus_2015_falls_back_once_to_real_fapi_v3_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        assert_signed_request(url, headers)
        path = urlsplit(url).path
        calls.append(path)
        if path == "/papi/v1/account":
            return response(400, {"code": -2015, "msg": "Invalid API-key, IP, or permissions."})
        if path == "/fapi/v3/account":
            # V3 has no top-level canTrade or updateTime configuration fields.
            return response(
                200,
                {
                    "totalWalletBalance": "250.125",
                    "availableBalance": "200.25",
                    "totalUnrealizedProfit": "-2.5",
                    "assets": [{"asset": "USDT", "walletBalance": "250.125", "updateTime": 10_000}],
                    "positions": [
                        {"symbol": "BTCUSDT", "positionAmt": "0.01", "updateTime": 12_000}
                    ],
                },
            )
        raise AssertionError(f"unexpected Binance path: {path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client()
    with client:
        headers = register_and_login(client, configure=True)
        result = client.get("/api/v2/me/binance-account", headers=headers)

    assert result.status_code == 200
    payload = result.json()
    assert payload["connected"] is True
    assert payload["account_type"] == "UM_FUTURE"
    assert payload["can_trade"] is None
    assert payload["wallet_balance"] == 250.125
    assert payload["available_balance"] == 200.25
    assert payload["unrealized_pnl"] == -2.5
    assert payload["updated_at"] == "1970-01-01T00:00:12Z"
    assert calls == ["/papi/v1/account", "/fapi/v3/account"]


def test_upstream_error_is_categorized_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        assert_signed_request(url, headers)
        return response(
            502,
            {"code": -1000, "msg": f"upstream detail {API_KEY} {API_SECRET}"},
        )

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client()
    with client:
        headers = register_and_login(client, configure=True)
        result = client.get("/api/v2/me/binance-account", headers=headers)

    assert result.status_code == 200
    payload = result.json()
    assert payload["configured"] is True
    assert payload["connected"] is False
    assert payload["can_trade"] is None
    assert payload["error_category"] == "upstream"
    assert calls == 1
    assert API_KEY not in result.text
    assert API_SECRET not in result.text
    assert "upstream detail" not in result.text


def test_binance_origins_are_allowlisted() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_allowed_origins="http://testserver",
        binance_portfolio_base_url="https://papi.binance.com.attacker.invalid",
    )
    with pytest.raises(RuntimeError, match="BINANCE_PORTFOLIO_BASE_URL"):
        settings.validate_runtime()
