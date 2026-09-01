from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantdesk_v2.tiger_quotes import (
    TigerQuoteClient,
    TigerQuoteClientError,
    TigerUsQuoteService,
)


def test_tiger_client_uses_direct_batch_endpoint_and_freshest_session() -> None:
    observed: dict[str, object] = {}
    now_ms = int(datetime.now(UTC).timestamp() * 1_000)

    def transport(url: str, headers: dict[str, str], body: bytes, timeout: float):
        observed.update(
            {
                "url": url,
                "headers": headers,
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        return 200, json.dumps(
            {
                "ret": 0,
                "items": [
                    {
                        "symbol": "TSLA",
                        "latestPrice": 367.95,
                        "preClose": 333.0,
                        "timestamp": now_ms - 60_000,
                        "delay": 0,
                        "hourTrading": {
                            "latestPrice": 368.66,
                            "preClose": 367.95,
                            "timestamp": now_ms,
                        },
                    }
                ],
            }
        ).encode()

    client = TigerQuoteClient(
        "https://hq2.skytigris.cn",
        "Bearer server-only-token",
        transport=transport,
    )
    quote = client.quotes(["TSLAUSDT"])["TSLA"]

    assert observed["url"] == "https://hq2.skytigris.cn/stock_info/brief/all"
    assert observed["body"] == {"items": [{"symbol": "TSLA"}]}
    headers = observed["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer server-only-token"
    assert quote.price == pytest.approx(368.66)
    assert quote.previous_close == pytest.approx(367.95)
    assert quote.session == "extended_hours"
    assert quote.live is True
    assert quote.delayed is False


def test_tiger_service_caches_batch() -> None:
    calls = 0
    now = int(datetime.now(UTC).timestamp())

    def transport(_url: str, _headers: dict[str, str], _body: bytes, _timeout: float):
        nonlocal calls
        calls += 1
        return 200, json.dumps(
            {
                "ret": 0,
                "items": [
                    {
                        "symbol": "NVDA",
                        "latestPrice": 220.25,
                        "preClose": 219.0,
                        "timestamp": now,
                        "delay": 0,
                    }
                ],
            }
        ).encode()

    service = TigerUsQuoteService(
        TigerQuoteClient(
            "https://hq2.skytigris.cn",
            "Bearer server-only-token",
            transport=transport,
        ),
        cache_seconds=30,
    )

    first = service.latest_many(["NVDA"])["NVDA"]
    second = service.latest_many(["NVDA"])["NVDA"]

    assert calls == 1
    assert first["price"] == pytest.approx(220.25)
    assert second["live"] is True
    assert second["error_category"] is None


def test_tiger_client_redacts_authentication_failure() -> None:
    client = TigerQuoteClient(
        "https://hq2.skytigris.cn",
        "Bearer server-only-token",
        transport=lambda *_args: (401, b"{}"),
    )

    with pytest.raises(TigerQuoteClientError) as exc_info:
        client.quotes(["AAPL"])

    assert exc_info.value.category == "authentication"
    assert "server-only-token" not in str(exc_info.value)
