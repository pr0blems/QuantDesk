from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import binance_client
from quantdesk_v2.binance_client import (
    BinanceAccountClient,
    BinanceAccountSnapshot,
    BinanceIncomeHistory,
    BinanceIncomeRecord,
)
from quantdesk_v2.binance_performance import (
    build_binance_performance,
    history_status_for_month,
    month_window_ms,
)
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app

FIXED_TIME_MS = 1_735_689_600_123
API_KEY = "K" * 64
API_SECRET = "S" * 64


def _client(mysql_test_engine: Engine) -> TestClient:
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


def _login(client: TestClient, *, configure: bool = True) -> dict[str, str]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": "income-user", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v2/auth/login",
        json={
            "username": "income-user",
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-QuantDesk-User-ID": str(registered.json()["id"]),
    }
    if configure:
        assert (
            client.put(
                "/api/v2/me/binance-credentials",
                headers=headers,
                json={
                    "api_key": API_KEY,
                    "api_secret": API_SECRET,
                    "permissions": ["READ"],
                },
            ).status_code
            == 200
        )
    return headers


def _response(status: int, payload: object) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode()


def _assert_signature(url: str, headers: dict[str, str]) -> dict[str, list[str]]:
    parsed = urlsplit(url)
    unsigned, signature = parsed.query.rsplit("&signature=", maxsplit=1)
    expected = hmac.new(API_SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert headers["X-MBX-APIKEY"] == API_KEY
    assert API_KEY not in url
    assert API_SECRET not in url
    return parse_qs(parsed.query, strict_parsing=True)


def _months_ago(value: datetime, count: int) -> str:
    absolute = value.year * 12 + value.month - 1 - count
    year, month_zero = divmod(absolute, 12)
    return f"{year:04d}-{month_zero + 1:02d}"


def test_current_month_fapi_performance_is_signed_and_grouped_per_asset(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    current_month = datetime.now(UTC).strftime("%Y-%m")
    start_ms, _ = month_window_ms(current_month, 0)
    income_time = start_ms + 1_000
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        query = _assert_signature(url, headers)
        path = urlsplit(url).path
        calls.append(path)
        if path == "/papi/v1/account":
            return _response(400, {"code": -2015, "msg": "not a portfolio key"})
        if path == "/fapi/v3/account":
            return _response(
                200,
                {
                    "totalWalletBalance": "250.125",
                    "availableBalance": "200.25",
                    "totalUnrealizedProfit": "2.5",
                    "assets": [
                        {
                            "asset": "USDT",
                            "unrealizedProfit": "2.5",
                            "updateTime": FIXED_TIME_MS - 1_000,
                        }
                    ],
                    "positions": [],
                },
            )
        if path == "/fapi/v1/income":
            assert query["startTime"] == [str(start_ms)]
            assert int(query["endTime"][0]) >= income_time
            assert query["page"] == ["1"]
            assert query["limit"] == ["1000"]
            common = {"asset": "USDT", "symbol": "BTCUSDT", "time": income_time}
            return _response(
                200,
                [
                    {**common, "incomeType": "REALIZED_PNL", "income": "12", "tranId": 1},
                    {**common, "incomeType": "REALIZED_PNL", "income": "-4", "tranId": 2},
                    {**common, "incomeType": "FUNDING_FEE", "income": "-1", "tranId": 3},
                    {**common, "incomeType": "COMMISSION", "income": "-0.5", "tranId": 4},
                    {**common, "incomeType": "TRANSFER", "income": "1000", "tranId": 5},
                ],
            )
        raise AssertionError(f"unexpected Binance path: {path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = _client(mysql_test_engine)
    with client:
        headers = _login(client)
        result = client.get(
            f"/api/v2/dashboard/binance-performance?month={current_month}"
            "&timezone_offset_minutes=0",
            headers=headers,
        )

    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    payload = result.json()
    assert payload["configured"] is True
    assert payload["connected"] is True
    assert payload["history_status"] == "available"
    assert payload["history_complete"] is True
    assert payload["aggregation_policy"] == "per_asset_no_conversion"
    assert payload["excluded_income_types"] == ["TRANSFER"]
    assert payload["records_received"] == 5
    assert payload["records_included"] == 4
    assert payload["account"] == {
        "account_type": "UM_FUTURE",
        "wallet_balance": 250.125,
        "available_balance": 200.25,
        "unrealized_pnl": 2.5,
        "currency": "USD",
        "updated_at": "2024-12-31T23:59:59.123000Z",
    }
    asset = payload["assets"][0]
    assert asset["asset"] == "USDT"
    assert asset["net_income"] == 6.5
    assert asset["realized_pnl"] == 8.0
    assert asset["funding_fee"] == -1.0
    assert asset["commission"] == -0.5
    assert asset["current_unrealized_pnl"] == 2.5
    assert asset["win_rate_pct"] == 50.0
    assert asset["profit_factor"] == 3.0
    assert "return_pct" not in result.text
    assert calls == ["/fapi/v3/account", "/fapi/v1/income"]


def test_expired_month_returns_account_but_does_not_request_income(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    selected_month = _months_ago(datetime.now(UTC), 4)
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        _assert_signature(url, headers)
        path = urlsplit(url).path
        calls.append(path)
        if path == "/fapi/v3/account":
            return _response(400, {"code": -2015, "msg": "not a standard futures key"})
        if path == "/papi/v1/account":
            return _response(
                200,
                {"accountEquity": "10", "totalAvailableBalance": "8"},
            )
        if path == "/papi/v1/balance":
            return _response(
                200,
                [{"asset": "USDT", "umUnrealizedPNL": "1", "updateTime": 10_000}],
            )
        raise AssertionError("income history must not be requested outside Binance retention")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = _client(mysql_test_engine)
    with client:
        headers = _login(client)
        result = client.get(
            f"/api/v2/dashboard/binance-performance?month={selected_month}",
            headers=headers,
        )

    assert result.status_code == 200
    payload = result.json()
    assert payload["connected"] is True
    assert payload["history_status"] == "history_unavailable"
    assert payload["history_complete"] is False
    assert payload["assets"] == []
    assert payload["account"]["wallet_balance"] == 10.0
    assert calls == ["/fapi/v3/account", "/papi/v1/account", "/papi/v1/balance"]


def test_portfolio_margin_income_uses_papi_endpoint_and_limit_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        query = _assert_signature(url, headers)
        calls.append(urlsplit(url).path)
        assert query["startTime"] == ["1000"]
        assert query["endTime"] == ["2000"]
        assert query["page"] == ["1"]
        assert query["limit"] == ["1000"]
        return _response(
            200,
            [
                {
                    "asset": "USDT",
                    "incomeType": "FUNDING_FEE",
                    "income": "0.25",
                    "time": 1_500,
                    "tranId": 99,
                }
            ],
        )

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    client = BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        transport=transport,
    )
    history = client.income_history(
        API_KEY,
        API_SECRET,
        account_type="PORTFOLIO_MARGIN",
        start_time_ms=1_000,
        end_time_ms=2_000,
    )

    assert calls == ["/papi/v1/um/income"]
    assert history.complete is True
    assert history.pages_fetched == 1
    assert history.records[0].income == Decimal("0.25")


def test_income_signature_matches_fixed_official_hmac_vector() -> None:
    unsigned = (
        "startTime=1733011200000&endTime=1735689599999&page=1&limit=1000"
        "&recvWindow=5000&timestamp=1735689600123"
    )
    query = binance_client.signed_query(
        API_SECRET,
        5_000,
        FIXED_TIME_MS,
        (
            ("startTime", 1_733_011_200_000),
            ("endTime", 1_735_689_599_999),
            ("page", 1),
            ("limit", 1_000),
        ),
    )

    assert query == (
        f"{unsigned}&signature=b60567d2d3cb85bb62a81d3e8ed5625aaaced0b054bd84cc78c353e5ec88285e"
    )


def test_repeated_full_income_page_stops_early_and_remains_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = [
        {
            "asset": "USDT",
            "incomeType": "REALIZED_PNL",
            "income": "1",
            "time": 1_500,
            "tranId": index,
        }
        for index in range(1_000)
    ]
    calls = 0

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        _assert_signature(url, headers)
        return _response(200, page)

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    client = BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        transport=transport,
    )
    history = client.income_history(
        API_KEY,
        API_SECRET,
        account_type="UM_FUTURE",
        start_time_ms=1_000,
        end_time_ms=2_000,
    )

    assert calls == 2
    assert history.pages_fetched == 2
    assert len(history.records) == 1_000
    assert history.complete is False


def test_full_unique_pages_stop_at_conservative_weight_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        query = _assert_signature(url, headers)
        page_number = int(query["page"][0])
        return _response(
            200,
            [
                {
                    "asset": "USDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "1",
                    "time": 1_500,
                    "tranId": f"{page_number}-{index}",
                }
                for index in range(1_000)
            ],
        )

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    client = BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        transport=transport,
    )
    history = client.income_history(
        API_KEY,
        API_SECRET,
        account_type="UM_FUTURE",
        start_time_ms=1_000,
        end_time_ms=2_000,
    )

    assert binance_client.MAX_INCOME_PAGES == 5
    assert calls == 5
    assert history.pages_fetched == 5
    assert len(history.records) == 5_000
    assert history.complete is False


def test_not_configured_is_renderable_and_never_calls_binance(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    def transport(*_: object) -> tuple[int, bytes]:
        raise AssertionError("network must not be called without credentials")

    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = _client(mysql_test_engine)
    with client:
        headers = _login(client, configure=False)
        result = client.get("/api/v2/dashboard/binance-performance", headers=headers)

    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    payload = result.json()
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert payload["history_status"] == "not_configured"
    assert payload["error_category"] == "not_configured"
    assert payload["account"] is None
    assert payload["assets"] == []


def test_binance_performance_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
) -> None:
    def transport(*_: object) -> tuple[int, bytes]:
        raise AssertionError("unauthenticated request must not call Binance")

    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = _client(mysql_test_engine)
    with client:
        result = client.get("/api/v2/dashboard/binance-performance")

    assert result.status_code == 401


@pytest.mark.parametrize(
    ("income_status", "income_payload", "expected_category"),
    [
        (
            502,
            {"code": -1000, "msg": f"upstream {API_KEY} {API_SECRET}"},
            "upstream",
        ),
        (
            200,
            [
                {
                    "asset": "USDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "1",
                    "time": True,
                    "tranId": 1,
                    "info": f"invalid row {API_KEY} {API_SECRET}",
                }
            ],
            "invalid_response",
        ),
    ],
)
def test_income_failure_keeps_connected_account_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_engine: Engine,
    income_status: int,
    income_payload: object,
    expected_category: str,
) -> None:
    current_month = datetime.now(UTC).strftime("%Y-%m")
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _: float) -> tuple[int, bytes]:
        _assert_signature(url, headers)
        path = urlsplit(url).path
        calls.append(path)
        if path == "/fapi/v3/account":
            return _response(400, {"code": -2015, "msg": "not a standard futures key"})
        if path == "/papi/v1/account":
            return _response(
                200,
                {"accountEquity": "50", "totalAvailableBalance": "40"},
            )
        if path == "/papi/v1/balance":
            return _response(
                200,
                [{"asset": "USDT", "umUnrealizedPNL": "2", "updateTime": 10_000}],
            )
        if path == "/papi/v1/um/income":
            return _response(income_status, income_payload)
        raise AssertionError(f"unexpected Binance path: {path}")

    monkeypatch.setattr(binance_client, "current_time_ms", lambda: FIXED_TIME_MS)
    monkeypatch.setattr(binance_client, "_http_transport", transport)
    client = _client(mysql_test_engine)
    with client:
        headers = _login(client)
        result = client.get(
            f"/api/v2/dashboard/binance-performance?month={current_month}",
            headers=headers,
        )

    assert result.status_code == 200
    payload = result.json()
    assert payload["configured"] is True
    assert payload["connected"] is True
    assert payload["history_status"] == "request_failed"
    assert payload["error_category"] == expected_category
    assert payload["account"]["wallet_balance"] == 50.0
    assert payload["assets"] == []
    assert calls == [
        "/fapi/v3/account",
        "/papi/v1/account",
        "/papi/v1/balance",
        "/papi/v1/um/income",
    ]
    assert API_KEY not in result.text
    assert API_SECRET not in result.text


