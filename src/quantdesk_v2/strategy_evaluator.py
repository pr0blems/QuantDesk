"""Shared deterministic evaluator for built-in indicator strategies.

The backtest and paper runtimes deliberately depend on this module instead of
depending on each other.  Full, versioned strategy specifications continue to
use :mod:`quantdesk_v2.strategy_runtime`; this evaluator is the compatibility
boundary for the built-in single-timeframe engines.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .domain.runtime import (
    DecisionEnvelope,
    StrategyDecisionType,
    build_decision_envelope,
    canonical_event_hash,
)

DEFAULT_BUILTIN_STRATEGY_TIMEFRAME = "4h"
SUPPORTED_STRATEGY_TIMEFRAMES = ("15m", "1h", "4h")
_TIMEFRAME_SECONDS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
_MISSING = object()


class StrategyEvaluationError(ValueError):
    """Raised when a strategy cannot be evaluated deterministically."""


@dataclass(frozen=True, slots=True)
class StrategyCandle:
    """Normalized OHLCV input shared by deterministic execution modes."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class TimeframePolicy:
    """Immutable trigger interval and holding horizon used by every runtime."""

    trigger_timeframe: str
    timeframe_seconds: int
    max_holding_bars: int

    @property
    def max_holding_seconds(self) -> int:
        if self.max_holding_bars <= 0:
            return 0
        return self.max_holding_bars * self.timeframe_seconds

    def expired(self, *, opened_at: int | float, observed_at: int | float) -> bool:
        if self.max_holding_seconds <= 0:
            return False
        try:
            opened = float(opened_at)
            observed = float(observed_at)
        except (TypeError, ValueError, OverflowError) as exc:
            raise StrategyEvaluationError("invalid holding-period timestamp") from exc
        if not math.isfinite(opened) or not math.isfinite(observed):
            raise StrategyEvaluationError("invalid holding-period timestamp")
        return observed - opened >= self.max_holding_seconds

    def snapshot(self) -> dict[str, int | str]:
        return {
            "trigger_timeframe": self.trigger_timeframe,
            "timeframe_seconds": self.timeframe_seconds,
            "max_holding_bars": self.max_holding_bars,
            "max_holding_seconds": self.max_holding_seconds,
        }


# Compatibility name for callers deployed before the unified policy was named.
StrategyTimingPolicy = TimeframePolicy


