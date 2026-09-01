from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantdesk_v2.tiger_quotes import (
    TigerDepthClient,
    TigerQuoteClient,
    TigerQuoteClientError,
    TigerUsDepthService,
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


def test_tiger_depth_client_uses_demo_level2_endpoint_and_midpoint() -> None:
    observed: dict[str, object] = {}
    now_ms = int(datetime.now(UTC).timestamp() * 1_000)

    def transport(url: str, headers: dict[str, str], timeout: float):
        observed.update({"url": url, "headers": headers, "timeout": timeout})
        return 200, json.dumps(
            {
                "timestamp": now_ms,
                "askBidDepth": {
                    "bid": [
                        {"price": 363.30, "volume": 120.0, "subVolume": [1, 2]},
                        {"price": 363.25, "volume": 80.0, "subVolume": [1]},
                    ],
                    "ask": [
                        {"price": 363.47, "volume": 90.0, "subVolume": [1, 2, 3]},
                        {"price": 363.52, "volume": 70.0, "subVolume": []},
                    ],
                },
            }
        ).encode()

    client = TigerDepthClient(
        "https://hq-depth.skytigris.cn",
        "Bearer server-only-token",
        transport=transport,
    )
    snapshot = client.depth("TSLAUSDT")
    market = snapshot.as_market_snapshot()

    assert observed["url"] == (
        "https://hq-depth.skytigris.cn/stock_info/ask_bid/arca/TSLA"
        "?props=askBidDepth"
    )
    headers = observed["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer server-only-token"
    assert snapshot.best_bid == pytest.approx(363.30)
    assert snapshot.best_ask == pytest.approx(363.47)
    assert snapshot.mid_price == pytest.approx((363.30 + 363.47) / 2)
    assert market["price"] == pytest.approx(snapshot.mid_price)
    assert market["venue"] == "us_cash_arca_level2"
    assert market["bids"][0]["order_count"] == 2
    assert market["asks"][0]["order_count"] == 3


def test_tiger_depth_service_caches_same_snapshot_for_card_and_popup() -> None:
    calls = 0
    now = int(datetime.now(UTC).timestamp())

    def transport(_url: str, _headers: dict[str, str], _timeout: float):
        nonlocal calls
        calls += 1
        return 200, json.dumps(
            {
                "serverTime": now,
                "askBidDepth": {
                    "bid": [{"price": 100.0, "volume": 10.0, "subVolume": []}],
                    "ask": [{"price": 100.2, "volume": 12.0, "subVolume": []}],
                },
            }
        ).encode()

    service = TigerUsDepthService(
        TigerDepthClient(
            "https://hq-depth.skytigris.cn",
            "Bearer server-only-token",
            transport=transport,
        ),
        cache_seconds=30,
    )

    card_snapshot = service.latest_many(["AAPLUSDT"])["AAPL"]
    popup_snapshot = service.latest_one("AAPL")

    assert calls == 1
    assert popup_snapshot is not None
    assert card_snapshot["price"] == pytest.approx(100.1)
    assert popup_snapshot["price"] == pytest.approx(card_snapshot["price"])
