"""Pure risk controls shared by the live trading worker.

The functions in this module deliberately do not access the database or Binance.
They turn an already observed account/market snapshot into deterministic decisions,
which keeps the order path auditable and makes every rejection easy to test.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True, slots=True)
class LiveRiskPolicy:
    """Conservative defaults for a newly enabled real-money deployment.

    ``max_positions`` remains a separate account setting and may be 20.  These
    limits govern actual risk, so reaching 20 positions is never a target.
    """

    risk_per_trade_pct: Decimal = Decimal("0.50")
    max_total_risk_pct: Decimal = Decimal("4.00")
    max_margin_per_trade_pct: Decimal = Decimal("2.00")
    max_leverage: int = 10
    max_cluster_positions: int = 2
    max_high_risk_positions: int = 1
    max_ticker_age_seconds: int = 120
    max_signal_age_seconds: int = 18_000
    signal_valid_bars: int = 1
    liquidation_buffer_pct: Decimal = Decimal("1.50")
    block_high_risk_products: bool = True
    round_trip_cost_bps: Decimal = Decimal("16")
    short_risk_multiplier: Decimal = Decimal("0.50")
    high_risk_multiplier: Decimal = Decimal("0.50")
    daily_loss_limit_pct: Decimal = Decimal("2.00")
    max_drawdown_pct: Decimal = Decimal("6.00")


@dataclass(frozen=True, slots=True)
class PositionSizeDecision:
    quantity: Decimal
    effective_leverage: int
    risk_budget: Decimal
    estimated_loss_at_stop: Decimal
    notional: Decimal
    initial_margin: Decimal
    limiting_factor: Literal["risk_budget", "margin_budget", "portfolio_risk_cap", "invalid_input"]

    @property
    def allowed(self) -> bool:
        return self.quantity > ZERO


def leverage_for_stop_distance(
    *,
    entry_price: Decimal | float | int | str,
    stop_distance: Decimal | float | int | str,
    requested_leverage: int,
    policy: LiveRiskPolicy | None = None,
    conservative_maintenance_margin_pct: Decimal | float | int | str = Decimal("1"),
) -> int:
    """Cap leverage so an ATR stop precedes estimated liquidation.

    Binance's exact maintenance margin depends on the symbol/notional bracket
    and cross-margin collateral. This pre-trade estimate is deliberately
    conservative; the caller must still verify the exchange liquidation price
    immediately after the fill.
    """

    selected = policy or LiveRiskPolicy()
    entry = _decimal(entry_price, "entry_price", positive=True)
    stop = _decimal(stop_distance, "stop_distance", positive=True)
    maintenance = _decimal(
        conservative_maintenance_margin_pct,
        "conservative_maintenance_margin_pct",
    )
    if maintenance < ZERO:
        raise ValueError("conservative_maintenance_margin_pct must not be negative")
    if isinstance(requested_leverage, bool):
        raise ValueError("requested_leverage must be a positive integer")
    requested = int(requested_leverage)
    if requested < 1:
        raise ValueError("requested_leverage must be a positive integer")
    stop_pct = stop / entry * ONE_HUNDRED
    required_distance_pct = stop_pct + selected.liquidation_buffer_pct + maintenance
    if required_distance_pct >= ONE_HUNDRED:
        return 1
    estimated_safe = int(ONE_HUNDRED / required_distance_pct)
    return max(1, min(requested, selected.max_leverage, estimated_safe))


@dataclass(frozen=True, slots=True)
class OpenPositionRisk:
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    exit_cost_bps: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class PortfolioRiskDecision:
    allowed: bool
    current_risk: Decimal
    proposed_risk: Decimal
    risk_cap: Decimal
    remaining_risk: Decimal


@dataclass(frozen=True, slots=True)
class LiquidationSafetyDecision:
    safe: bool
    reason: str
    stop_distance_pct: Decimal | None
    liquidation_distance_pct: Decimal | None
    stop_to_liquidation_buffer_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    fresh: bool
    reason: str
    age_seconds: float
    valid_from: float | None = None
    valid_until: float | None = None


@dataclass(frozen=True, slots=True)
class SymbolRiskProfile:
    symbol: str
    correlation_groups: tuple[str, ...]
    high_risk: bool
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SymbolAdmissionDecision:
    allowed: bool
    reason: str
    profile: SymbolRiskProfile
    group_counts: tuple[tuple[str, int], ...]
    high_risk_count: int


@dataclass(frozen=True, slots=True)
class AccountRiskDecision:
    allow_new_entries: bool
    daily_loss_pct: Decimal
    drawdown_pct: Decimal
    reasons: tuple[str, ...]


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or (positive and result <= ZERO):
        condition = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {condition}")
    return result


def _bounded_decimal(
    config: Mapping[str, Any],
    key: str,
    default: Decimal,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    try:
        candidate = _decimal(config.get(key, default), key)
    except ValueError:
        candidate = default
    return min(maximum, max(minimum, candidate))


def _bounded_int(
    config: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        candidate = int(value)
    except (TypeError, ValueError, OverflowError):
        candidate = default
    return min(maximum, max(minimum, candidate))


def _boolean(config: Mapping[str, Any], key: str, default: bool) -> bool:
    candidate = config.get(key, default)
    return candidate if isinstance(candidate, bool) else default


def policy_from_config(config: Mapping[str, Any] | None) -> LiveRiskPolicy:
    """Normalize persisted risk overrides without mutating the source mapping."""

    raw = config if isinstance(config, Mapping) else {}
    # Keep the old position_size_pct as a backwards-compatible *margin ceiling*.
    # It no longer defines the amount of account equity that may be lost.
    margin_default = _bounded_decimal(
        raw, "position_size_pct", Decimal("2"), Decimal("0.10"), Decimal("10")
    )
    return LiveRiskPolicy(
        risk_per_trade_pct=_bounded_decimal(
            raw,
            "risk_per_trade_pct",
            Decimal("0.50"),
            Decimal("0.05"),
            Decimal("1.00"),
        ),
        max_total_risk_pct=_bounded_decimal(
            raw,
            "max_total_risk_pct",
            Decimal("4.00"),
            Decimal("0.25"),
            Decimal("8.00"),
        ),
        max_margin_per_trade_pct=_bounded_decimal(
            raw,
            "max_margin_per_trade_pct",
            margin_default,
            Decimal("0.10"),
            Decimal("10.00"),
        ),
        max_leverage=_bounded_int(raw, "risk_max_leverage", 10, 1, 20),
        max_cluster_positions=_bounded_int(raw, "max_cluster_positions", 2, 1, 20),
        max_high_risk_positions=_bounded_int(raw, "max_high_risk_positions", 1, 0, 20),
        max_ticker_age_seconds=_bounded_int(raw, "max_ticker_age_seconds", 120, 5, 900),
        max_signal_age_seconds=_bounded_int(raw, "max_signal_age_seconds", 18_000, 60, 172_800),
        signal_valid_bars=_bounded_int(raw, "signal_valid_bars", 1, 1, 10),
        liquidation_buffer_pct=_bounded_decimal(
            raw,
            "liquidation_buffer_pct",
            Decimal("1.50"),
            Decimal("0.10"),
            Decimal("10.00"),
        ),
        block_high_risk_products=_boolean(raw, "block_high_risk_products", True),
        round_trip_cost_bps=_bounded_decimal(
            raw,
            "round_trip_cost_bps",
            Decimal("16"),
            ZERO,
            Decimal("500"),
        ),
        short_risk_multiplier=_bounded_decimal(
            raw,
            "short_risk_multiplier",
            Decimal("0.50"),
            ZERO,
            Decimal("1.00"),
        ),
        high_risk_multiplier=_bounded_decimal(
            raw,
            "high_risk_multiplier",
            Decimal("0.50"),
            Decimal("0.10"),
            Decimal("1.00"),
        ),
        daily_loss_limit_pct=_bounded_decimal(
            raw,
            "daily_loss_limit_pct",
            Decimal("2.00"),
            Decimal("0.25"),
            Decimal("20.00"),
        ),
        max_drawdown_pct=_bounded_decimal(
            raw,
            "max_drawdown_pct",
            Decimal("6.00"),
            Decimal("1.00"),
            Decimal("30.00"),
        ),
    )


def tighten_policy_with_strategy(
    policy: LiveRiskPolicy,
    risk_proposal: Mapping[str, Any],
) -> LiveRiskPolicy:
    """Apply a full strategy's immutable risk proposal as a tightening cap.

    Account controls may make a strategy safer, but can never enlarge the
    leverage, margin, or stop-loss budget declared by the published strategy.
    """

    required = {"risk_per_trade_pct", "max_margin_pct", "max_leverage"}
    if not required.issubset(risk_proposal):
        raise ValueError("strategy risk proposal is incomplete")
    risk_per_trade = _decimal(
        risk_proposal["risk_per_trade_pct"],
        "risk_per_trade_pct",
        positive=True,
    )
    margin = _decimal(risk_proposal["max_margin_pct"], "max_margin_pct", positive=True)
    raw_leverage = risk_proposal["max_leverage"]
    if isinstance(raw_leverage, bool):
        raise ValueError("max_leverage must be a positive integer")
    try:
        leverage = int(raw_leverage)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_leverage must be a positive integer") from exc
    if leverage < 1:
        raise ValueError("max_leverage must be a positive integer")
    return replace(
        policy,
        risk_per_trade_pct=min(policy.risk_per_trade_pct, risk_per_trade),
        max_margin_per_trade_pct=min(policy.max_margin_per_trade_pct, margin),
        max_leverage=min(policy.max_leverage, leverage),
    )


def atr_risk_position_size(
    *,
    equity: Decimal | float | int | str,
    available_balance: Decimal | float | int | str,
    entry_price: Decimal | float | int | str,
    stop_distance: Decimal | float | int | str,
    requested_leverage: int,
    current_open_risk: Decimal | float | int | str = ZERO,
    direction: int = 1,
    high_risk: bool = False,
    policy: LiveRiskPolicy | None = None,
) -> PositionSizeDecision:
    """Size a position from its ATR stop loss instead of a fixed notional.

    Costs are included in the loss estimate.  Exchange lot-size rounding is
    intentionally left to ``symbol_rules.quantity`` in the caller; it must round
    down and then re-check minimum notional.
    """

    selected = policy or LiveRiskPolicy()
    try:
        account_equity = _decimal(equity, "equity", positive=True)
        available = max(ZERO, _decimal(available_balance, "available_balance"))
        entry = _decimal(entry_price, "entry_price", positive=True)
        stop = _decimal(stop_distance, "stop_distance", positive=True)
        open_risk = max(ZERO, _decimal(current_open_risk, "current_open_risk"))
        leverage = int(requested_leverage)
        if isinstance(requested_leverage, bool) or leverage < 1:
            raise ValueError("requested_leverage must be a positive integer")
    except (TypeError, ValueError, OverflowError):
        return PositionSizeDecision(ZERO, 1, ZERO, ZERO, ZERO, ZERO, "invalid_input")

    leverage = min(leverage, selected.max_leverage)
    multiplier = Decimal("1")
    if direction < 0:
        multiplier *= selected.short_risk_multiplier
    if high_risk:
        multiplier *= selected.high_risk_multiplier

    per_trade_budget = account_equity * selected.risk_per_trade_pct / ONE_HUNDRED * multiplier
    portfolio_cap = account_equity * selected.max_total_risk_pct / ONE_HUNDRED
    remaining_portfolio_budget = max(ZERO, portfolio_cap - open_risk)
    risk_budget = min(per_trade_budget, remaining_portfolio_budget)
    if risk_budget <= ZERO:
        return PositionSizeDecision(ZERO, leverage, ZERO, ZERO, ZERO, ZERO, "portfolio_risk_cap")

    loss_per_unit = stop + entry * selected.round_trip_cost_bps / TEN_THOUSAND
    quantity_by_risk = risk_budget / loss_per_unit
    margin_budget = min(
        available,
        account_equity * selected.max_margin_per_trade_pct / ONE_HUNDRED,
    )
    quantity_by_margin = margin_budget * Decimal(leverage) / entry
    quantity = max(ZERO, min(quantity_by_risk, quantity_by_margin))
    limiting_factor: Literal["risk_budget", "margin_budget"] = (
        "risk_budget" if quantity_by_risk <= quantity_by_margin else "margin_budget"
    )
    notional = quantity * entry
    initial_margin = notional / Decimal(leverage)
    estimated_loss = quantity * loss_per_unit
    return PositionSizeDecision(
        quantity,
        leverage,
        risk_budget,
        estimated_loss,
        notional,
        initial_margin,
        limiting_factor,
    )


def estimate_open_position_risk(position: OpenPositionRisk) -> Decimal:
    quantity = abs(_decimal(position.quantity, "quantity"))
    entry = _decimal(position.entry_price, "entry_price", positive=True)
    stop = _decimal(position.stop_price, "stop_price", positive=True)
    costs = _decimal(position.exit_cost_bps, "exit_cost_bps")
    if costs < ZERO:
        raise ValueError("exit_cost_bps must not be negative")
    return quantity * (abs(entry - stop) + entry * costs / TEN_THOUSAND)


def total_open_risk(positions: Iterable[OpenPositionRisk]) -> Decimal:
    return sum((estimate_open_position_risk(item) for item in positions), start=ZERO)


def portfolio_risk_admission(
    *,
    equity: Decimal | float | int | str,
    current_risk: Decimal | float | int | str,
    proposed_risk: Decimal | float | int | str,
    cap_pct: Decimal | float | int | str,
) -> PortfolioRiskDecision:
    account_equity = _decimal(equity, "equity", positive=True)
    current = max(ZERO, _decimal(current_risk, "current_risk"))
    proposed = max(ZERO, _decimal(proposed_risk, "proposed_risk"))
    cap = account_equity * _decimal(cap_pct, "cap_pct") / ONE_HUNDRED
    if cap < ZERO:
        raise ValueError("cap_pct must not be negative")
    remaining = max(ZERO, cap - current)
    return PortfolioRiskDecision(proposed <= remaining, current, proposed, cap, remaining)


def liquidation_stop_safety(
    *,
    entry_price: Decimal | float | int | str,
    stop_price: Decimal | float | int | str,
    liquidation_price: Decimal | float | int | str | None,
    direction: int,
    min_buffer_pct: Decimal | float | int | str = Decimal("1.5"),
) -> LiquidationSafetyDecision:
    """Require the stop to execute before liquidation with an extra price buffer.

    A missing liquidation price is rejected instead of making a potentially
    unsafe approximation for cross-margin accounts.
    """

    entry = _decimal(entry_price, "entry_price", positive=True)
    stop = _decimal(stop_price, "stop_price", positive=True)
    required_buffer = _decimal(min_buffer_pct, "min_buffer_pct")
    if required_buffer < ZERO:
        raise ValueError("min_buffer_pct must not be negative")
    if liquidation_price is None:
        return LiquidationSafetyDecision(False, "missing_liquidation_price", None, None, None)
    liquidation = _decimal(liquidation_price, "liquidation_price", positive=True)
    if direction not in {-1, 1}:
        return LiquidationSafetyDecision(False, "invalid_direction", None, None, None)

    stop_distance_pct = abs(entry - stop) / entry * ONE_HUNDRED
    liquidation_distance_pct = abs(entry - liquidation) / entry * ONE_HUNDRED
    if direction > 0:
        if stop >= entry:
            reason = "stop_not_adverse"
        elif liquidation >= entry:
            reason = "liquidation_not_adverse"
        elif stop <= liquidation:
            reason = "stop_beyond_liquidation"
        else:
            reason = "ok"
    else:
        if stop <= entry:
            reason = "stop_not_adverse"
        elif liquidation <= entry:
            reason = "liquidation_not_adverse"
        elif stop >= liquidation:
            reason = "stop_beyond_liquidation"
        else:
            reason = "ok"
    buffer_pct = abs(stop - liquidation) / entry * ONE_HUNDRED
    if reason == "ok" and buffer_pct < required_buffer:
        reason = "liquidation_buffer_too_small"
    return LiquidationSafetyDecision(
        reason == "ok",
        reason,
        stop_distance_pct,
        liquidation_distance_pct,
        buffer_pct,
    )


def _epoch_seconds(value: int | float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp must be numeric")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("timestamp must be finite and non-negative")
    # Binance/store timestamps may be seconds, milliseconds, microseconds, or ns.
    while timestamp >= 100_000_000_000:
        timestamp /= 1000
    return timestamp


def market_data_freshness(
    timestamp: int | float,
    *,
    now: int | float | None = None,
    max_age_seconds: int = 120,
    max_future_skew_seconds: int = 5,
) -> FreshnessDecision:
    observed = _epoch_seconds(timestamp)
    current = _epoch_seconds(now) if now is not None else time.time()
    age = current - observed
    if age < -max_future_skew_seconds:
        return FreshnessDecision(False, "timestamp_in_future", age)
    if age > max_age_seconds:
        return FreshnessDecision(False, "market_data_stale", age)
    return FreshnessDecision(True, "ok", age)


def signal_freshness(
    timestamp: int | float,
    *,
    now: int | float | None = None,
    max_age_seconds: int = 18_000,
    max_future_skew_seconds: int = 5,
) -> FreshnessDecision:
    """Reject old strategy decisions before they are allowed to create orders."""

    observed = _epoch_seconds(timestamp)
    current = _epoch_seconds(now) if now is not None else time.time()
    age = current - observed
    if age < -max_future_skew_seconds:
        return FreshnessDecision(False, "timestamp_in_future", age)
    if age > max_age_seconds:
        return FreshnessDecision(False, "signal_stale", age)
    return FreshnessDecision(True, "ok", age)


def closed_bar_signal_freshness(
    bar_open_time: int | float,
    *,
    timeframe_seconds: int,
    now: int | float | None = None,
    valid_bars: int = 1,
) -> FreshnessDecision:
    """Validate a closed-bar signal from close until ``valid_bars`` later bars.

    Legacy strategy signal times identify the candle *open*, so a 4h signal is
    not executable until open+4h and, with valid_bars=1, expires at open+8h.
    """

    opened = _epoch_seconds(bar_open_time)
    current = _epoch_seconds(now) if now is not None else time.time()
    if timeframe_seconds <= 0 or valid_bars < 1:
        raise ValueError("timeframe_seconds and valid_bars must be positive")
    valid_from = opened + timeframe_seconds
    valid_until = valid_from + timeframe_seconds * valid_bars
    age = current - opened
    if current < valid_from:
        return FreshnessDecision(False, "bar_not_closed", age, valid_from, valid_until)
    if current > valid_until:
        return FreshnessDecision(False, "signal_stale", age, valid_from, valid_until)
    return FreshnessDecision(True, "ok", age, valid_from, valid_until)


_CORRELATION_GROUPS: dict[str, frozenset[str]] = {
    "semiconductor": frozenset(
        {
            "ALAB",
            "AMAT",
            "AMD",
            "ARM",
            "ASML",
            "AVGO",
            "AXTI",
            "DRAM",
            "INTC",
            "KLAC",
            "LRCX",
            "MRVL",
            "MU",
            "MUU",
            "NVDA",
            "QCOM",
            "SAMSUNG",
            "SKHYNIX",
            "SKHY",
            "SMH",
            "SNDK",
            "SOXL",
            "SOXS",
            "TER",
            "TSM",
            "TXN",
            "WDC",
        }
    ),
    "nasdaq_index": frozenset({"QQQ", "SQQQ", "TQQQ"}),
    "small_cap_index": frozenset({"IWM", "TZA"}),
    "crypto_equity": frozenset({"BITO", "BMNR", "BNC", "COIN", "HOOD", "IREN", "MSTR"}),
    "long_duration_bonds": frozenset({"TBT", "TMF"}),
    "precious_metals": frozenset({"XAG", "XAU", "XPD", "XPT"}),
}

_LEVERAGED_OR_INVERSE = frozenset(
    {"KORU", "SOXL", "SOXS", "SQQQ", "TBT", "TMF", "TQQQ", "TZA", "UVXY"}
)
_PREMARKET = frozenset({"ANTHROPIC", "MINIMAX", "OPENAI", "ZHIPU"})
_VOLATILITY_PRODUCTS = frozenset({"UVXY"})


def _base_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    for suffix in ("USDT", "USD1"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def symbol_risk_profile(
    symbol: str,
    *,
    group_overrides: Mapping[str, Sequence[str]] | None = None,
) -> SymbolRiskProfile:
    normalized = str(symbol).strip().upper()
    base = _base_symbol(normalized)
    groups = [name for name, members in _CORRELATION_GROUPS.items() if base in members]
    if group_overrides:
        override = group_overrides.get(normalized, group_overrides.get(base))
        if override is not None:
            groups = sorted({str(item).strip().lower() for item in override if str(item).strip()})
    flags: list[str] = []
    if base in _LEVERAGED_OR_INVERSE:
        flags.append("leveraged_or_inverse")
    if base in _PREMARKET:
        flags.append("premarket_reference")
    if base in _VOLATILITY_PRODUCTS:
        flags.append("volatility_product")
    return SymbolRiskProfile(normalized, tuple(sorted(groups)), bool(flags), tuple(flags))


def symbol_admission(
    symbol: str,
    open_symbols: Sequence[str],
    *,
    policy: LiveRiskPolicy | None = None,
    group_overrides: Mapping[str, Sequence[str]] | None = None,
) -> SymbolAdmissionDecision:
    selected = policy or LiveRiskPolicy()
    candidate = symbol_risk_profile(symbol, group_overrides=group_overrides)
    profiles = [symbol_risk_profile(item, group_overrides=group_overrides) for item in open_symbols]
    counts = tuple(
        sorted(
            (
                group,
                sum(group in profile.correlation_groups for profile in profiles),
            )
            for group in candidate.correlation_groups
        )
    )
    high_risk_count = sum(profile.high_risk for profile in profiles)
    if candidate.high_risk and selected.block_high_risk_products:
        return SymbolAdmissionDecision(
            False, "high_risk_product_blocked", candidate, counts, high_risk_count
        )
    if candidate.high_risk and high_risk_count >= selected.max_high_risk_positions:
        return SymbolAdmissionDecision(
            False, "high_risk_position_cap", candidate, counts, high_risk_count
        )
    if any(count >= selected.max_cluster_positions for _, count in counts):
        return SymbolAdmissionDecision(
            False, "correlation_group_cap", candidate, counts, high_risk_count
        )
    return SymbolAdmissionDecision(True, "ok", candidate, counts, high_risk_count)


def account_loss_limits(
    *,
    current_equity: Decimal | float | int | str,
    start_of_day_equity: Decimal | float | int | str,
    high_watermark_equity: Decimal | float | int | str,
    policy: LiveRiskPolicy | None = None,
) -> AccountRiskDecision:
    selected = policy or LiveRiskPolicy()
    equity = _decimal(current_equity, "current_equity", positive=True)
    day_start = _decimal(start_of_day_equity, "start_of_day_equity", positive=True)
    high_watermark = _decimal(high_watermark_equity, "high_watermark_equity", positive=True)
    daily_loss = max(ZERO, (day_start - equity) / day_start * ONE_HUNDRED)
    drawdown = max(ZERO, (high_watermark - equity) / high_watermark * ONE_HUNDRED)
    reasons: list[str] = []
    if daily_loss >= selected.daily_loss_limit_pct:
        reasons.append("daily_loss_limit")
    if drawdown >= selected.max_drawdown_pct:
        reasons.append("max_drawdown_limit")
    return AccountRiskDecision(not reasons, daily_loss, drawdown, tuple(reasons))