class StrategyEvaluator:
    """Evaluate one built-in indicator strategy over an ordered candle series."""

    def evaluate(
        self,
        strategy_id: str,
        candles: Sequence[StrategyCandle],
        params: Mapping[str, int | float],
    ) -> list[int]:
        """Public strategy-evaluation entry point for deterministic engines."""

        return self.evaluate_builtin(strategy_id, candles, params)

    def evaluate_builtin(
        self,
        strategy_id: str,
        candles: Sequence[StrategyCandle],
        params: Mapping[str, int | float],
    ) -> list[int]:
        """Return ``-1``, ``0`` or ``1`` for every input candle.

        Signals are emitted on the candle that confirms the condition.  Order
        timing remains the responsibility of the caller, so the backtest can
        keep filling a confirmed signal at the next candle open.
        """

        closes = [item.close for item in candles]
        signals = [0] * len(candles)
        if strategy_id == "ma_cross":
            fast = simple_moving_average(closes, int(params["fast_period"]))
            slow = simple_moving_average(closes, int(params["slow_period"]))
            return cross_signals(fast, slow)
        if strategy_id == "macd_momentum":
            fast = exponential_moving_average(closes, int(params["fast_period"]))
            slow = exponential_moving_average(closes, int(params["slow_period"]))
            macd = [
                fast_value - slow_value
                if fast_value is not None and slow_value is not None
                else None
                for fast_value, slow_value in zip(fast, slow, strict=True)
            ]
            signal_line = optional_exponential_moving_average(
                macd, int(params["signal_period"])
            )
            histogram = [
                value - signal if value is not None and signal is not None else None
                for value, signal in zip(macd, signal_line, strict=True)
            ]
            return zero_cross_signals(histogram)
        if strategy_id == "rsi_reversal":
            rsi = relative_strength_index(closes, int(params["period"]))
            oversold = float(params["oversold"])
            overbought = float(params["overbought"])
            for index in range(1, len(candles)):
                previous, current = rsi[index - 1], rsi[index]
                if previous is None or current is None:
                    continue
                if previous <= oversold < current:
                    signals[index] = 1
                elif previous >= overbought > current:
                    signals[index] = -1
            return signals
        if strategy_id == "bollinger_reversion":
            _, lower, upper = bollinger_bands(
                closes, int(params["period"]), float(params["stddev"])
            )
            for index in range(1, len(candles)):
                if lower[index - 1] is None or lower[index] is None:
                    continue
                if closes[index - 1] < lower[index - 1] and closes[index] >= lower[index]:
                    signals[index] = 1
                elif closes[index - 1] > upper[index - 1] and closes[index] <= upper[index]:
                    signals[index] = -1
            return signals
        if strategy_id != "multi_factor":
            raise StrategyEvaluationError(f"unsupported built-in strategy: {strategy_id}")

        fast = simple_moving_average(closes, int(params["fast_period"]))
        slow = simple_moving_average(closes, int(params["slow_period"]))
        macd_fast = exponential_moving_average(closes, 12)
        macd_slow = exponential_moving_average(closes, 26)
        rsi = relative_strength_index(closes, int(params["rsi_period"]))
        _, lower, upper = bollinger_bands(closes, 20, 2)
        scores: list[float | None] = [None] * len(candles)
        for index, close in enumerate(closes):
            if fast[index] is None or slow[index] is None:
                continue
            score = 1 if fast[index] > slow[index] else -1
            if macd_fast[index] is not None and macd_slow[index] is not None:
                score += 1 if macd_fast[index] > macd_slow[index] else -1
            if rsi[index] is not None:
                if rsi[index] <= 35:
                    score += 1
                elif rsi[index] >= 65:
                    score -= 1
            if lower[index] is not None:
                if close < lower[index]:
                    score += 1
                elif close > upper[index]:
                    score -= 1
            scores[index] = score
        threshold = float(params["threshold"])
        for index, score in enumerate(scores):
            if score is None:
                continue
            previous = scores[index - 1] if index else None
            if score >= threshold and (previous is None or previous < threshold):
                signals[index] = 1
            elif score <= -threshold and (previous is None or previous > -threshold):
                signals[index] = -1
        return signals

    def evaluate_envelopes(
        self,
        strategy_id: str,
        candles: Sequence[StrategyCandle],
        params: Mapping[str, int | float],
        *,
        symbol: str,
        timeframe: str,
        revision_fingerprint: str | None = None,
    ) -> list[DecisionEnvelope]:
        """Evaluate built-in signals into the canonical cross-mode contract."""

        normalized_timeframe = _validate_timeframe(timeframe)
        fingerprint = revision_fingerprint or canonical_event_hash(
            {
                "strategy_id": str(strategy_id),
                "params": dict(params),
                "timeframe": normalized_timeframe,
            }
        )
        signals = self.evaluate_builtin(strategy_id, candles, params)
        validity = timedelta(seconds=strategy_timeframe_seconds(normalized_timeframe))
        envelopes: list[DecisionEnvelope] = []
        for candle, direction in zip(candles, signals, strict=True):
            event_time = datetime.fromtimestamp(int(candle.ts), tz=UTC)
            decision = {
                1: StrategyDecisionType.LONG_ENTRY,
                -1: StrategyDecisionType.SHORT_ENTRY,
            }.get(int(direction), StrategyDecisionType.HOLD)
            event_id = canonical_event_hash(
                {
                    "type": "bar_closed",
                    "symbol": str(symbol).strip().upper(),
                    "timeframe": normalized_timeframe,
                    "open_time": int(candle.ts),
                }
            )
            envelopes.append(
                build_decision_envelope(
                    revision_fingerprint=fingerprint,
                    event_id=event_id,
                    symbol=symbol,
                    timeframe=normalized_timeframe,
                    event_time=event_time,
                    decision=decision,
                    confidence=(
                        Decimal("1")
                        if decision is not StrategyDecisionType.HOLD
                        else None
                    ),
                    reason_codes=(
                        f"LEGACY_{decision.value}",
                    ),
                    evidence={
                        "engine_key": str(strategy_id),
                        "signal": int(direction),
                    },
                    valid_until=event_time + validity,
                )
            )
        return envelopes


DEFAULT_STRATEGY_EVALUATOR = StrategyEvaluator()


