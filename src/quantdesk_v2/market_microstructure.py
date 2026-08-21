"""Pure, point-in-time market-microstructure admission rules."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

ORDER_BOOK_MAX_AGE_SECONDS = 30
ORDER_BOOK_FUTURE_SKEW_TOLERANCE_SECONDS = 15
ORDER_BOOK_MAX_SPREAD_BPS = 25.0
ORDER_BOOK_MIN_LEVELS_PER_SIDE = 5
ORDER_BOOK_CONFLICT_PRESSURE = -0.20
ORDER_BOOK_CONFIRM_PRESSURE = 0.10
ORDER_BOOK_COMPONENT_CONFLICT_PRESSURE = -0.10


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def order_book_gate_snapshot(
    depth: Mapping[str, Any] | None,
    *,
    direction: str,
    now_seconds: int,
    maximum_age_seconds: int = ORDER_BOOK_MAX_AGE_SECONDS,
    future_skew_tolerance_seconds: int = ORDER_BOOK_FUTURE_SKEW_TOLERANCE_SECONDS,
    maximum_spread_bps: float = ORDER_BOOK_MAX_SPREAD_BPS,
    minimum_levels_per_side: int = ORDER_BOOK_MIN_LEVELS_PER_SIDE,
) -> dict[str, Any]:
    """Return a conservative veto gate from one synchronized Binance depth snapshot.

    Visible depth can be canceled or spoofed, so it never creates a signal by
    itself. It may only reject an entry when the book is missing/unsafe or when
    top-of-book and broad pressure jointly oppose the proposed direction.
    """

    source = dict(depth or {})
    normalized_direction = str(direction or "").lower()
    direction_sign = 1.0 if normalized_direction == "long" else -1.0
    direction_valid = normalized_direction in {"long", "short"}
    try:
        captured_at = int(source.get("ts") or 0)
    except (TypeError, ValueError, OverflowError):
        captured_at = 0
    clock_skew_seconds = captured_at - int(now_seconds) if captured_at else None
    timestamp_valid = bool(
        captured_at
        and clock_skew_seconds is not None
        and clock_skew_seconds <= max(0, int(future_skew_tolerance_seconds))
    )
    age_seconds = (
        max(0, int(now_seconds) - captured_at) if timestamp_valid else None
    )
    fresh = bool(
        timestamp_valid
        and age_seconds is not None
        and age_seconds <= max(1, int(maximum_age_seconds))
    )

    bid_notional = _finite(source.get("bid_depth_notional"))
    ask_notional = _finite(source.get("ask_depth_notional"))
    broad_imbalance = _finite(source.get("book_imbalance"))
    near_imbalance = _finite(source.get("book_imbalance_5"))
    spread_bps = _finite(source.get("spread_bps"))
    imbalance_change_5s = _finite(source.get("imbalance_change_5s"))
    try:
        bid_levels = int(source.get("bid_level_count") or 0)
        ask_levels = int(source.get("ask_level_count") or 0)
    except (TypeError, ValueError, OverflowError):
        bid_levels = 0
        ask_levels = 0

    values_valid = bool(
        bid_notional is not None
        and ask_notional is not None
        and bid_notional > 0
        and ask_notional > 0
        and broad_imbalance is not None
        and near_imbalance is not None
        and -1 <= broad_imbalance <= 1
        and -1 <= near_imbalance <= 1
    )
    levels_sufficient = bool(
        bid_levels >= max(1, int(minimum_levels_per_side))
        and ask_levels >= max(1, int(minimum_levels_per_side))
    )
    spread_acceptable = bool(
        spread_bps is not None and 0 <= spread_bps <= float(maximum_spread_bps)
    )
    quality_passed = bool(
        direction_valid
        and fresh
        and values_valid
        and levels_sufficient
        and spread_acceptable
    )
    directional_pressure = (
        direction_sign * (near_imbalance * 0.70 + broad_imbalance * 0.30)
        if direction_valid and values_valid
        else None
    )
    directional_near_pressure = (
        direction_sign * near_imbalance
        if direction_valid and near_imbalance is not None
        else None
    )
    directional_broad_pressure = (
        direction_sign * broad_imbalance
        if direction_valid and broad_imbalance is not None
        else None
    )
    directional_change_5s = (
        direction_sign * imbalance_change_5s
        if direction_valid and imbalance_change_5s is not None
        else None
    )
    direction_conflict = bool(
        quality_passed
        and directional_pressure is not None
        and directional_pressure <= ORDER_BOOK_CONFLICT_PRESSURE
        and directional_near_pressure is not None
        and directional_near_pressure <= ORDER_BOOK_COMPONENT_CONFLICT_PRESSURE
        and directional_broad_pressure is not None
        and directional_broad_pressure <= ORDER_BOOK_COMPONENT_CONFLICT_PRESSURE
    )
    direction_clear = bool(quality_passed and not direction_conflict)
    confirms_direction = bool(
        quality_passed
        and directional_pressure is not None
        and directional_pressure >= ORDER_BOOK_CONFIRM_PRESSURE
        and directional_near_pressure is not None
        and directional_near_pressure >= 0
        and directional_broad_pressure is not None
        and directional_broad_pressure >= 0
    )

    if not source:
        snapshot_status = "missing"
        status = "missing"
        reason = "BINANCE_ORDER_BOOK_MISSING"
    elif not timestamp_valid:
        snapshot_status = "invalid"
        status = "invalid"
        reason = "BINANCE_ORDER_BOOK_CLOCK_SKEW"
    elif not fresh:
        snapshot_status = "stale"
        status = "stale"
        reason = "BINANCE_ORDER_BOOK_STALE"
    else:
        snapshot_status = "fresh"
        if not values_valid:
            status = "invalid"
            reason = "BINANCE_ORDER_BOOK_VALUES_INVALID"
        elif not levels_sufficient:
            status = "insufficient_depth"
            reason = "BINANCE_ORDER_BOOK_LEVELS_INSUFFICIENT"
        elif not spread_acceptable:
            status = "spread_too_wide"
            reason = "BINANCE_ORDER_BOOK_SPREAD_TOO_WIDE"
        elif direction_conflict:
            status = "direction_conflict"
            reason = "BINANCE_ORDER_BOOK_DIRECTION_CONFLICT"
        else:
            status = "passed"
            reason = None

    return {
        "version": "binance_order_book_gate_v1",
        "source": "binance_futures_market_by_price",
        "direction": normalized_direction,
        "status": status,
        "snapshot_status": snapshot_status,
        "passed": direction_clear,
        "quality_passed": quality_passed,
        "direction_clear": direction_clear,
        "direction_conflict": direction_conflict,
        "confirms_direction": confirms_direction,
        "reason": reason,
        "captured_at": captured_at or None,
        "age_seconds": age_seconds,
        "clock_skew_seconds": clock_skew_seconds,
        "maximum_age_seconds": max(1, int(maximum_age_seconds)),
        "future_skew_tolerance_seconds": max(
            0, int(future_skew_tolerance_seconds)
        ),
        "bid_depth_notional": bid_notional,
        "ask_depth_notional": ask_notional,
        "book_imbalance": broad_imbalance,
        "book_imbalance_5": near_imbalance,
        "imbalance_change_5s": imbalance_change_5s,
        "directional_pressure": (
            round(directional_pressure, 8)
            if directional_pressure is not None
            else None
        ),
        "directional_near_pressure": (
            round(directional_near_pressure, 8)
            if directional_near_pressure is not None
            else None
        ),
        "directional_broad_pressure": (
            round(directional_broad_pressure, 8)
            if directional_broad_pressure is not None
            else None
        ),
        "directional_change_5s": (
            round(directional_change_5s, 8)
            if directional_change_5s is not None
            else None
        ),
        "spread_bps": spread_bps,
        "maximum_spread_bps": float(maximum_spread_bps),
        "bid_level_count": bid_levels,
        "ask_level_count": ask_levels,
        "minimum_levels_per_side": max(1, int(minimum_levels_per_side)),
        "checks": {
            "timestamp_valid": timestamp_valid,
            "fresh": fresh,
            "values_valid": values_valid,
            "levels_sufficient": levels_sufficient,
            "spread_acceptable": spread_acceptable,
            "direction_clear": direction_clear,
        },
        "policy": "quality_and_strong_conflict_veto_only",
    }
