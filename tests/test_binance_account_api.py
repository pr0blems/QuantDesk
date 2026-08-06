from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import binance_client
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app

FIXED_TIME_MS = 1_735_689_600_123
API_KEY = "K" * 64
API_SECRET = "S" * 64
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def build_test_client(mysql_test_engine: Engine) -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    test_session = sessionmaker(
        bind=mysql_test_engine, autoflush=False, expire_on_commit=False
    )
    app = create_app(settings)
    app.state.database_engine.dispose()
    app.state.database_engine = mysql_test_engine

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
    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-QuantDesk-User-ID": str(registered.json()["id"]),
    }
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


def test_position_risk_fields_are_normalized_without_false_defaults() -> None:
    positions = binance_client._account_positions(
        [
            {
                "symbol": "SAMSUNGUSDT",
                "positionAmt": "0.10",
                "positionSide": "LONG",
                "entryPrice": "161.5",
                "markPrice": "161.49",
                "notional": "16.149",
                "positionInitialMargin": "1.6149",
                "unRealizedProfit": "-0.001",
                "updateTime": 1_785_816_452_179,
            }
        ],
        FIXED_TIME_MS,
    )

    assert positions == (
        {
            "symbol": "SAMSUNGUSDT",
            "amt": 0.1,
            "side": "long",
            "position_side": "LONG",
            "entry_price": 161.5,
            "mark_price": 161.49,
            "break_even_price": None,
            "liquidation_price": None,
            "notional": 16.149,
            "initial_margin": 1.6149,
            "maintenance_margin": None,
            "margin_type": None,
            "isolated": None,
            "upnl": -0.001,
            "leverage": 10,
            "ts": 1_785_816_452_179,
        },
    )


def test_portfolio_margin_summary_is_signed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        assert timeout == 10.0
        assert_signed_request(url, headers)
        calls.append(urlsplit(url).path)
        if calls[-1] == "/fapi/v3/account":
            return response(400, {"code": -2015, "msg": "not a standard futures key"})
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
    client = build_test_client(mysql_test_engine)
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
    assert calls == ["/fapi/v3/account", "/papi/v1/account", "/papi/v1/balance"]
    assert API_KEY not in result.text
    assert API_SECRET not in result.text


def test_credential_control_characters_are_rejected(mysql_test_engine: Engine) -> None:
    client = build_test_client(mysql_test_engine)
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
    mysql_test_engine: Engine,
) -> None:
    def transport(*_: object) -> tuple[int, bytes]:
        raise AssertionError("network must not be called without credentials")

    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client(mysql_test_engine)
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