def resolve_builtin_strategy_timeframe(
    snapshot: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    default: str = DEFAULT_BUILTIN_STRATEGY_TIMEFRAME,
) -> str:
    """Resolve an immutable built-in strategy's candle timeframe.

    A timeframe embedded in the strategy snapshot wins over an account-level
    runtime override.  Snapshots created before timeframes were explicit retain
    the historical ``4h`` behavior.  An explicitly configured but unsupported
    value is rejected instead of silently trading on a different timeframe.
    """

    for source in (snapshot, config):
        candidate = _configured_timeframe(source)
        if candidate is not _MISSING:
            return _validate_timeframe(candidate)
    return _validate_timeframe(default)


def strategy_timeframe_seconds(timeframe: str) -> int:
    """Return seconds for a supported execution timeframe."""

    normalized = _validate_timeframe(timeframe)
    return _TIMEFRAME_SECONDS[normalized]


def resolve_strategy_trigger_timeframe(
    snapshot: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Resolve the immutable trigger timeframe for every strategy kind."""

    selected = snapshot if isinstance(snapshot, Mapping) else {}
    strategy_kind = str(selected.get("strategy_kind") or "builtin_strategy")
    if strategy_kind == "source_strategy":
        validation = selected.get("source_validation")
        requirements = (
            validation.get("data_requirements")
            if isinstance(validation, Mapping)
            else None
        )
        candidate = (
            validation.get("trigger_timeframe")
            if isinstance(validation, Mapping)
            else None
        )
        if candidate is None and isinstance(requirements, Mapping):
            candidate = requirements.get("trigger_timeframe")
        if candidate is None:
            raise StrategyEvaluationError(
                "source strategy trigger timeframe is unavailable"
            )
        return _validate_timeframe(candidate)
    if strategy_kind == "full_strategy":
        spec = selected.get("spec") or selected.get("spec_json")
        timeframes = spec.get("timeframes") if isinstance(spec, Mapping) else None
        candidate = timeframes.get("trigger") if isinstance(timeframes, Mapping) else None
        if candidate is None:
            raise StrategyEvaluationError(
                "full strategy trigger timeframe is unavailable"
            )
        return _validate_timeframe(candidate)
    return resolve_builtin_strategy_timeframe(selected, config)


def resolve_strategy_timing_policy(
    snapshot: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    evidence: Mapping[str, Any] | None = None,
    captured: Mapping[str, Any] | None = None,
    default_max_holding_bars: int = 12,
) -> TimeframePolicy:
    """Resolve the immutable timing policy used for one position."""

    if isinstance(captured, Mapping) and captured:
        timeframe = _validate_timeframe(captured.get("trigger_timeframe"))
        max_holding_bars = _validate_max_holding_bars(
            captured.get("max_holding_bars")
        )
        timeframe_seconds = strategy_timeframe_seconds(timeframe)
        captured_seconds = captured.get("timeframe_seconds")
        if captured_seconds is not None and captured_seconds != timeframe_seconds:
            raise StrategyEvaluationError("captured strategy timeframe is inconsistent")
        return TimeframePolicy(timeframe, timeframe_seconds, max_holding_bars)

    selected = snapshot if isinstance(snapshot, Mapping) else {}
    timeframe = resolve_strategy_trigger_timeframe(selected, config)
    candidates: list[Any] = []
    if isinstance(evidence, Mapping):
        for key in ("risk_proposal", "risk_decision", "risk_plan"):
            risk_proposal = evidence.get(key)
            if isinstance(risk_proposal, Mapping):
                candidates.append(risk_proposal.get("max_holding_bars", _MISSING))
    if str(selected.get("strategy_kind") or "") == "full_strategy":
        spec = selected.get("spec") or selected.get("spec_json")
        exit_policy = spec.get("exit") if isinstance(spec, Mapping) else None
        if isinstance(exit_policy, Mapping):
            candidates.append(exit_policy.get("max_holding_bars", _MISSING))
    risk_defaults = selected.get("risk_defaults")
    if isinstance(risk_defaults, Mapping):
        candidates.append(risk_defaults.get("max_holding_bars", _MISSING))
    if isinstance(config, Mapping):
        candidates.append(config.get("max_holding_bars", _MISSING))
    candidates.append(default_max_holding_bars)
    max_holding_bars = next(
        _validate_max_holding_bars(candidate)
        for candidate in candidates
        if candidate is not _MISSING and candidate is not None
    )
    return TimeframePolicy(
        timeframe,
        strategy_timeframe_seconds(timeframe),
        max_holding_bars,
    )


def simple_moving_average(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    rolling = 0.0
    for index, value in enumerate(values):
        rolling += value
        if index >= period:
            rolling -= values[index - period]
        if index >= period - 1:
            result[index] = rolling / period
    return result


def exponential_moving_average(
    values: Sequence[float], period: int
) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2 / (period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1 - alpha)
        result[index] = previous
    return result


def optional_exponential_moving_average(
    values: Sequence[float | None], period: int
) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    valid = [(index, value) for index, value in enumerate(values) if value is not None]
    if len(valid) < period:
        return result
    seed_values = [float(value) for _, value in valid[:period]]
    seed = sum(seed_values) / period
    seed_index = valid[period - 1][0]
    result[seed_index] = seed
    alpha = 2 / (period + 1)
    previous = seed
    for index, value in valid[period:]:
        previous = float(value) * alpha + previous * (1 - alpha)
        result[index] = previous
    return result


def relative_strength_index(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[index] - values[index - 1], 0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0) for index in range(1, len(values))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    result[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        average_gain = (average_gain * (period - 1) + gains[index - 1]) / period
        average_loss = (average_loss * (period - 1) + losses[index - 1]) / period
        result[index] = rsi_value(average_gain, average_loss)
    return result


def bollinger_bands(
    values: Sequence[float], period: int, multiplier: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    middle: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    upper: list[float | None] = [None] * len(values)
    rolling = 0.0
    rolling_squared = 0.0
    for index, value in enumerate(values):
        rolling += value
        rolling_squared += value * value
        if index >= period:
            removed = values[index - period]
            rolling -= removed
            rolling_squared -= removed * removed
        if index >= period - 1:
            mean = rolling / period
            variance = max(0.0, rolling_squared / period - mean * mean)
            deviation = math.sqrt(variance) * multiplier
            middle[index] = mean
            lower[index] = mean - deviation
            upper[index] = mean + deviation
    return middle, lower, upper


def _configured_timeframe(source: Mapping[str, Any] | None) -> Any:
    if not isinstance(source, Mapping):
        return _MISSING
    for key in ("strategy_timeframe", "timeframe", "primary_timeframe", "trigger_timeframe"):
        if key in source:
            return source[key]
    timeframes = source.get("timeframes")
    if isinstance(timeframes, Mapping) and "trigger" in timeframes:
        return timeframes["trigger"]
    return _MISSING


def _validate_timeframe(value: Any) -> str:
    if not isinstance(value, str):
        raise StrategyEvaluationError("strategy timeframe must be a string")
    normalized = value.strip()
    if normalized not in _TIMEFRAME_SECONDS:
        raise StrategyEvaluationError(f"unsupported strategy timeframe: {normalized or '<empty>'}")
    return normalized


def _validate_max_holding_bars(value: Any) -> int:
    if isinstance(value, bool):
        raise StrategyEvaluationError("max holding bars must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrategyEvaluationError("max holding bars must be an integer") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise StrategyEvaluationError("max holding bars must be an integer")
    bars = int(numeric)
    if bars < 0 or bars > 50_000:
        raise StrategyEvaluationError("max holding bars is outside the supported range")
    return bars


def cross_signals(
    fast: Sequence[float | None], slow: Sequence[float | None]
) -> list[int]:
    signals = [0] * len(fast)
    for index in range(1, len(fast)):
        values = (fast[index - 1], slow[index - 1], fast[index], slow[index])
        if any(value is None for value in values):
            continue
        previous_fast, previous_slow, current_fast, current_slow = values
        if previous_fast <= previous_slow and current_fast > current_slow:
            signals[index] = 1
        elif previous_fast >= previous_slow and current_fast < current_slow:
            signals[index] = -1
    return signals


def zero_cross_signals(values: Sequence[float | None]) -> list[int]:
    signals = [0] * len(values)
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if previous is None or current is None:
            continue
        if previous <= 0 < current:
            signals[index] = 1
        elif previous >= 0 > current:
            signals[index] = -1
    return signals


def rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain else 50.0
    ratio = average_gain / average_loss
    return 100 - 100 / (1 + ratio)