def test_calendar_uses_requested_local_offset_and_excludes_transfer() -> None:
    record_time = int(datetime(2026, 1, 31, 17, 0, tzinfo=UTC).timestamp() * 1_000)
    snapshot = BinanceAccountSnapshot(
        account_type="UM_FUTURE",
        can_trade=None,
        wallet_balance=Decimal("100"),
        available_balance=Decimal("90"),
        unrealized_pnl=Decimal("0"),
        currency="USD",
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        unrealized_pnl_by_asset=(("BNB", Decimal("0")),),
    )
    history = BinanceIncomeHistory(
        account_type="UM_FUTURE",
        records=(
            BinanceIncomeRecord(
                asset="USDT",
                income_type="REALIZED_PNL",
                income=Decimal("5"),
                time_ms=record_time,
                symbol="BTCUSDT",
                transaction_id="1",
                trade_id="1",
            ),
            BinanceIncomeRecord(
                asset="USDT",
                income_type="WELCOME_BONUS",
                income=Decimal("99"),
                time_ms=record_time,
                symbol=None,
                transaction_id="2",
                trade_id=None,
            ),
        ),
        pages_fetched=1,
        complete=True,
    )
    result = build_binance_performance(
        snapshot=snapshot,
        history=history,
        month="2026-02",
        timezone_offset_minutes=480,
        generated_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert result["timezone_label"] == "UTC+08:00"
    assert result["excluded_income_types"] == ["WELCOME_BONUS"]
    assert [asset["asset"] for asset in result["assets"]] == ["USDT"]
    assert result["assets"][0]["net_income"] == 5.0
    assert result["assets"][0]["days"][0]["date"].isoformat() == "2026-02-01"


def test_three_month_retention_states_are_explicit() -> None:
    now = datetime(2026, 8, 3, 12, tzinfo=UTC)

    assert history_status_for_month("2026-09", 480, now) == "future_month"
    assert history_status_for_month("2026-05", 480, now) == "history_limited"
    assert history_status_for_month("2026-04", 480, now) == "history_unavailable"
    assert history_status_for_month("2026-06", 480, now) == "available"
