from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


def _positive(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _nonnegative(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_total_notional: Decimal
    max_leverage: int
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_market_age_seconds: int = 10

    def __post_init__(self) -> None:
        _positive(self.max_order_notional, "max_order_notional")
        _positive(self.max_symbol_notional, "max_symbol_notional")
        _positive(self.max_total_notional, "max_total_notional")
        _positive(self.max_daily_loss_pct, "max_daily_loss_pct")
        _positive(self.max_drawdown_pct, "max_drawdown_pct")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be at least 1")
        if self.max_market_age_seconds < 1:
            raise ValueError("max_market_age_seconds must be at least 1")


@dataclass(frozen=True, slots=True)
class OrderProposal:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    leverage: int
    reduce_only: bool
    account_equity: Decimal
    symbol_notional: Decimal
    total_notional: Decimal
    daily_pnl: Decimal
    drawdown_pct: Decimal
    market_age_seconds: int

    def __post_init__(self) -> None:
        if not self.symbol or len(self.symbol) > 32:
            raise ValueError("symbol must contain 1 to 32 characters")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        _positive(self.quantity, "quantity")
        _positive(self.price, "price")
        _positive(self.account_equity, "account_equity")
        _nonnegative(self.symbol_notional, "symbol_notional")
        _nonnegative(self.total_notional, "total_notional")
        _nonnegative(self.drawdown_pct, "drawdown_pct")
        if self.leverage < 1:
            raise ValueError("leverage must be at least 1")
        if self.market_age_seconds < 0:
            raise ValueError("market_age_seconds must be nonnegative")

    @property
    def order_notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason_codes: tuple[str, ...]
    order_notional: Decimal
    projected_symbol_notional: Decimal
    projected_total_notional: Decimal


def evaluate_pre_trade(proposal: OrderProposal, limits: RiskLimits) -> RiskDecision:
    """Evaluate deterministic pre-trade limits without floats or side effects."""

    order_notional = proposal.order_notional
    exposure_delta = ZERO if proposal.reduce_only else order_notional
    projected_symbol = proposal.symbol_notional + exposure_delta
    projected_total = proposal.total_notional + exposure_delta
    reasons: list[str] = []

    if proposal.market_age_seconds > limits.max_market_age_seconds:
        reasons.append("MARKET_DATA_STALE")
    if proposal.leverage > limits.max_leverage:
        reasons.append("LEVERAGE_LIMIT")
    if not proposal.reduce_only and order_notional > limits.max_order_notional:
        reasons.append("ORDER_NOTIONAL_LIMIT")
    if not proposal.reduce_only and projected_symbol > limits.max_symbol_notional:
        reasons.append("SYMBOL_EXPOSURE_LIMIT")
    if not proposal.reduce_only and projected_total > limits.max_total_notional:
        reasons.append("TOTAL_EXPOSURE_LIMIT")

    daily_loss_pct = max(ZERO, -proposal.daily_pnl / proposal.account_equity * ONE_HUNDRED)
    if daily_loss_pct >= limits.max_daily_loss_pct:
        reasons.append("DAILY_LOSS_LIMIT")
    if proposal.drawdown_pct >= limits.max_drawdown_pct:
        reasons.append("DRAWDOWN_LIMIT")

    return RiskDecision(
        approved=not reasons,
        reason_codes=tuple(reasons),
        order_notional=order_notional,
        projected_symbol_notional=projected_symbol,
        projected_total_notional=projected_total,
    )
