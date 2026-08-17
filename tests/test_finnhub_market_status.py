from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2 import finnhub, finnhub_quotes
from quantdesk_v2.config import Settings
from quantdesk_v2.finnhub import (
    FinnhubClient,
    FinnhubClientError,
    FinnhubMarketStatus,
    FinnhubMarketStatusResult,
    FinnhubMarketStatusService,
    FinnhubQuote,
)
from quantdesk_v2.finnhub_quotes import FinnhubUsQuoteService
from quantdesk_v2.main import create_app

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def response(status: int, payload: object) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def payload() -> dict[str, object]:
    return {
        "exchange": "US",
        "holiday": None,
        "isOpen": False,
        "session": "pre-market",
        "timezone": "America/New_York",
        "t": 1_697_018_041,
    }


def test_market_status_uses_fixed_us_endpoint_and_header_auth() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        calls.append((url, headers, timeout))
        return response(200, payload())

    status = FinnhubClient(
        "https://finnhub.io",
        "server-secret-token",
        timeout_seconds=4,
        transport=transport,
    ).market_status()

    assert status.exchange == "US"
    assert status.session == "pre-market"
    assert status.is_open is False
    assert calls[0][0] == "https://finnhub.io/api/v1/stock/market-status?exchange=US"
    assert calls[0][1]["X-Finnhub-Token"] == "server-secret-token"
    assert "server-secret-token" not in calls[0][0]
    assert calls[0][2] == 4


def test_quote_uses_finnhub_us_symbol_and_normalizes_snapshot() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        calls.append((url, headers, timeout))
        return response(
            200,
            {
                "c": 261.74,
                "d": 2.29,
                "dp": 0.8826,
                "h": 263.31,
                "l": 260.68,
                "o": 261.07,
                "pc": 259.45,
                "t": 1_786_000_000,
            },
        )

    quote = FinnhubClient(
        "https://finnhub.io",
        "server-secret-token",
        transport=transport,
    ).quote("aapl")

    assert quote.symbol == "AAPL"
    assert quote.price == 261.74
    assert quote.previous_close == 259.45
    assert calls[0][0] == "https://finnhub.io/api/v1/quote?symbol=AAPL"
    assert calls[0][1]["X-Finnhub-Token"] == "server-secret-token"
    assert "server-secret-token" not in calls[0][0]


def test_market_status_redacts_rate_limit_and_invalid_responses() -> None:
    for status_code, body, expected in (
        (429, b'{"error":"sensitive upstream detail"}', "rate_limit"),
        (200, b'{"exchange":"US"}', "invalid_response"),
    ):
        client = FinnhubClient(
            "https://finnhub.io",
            "secret",
            transport=lambda *_args, status_code=status_code, body=body: (status_code, body),
        )
        try:
            client.market_status()
        except FinnhubClientError as exc:
            assert exc.category == expected
            assert "sensitive" not in str(exc)
        else:
            raise AssertionError("expected FinnhubClientError")


def test_service_caches_and_returns_stale_value_on_upstream_failure(monkeypatch) -> None:
    calls = 0

    def transport(*_args) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(200, payload())
        return response(429, {"error": "limit"})

    now = 100.0
    monkeypatch.setattr(finnhub.time, "monotonic", lambda: now)
    service = FinnhubMarketStatusService(
        FinnhubClient("https://finnhub.io", "secret", transport=transport),
        cache_seconds=30,
        stale_seconds=900,
    )

    first = service.status()
    second = service.status()
    assert first.available is True and first.cached is False
    assert second.available is True and second.cached is True
    assert calls == 1

    now = 131.0
    stale = service.status()
    assert stale.available is True
    assert stale.cached is True
    assert stale.stale is True
    assert stale.error_category == "rate_limit"
    assert calls == 2


def test_unconfigured_service_never_calls_transport() -> None:
    def forbidden(*_args) -> tuple[int, bytes]:
        raise AssertionError("transport must not be called")

    result = FinnhubMarketStatusService(
        FinnhubClient("https://finnhub.io", "", transport=forbidden)
    ).status()
    assert result.configured is False
    assert result.available is False
    assert result.error_category == "not_configured"


def test_us_quote_service_has_separate_equity_universe_and_stream_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        finnhub_quotes,
        "_load_us_symbols",
        lambda _path: ("AAPL", "BRK.B"),
    )
    service = FinnhubUsQuoteService(
        FinnhubClient("https://finnhub.io", "secret", transport=lambda *_: response(429, {})),
        Path("unused.json"),
        websocket_enabled=False,
    )

    assert service.symbols == ("AAPL", "BRK.B")
    service._ingest_stream_message(
        json.dumps(
            {
                "type": "trade",
                "data": [{"s": "AAPL", "p": 262.1, "t": 1_786_000_000_123, "v": 10}],
            }
        )
    )
    snapshot = service.snapshot()

    assert snapshot["source"] == "finnhub"
    assert snapshot["exchange"] == "US"
    assert snapshot["total"] == 2
    assert snapshot["available"] == 1
    assert snapshot["quotes"][0]["symbol"] == "AAPL"
    assert snapshot["quotes"][0]["price"] == 262.1
    assert snapshot["quotes"][0]["live"] is True


