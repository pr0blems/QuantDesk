from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from quantdesk_v2.binance_trading import BinanceUsdMTradingClient


def test_symbol_rules_round_down_and_market_order_is_signed() -> None:
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, _headers: dict[str, str], _timeout: float):
        calls.append((method, url))
        path = urlsplit(url).path
        if path == "/fapi/v1/exchangeInfo":
            return 200, json.dumps(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "filters": [
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.001",
                                    "maxQty": "100",
                                    "stepSize": "0.001",
                                },
                                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            ).encode()
        if path == "/fapi/v1/order":
            return 200, b'{"orderId":123,"status":"FILLED","executedQty":"0.123"}'
        raise AssertionError(path)

    client = BinanceUsdMTradingClient("https://fapi.binance.com", transport=transport)
    rules = client.symbol_rules("btcusdt")
    assert rules.quantity(Decimal("0.12399")) == Decimal("0.123")
    response = client.place_market_order(
        "api-key",
        "api-secret",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.123"),
        client_order_id="qd1-abc",
    )

    assert response["status"] == "FILLED"
    method, url = calls[-1]
    query = parse_qs(urlsplit(url).query)
    assert method == "POST"
    assert query["symbol"] == ["BTCUSDT"]
    assert query["quantity"] == ["0.123"]
    assert query["newClientOrderId"] == ["qd1-abc"]
    assert "timestamp" in query
    assert "signature" in query
    assert "api-secret" not in url


def test_close_trigger_is_exchange_side_mark_price_protection() -> None:
    captured: list[str] = []

    def transport(_method: str, url: str, _headers: dict[str, str], _timeout: float):
        captured.append(url)
        return 200, b'{"algoId":456,"algoStatus":"NEW"}'

    client = BinanceUsdMTradingClient("https://fapi.binance.com", transport=transport)
    client.place_close_trigger(
        "api-key",
        "api-secret",
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        stop_price=Decimal("62000.1"),
        client_order_id="qd1-stop",
    )
    query = parse_qs(urlsplit(captured[0]).query)
    assert urlsplit(captured[0]).path == "/fapi/v1/algoOrder"
    assert query["algoType"] == ["CONDITIONAL"]
    assert query["closePosition"] == ["true"]
    assert query["workingType"] == ["MARK_PRICE"]
    assert query["priceProtect"] == ["true"]
    assert query["triggerPrice"] == ["62000.1"]
    assert query["clientAlgoId"] == ["qd1-stop"]
    assert "quantity" not in query


def test_unapproved_origin_and_over_limit_leverage_are_rejected_locally() -> None:
    with pytest.raises(ValueError):
        BinanceUsdMTradingClient("https://example.com")
    client = BinanceUsdMTradingClient(
        "https://fapi.binance.com",
        transport=lambda *_: (200, b"{}"),
    )
    with pytest.raises(ValueError):
        client.change_leverage("key", "secret", symbol="BTCUSDT", leverage=21)
