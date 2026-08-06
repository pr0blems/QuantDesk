from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quantdesk_v2.application.ports import Broker, MarketDataFeed
from quantdesk_v2.binance_client import (
    BinanceAccountClientError,
    BinanceAccountSnapshot,
)
from quantdesk_v2.binance_trading import FuturesSymbolRules
from quantdesk_v2.domain.trading import (
    AccountType,
    BrokerError,
    MarketOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionDirection,
    PositionMode,
    PositionSide,
    TriggerOrder,
)
from quantdesk_v2.infrastructure.binance_broker import BinanceBroker
from quantdesk_v2.infrastructure.store_market_data import StoreMarketDataFeed

_API_SECRET = "api-secret"


def _account_snapshot() -> BinanceAccountSnapshot:
    return BinanceAccountSnapshot(
        account_type="UM_FUTURE",
        can_trade=None,
        wallet_balance=Decimal("100.25"),
        available_balance=Decimal("80.5"),
        unrealized_pnl=Decimal("1.75"),
        currency="USD",
        updated_at=datetime(2026, 8, 5, tzinfo=UTC),
        observed_at=datetime(2026, 8, 6, tzinfo=UTC),
        positions=(
            {
                "symbol": "BTCUSDT",
                "amt": 0.125,
                "side": "long",
                "position_side": "BOTH",
                "entry_price": 60_000.5,
                "mark_price": 61_000,
                "break_even_price": 60_010,
                "liquidation_price": 50_000,
                "notional": 7_625,
                "initial_margin": 762.5,
                "maintenance_margin": 38.125,
                "upnl": 124.9375,
                "leverage": 10,
                "ts": 1_754_332_800_000,
            },
        ),
    )


class _AccountClient:
    def __init__(self) -> None:
        self.account_calls: list[tuple[str, str]] = []
        self.open_orders_calls: list[tuple[str, str, str]] = []

    def account(self, api_key: str, api_secret: str) -> BinanceAccountSnapshot:
        self.account_calls.append((api_key, api_secret))
        return _account_snapshot()

    def open_orders(
        self,
        api_key: str,
        api_secret: str,
        *,
        account_type: str,
    ) -> tuple[dict[str, Any], ...]:
        self.open_orders_calls.append((api_key, api_secret, account_type))
        return (
            {
                "order_id": "42",
                "client_order_id": "qd-open",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "position_side": "BOTH",
                "type": "LIMIT",
                "status": "NEW",
                "quantity": 0.125,
                "executed_quantity": 0,
                "average_price": 0,
                "stop_price": None,
                "reduce_only": False,
                "conditional": False,
            },
        )


class _TradingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def symbol_rules(self, symbol: str) -> FuturesSymbolRules:
        self.calls.append(("symbol_rules", (symbol,), {}))
        return FuturesSymbolRules(
            symbol="BTCUSDT",
            market_step_size=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("100"),
            tick_size=Decimal("0.10"),
            min_notional=Decimal("5"),
        )

    def position_mode(self, api_key: str, api_secret: str) -> str:
        self.calls.append(("position_mode", (api_key, api_secret), {}))
        return "one_way"

    def change_leverage(
        self, api_key: str, api_secret: str, *, symbol: str, leverage: int
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "change_leverage",
                (api_key, api_secret),
                {"symbol": symbol, "leverage": leverage},
            )
        )
        return {"symbol": symbol, "leverage": leverage}

    def place_market_order(
        self, api_key: str, api_secret: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("place_market_order", (api_key, api_secret), kwargs))
        return {
            "orderId": 123,
            "status": "FILLED",
            "executedQty": "0.125",
            "avgPrice": "61000.25",
        }

    def place_close_trigger(
        self, api_key: str, api_secret: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("place_close_trigger", (api_key, api_secret), kwargs))
        return {"algoId": 456, "algoStatus": "NEW"}

    def query_order(self, api_key: str, api_secret: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query_order", (api_key, api_secret), kwargs))
        return {
            "orderId": 123,
            "clientOrderId": kwargs["client_order_id"],
            "symbol": kwargs["symbol"],
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "status": "FILLED",
            "origQty": "0.125",
            "executedQty": "0.125",
        }

    def query_algo_order(
        self, api_key: str, api_secret: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("query_algo_order", (api_key, api_secret), kwargs))
        return {
            "algoId": 456,
            "clientAlgoId": kwargs["client_order_id"],
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0",
            "triggerPrice": "59000",
        }

    def cancel_order(self, api_key: str, api_secret: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("cancel_order", (api_key, api_secret), kwargs))
        return {"status": "CANCELED"}

    def cancel_algo_order(
        self, api_key: str, api_secret: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("cancel_algo_order", (api_key, api_secret), kwargs))
        return {"algoStatus": "CANCELED"}


def _broker() -> tuple[BinanceBroker, _AccountClient, _TradingClient]:
    account_client = _AccountClient()
    trading_client = _TradingClient()
    broker = BinanceBroker(
        account_client,  # type: ignore[arg-type]
        trading_client,  # type: ignore[arg-type]
        api_key="api-key",
        api_secret=_API_SECRET,
        account_scope="binance-account-1",
        physical_account_id="binance-uid-12345",
    )
    return broker, account_client, trading_client


def test_binance_facade_satisfies_broker_port_and_normalizes_account() -> None:
    broker, account_client, _ = _broker()

    assert isinstance(broker, Broker)
    assert broker.physical_account_id == "binance-uid-12345"
    snapshot = broker.account_snapshot()

    assert snapshot.account_type is AccountType.USD_M_FUTURES
    assert snapshot.wallet_balance == Decimal("100.25")
    assert snapshot.updated_at == datetime(2026, 8, 5, tzinfo=UTC)
    assert snapshot.observed_at == datetime(2026, 8, 6, tzinfo=UTC)
    assert snapshot.positions[0].direction is PositionDirection.LONG
    assert snapshot.positions[0].quantity == Decimal("0.125")
    assert snapshot.positions[0].position_side is PositionSide.BOTH
    assert account_client.account_calls == [("api-key", "api-secret")]


def test_binance_facade_delegates_without_changing_order_parameters() -> None:
    broker, _, trading_client = _broker()
    order = MarketOrder(
        symbol="btcusdt",
        side=OrderSide.BUY,
        quantity=Decimal("0.125"),
        client_order_id="qd-market-1",
    )

    result = broker.place_market_order(order)

    assert result.status is OrderStatus.FILLED
    assert result.reference.client_order_id == "qd-market-1"
    assert result.exchange_order_id == "123"
    assert result.average_price == Decimal("61000.25")
    assert trading_client.calls[-1] == (
        "place_market_order",
        ("api-key", "api-secret"),
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": Decimal("0.125"),
            "client_order_id": "qd-market-1",
            "position_side": "BOTH",
            "reduce_only": False,
        },
    )