def test_rest_quote_does_not_replace_equal_or_newer_stream_trade(monkeypatch) -> None:
    monkeypatch.setattr(finnhub_quotes, "_load_us_symbols", lambda _path: ("AAPL",))
    service = FinnhubUsQuoteService(
        FinnhubClient("https://finnhub.io", "secret", transport=lambda *_: response(429, {})),
        Path("unused.json"),
        websocket_enabled=False,
    )
    service._ingest_stream_message(
        json.dumps(
            {
                "type": "trade",
                "data": [{"s": "AAPL", "p": 262.1, "t": 1_786_000_000_900, "v": 10}],
            }
        )
    )
    service._store_rest_quote(
        FinnhubQuote(
            symbol="AAPL",
            price=261.5,
            change=1.0,
            change_percent=0.4,
            day_high=263.0,
            day_low=259.0,
            day_open=260.0,
            previous_close=260.5,
            source_timestamp=1_786_000_000,
            fetched_at=datetime.now(UTC),
        )
    )

    quote = service.snapshot()["quotes"][0]
    assert quote["price"] == 262.1
    assert quote["live"] is True


def test_us_quote_collection_is_disabled_outside_regular_market(monkeypatch) -> None:
    monkeypatch.setattr(finnhub_quotes, "_load_us_symbols", lambda _path: ("AAPL",))
    service = FinnhubUsQuoteService(
        FinnhubClient("https://finnhub.io", "secret", transport=lambda *_: response(200, {})),
        Path("unused.json"),
        websocket_enabled=False,
        market_open_checker=lambda: False,
    )

    service._ingest_stream_message(
        json.dumps(
            {
                "type": "trade",
                "data": [{"s": "AAPL", "p": 262.1, "t": 1_786_000_000_123}],
            }
        )
    )
    snapshot = service.snapshot()

    assert snapshot["market_open"] is False
    assert snapshot["collection_active"] is False
    assert snapshot["available"] == 0


def test_us_quote_latest_many_normalizes_contract_symbol(monkeypatch) -> None:
    monkeypatch.setattr(finnhub_quotes, "_load_us_symbols", lambda _path: ("AAPL",))
    service = FinnhubUsQuoteService(
        FinnhubClient("https://finnhub.io", "secret", transport=lambda *_: response(429, {})),
        Path("unused.json"),
        websocket_enabled=False,
    )
    service._ingest_stream_message(
        json.dumps(
            {
                "type": "trade",
                "data": [{"s": "AAPL", "p": 262.1, "t": 1_786_000_000_123}],
            }
        )
    )

    latest = service.latest_many(["AAPLUSDT"])

    assert latest["AAPL"]["price"] == 262.1
    assert latest["AAPL"]["storage"] == "memory_pending"


def test_public_us_market_status_route() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    app = create_app(settings)

    class FakeService:
        def status(self) -> FinnhubMarketStatusResult:
            snapshot = FinnhubMarketStatus(
                exchange="US",
                holiday=None,
                is_open=True,
                session="regular",
                timezone="America/New_York",
                source_timestamp=1_786_000_000,
                fetched_at=datetime(2026, 8, 5, tzinfo=UTC),
            )
            return FinnhubMarketStatusService._result(snapshot, cached=False)

    app.state.finnhub_market_status_service = FakeService()
    with TestClient(app) as client:
        result = client.get("/api/v2/market/us/status")
        quotes = client.get("/api/v2/market/us/quotes")

    assert result.status_code == 200
    assert result.headers["cache-control"] == "public, max-age=5, stale-if-error=60"
    assert result.json()["session"] == "regular"
    assert result.json()["is_open"] is True
    assert quotes.status_code == 200
    assert quotes.headers["cache-control"] == "public, max-age=2, stale-if-error=30"
    assert quotes.json()["source"] == "finnhub"
    assert quotes.json()["exchange"] == "US"


def test_finnhub_webhook_readiness_and_authenticated_post() -> None:
    secret = "new-webhook-secret-at-least-16"
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
        finnhub_webhook_secret=SecretStr(secret),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/api/v2/integrations/finnhub/webhook")
        missing = client.post(
            "/api/v2/integrations/finnhub/webhook",
            json={"type": "test"},
        )
        invalid = client.post(
            "/api/v2/integrations/finnhub/webhook",
            headers={"X-Finnhub-Secret": "incorrect-secret-value"},
            json={"type": "test"},
        )
        accepted = client.post(
            "/api/v2/integrations/finnhub/webhook",
            headers={"X-Finnhub-Secret": secret},
            json={"type": "test", "data": []},
        )
        after = client.get("/api/v2/integrations/finnhub/webhook")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json() == {"accepted": True}
    assert after.json()["received_events"] == 1
    assert after.json()["last_received_at"] is not None


def test_unconfigured_webhook_is_visible_but_rejects_posts() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/api/v2/integrations/finnhub/webhook")
        rejected = client.post(
            "/api/v2/integrations/finnhub/webhook",
            headers={"X-Finnhub-Secret": "some-long-secret-value"},
            json={"type": "test"},
        )

    assert ready.status_code == 200
    assert ready.json()["status"] == "not_configured"
    assert rejected.status_code == 503
