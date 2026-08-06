"""Binance implementation of the exchange-neutral :class:`Broker` port.

The legacy clients remain the only code that signs or transports requests.
This facade binds credentials once and translates their vendor payloads into
domain value objects; it does not alter retry, rounding, or order behavior.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

from ..binance_client import (
    BinanceAccountClient,
    BinanceAccountClientError,
    BinanceAccountSnapshot,
)
from ..binance_trading import BinanceUsdMTradingClient, FuturesSymbolRules
from ..domain.execution import ExecutionMode
from ..domain.trading import (
    AccountSnapshot,
    AccountType,
    BrokerError,
    BrokerOrder,
    InstrumentRules,
    MarketOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionDirection,
    PositionMode,
    PositionSide,
    TriggerOrder,
)

_OPEN_STATUSES = {"ACCEPTED", "NEW", "PARTIALLY_FILLED", "PENDING_NEW", "TRIGGERING", "WORKING"}
_FILLED_STATUSES = {"FILLED", "FINISHED", "TRIGGERED"}
_CANCELED_STATUSES = {"CANCELED", "CANCELLED"}
_EXPIRED_STATUSES = {"EXPIRED", "EXPIRED_IN_MATCH"}
_PHYSICAL_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,190}")


def _execution_identifier(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 191:
        raise ValueError(f"{name} must contain 1-191 characters")
    if name == "physical_account_id" and not _PHYSICAL_ACCOUNT_PATTERN.fullmatch(
        normalized
    ):
        raise ValueError("physical_account_id contains invalid characters")
    return normalized


class BinanceBroker:
    """A USD-M Binance broker account backed by the existing narrow clients."""

    __slots__ = (
        "_account_scope",
        "_account_client",
        "_account_type",
        "_api_key",
        "_api_secret",
        "_physical_account_id",
        "_trading_client",
    )

    def __init__(
        self,
        account_client: BinanceAccountClient,
        trading_client: BinanceUsdMTradingClient,
        *,
        api_key: str,
        api_secret: str,
        account_scope: str,
        physical_account_id: str,
    ) -> None:
        # Credential validation and redaction remain owned by the existing clients.
        self._account_client = account_client
        self._trading_client = trading_client
        self._api_key = api_key
        self._api_secret = api_secret
        self._account_scope = _execution_identifier(account_scope, "account_scope")
        # This must be the exchange wallet/UID identity supplied by credential
        # onboarding. API keys can rotate and multiple keys may share one wallet.
        self._physical_account_id = _execution_identifier(
            physical_account_id,
            "physical_account_id",
        )
        self._account_type: AccountType | None = None

    @property
    def name(self) -> str:
        return "binance-usdm"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.LIVE

    @property
    def market(self) -> str:
        return "binance_usdm"

    @property
    def account_scope(self) -> str:
        return self._account_scope

    @property
    def physical_account_id(self) -> str:
        return self._physical_account_id

    def account_snapshot(self) -> AccountSnapshot:
        with _translate_errors():
            payload = self._account_client.account(self._api_key, self._api_secret)
        snapshot = _account_snapshot(payload)
        self._account_type = snapshot.account_type
        return snapshot

    def open_orders(self) -> tuple[BrokerOrder, ...]:
        account_type = self._account_type
        if account_type is None:
            account_type = self.account_snapshot().account_type
        with _translate_errors():
            payloads = self._account_client.open_orders(
                self._api_key,
                self._api_secret,
                account_type=account_type.value,
            )
        return tuple(_broker_order(payload) for payload in payloads)

    def instrument_rules(self, symbol: str) -> InstrumentRules:
        with _translate_errors():
            payload = self._trading_client.symbol_rules(symbol)
        return _instrument_rules(payload)

    def position_mode(self) -> PositionMode:
        with _translate_errors():
            value = self._trading_client.position_mode(self._api_key, self._api_secret)
        return PositionMode(value)

    def configure_leverage(self, symbol: str, leverage: int) -> None:
        with _translate_errors():
            self._trading_client.change_leverage(
                self._api_key,
                self._api_secret,
                symbol=symbol,
                leverage=leverage,
            )

    def place_market_order(self, order: MarketOrder) -> BrokerOrder:
        with _translate_errors():
            payload = self._trading_client.place_market_order(
                self._api_key,
                self._api_secret,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                client_order_id=order.client_order_id,
                position_side=order.position_side.value,
                reduce_only=order.reduce_only,
            )
        return _broker_order(payload, fallback=order)

    def place_trigger_order(self, order: TriggerOrder) -> BrokerOrder:
        with _translate_errors():
            payload = self._trading_client.place_close_trigger(
                self._api_key,
                self._api_secret,
                symbol=order.symbol,
                side=order.side.value,
                order_type=order.order_type.value,
                stop_price=order.trigger_price,
                client_order_id=order.client_order_id,
                position_side=order.position_side.value,
                quantity=order.quantity,
            )
        return _broker_order(payload, fallback=order, conditional=True)

    def get_order(self, reference: OrderReference) -> BrokerOrder:
        with _translate_errors():
            if reference.conditional:
                payload = self._trading_client.query_algo_order(
                    self._api_key,
                    self._api_secret,
                    client_order_id=reference.client_order_id,
                )
            else:
                payload = self._trading_client.query_order(
                    self._api_key,
                    self._api_secret,
                    symbol=_regular_order_symbol(reference),
                    client_order_id=reference.client_order_id,
                )
        return _broker_order(payload, reference=reference)

    def cancel_order(self, reference: OrderReference) -> None:
        with _translate_errors():
            if reference.conditional:
                self._trading_client.cancel_algo_order(
                    self._api_key,
                    self._api_secret,
                    client_order_id=reference.client_order_id,
                )
            else:
                self._trading_client.cancel_order(
                    self._api_key,
                    self._api_secret,
                    symbol=_regular_order_symbol(reference),
                    client_order_id=reference.client_order_id,
                )


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except BinanceAccountClientError as exc:
        raise BrokerError(exc.category, exchange_code=exc.code) from None


def _account_snapshot(payload: BinanceAccountSnapshot) -> AccountSnapshot:
    try:
        account_type = AccountType(payload.account_type)
        positions = tuple(_position(item) for item in payload.positions)
        return AccountSnapshot(
            account_type=account_type,
            can_trade=payload.can_trade,
            wallet_balance=payload.wallet_balance,
            available_balance=payload.available_balance,
            unrealized_pnl=payload.unrealized_pnl,
            currency=payload.currency,
            updated_at=payload.updated_at,
            positions=positions,
            observed_at=payload.observed_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("invalid_response") from exc


def _instrument_rules(payload: FuturesSymbolRules) -> InstrumentRules:
    try:
        return InstrumentRules(
            symbol=payload.symbol,
            quantity_step=payload.market_step_size,
            minimum_quantity=payload.min_quantity,
            maximum_quantity=payload.max_quantity,
            price_tick=payload.tick_size,
            minimum_notional=payload.min_notional,
        )
    except (TypeError, ValueError) as exc:
        raise BrokerError("invalid_response") from exc


def _position(payload: Mapping[str, Any]) -> Position:
    try:
        return Position(
            symbol=str(payload["symbol"]),
            direction=PositionDirection(str(payload["side"]).lower()),
            position_side=PositionSide(str(payload.get("position_side") or "BOTH").upper()),
            quantity=_decimal(payload["amt"], "position quantity"),
            entry_price=_optional_decimal(payload.get("entry_price"), "entry price"),
            mark_price=_optional_decimal(payload.get("mark_price"), "mark price"),
            break_even_price=_optional_decimal(
                payload.get("break_even_price"), "break-even price"
            ),
            liquidation_price=_optional_decimal(
                payload.get("liquidation_price"), "liquidation price"
            ),
            notional=_optional_decimal(payload.get("notional"), "notional"),
            initial_margin=_optional_decimal(payload.get("initial_margin"), "initial margin"),
            maintenance_margin=_optional_decimal(
                payload.get("maintenance_margin"), "maintenance margin"
            ),
            unrealized_pnl=_optional_decimal(payload.get("upnl"), "unrealized PnL"),
            leverage=_optional_int(payload.get("leverage"), "leverage"),
            updated_at_ms=_optional_int(payload.get("ts"), "position timestamp"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("invalid_response") from exc


def _broker_order(
    payload: Mapping[str, Any],
    *,
    fallback: MarketOrder | TriggerOrder | None = None,
    reference: OrderReference | None = None,
    conditional: bool | None = None,
) -> BrokerOrder:
    try:
        is_conditional = (
            conditional
            if conditional is not None
            else reference.conditional
            if reference is not None
            else payload.get("conditional") is True
            or any(key in payload for key in ("algoId", "algoStatus", "clientAlgoId"))
        )
        client_order_id = str(
            payload.get("client_order_id")
            or payload.get("clientAlgoId")
            or payload.get("clientOrderId")
            or (fallback.client_order_id if fallback is not None else "")
            or (reference.client_order_id if reference is not None else "")
        )
        symbol = str(
            payload.get("symbol")
            or (fallback.symbol if fallback is not None else "")
            or (reference.symbol if reference is not None else "")
        )
        resolved_reference = reference or OrderReference(
            client_order_id=client_order_id,
            symbol=symbol or None,
            conditional=is_conditional,
        )
        raw_status = str(
            payload.get("status")
            or payload.get("algoStatus")
            or payload.get("strategyStatus")
            or ""
        ).upper()
        raw_type = str(
            payload.get("type")
            or payload.get("orderType")
            or payload.get("strategyType")
            or (fallback.order_type.value if isinstance(fallback, TriggerOrder) else "MARKET")
        ).upper()
        side = str(payload.get("side") or (fallback.side.value if fallback else "")).upper()
        position_side = str(
            payload.get("position_side")
            or payload.get("positionSide")
            or (fallback.position_side.value if fallback else "BOTH")
        ).upper()
        quantity_fallback = (
            fallback.quantity
            if fallback is not None and fallback.quantity is not None
            else Decimal(0)
        )
        quantity = _decimal(
            payload.get("quantity", payload.get("origQty", quantity_fallback)),
            "order quantity",
        )
        return BrokerOrder(
            reference=resolved_reference,
            exchange_order_id=_exchange_order_id(payload),
            symbol=symbol,
            side=OrderSide(side),
            position_side=PositionSide(position_side),
            order_type=_order_type(raw_type),
            status=_order_status(raw_status),
            exchange_status=raw_status,
            quantity=quantity,
            executed_quantity=_decimal(
                payload.get("executed_quantity", payload.get("executedQty", 0)),
                "executed quantity",
            ),
            average_price=_optional_decimal(
                payload.get("average_price", payload.get("avgPrice")), "average price"
            ),
            trigger_price=_optional_decimal(
                payload.get(
                    "stop_price",
                    payload.get("triggerPrice", fallback.trigger_price if isinstance(fallback, TriggerOrder) else None),
                ),
                "trigger price",
            ),
            reduce_only=(
                payload.get("reduce_only") is True
                or payload.get("reduceOnly") is True
                or isinstance(fallback, MarketOrder)
                and fallback.reduce_only
            ),
        )
    except BrokerError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("invalid_response") from exc


def _order_type(raw: str) -> OrderType:
    try:
        return OrderType(raw)
    except ValueError:
        return OrderType.UNKNOWN


def _order_status(raw: str) -> OrderStatus:
    if raw in _OPEN_STATUSES:
        return OrderStatus.PARTIALLY_FILLED if raw == "PARTIALLY_FILLED" else OrderStatus.NEW
    if raw in _FILLED_STATUSES:
        return OrderStatus.FILLED
    if raw in _CANCELED_STATUSES:
        return OrderStatus.CANCELED
    if raw in _EXPIRED_STATUSES:
        return OrderStatus.EXPIRED
    if raw == "REJECTED":
        return OrderStatus.REJECTED
    return OrderStatus.UNKNOWN


def _exchange_order_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("order_id", payload.get("orderId"))
    if value is None:
        value = payload.get("algoId", payload.get("strategyId"))
    return str(value) if value is not None else None


def _regular_order_symbol(reference: OrderReference) -> str:
    if reference.symbol is None:
        # Defensive guard for callers that bypass dataclass construction.
        raise ValueError("a regular order reference requires symbol")
    return reference.symbol


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise BrokerError("invalid_response")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BrokerError("invalid_response") from None
    if not parsed.is_finite():
        raise BrokerError("invalid_response")
    return parsed


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, name)


def _optional_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BrokerError("invalid_response")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise BrokerError("invalid_response") from None
    return parsed