def test_binance_facade_routes_conditional_orders_to_algo_endpoints() -> None:
    broker, _, trading_client = _broker()
    order = TriggerOrder(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.STOP_MARKET,
        trigger_price=Decimal("59000"),
        client_order_id="qd-stop-1",
    )

    placed = broker.place_trigger_order(order)
    reference = OrderReference(client_order_id="qd-stop-1", conditional=True)
    queried = broker.get_order(reference)
    broker.cancel_order(reference)

    assert placed.reference.conditional is True
    assert placed.trigger_price == Decimal("59000")
    assert queried.status is OrderStatus.NEW
    assert [call[0] for call in trading_client.calls] == [
        "place_close_trigger",
        "query_algo_order",
        "cancel_algo_order",
    ]


def test_binance_facade_uses_detected_account_type_for_open_orders() -> None:
    broker, account_client, _ = _broker()

    orders = broker.open_orders()

    assert len(orders) == 1
    assert orders[0].order_type is OrderType.LIMIT
    assert account_client.account_calls == [("api-key", "api-secret")]
    assert account_client.open_orders_calls == [
        ("api-key", "api-secret", "UM_FUTURE")
    ]


def test_binance_errors_are_redacted_at_the_port_boundary() -> None:
    class _FailingTradingClient(_TradingClient):
        def position_mode(self, api_key: str, api_secret: str) -> str:
            raise BinanceAccountClientError("authentication", code=-2015)

    broker = BinanceBroker(
        _AccountClient(),  # type: ignore[arg-type]
        _FailingTradingClient(),  # type: ignore[arg-type]
        api_key="api-key",
        api_secret=_API_SECRET,
        account_scope="binance-account-1",
        physical_account_id="binance-uid-12345",
    )

    with pytest.raises(BrokerError) as caught:
        broker.position_mode()

    assert str(caught.value) == "broker operation failed"
    assert caught.value.category == "authentication"
    assert caught.value.exchange_code == -2015
    assert caught.value.retryable is False


def test_store_market_data_feed_satisfies_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def query(_sql: str, params: tuple[str]) -> list[dict[str, Any]]:
        assert params == ("BTCUSDT",)
        return [{"symbol": "BTCUSDT", "price": "61000.25", "ts": 1_754_332_800}]

    def get_klines(
        symbol: str, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        assert (symbol, timeframe, limit) == ("BTCUSDT", "4h", 2)
        return [
            {
                "open_time": 1_754_318_400_000,
                "open": "60000",
                "high": "61500",
                "low": "59800",
                "close": "61000",
                "volume": "12.5",
            }
        ]

    monkeypatch.setattr(
        "quantdesk_v2.infrastructure.store_market_data.market_store.query", query
    )
    monkeypatch.setattr(
        "quantdesk_v2.infrastructure.store_market_data.market_store.get_klines",
        get_klines,
    )
    feed = StoreMarketDataFeed()

    assert isinstance(feed, MarketDataFeed)
    assert feed.latest_quote("btcusdt").price == Decimal("61000.25")  # type: ignore[union-attr]
    bars = feed.bars("btcusdt", "4H", limit=2)
    assert bars[0].close == Decimal("61000")
    assert bars[0].timeframe == "4h"


def test_domain_orders_reject_hedge_mode_ambiguity() -> None:
    with pytest.raises(ValueError, match="reduce_only"):
        MarketOrder(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.1"),
            client_order_id="qd-close",
            position_side=PositionSide.LONG,
            reduce_only=True,
        )

    with pytest.raises(ValueError, match="require quantity"):
        TriggerOrder(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            order_type=OrderType.STOP_MARKET,
            trigger_price=Decimal("59000"),
            client_order_id="qd-stop",
            position_side=PositionSide.LONG,
        )


def test_instrument_rules_keep_binance_round_down_semantics() -> None:
    broker, _, _ = _broker()

    rules = broker.instrument_rules("btcusdt")

    assert rules.quantity(Decimal("0.12399")) == Decimal("0.123")
    assert rules.price(Decimal("60000.19")) == Decimal("60000.10")
    assert broker.position_mode() is PositionMode.ONE_WAY
