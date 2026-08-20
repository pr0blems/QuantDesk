from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from quantdesk_v2.binance_client import MAX_RESPONSE_BYTES
from quantdesk_v2.binance_trading import (
    MAX_EXCHANGE_INFO_BYTES,
    BinanceUsdMTradingClient,
    _trade_response_limit,
)


def test_exchange_info_has_a_larger_but_bounded_response_limit() -> None:
    assert _trade_response_limit("/fapi/v1/exchangeInfo") == MAX_EXCHANGE_INFO_BYTES
    assert MAX_EXCHANGE_INFO_BYTES == 2 * 1024 * 1024
    assert _trade_response_limit("/fapi/v1/order") == MAX_RESPONSE_BYTES


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
    assert query["positionSide"] == ["BOTH"]
    assert query["quantity"] == ["0.123"]
    assert query["newClientOrderId"] == ["qd1-abc"]
    assert "timestamp" in query
    assert "signature" in query
    assert "api-secret" not in url


def test_ticker_price_reads_current_binance_price_without_credentials() -> None:
    captured: list[tuple[str, str, dict[str, str]]] = []

    def transport(method: str, url: str, headers: dict[str, str], _timeout: float):
        captured.append((method, url, headers))
        return 200, b'{"symbol":"TXNUSDT","price":"217.42","time":1787241600000}'

    client = BinanceUsdMTradingClient("https://fapi.binance.com", transport=transport)

    assert client.ticker_price("txnusdt") == Decimal("217.42")
    method, url, headers = captured[0]
    assert method == "GET"
    assert urlsplit(url).path == "/fapi/v1/ticker/price"
    assert parse_qs(urlsplit(url).query) == {"symbol": ["TXNUSDT"]}
    assert "X-MBX-APIKEY" not in headers


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
    assert query["positionSide"] == ["BOTH"]
    assert query["triggerPrice"] == ["62000.1"]
    assert query["clientAlgoId"] == ["qd1-stop"]
    assert "quantity" not in query


def test_hedge_orders_bind_position_side_without_reduce_only() -> None:
    captured: list[str] = []

    def transport(_method: str, url: str, _headers: dict[str, str], _timeout: float):
        captured.append(url)
        if urlsplit(url).path == "/fapi/v1/order":
            return 200, b'{"orderId":123,"status":"FILLED"}'
        return 200, b'{"algoId":456,"algoStatus":"NEW"}'

    client = BinanceUsdMTradingClient("https://fapi.binance.com", transport=transport)
    client.place_market_order(
        "api-key",
        "api-secret",
        symbol="AAPLUSDT",
        side="BUY",
        position_side="LONG",
        quantity=Decimal("0.5"),
        client_order_id="qd-hedge-open",
    )
    client.place_close_trigger(
        "api-key",
        "api-secret",
        symbol="AAPLUSDT",
        side="SELL",
        position_side="LONG",
        order_type="STOP_MARKET",
        stop_price=Decimal("200"),
        quantity=Decimal("0.5"),
        client_order_id="qd-hedge-stop",
    )

    market_query = parse_qs(urlsplit(captured[0]).query)
    protection_query = parse_qs(urlsplit(captured[1]).query)
    assert market_query["positionSide"] == ["LONG"]
    assert "reduceOnly" not in market_query
    assert protection_query["positionSide"] == ["LONG"]
    assert protection_query["quantity"] == ["0.5"]
    assert "closePosition" not in protection_query

    with pytest.raises(ValueError, match="reduceOnly"):
        client.place_market_order(
            "api-key",
            "api-secret",
            symbol="AAPLUSDT",
            side="SELL",
            position_side="LONG",
            quantity=Decimal("0.5"),
            client_order_id="qd-invalid-close",
            reduce_only=True,
        )


def test_unapproved_origin_and_over_limit_leverage_are_rejected_locally() -> None:
    with pytest.raises(ValueError):
        BinanceUsdMTradingClient("https://example.com")
    client = BinanceUsdMTradingClient(
        "https://fapi.binance.com",
        transport=lambda *_: (200, b"{}"),
    )
    with pytest.raises(ValueError):
        client.change_leverage("key", "secret", symbol="BTCUSDT", leverage=21)
