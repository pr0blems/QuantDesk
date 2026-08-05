from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2 import finnhub
from quantdesk_v2.config import Settings
from quantdesk_v2.dependencies import get_current_user
from quantdesk_v2.finnhub import (
    FinnhubClient,
    FinnhubClientError,
    FinnhubMarketStatus,
    FinnhubMarketStatusResult,
    FinnhubMarketStatusService,
)
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


def test_authenticated_us_market_status_route() -> None:
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
    app.dependency_overrides[get_current_user] = lambda: object()

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

    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert result.json()["session"] == "regular"
    assert result.json()["is_open"] is True