def test_standard_futures_key_uses_fapi_v3_without_papi_probe(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        assert_signed_request(url, headers)
        path = urlsplit(url).path
        calls.append(path)
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
        if path == "/fapi/v3/positionRisk":
            return response(
                200,
                [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.01",
                        "positionSide": "BOTH",
                        "entryPrice": "60000",
                        "markPrice": "61000",
                        "notional": "610",
                        "positionInitialMargin": "122",
                        "unRealizedProfit": "10",
                        "updateTime": 13_000,
                    }
                ],
            )
        raise AssertionError(f"unexpected Binance path: {path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client(mysql_test_engine)
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
    assert payload["updated_at"] == "1970-01-01T00:00:13Z"
    assert payload["positions"][0]["entry_price"] == 60000.0
    assert payload["positions"][0]["mark_price"] == 61000.0
    assert payload["positions"][0]["notional"] == 610.0
    assert payload["positions"][0]["leverage"] == 5
    assert calls == ["/fapi/v3/account", "/fapi/v3/positionRisk"]


def test_binance_orders_returns_current_positions_and_open_orders(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        assert_signed_request(url, headers)
        path = urlsplit(url).path
        calls.append(path)
        if path == "/fapi/v3/account":
            return response(
                200,
                {
                    "totalWalletBalance": "250.125",
                    "availableBalance": "200.25",
                    "totalUnrealizedProfit": "12.5",
                    "assets": [],
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0.01",
                            "entryPrice": "60000",
                            "markPrice": "61000",
                            "unrealizedProfit": "10",
                            "leverage": "5",
                            "updateTime": 12_000,
                        }
                    ],
                },
            )
        if path == "/fapi/v1/openOrders":
            return response(
                200,
                [
                    {
                        "symbol": "ETHUSDT",
                        "orderId": 9_007_199_254_740_993,
                        "clientOrderId": "client-order-1",
                        "side": "SELL",
                        "positionSide": "BOTH",
                        "type": "LIMIT",
                        "status": "NEW",
                        "timeInForce": "GTC",
                        "price": "4000.5",
                        "avgPrice": "0",
                        "stopPrice": "0",
                        "origQty": "2.25",
                        "executedQty": "0.25",
                        "reduceOnly": True,
                        "closePosition": False,
                        "time": 13_000,
                        "updateTime": 14_000,
                    }
                ],
            )
        if path == "/fapi/v3/positionRisk":
            return response(
                200,
                [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.01",
                        "positionSide": "LONG",
                        "entryPrice": "60000",
                        "markPrice": "61000",
                        "notional": "610",
                        "positionInitialMargin": "122",
                        "unRealizedProfit": "10",
                        "updateTime": 12_500,
                    }
                ],
            )
        if path == "/fapi/v1/openAlgoOrders":
            return response(
                200,
                [
                    {
                        "algoId": 123_456,
                        "clientAlgoId": "conditional-1",
                        "algoType": "CONDITIONAL",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "positionSide": "BOTH",
                        "timeInForce": "GTC",
                        "quantity": "0.01",
                        "algoStatus": "NEW",
                        "triggerPrice": "70000",
                        "price": "0",
                        "reduceOnly": True,
                        "closePosition": False,
                        "createTime": 15_000,
                        "updateTime": 16_000,
                    }
                ],
            )
        raise AssertionError(f"unexpected Binance path: {path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, configure=True)
        result = client.get("/api/v2/me/binance-orders", headers=headers)

    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    payload = result.json()
    assert payload["configured"] is True
    assert payload["connected"] is True
    assert payload["account_type"] == "UM_FUTURE"
    assert payload["positions"] == [
        {
            "symbol": "BTCUSDT",
            "amt": 0.01,
            "side": "long",
            "position_side": "LONG",
            "entry_price": 60000.0,
            "mark_price": 61000.0,
            "notional": 610.0,
            "upnl": 10.0,
            "leverage": 5,
            "ts": 12_500,
        }
    ]
    assert payload["open_orders"][0]["order_id"] == "123456"
    assert payload["open_orders"][0]["conditional"] is True
    assert payload["open_orders"][0]["stop_price"] == 70000.0
    assert payload["open_orders"][1]["order_id"] == "9007199254740993"
    assert payload["open_orders"][1]["quantity"] == 2.25
    assert payload["open_orders"][1]["executed_quantity"] == 0.25
    assert payload["open_orders"][0]["reduce_only"] is True
    assert calls == [
        "/fapi/v3/account",
        "/fapi/v3/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
    ]
    assert API_KEY not in result.text
    assert API_SECRET not in result.text


def test_timestamp_error_synchronizes_binance_clock_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    account_attempts = 0

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        nonlocal account_attempts
        assert timeout == 10.0
        parsed = urlsplit(url)
        calls.append(parsed.path)
        if parsed.path == "/fapi/v1/time":
            assert "X-MBX-APIKEY" not in headers
            return response(200, {"serverTime": FIXED_TIME_MS + 4_500})
        if parsed.path == "/fapi/v3/account":
            account_attempts += 1
            query = parse_qs(parsed.query, strict_parsing=True)
            expected_time = FIXED_TIME_MS if account_attempts == 1 else FIXED_TIME_MS + 4_500
            assert query["timestamp"] == [str(expected_time)]
            if account_attempts == 1:
                return response(400, {"code": -1021, "msg": "timestamp outside recvWindow"})
            return response(
                200,
                {
                    "totalWalletBalance": "100",
                    "availableBalance": "90",
                    "totalUnrealizedProfit": "1",
                    "assets": [],
                    "positions": [],
                },
            )
        raise AssertionError(f"unexpected Binance path: {parsed.path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    client = binance_client.BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        timeout_seconds=10,
        transport=transport,
    )

    snapshot = client.account(API_KEY, API_SECRET)

    assert snapshot.account_type == "UM_FUTURE"
    assert snapshot.wallet_balance == 100
    assert calls == ["/fapi/v3/account", "/fapi/v1/time", "/fapi/v3/account"]


def test_account_snapshot_separates_source_update_from_fetch_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_updated_ms = FIXED_TIME_MS - 60_000
    fetch_completed_ms = FIXED_TIME_MS + 250
    clock_values = iter((FIXED_TIME_MS, fetch_completed_ms))

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        assert_signed_request(url, headers)
        return response(
            200,
            {
                "totalWalletBalance": "100",
                "availableBalance": "90",
                "totalUnrealizedProfit": "1",
                "assets": [{"asset": "USDT", "updateTime": source_updated_ms}],
                "positions": [],
            },
        )

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: next(clock_values))
    client = binance_client.BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        transport=transport,
    )

    snapshot = client.account(API_KEY, API_SECRET)

    assert snapshot.updated_at == datetime.fromtimestamp(source_updated_ms / 1_000, UTC)
    assert snapshot.observed_at == datetime.fromtimestamp(fetch_completed_ms / 1_000, UTC)


def test_portfolio_open_orders_uses_um_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        assert_signed_request(url, headers)
        calls.append(urlsplit(url).path)
        return response(200, [])

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    client = binance_client.BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        timeout_seconds=10,
        transport=transport,
    )

    assert client.open_orders(
        API_KEY,
        API_SECRET,
        account_type="PORTFOLIO_MARGIN",
    ) == ()
    assert calls == [
        "/papi/v1/um/openOrders",
        "/papi/v1/um/conditional/openOrders",
    ]


def test_upstream_error_is_categorized_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
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
    client = build_test_client(mysql_test_engine)
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
        database_url=(
            "mysql+pymysql://test:test@127.0.0.1:3306/quantdesk_test_validation"
        ),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_allowed_origins="http://testserver",
        binance_portfolio_base_url="https://papi.binance.com.attacker.invalid",
    )
    with pytest.raises(RuntimeError, match="BINANCE_PORTFOLIO_BASE_URL"):
        settings.validate_runtime()
