"""Shared performance metrics for every backtest engine."""

from __future__ import annotations

import math
from collections.abc import Sequence

SECONDS_PER_YEAR = 365 * 86_400
MAXIMUM_ANNUALIZED_RETURN_PCT = 1_000_000_000.0


def annualized_return_pct(
    initial_equity: float,
    final_equity: float,
    duration_seconds: int,
) -> float | None:
    """Return the compounded annual return without emitting non-finite JSON values."""

    if initial_equity <= 0 or duration_seconds <= 0:
        return None
    if final_equity <= 0:
        return -100.0
    exponent = math.log(final_equity / initial_equity) * (
        SECONDS_PER_YEAR / duration_seconds
    )
    if exponent >= math.log1p(MAXIMUM_ANNUALIZED_RETURN_PCT / 100):
        return MAXIMUM_ANNUALIZED_RETURN_PCT
    if exponent <= -50:
        return -100.0
    value = math.expm1(exponent) * 100
    return round(value, 4) if math.isfinite(value) else None


def annualized_sharpe_ratio(
    equity_values: Sequence[float],
    interval_seconds: int | None,
) -> float | None:
    """Calculate zero-risk-rate Sharpe from consecutive per-bar equity returns."""

    returns: list[float] = []
    for previous, current in zip(equity_values, equity_values[1:], strict=False):
        if previous > 0:
            value = current / previous - 1
            if math.isfinite(value):
                returns.append(value)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0 or not interval_seconds:
        return None
    periods_per_year = SECONDS_PER_YEAR / interval_seconds
    sharpe = mean / math.sqrt(variance) * math.sqrt(periods_per_year)
    return round(sharpe, 4) if math.isfinite(sharpe) else None
