"""Exchange-neutral trading and market-data value objects.

These objects intentionally contain no database, HTTP, or Binance concepts.  A
broker adapter converts exchange payloads at the infrastructure boundary so
strategy and risk code do not need to depend on vendor-shaped dictionaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

_SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"[A-Za-z0-9._:/-]{1,36}")


class AccountType(StrEnum):
    USD_M_FUTURES = "UM_FUTURE"
    PORTFOLIO_MARGIN = "PORTFOLIO_MARGIN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    BOTH = "BOTH"
    LONG = "LONG"
    SHORT = "SHORT"


class PositionDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class PositionMode(StrEnum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    UNKNOWN = "UNKNOWN"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class BrokerError(RuntimeError):
    """Redacted infrastructure failure exposed through the broker port."""

    def __init__(self, category: str, *, exchange_code: int | None = None) -> None:
        super().__init__("broker operation failed")
        self.category = str(category or "unknown")[:64]
        self.exchange_code = exchange_code

    @property
    def retryable(self) -> bool:
        return self.category in {"network", "rate_limit", "timeout", "upstream"}


def _symbol(value: str) -> str:
    normalized = str(value).strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("invalid instrument symbol")
    return normalized


def _client_order_id(value: str) -> str:
    normalized = str(value).strip()
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(normalized):
        raise ValueError("invalid client order id")
    return normalized


def _finite_decimal(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_decimal(value: Decimal | None, name: str) -> Decimal | None:
    if value is None:
        return None
    return _finite_decimal(value, name)


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    symbol: str
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    price_tick: Decimal
    minimum_notional: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        for name in (
            "quantity_step",
            "minimum_quantity",
            "maximum_quantity",
            "price_tick",
            "minimum_notional",
        ):
            _finite_decimal(getattr(self, name), name, positive=True)
        if self.maximum_quantity < self.minimum_quantity:
            raise ValueError("maximum quantity must not be below minimum quantity")

    def quantity(self, raw: Decimal) -> Decimal:
        """Round a quantity down exactly as an exchange lot-size filter requires."""
        _finite_decimal(raw, "quantity", positive=True)
        steps = (raw / self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        value = steps * self.quantity_step
        if value < self.minimum_quantity or value > self.maximum_quantity:
            raise ValueError("quantity is outside instrument limits")
        return value

    def price(self, raw: Decimal) -> Decimal:
        """Round a price down to the instrument tick size."""
        _finite_decimal(raw, "price", positive=True)
        ticks = (raw / self.price_tick).to_integral_value(rounding=ROUND_DOWN)
        value = ticks * self.price_tick
        if value <= 0:
            raise ValueError("price is below instrument tick size")
        return value


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    direction: PositionDirection
    position_side: PositionSide
    quantity: Decimal
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    break_even_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    notional: Decimal | None = None
    initial_margin: Decimal | None = None
    maintenance_margin: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    leverage: int | None = None
    updated_at_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        _finite_decimal(self.quantity, "quantity", positive=True)
        for name in (
            "entry_price",
            "mark_price",
            "break_even_price",
            "liquidation_price",
            "notional",
            "initial_margin",
            "maintenance_margin",
            "unrealized_pnl",
        ):
            _optional_decimal(getattr(self, name), name)
        if self.leverage is not None and (
            isinstance(self.leverage, bool) or self.leverage <= 0
        ):
            raise ValueError("leverage must be a positive integer")
        if self.updated_at_ms is not None and (
            isinstance(self.updated_at_ms, bool) or self.updated_at_ms < 0
        ):
            raise ValueError("updated_at_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_type: AccountType
    can_trade: bool | None
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    currency: str
    updated_at: datetime
    positions: tuple[Position, ...] = ()
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("wallet_balance", "available_balance", "unrealized_pnl"):
            _finite_decimal(getattr(self, name), name)
        normalized_currency = str(self.currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3,12}", normalized_currency):
            raise ValueError("invalid account currency")
        object.__setattr__(self, "currency", normalized_currency)
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        observed_at = self.observed_at or self.updated_at
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class MarketOrder:
    symbol: str
    side: OrderSide
    quantity: Decimal
    client_order_id: str
    position_side: PositionSide = PositionSide.BOTH
    reduce_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "client_order_id", _client_order_id(self.client_order_id))
        _finite_decimal(self.quantity, "quantity", positive=True)
        if self.reduce_only and self.position_side is not PositionSide.BOTH:
            raise ValueError("reduce_only cannot be used for a hedge-mode order")


@dataclass(frozen=True, slots=True)
class TriggerOrder:
    symbol: str
    side: OrderSide
    order_type: OrderType
    trigger_price: Decimal
    client_order_id: str
    position_side: PositionSide = PositionSide.BOTH
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "client_order_id", _client_order_id(self.client_order_id))
        if self.order_type not in {OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET}:
            raise ValueError("unsupported trigger order type")
        _finite_decimal(self.trigger_price, "trigger_price", positive=True)
        if self.quantity is not None:
            _finite_decimal(self.quantity, "quantity", positive=True)
        if self.position_side is not PositionSide.BOTH and self.quantity is None:
            raise ValueError("hedge-mode trigger orders require quantity")


@dataclass(frozen=True, slots=True)
class OrderReference:
    client_order_id: str
    symbol: str | None = None
    conditional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", _client_order_id(self.client_order_id))
        if self.symbol is not None:
            object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not self.conditional and self.symbol is None:
            raise ValueError("a regular order reference requires symbol")


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    reference: OrderReference
    exchange_order_id: str | None
    symbol: str
    side: OrderSide
    position_side: PositionSide
    order_type: OrderType
    status: OrderStatus
    exchange_status: str
    quantity: Decimal
    executed_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    trigger_price: Decimal | None = None
    reduce_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        _finite_decimal(self.quantity, "quantity")
        _finite_decimal(self.executed_quantity, "executed_quantity")
        _optional_decimal(self.average_price, "average_price")
        _optional_decimal(self.trigger_price, "trigger_price")
        if self.quantity < 0 or self.executed_quantity < 0:
            raise ValueError("order quantities must be non-negative")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        _finite_decimal(self.price, "price", positive=True)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: str
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        normalized_timeframe = str(self.timeframe).strip().lower()
        if not re.fullmatch(r"[1-9][0-9]*[mhdw]", normalized_timeframe):
            raise ValueError("invalid market-data timeframe")
        object.__setattr__(self, "timeframe", normalized_timeframe)
        if isinstance(self.open_time_ms, bool) or self.open_time_ms < 0:
            raise ValueError("open_time_ms must be non-negative")
        for name in ("open", "high", "low", "close"):
            _finite_decimal(getattr(self, name), name, positive=True)
        _finite_decimal(self.volume, "volume")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar high/low does not contain open and close")
        if self.high < self.low:
            raise ValueError("bar high must not be below low")
