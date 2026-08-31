"""Application orchestration for deterministic prediction settlement."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult

TIMEFRAME_SECONDS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
FEE_BPS_PER_SIDE = 5.0
SLIPPAGE_BPS_PER_SIDE = 3.0
FUNDING_BPS_PER_8H = 1.0
SCORE_EXIT_BAR_MS = 15 * 60 * 1_000
SCORE_EXIT_MIN_HOLD_MS = 30 * 60 * 1_000
SCORE_EXIT_CONFIRMATION_BARS = 2
SOFT_EXIT_MIN_HORIZON_FRACTION = 0.5
PREDICTION_PROFIT_PROTECTION_TRIGGER_BPS = 20.0
PREDICTION_PROFIT_PROTECTION_MIN_BPS = 20.0
PREDICTION_TRAILING_TRIGGER_BPS = 50.0
PREDICTION_TRAILING_GIVEBACK_BPS = 30.0
PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R = 0.5
PREDICTION_RISK_UNIT_TRAILING_ACTIVATION_R = 1.0
PREDICTION_RISK_UNIT_TRAILING_GIVEBACK_R = 0.5
PREDICTION_RISK_UNIT_MINIMUM_PROTECTED_R = 0.0
PREDICTION_RISK_UNIT_PROTECTION_VERSION = "risk_unit_profit_guard_v2"
PREDICTION_RISK_UNIT_MINIMUM_NET_PROTECTED_R = 0.25
PREDICTION_RISK_UNIT_EARLY_GIVEBACK_R = 0.35
PREDICTION_FOLLOW_THROUGH_BARS = 3
PREDICTION_FOLLOW_THROUGH_LOSS_BPS = -15.0
PREDICTION_FOLLOW_THROUGH_MAXIMUM_ADVERSE_R = 0.20
PREDICTION_FOLLOW_THROUGH_CONFIRMATION_CLOSES = 2
PREDICTION_SETTLEMENT_VERSION = "cost_consistent_exit_v8"
PREDICTION_SCORE_EXIT_BAR_MS = SCORE_EXIT_BAR_MS
PREDICTION_SCORE_EXIT_MIN_HOLD_MS = SCORE_EXIT_MIN_HOLD_MS
PREDICTION_SCORE_EXIT_CONFIRMATION_BARS = SCORE_EXIT_CONFIRMATION_BARS
COST_CONSISTENT_SETTLEMENT_VERSIONS = {
    "cost_consistent_exit_v6",
    "cost_consistent_exit_v7",
    "cost_consistent_exit_v8",
}


def _datetime_ms(value: datetime) -> int:
    current = value if value.tzinfo else value.replace(tzinfo=UTC)
    return int(current.timestamp() * 1_000)


def historical_settlement_price(
    candles: Sequence[Mapping[str, Any]],
    expires_at_ms: int,
    *,
    timeframe_ms: int = 15 * 60 * 1_000,
) -> dict[str, Any] | None:
    """Pick the nearest real candle price around an opportunity's expiry."""

    normalized: list[tuple[int, float, float]] = []
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            open_price = float(candle.get("open") or 0)
            close_price = float(candle.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if open_time > 0 and open_price > 0 and close_price > 0:
            normalized.append((open_time, open_price, close_price))
    if not normalized:
        return None
    normalized.sort(key=lambda item: item[0])
    times = [item[0] for item in normalized]
    position = bisect_left(times, int(expires_at_ms))
    tolerance_ms = timeframe_ms * 3
    candidates: list[tuple[int, float, int]] = []
    if position < len(normalized):
        open_time, open_price, _ = normalized[position]
        candidates.append((abs(open_time - expires_at_ms), open_price, open_time))
    if position > 0:
        open_time, _, close_price = normalized[position - 1]
        close_time = open_time + timeframe_ms
        candidates.append((abs(close_time - expires_at_ms), close_price, close_time))
    if not candidates:
        return None
    distance, price, price_time = min(candidates, key=lambda item: item[0])
    if distance > tolerance_ms:
        return None
    return {"price": price, "price_time_ms": price_time}


def historical_closed_settlement_price(
    candles: Sequence[Mapping[str, Any]],
    settles_at_ms: int,
    *,
    timeframe_ms: int = 15 * 60 * 1_000,
    not_before_ms: int | None = None,
) -> dict[str, Any] | None:
    """Return the first causally executable candle open at/after a hard cap."""

    candidates: list[tuple[int, float]] = []
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            open_price = float(candle.get("open") or 0)
        except (TypeError, ValueError):
            continue
        if (
            open_time >= settles_at_ms
            and open_price > 0
            and (not_before_ms is None or open_time >= not_before_ms)
            and open_time - settles_at_ms <= timeframe_ms
        ):
            candidates.append((open_time, open_price))
    if not candidates:
        return None
    open_time, open_price = min(candidates, key=lambda item: item[0])
    return {
        "price": open_price,
        "price_time_ms": open_time,
        "price_source": "first_executable_open_at_or_after_cap",
    }


def prediction_soft_exit_policy(
    timeframe: str,
    *,
    start_ms: int,
    due_ms: int,
) -> dict[str, Any]:
    """Return the frozen, horizon-aligned policy for non-emergency exits."""

    native_bar_ms = max(
        SCORE_EXIT_BAR_MS,
        int(TIMEFRAME_SECONDS.get(str(timeframe), 15 * 60)) * 1_000,
    )
    planned_horizon_ms = max(native_bar_ms, int(due_ms) - int(start_ms))
    minimum_hold_ms = min(
        planned_horizon_ms,
        max(
            SCORE_EXIT_MIN_HOLD_MS,
            native_bar_ms * SCORE_EXIT_CONFIRMATION_BARS,
            int(planned_horizon_ms * SOFT_EXIT_MIN_HORIZON_FRACTION),
        ),
    )
    timeframe_label = str(timeframe or "15m").lower()
    return {
        "bar_ms": native_bar_ms,
        "minimum_hold_ms": minimum_hold_ms,
        "confirmation_bars": SCORE_EXIT_CONFIRMATION_BARS,
        "confirmation_unit": f"closed_{timeframe_label}_bar",
        "planned_horizon_ms": planned_horizon_ms,
    }


def virtual_risk_plan_snapshot(
    *,
    entry_price: float,
    direction: str,
    timeframe: str,
    atr_pct: float | None = None,
) -> dict[str, Any]:
    """Freeze transparent research stop/target levels at virtual entry."""

    fallback_risk_pct = {"15m": 0.8, "1h": 1.5, "4h": 3.0}.get(timeframe, 1.5)
    minimum_risk_pct = {"15m": 0.5, "1h": 0.8, "4h": 1.5}.get(timeframe, 0.8)
    maximum_risk_pct = {"15m": 2.0, "1h": 3.5, "4h": 6.0}.get(timeframe, 3.5)
    volatility_risk = float(atr_pct or 0) * 1.5
    stop_loss_pct = (
        max(minimum_risk_pct, min(maximum_risk_pct, volatility_risk))
        if volatility_risk > 0
        else fallback_risk_pct
    )
    take_profit_pct = stop_loss_pct * 2.0
    normalized_direction = "short" if direction == "short" else "long"
    if entry_price > 0 and normalized_direction == "short":
        stop_loss_price = entry_price * (1 + stop_loss_pct / 100)
        take_profit_price = entry_price * (1 - take_profit_pct / 100)
    elif entry_price > 0:
        stop_loss_price = entry_price * (1 - stop_loss_pct / 100)
        take_profit_price = entry_price * (1 + take_profit_pct / 100)
    else:
        stop_loss_price = 0.0
        take_profit_price = 0.0
    return {
        "version": "atr_risk_reward_guard_v6",
        "settlement_version": PREDICTION_SETTLEMENT_VERSION,
        "method": "atr14_x_1_5" if volatility_risk > 0 else "timeframe_fallback",
        "timeframe": timeframe,
        "direction": normalized_direction,
        "entry_price": round(float(entry_price), 12) if entry_price > 0 else None,
        "atr_pct": round(float(atr_pct), 8) if atr_pct is not None else None,
        "stop_loss_pct": round(stop_loss_pct, 6),
        "take_profit_pct": round(take_profit_pct, 6),
        "stop_loss_price": round(stop_loss_price, 12) if stop_loss_price > 0 else None,
        "take_profit_price": (
            round(take_profit_price, 12) if take_profit_price > 0 else None
        ),
        "risk_reward_ratio": 2.0,
        "profit_protection": {
            "version": PREDICTION_RISK_UNIT_PROTECTION_VERSION,
            "mode": "risk_unit",
            "risk_bps": round(stop_loss_pct * 100.0, 8),
            "activation_r": PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R,
            "minimum_net_protected_r": PREDICTION_RISK_UNIT_MINIMUM_NET_PROTECTED_R,
            "early_maximum_giveback_r": PREDICTION_RISK_UNIT_EARLY_GIVEBACK_R,
            "trailing_activation_r": PREDICTION_RISK_UNIT_TRAILING_ACTIVATION_R,
            "maximum_giveback_r": PREDICTION_RISK_UNIT_TRAILING_GIVEBACK_R,
            "activation_boundary": "prior_closed_15m_bar",
            "minimum_hold_policy": "immediate_after_activation",
        },
        "failed_follow_through": {
            "closed_bars": PREDICTION_FOLLOW_THROUGH_BARS,
            "maximum_favorable_r": PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R,
            "maximum_favorable_bps": round(
                stop_loss_pct
                * 100.0
                * PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R,
                8,
            ),
            "maximum_adverse_r": PREDICTION_FOLLOW_THROUGH_MAXIMUM_ADVERSE_R,
            "minimum_adverse_bps": abs(PREDICTION_FOLLOW_THROUGH_LOSS_BPS),
            "confirmation_closes": PREDICTION_FOLLOW_THROUGH_CONFIRMATION_CLOSES,
            "confirmation_unit": "closed_15m_bar",
            "minimum_hold_policy": "horizon_aligned",
        },
        "execution_policy": "cost_consistent_risk_guard_v9",
    }


def prediction_outcome(
    entry_price: float,
    exit_price: float,
    direction: str,
) -> dict[str, float | str]:
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("prediction prices must be positive")
    raw_return_bps = (exit_price / entry_price - 1.0) * 10_000.0
    directional_return_bps = raw_return_bps if direction == "long" else -raw_return_bps
    result = "win" if directional_return_bps > 0 else "loss"
    if abs(directional_return_bps) < 0.000001:
        result = "flat"
    return {
        "raw_return_bps": round(raw_return_bps, 8),
        "directional_return_bps": round(directional_return_bps, 8),
        "result": result,
    }


def prediction_cost_settings(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values = config or {}
    return {
        "fee_enabled": bool(values.get("prediction_fee_enabled", True)),
        "fee_bps_per_side": max(
            float(values.get("prediction_fee_bps_per_side", FEE_BPS_PER_SIDE)),
            0.0,
        ),
        "slippage_enabled": bool(values.get("prediction_slippage_enabled", True)),
        "slippage_bps_per_side": max(
            float(
                values.get(
                    "prediction_slippage_bps_per_side",
                    SLIPPAGE_BPS_PER_SIDE,
                )
            ),
            0.0,
        ),
        "funding_enabled": bool(values.get("prediction_funding_enabled", True)),
        "funding_bps_per_8h": max(
            float(
                values.get(
                    "prediction_funding_bps_per_8h",
                    FUNDING_BPS_PER_8H,
                )
            ),
            0.0,
        ),
    }


def readiness_cost_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settings = prediction_cost_settings(config)
    return {
        "prediction_fee_enabled": True,
        "prediction_fee_bps_per_side": max(
            float(settings["fee_bps_per_side"]), FEE_BPS_PER_SIDE
        ),
        "prediction_slippage_enabled": True,
        "prediction_slippage_bps_per_side": max(
            float(settings["slippage_bps_per_side"]), SLIPPAGE_BPS_PER_SIDE
        ),
        "prediction_funding_enabled": True,
        "prediction_funding_bps_per_8h": max(
            float(settings["funding_bps_per_8h"]), FUNDING_BPS_PER_8H
        ),
        "forced_for_readiness": True,
    }


def prediction_settlement_cost_config(
    cost_model: Mapping[str, Any] | None,
    *,
    settlement_version: str,
) -> dict[str, Any]:
    frozen = dict(cost_model or {})
    config = {
        "prediction_fee_enabled": bool(frozen.get("fee_enabled", True)),
        "prediction_fee_bps_per_side": float(
            frozen.get("fee_bps_per_side", FEE_BPS_PER_SIDE)
        ),
        "prediction_slippage_enabled": bool(frozen.get("slippage_enabled", True)),
        "prediction_slippage_bps_per_side": float(
            frozen.get("slippage_bps_per_side", SLIPPAGE_BPS_PER_SIDE)
        ),
        "prediction_funding_enabled": bool(frozen.get("funding_enabled", True)),
        "prediction_funding_bps_per_8h": float(
            frozen.get("funding_bps_per_8h", FUNDING_BPS_PER_8H)
        ),
    }
    if settlement_version in COST_CONSISTENT_SETTLEMENT_VERSIONS:
        return readiness_cost_config(config)
    return config


def prediction_cost_breakdown(
    predicted_at: datetime,
    due_at: datetime,
    config: Mapping[str, Any] | None = None,
) -> dict[str, float | bool]:
    settings = prediction_cost_settings(config)
    holding_seconds = max((due_at - predicted_at).total_seconds(), 0.0)
    fee_bps = (
        2 * float(settings["fee_bps_per_side"])
        if settings["fee_enabled"]
        else 0.0
    )
    slippage_bps = (
        2 * float(settings["slippage_bps_per_side"])
        if settings["slippage_enabled"]
        else 0.0
    )
    funding_bps = (
        holding_seconds / (8 * 60 * 60) * float(settings["funding_bps_per_8h"])
        if settings["funding_enabled"]
        else 0.0
    )
    return {
        **settings,
        "holding_seconds": round(holding_seconds, 3),
        "fee_cost_bps": round(fee_bps, 8),
        "slippage_cost_bps": round(slippage_bps, 8),
        "funding_cost_bps": round(funding_bps, 8),
        "total_cost_bps": round(fee_bps + slippage_bps + funding_bps, 8),
    }


def prediction_estimated_cost_bps(
    predicted_at: datetime,
    due_at: datetime,
    config: Mapping[str, Any] | None = None,
) -> float:
    return float(
        prediction_cost_breakdown(predicted_at, due_at, config)["total_cost_bps"]
    )


def prediction_net_outcome(
    gross_return_bps: float,
    estimated_cost_bps: float,
) -> dict[str, Any]:
    net_return_bps = gross_return_bps - estimated_cost_bps
    result = "win" if net_return_bps > 0 else "loss"
    if abs(net_return_bps) < 0.000001:
        result = "flat"
    return {
        "estimated_cost_bps": round(estimated_cost_bps, 8),
        "net_directional_return_bps": round(net_return_bps, 8),
        "net_result": result,
    }


def prediction_path_metrics(
    candles: Sequence[Mapping[str, Any]],
    entry_price: float,
    direction: str,
    start_ms: int,
    due_ms: int,
) -> dict[str, float | None]:
    if entry_price <= 0:
        return {"max_favorable_bps": None, "max_adverse_bps": None}
    observed_prices: list[float] = [entry_price]
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            if not (start_ms <= open_time <= due_ms):
                continue
            values = [
                float(candle.get(key) or 0)
                for key in ("open", "high", "low", "close")
            ]
        except (TypeError, ValueError):
            continue
        observed_prices.extend(value for value in values if value > 0)
    if len(observed_prices) == 1:
        return {"max_favorable_bps": None, "max_adverse_bps": None}
    highest = max(observed_prices)
    lowest = min(observed_prices)
    if direction == "short":
        favorable = (1.0 - lowest / entry_price) * 10_000.0
        adverse = (1.0 - highest / entry_price) * 10_000.0
    else:
        favorable = (highest / entry_price - 1.0) * 10_000.0
        adverse = (lowest / entry_price - 1.0) * 10_000.0
    return {
        "max_favorable_bps": round(max(favorable, 0.0), 8),
        "max_adverse_bps": round(min(adverse, 0.0), 8),
    }


def prediction_price_barrier_exit(
    candles: Sequence[Mapping[str, Any]],
    entry_price: float,
    direction: str,
    risk_plan: Mapping[str, Any],
    start_ms: int,
    end_ms: int,
    *,
    timeframe_ms: int = 15 * 60 * 1_000,
) -> dict[str, Any] | None:
    """Return the first causal stop/target touched by the observed OHLC path."""

    if entry_price <= 0 or end_ms < start_ms:
        return None
    stop_price = float(risk_plan.get("stop_loss_price") or 0)
    target_price = float(risk_plan.get("take_profit_price") or 0)
    if stop_price <= 0 or target_price <= 0:
        return None
    normalized_direction = "short" if direction == "short" else "long"
    normalized: list[tuple[int, int, float, float, float]] = []
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            open_price = float(candle.get("open") or 0)
            high_price = float(candle.get("high") or 0)
            low_price = float(candle.get("low") or 0)
        except (TypeError, ValueError):
            continue
        close_time = open_time + timeframe_ms
        if not (start_ms <= open_time and close_time <= end_ms):
            continue
        if open_price <= 0 or high_price <= 0 or low_price <= 0:
            continue
        normalized.append((open_time, close_time, open_price, high_price, low_price))
    for open_time, close_time, open_price, high_price, low_price in sorted(normalized):
        if normalized_direction == "short":
            if open_price >= stop_price:
                return {
                    "reason": "stop_loss",
                    "price": open_price,
                    "price_time_ms": open_time,
                    "same_bar_conflict": False,
                    "gap_execution": True,
                }
            if open_price <= target_price:
                return {
                    "reason": "take_profit",
                    "exit_subreason": "hard_target",
                    "price": target_price,
                    "price_time_ms": open_time,
                    "same_bar_conflict": False,
                    "gap_execution": True,
                }
            stop_hit = high_price >= stop_price
            target_hit = low_price <= target_price
        else:
            if open_price <= stop_price:
                return {
                    "reason": "stop_loss",
                    "price": open_price,
                    "price_time_ms": open_time,
                    "same_bar_conflict": False,
                    "gap_execution": True,
                }
            if open_price >= target_price:
                return {
                    "reason": "take_profit",
                    "exit_subreason": "hard_target",
                    "price": target_price,
                    "price_time_ms": open_time,
                    "same_bar_conflict": False,
                    "gap_execution": True,
                }
            stop_hit = low_price <= stop_price
            target_hit = high_price >= target_price
        reason = "stop_loss" if stop_hit else "take_profit" if target_hit else ""
        if not reason:
            continue
        return {
            "reason": reason,
            "exit_subreason": "hard_target" if reason == "take_profit" else None,
            "price": stop_price if reason == "stop_loss" else target_price,
            "price_time_ms": close_time,
            "same_bar_conflict": bool(stop_hit and target_hit),
            "gap_execution": False,
        }
    return None


def prediction_adaptive_path_exit(
    candles: Sequence[Mapping[str, Any]],
    entry_price: float,
    direction: str,
    start_ms: int,
    end_ms: int,
    *,
    estimated_cost_bps: float = 0.0,
    timeframe_ms: int = 15 * 60 * 1_000,
    minimum_soft_exit_ms: int = 0,
    minimum_profit_protection_ms: int | None = None,
    minimum_failed_follow_through_ms: int | None = None,
    profit_protection: Mapping[str, Any] | None = None,
    failed_follow_through: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Protect proven profit and cut a failed follow-through without look-ahead.

    A protective level is activated only *after* a candle has fully closed.  A
    favorable high/low therefore cannot create and hit a trailing stop inside
    the same candle.  Profit protection and failed-follow-through may use
    separate waiting periods: a frozen R-based guard can protect established
    profit immediately while thesis invalidation remains horizon-aligned.  This
    keeps live settlement and point-in-time replay on the same causal boundary.
    """

    if entry_price <= 0 or end_ms < start_ms:
        return None
    normalized_direction = "short" if direction == "short" else "long"
    normalized: list[tuple[int, int, float, float, float, float]] = []
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            close_time = open_time + timeframe_ms
            open_price = float(candle.get("open") or 0)
            high_price = float(candle.get("high") or 0)
            low_price = float(candle.get("low") or 0)
            close_price = float(candle.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if open_time < start_ms or close_time > end_ms:
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            continue
        normalized.append(
            (open_time, close_time, open_price, high_price, low_price, close_price)
        )

    peak_favorable_bps = 0.0
    protected_bps: float | None = None
    observed_bar_count = 0
    consecutive_failed_closes = 0
    failed_confirmation_times_ms: list[int] = []
    profit_delay_ms = (
        minimum_soft_exit_ms
        if minimum_profit_protection_ms is None
        else minimum_profit_protection_ms
    )
    failed_follow_delay_ms = (
        minimum_soft_exit_ms
        if minimum_failed_follow_through_ms is None
        else minimum_failed_follow_through_ms
    )
    profit_exit_not_before_ms = start_ms + max(0, int(profit_delay_ms))
    failed_follow_not_before_ms = start_ms + max(
        0, int(failed_follow_delay_ms)
    )
    protection = dict(profit_protection or {})
    protection_mode = str(protection.get("mode") or "fixed_bps")
    risk_bps = 0.0
    if protection_mode == "risk_unit":
        risk_bps = max(0.0, float(protection.get("risk_bps") or 0.0))
        activation_bps = risk_bps * max(
            0.0,
            float(
                protection.get(
                    "activation_r",
                    PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R,
                )
            ),
        )
        trailing_activation_bps = risk_bps * max(
            0.0,
            float(
                protection.get(
                    "trailing_activation_r",
                    PREDICTION_RISK_UNIT_TRAILING_ACTIVATION_R,
                )
            ),
        )
        trailing_giveback_bps = risk_bps * max(
            0.0,
            float(
                protection.get(
                    "maximum_giveback_r",
                    PREDICTION_RISK_UNIT_TRAILING_GIVEBACK_R,
                )
            ),
        )
        minimum_protected_bps = risk_bps * max(
            0.0,
            float(
                protection.get(
                    "minimum_protected_r",
                    PREDICTION_RISK_UNIT_MINIMUM_PROTECTED_R,
                )
            ),
        )
        minimum_net_protected_r = protection.get("minimum_net_protected_r")
        minimum_net_protected_bps = (
            risk_bps * max(0.0, float(minimum_net_protected_r))
            if minimum_net_protected_r is not None
            else None
        )
        early_giveback_r = protection.get("early_maximum_giveback_r")
        early_giveback_bps = (
            risk_bps * max(0.0, float(early_giveback_r))
            if early_giveback_r is not None
            else None
        )
        if risk_bps <= 0:
            protection_mode = "fixed_bps"
    if protection_mode != "risk_unit":
        activation_bps = PREDICTION_PROFIT_PROTECTION_TRIGGER_BPS
        trailing_activation_bps = PREDICTION_TRAILING_TRIGGER_BPS
        trailing_giveback_bps = PREDICTION_TRAILING_GIVEBACK_BPS
        minimum_protected_bps = PREDICTION_PROFIT_PROTECTION_MIN_BPS
        minimum_net_protected_bps = None
        early_giveback_bps = None
    failed_policy = dict(failed_follow_through or {})
    required_follow_bars = max(
        1,
        int(failed_policy.get("closed_bars") or PREDICTION_FOLLOW_THROUGH_BARS),
    )
    required_failed_closes = max(
        1,
        int(failed_policy.get("confirmation_closes") or 1),
    )
    maximum_adverse_r = failed_policy.get("maximum_adverse_r")
    if maximum_adverse_r is not None and risk_bps > 0:
        failed_loss_threshold_bps = -max(
            float(failed_policy.get("minimum_adverse_bps") or 0.0),
            risk_bps * max(0.0, float(maximum_adverse_r)),
        )
        failed_threshold_mode = "risk_unit"
    else:
        failed_loss_threshold_bps = float(
            failed_policy.get(
                "directional_loss_bps", PREDICTION_FOLLOW_THROUGH_LOSS_BPS
            )
        )
        failed_threshold_mode = "fixed_bps"
    cost_floor_bps = max(
        minimum_protected_bps,
        max(0.0, float(estimated_cost_bps)) + 2.0,
    )
    if minimum_net_protected_bps is not None:
        cost_floor_bps = max(
            cost_floor_bps,
            max(0.0, float(estimated_cost_bps))
            + minimum_net_protected_bps,
        )
    effective_activation_bps = max(activation_bps, cost_floor_bps)
    for (
        open_time,
        close_time,
        open_price,
        high_price,
        low_price,
        close_price,
    ) in sorted(normalized):
        # This stop was frozen from prior closed bars, so it is executable from
        # the current open without relying on the current candle's future path.
        if protected_bps is not None and open_time >= profit_exit_not_before_ms:
            protected_price = (
                entry_price * (1 - protected_bps / 10_000.0)
                if normalized_direction == "short"
                else entry_price * (1 + protected_bps / 10_000.0)
            )
            gap_execution = (
                open_price >= protected_price
                if normalized_direction == "short"
                else open_price <= protected_price
            )
            touched = (
                high_price >= protected_price
                if normalized_direction == "short"
                else low_price <= protected_price
            )
            if gap_execution or touched:
                subreason = (
                    "trailing_profit"
                    if peak_favorable_bps >= trailing_activation_bps
                    else "profit_lock"
                )
                return {
                    "reason": "take_profit",
                    "exit_subreason": subreason,
                    "price": open_price if gap_execution else protected_price,
                    "price_time_ms": open_time if gap_execution else close_time,
                    "same_bar_conflict": False,
                    "gap_execution": gap_execution,
                    "observed_bar_count": observed_bar_count + 1,
                    "peak_favorable_bps": round(peak_favorable_bps, 8),
                    "protected_bps": round(protected_bps, 8),
                    "protection_mode": protection_mode,
                    "activation_bps": round(activation_bps, 8),
                    "effective_activation_bps": round(
                        effective_activation_bps, 8
                    ),
                    "trailing_activation_bps": round(
                        trailing_activation_bps, 8
                    ),
                    "trailing_giveback_bps": round(
                        trailing_giveback_bps, 8
                    ),
                    "early_giveback_bps": (
                        round(early_giveback_bps, 8)
                        if early_giveback_bps is not None
                        else None
                    ),
                }

        observed_bar_count += 1
        favorable_price = (
            low_price if normalized_direction == "short" else high_price
        )
        favorable_bps = (
            (1.0 - favorable_price / entry_price) * 10_000.0
            if normalized_direction == "short"
            else (favorable_price / entry_price - 1.0) * 10_000.0
        )
        peak_favorable_bps = max(peak_favorable_bps, favorable_bps)
        close_directional_bps = (
            (1.0 - close_price / entry_price) * 10_000.0
            if normalized_direction == "short"
            else (close_price / entry_price - 1.0) * 10_000.0
        )
        failed_close = bool(
            close_time >= failed_follow_not_before_ms
            and observed_bar_count >= required_follow_bars
            and peak_favorable_bps < activation_bps
            and close_directional_bps <= failed_loss_threshold_bps
        )
        if failed_close:
            consecutive_failed_closes += 1
            failed_confirmation_times_ms.append(close_time)
        else:
            consecutive_failed_closes = 0
            failed_confirmation_times_ms = []
        if consecutive_failed_closes >= required_failed_closes:
            return {
                "reason": "score_breakdown",
                "exit_subreason": "failed_follow_through",
                "price": close_price,
                "price_time_ms": close_time,
                "same_bar_conflict": False,
                "gap_execution": False,
                "observed_bar_count": observed_bar_count,
                "peak_favorable_bps": round(peak_favorable_bps, 8),
                "protected_bps": None,
                "confirmation_points": consecutive_failed_closes,
                "confirmation_unit": str(
                    failed_policy.get("confirmation_unit") or "closed_15m_bar"
                ),
                "confirmation_bar_times_ms": failed_confirmation_times_ms[-required_failed_closes:],
                "failed_loss_threshold_bps": round(failed_loss_threshold_bps, 8),
                "failed_threshold_mode": failed_threshold_mode,
            }
        if peak_favorable_bps >= effective_activation_bps:
            if peak_favorable_bps >= trailing_activation_bps:
                candidate_floor = peak_favorable_bps - trailing_giveback_bps
            elif early_giveback_bps is not None:
                candidate_floor = peak_favorable_bps - early_giveback_bps
            else:
                # Frozen v6 plans do not have an early giveback rule and retain
                # their original cost-plus-2-bps protection for auditability.
                candidate_floor = cost_floor_bps
            protected_bps = max(
                float(protected_bps or 0.0),
                cost_floor_bps,
                candidate_floor,
            )
    return None


def adaptive_exit_precedes(
    current: Mapping[str, Any] | None,
    adaptive: Mapping[str, Any],
) -> bool:
    """Return whether an adaptive exit should replace the current decision."""

    adaptive_time_ms = int(adaptive["price_time_ms"])
    if current is None:
        return True
    current_time_ms = int(current["price_time_ms"])
    if adaptive_time_ms != current_time_ms:
        return adaptive_time_ms < current_time_ms
    return bool(
        adaptive.get("exit_subreason") in {"profit_lock", "trailing_profit"}
        and current.get("reason") != "stop_loss"
    )


def settlement_exit_subreason(
    exit_decision: Mapping[str, Any],
    *,
    net_result: str,
) -> str | None:
    """Keep protective-gap losses out of successful lock-profit statistics."""

    exit_subreason = str(exit_decision.get("exit_subreason") or "") or None
    if (
        str(exit_decision.get("reason") or "") == "take_profit"
        and exit_subreason in {"profit_lock", "trailing_profit"}
        and bool(exit_decision.get("gap_execution"))
        and net_result == "loss"
    ):
        return f"{exit_subreason}_gap_loss"
    return exit_subreason


def prediction_score_exit_signal(
    evidence: Mapping[str, Any] | None,
    direction: str,
    *,
    start_ms: int,
    end_ms: int,
    confirmation_bar_ms: int = PREDICTION_SCORE_EXIT_BAR_MS,
    minimum_hold_ms: int = PREDICTION_SCORE_EXIT_MIN_HOLD_MS,
    confirmation_bars: int = PREDICTION_SCORE_EXIT_CONFIRMATION_BARS,
    confirmation_unit: str = "closed_15m_bar",
) -> dict[str, Any] | None:
    """Detect a confirmed score breakdown or directional reversal.

    Poll frequency is not market evidence.  Both score weakening and a
    directional reversal therefore need distinct native-timeframe evidence
    buckets and must respect the frozen minimum holding period.
    """

    source = evidence or {}
    raw_history = source.get("live_score_history") or source.get("score_history")
    if not isinstance(raw_history, list):
        return None
    readiness = source.get("live_readiness")
    readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
    entry_threshold = float(readiness.get("minimum_combined_score", 70.0))
    exit_threshold = max(0.0, entry_threshold - 5.0)
    observations: list[dict[str, Any]] = []
    for raw in raw_history:
        if not isinstance(raw, Mapping):
            continue
        calculated_at = raw.get("calculated_at")
        try:
            observed_at = datetime.fromisoformat(str(calculated_at).replace("Z", "+00:00"))
            observed_ms = _datetime_ms(observed_at)
            combined = float(raw.get("combined"))
        except (TypeError, ValueError):
            continue
        if not (start_ms <= observed_ms <= end_ms):
            continue
        try:
            technical = (
                float(raw.get("technical"))
                if raw.get("technical") is not None
                else None
            )
        except (TypeError, ValueError):
            technical = None
        try:
            reference_price = (
                float(raw.get("reference_price"))
                if raw.get("reference_price") is not None
                else None
            )
        except (TypeError, ValueError):
            reference_price = None
        try:
            reference_price_time_ms = (
                int(raw.get("reference_price_time_ms"))
                if raw.get("reference_price_time_ms") is not None
                else None
            )
        except (TypeError, ValueError):
            reference_price_time_ms = None
        evidence_time_ms = reference_price_time_ms or observed_ms
        if not (start_ms <= evidence_time_ms <= end_ms):
            continue
        closed_bar_time_ms = (
            evidence_time_ms // max(1, int(confirmation_bar_ms))
        ) * max(1, int(confirmation_bar_ms))
        observations.append(
            {
                "calculated_at": str(calculated_at),
                "price_time_ms": observed_ms,
                "combined": combined,
                "direction": str(raw.get("direction") or direction),
                "technical": technical,
                "reference_price": reference_price,
                "reference_price_time_ms": reference_price_time_ms,
                "closed_bar_time_ms": closed_bar_time_ms,
            }
        )
    observations.sort(key=lambda item: item["price_time_ms"])
    if not observations:
        return None
    # Keep only the latest calculation for each closed market bar.  Repeated
    # scheduler scans inside one bar can update the audit trail, but must never
    # count as independent confirmation points.
    latest_by_closed_bar: dict[int, dict[str, Any]] = {}
    for observation in observations:
        closed_bar_time_ms = int(observation["closed_bar_time_ms"])
        if closed_bar_time_ms <= start_ms:
            continue
        latest_by_closed_bar[closed_bar_time_ms] = observation
    bar_observations = [
        latest_by_closed_bar[key] for key in sorted(latest_by_closed_bar)
    ]
    ignored_duplicate_points = len(observations) - len(bar_observations)
    required_confirmations = max(1, int(confirmation_bars))
    not_before_ms = start_ms + max(0, int(minimum_hold_ms))
    consecutive_reversals: list[dict[str, Any]] = []
    for observation in bar_observations:
        if (
            observation["direction"] in {"long", "short"}
            and observation["direction"] != direction
        ):
            consecutive_reversals.append(observation)
        else:
            consecutive_reversals = []
        if (
            len(consecutive_reversals) >= required_confirmations
            and int(observation["price_time_ms"]) >= not_before_ms
        ):
            confirmations = consecutive_reversals[-required_confirmations:]
            return {
                **observation,
                "reason": "score_reversal",
                "exit_threshold": exit_threshold,
                "confirmation_points": required_confirmations,
                "confirmation_unit": confirmation_unit,
                "confirmation_bar_times_ms": [
                    int(item["closed_bar_time_ms"]) for item in confirmations
                ],
                "minimum_hold_ms": max(0, int(minimum_hold_ms)),
                "ignored_duplicate_points": max(0, ignored_duplicate_points),
            }

    consecutive_low_scores: list[dict[str, Any]] = []
    for observation in bar_observations:
        if observation["combined"] < exit_threshold:
            consecutive_low_scores.append(observation)
        else:
            consecutive_low_scores = []
        if (
            len(consecutive_low_scores) >= required_confirmations
            and int(observation["price_time_ms"]) >= not_before_ms
        ):
            confirmations = consecutive_low_scores[-required_confirmations:]
            return {
                **observation,
                "reason": "score_breakdown",
                "exit_threshold": exit_threshold,
                "confirmation_points": required_confirmations,
                "confirmation_unit": confirmation_unit,
                "confirmation_bar_times_ms": [
                    int(item["closed_bar_time_ms"]) for item in confirmations
                ],
                "confirmation_scores": [
                    float(item["combined"]) for item in confirmations
                ],
                "minimum_hold_ms": max(0, int(minimum_hold_ms)),
                "ignored_duplicate_points": max(0, ignored_duplicate_points),
            }
    return None


def prediction_score_exit_price(
    candles: Sequence[Mapping[str, Any]],
    score_signal: Mapping[str, Any],
    *,
    end_ms: int,
) -> dict[str, Any] | None:
    """Resolve a causal execution price for a score-driven virtual exit.

    New observations freeze the executable reference price at scoring time.
    Legacy observations without such a price may use only the first candle open
    at or after the signal, never a nearest price from before the signal or a
    future candle close.
    """

    try:
        signal_ms = int(score_signal["price_time_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    if signal_ms > end_ms:
        return None
    try:
        reference_price = float(score_signal.get("reference_price") or 0)
        reference_time_ms = int(score_signal.get("reference_price_time_ms") or 0)
    except (TypeError, ValueError):
        reference_price = 0.0
        reference_time_ms = 0
    if (
        reference_price > 0
        and 0 < reference_time_ms <= signal_ms
    ):
        return {
            "price": reference_price,
            # The decision cannot execute before the score was calculated,
            # even when the frozen ticker snapshot itself is slightly older.
            "price_time_ms": signal_ms,
            "price_source": "frozen_score_reference",
            "reference_price_time_ms": reference_time_ms,
        }

    candidates: list[tuple[int, float]] = []
    for candle in candles:
        try:
            open_time = int(candle.get("open_time") or 0)
            if 0 < open_time < 1_000_000_000_000:
                open_time *= 1_000
            open_price = float(candle.get("open") or 0)
        except (TypeError, ValueError):
            continue
        if signal_ms <= open_time <= end_ms and open_price > 0:
            candidates.append((open_time, open_price))
    if not candidates:
        return None
    open_time, open_price = min(candidates, key=lambda item: item[0])
    return {
        "price": open_price,
        "price_time_ms": open_time,
        "price_source": "first_open_after_score",
        "reference_price_time_ms": None,
    }



class PredictionSettlementService:
    """Settle due facts and refresh projections without AI authority."""

    def __init__(
        self,
        *,
        settle: Callable[..., dict[str, Any]],
        reopen_legacy: Callable[[Any], int],
        refresh_projection: Callable[..., dict[str, Any]],
        version: str,
    ) -> None:
        self._settle = settle
        self._reopen_legacy = reopen_legacy
        self._refresh_projection = refresh_projection
        self._version = version

    def execute_cycle(self, db: Any, repository: Any) -> AiMonitorStageResult:
        reopened = self._reopen_legacy(db)
        settlement = self._settle(db, repository)
        projection: dict[str, Any] | None = None
        if settlement.get("completed") or settlement.get("unavailable") or reopened:
            projection = self._refresh_projection(
                db,
                prediction_limit=1000,
                score_limit=5000,
            )
        return AiMonitorStageResult(
            stage="prediction_settlement",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={
                "reopened_legacy": reopened,
                "settlement": settlement,
                "read_models": projection,
            },
        )
