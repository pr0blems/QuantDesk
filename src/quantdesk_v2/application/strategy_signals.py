"""Public strategy-decision service shared by paper and Shadow workers."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import indicators as ind
from ..domain.runtime import (
    DecisionEnvelope,
    build_decision_envelope,
    canonical_event_hash,
)
from ..strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    StrategyCandle,
    StrategyEvaluationError,
    bollinger_bands,
    exponential_moving_average,
    optional_exponential_moving_average,
    relative_strength_index,
    resolve_builtin_strategy_timeframe,
    simple_moving_average,
)
from ..strategy_runtime import (
    StrategyMarketDataError,
    StrategySpecError,
    evaluate_strategy,
    validate_strategy_spec,
)
from ..strategy_source_runtime import (
    StrategySourceError,
    StrategySourceExecutionError,
    evaluate_source,
    validate_source,
)

KlineLoader = Callable[[str, str, int], Sequence[Mapping[str, Any]]]
BuiltinEvidenceBuilder = Callable[
    [str, list[StrategyCandle], dict[str, Any]], dict[str, Any]
]


@dataclass(frozen=True, slots=True)
class EvaluatedStrategySignal:
    """One mode-neutral strategy result before risk and order construction."""

    direction: int
    atr: float | None
    basis: tuple[str, ...]
    signal_time: int | None
    evidence: dict[str, Any]
    envelope: DecisionEnvelope | None
    runtime_decision: Any = None
    audit_spec: dict[str, Any] | None = None

    def execution_tuple(
        self,
    ) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
        return (
            self.direction,
            self.atr,
            list(self.basis),
            self.signal_time,
            dict(self.evidence),
        )


def build_builtin_strategy_evidence(
    engine_key: str,
    candles: list[StrategyCandle],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Explain the exact final-bar values used by the built-in signal engines."""

    closes = [item.close for item in candles]
    if not closes:
        return {}
    index = len(closes) - 1
    evidence: dict[str, Any] = {
        "engine_key": engine_key,
        "market": {
            "open": candles[index].open,
            "high": candles[index].high,
            "low": candles[index].low,
            "close": candles[index].close,
            "volume": candles[index].volume,
        },
        "components": [],
        "reason_codes": [],
    }

    if engine_key == "ma_cross":
        fast = simple_moving_average(closes, int(parameters["fast_period"]))
        slow = simple_moving_average(closes, int(parameters["slow_period"]))
        evidence["indicators"] = {"fast_ma": fast[index], "slow_ma": slow[index]}
        evidence["reason_codes"] = ["MA_CROSS"]
        return evidence

    if engine_key == "macd_momentum":
        fast = exponential_moving_average(closes, int(parameters["fast_period"]))
        slow = exponential_moving_average(closes, int(parameters["slow_period"]))
        macd = [
            left - right if left is not None and right is not None else None
            for left, right in zip(fast, slow, strict=True)
        ]
        signal_line = optional_exponential_moving_average(
            macd, int(parameters["signal_period"])
        )
        histogram = [
            value - signal if value is not None and signal is not None else None
            for value, signal in zip(macd, signal_line, strict=True)
        ]
        evidence["indicators"] = {
            "macd": macd[index],
            "signal": signal_line[index],
            "histogram": histogram[index],
        }
        evidence["reason_codes"] = ["MACD_ZERO_CROSS"]
        return evidence

    if engine_key == "rsi_reversal":
        values = relative_strength_index(closes, int(parameters["period"]))
        evidence["indicators"] = {
            "rsi": values[index],
            "oversold": float(parameters["oversold"]),
            "overbought": float(parameters["overbought"]),
        }
        evidence["reason_codes"] = ["RSI_REENTRY"]
        return evidence

    if engine_key == "bollinger_reversion":
        middle, lower, upper = bollinger_bands(
            closes, int(parameters["period"]), float(parameters["stddev"])
        )
        evidence["indicators"] = {
            "middle": middle[index],
            "lower": lower[index],
            "upper": upper[index],
        }
        evidence["reason_codes"] = ["BOLLINGER_REENTRY"]
        return evidence

    fast = simple_moving_average(closes, int(parameters["fast_period"]))
    slow = simple_moving_average(closes, int(parameters["slow_period"]))
    ema_fast = exponential_moving_average(closes, 12)
    ema_slow = exponential_moving_average(closes, 26)
    rsi = relative_strength_index(closes, int(parameters["rsi_period"]))
    _, lower, upper = bollinger_bands(closes, 20, 2)
    components: list[dict[str, Any]] = []
    score = 0

    ma_value = 1 if fast[index] > slow[index] else -1
    score += ma_value
    components.append(
        {"code": "MA_BULLISH" if ma_value > 0 else "MA_BEARISH", "value": ma_value}
    )
    macd_value = 1 if ema_fast[index] > ema_slow[index] else -1
    score += macd_value
    components.append(
        {
            "code": "MACD_BULLISH" if macd_value > 0 else "MACD_BEARISH",
            "value": macd_value,
        }
    )
    rsi_value = 0
    if rsi[index] is not None and rsi[index] <= 35:
        rsi_value = 1
    elif rsi[index] is not None and rsi[index] >= 65:
        rsi_value = -1
    score += rsi_value
    components.append(
        {
            "code": (
                "RSI_OVERSOLD"
                if rsi_value > 0
                else "RSI_OVERBOUGHT"
                if rsi_value < 0
                else "RSI_NEUTRAL"
            ),
            "value": rsi_value,
        }
    )
    band_value = 0
    if lower[index] is not None and closes[index] < lower[index]:
        band_value = 1
    elif upper[index] is not None and closes[index] > upper[index]:
        band_value = -1
    score += band_value
    components.append(
        {
            "code": (
                "BELOW_LOWER_BAND"
                if band_value > 0
                else "ABOVE_UPPER_BAND"
                if band_value < 0
                else "INSIDE_BANDS"
            ),
            "value": band_value,
        }
    )
    evidence.update(
        {
            "score": score,
            "threshold": float(parameters["threshold"]),
            "components": components,
            "reason_codes": [item["code"] for item in components],
            "indicators": {
                "fast_ma": fast[index],
                "slow_ma": slow[index],
                "ema12": ema_fast[index],
                "ema26": ema_slow[index],
                "rsi": rsi[index],
                "bollinger_lower": lower[index],
                "bollinger_upper": upper[index],
            },
        }
    )
    return evidence


def evaluate_strategy_snapshot(
    snapshot: Mapping[str, Any] | None,
    symbol: str,
    config: Mapping[str, Any] | None,
    *,
    load_klines: KlineLoader,
    evidence_builder: BuiltinEvidenceBuilder | None = None,
    full_validator: Callable[[Any], dict[str, Any]] = validate_strategy_spec,
    full_evaluator: Callable[[dict[str, Any], dict[str, Any]], Any] = evaluate_strategy,
    source_validator: Callable[[str, str], Any] = validate_source,
    source_evaluator: Callable[..., Any] = evaluate_source,
    clock: Callable[[], float] | None = None,
) -> EvaluatedStrategySignal:
    """Evaluate one immutable revision without depending on an execution mode."""

    selected = dict(snapshot or {})
    strategy_kind = str(selected.get("strategy_kind") or "builtin_strategy")
    if strategy_kind == "source_strategy":
        return _evaluate_source(
            selected,
            symbol,
            load_klines,
            clock or time.time,
            source_validator,
            source_evaluator,
        )
    if strategy_kind == "full_strategy":
        return _evaluate_full(
            selected,
            symbol,
            load_klines,
            full_validator,
            full_evaluator,
        )
    if strategy_kind == "builtin_strategy":
        return _evaluate_builtin(
            selected,
            symbol,
            config,
            load_klines,
            evidence_builder,
        )
    return _empty(f"不支持的策略类型：{strategy_kind}")


def _evaluate_builtin(
    snapshot: dict[str, Any],
    symbol: str,
    config: Mapping[str, Any] | None,
    load_klines: KlineLoader,
    evidence_builder: BuiltinEvidenceBuilder | None,
) -> EvaluatedStrategySignal:
    engine_key = str(snapshot.get("engine_key") or "multi_factor")
    parameters = _object(snapshot.get("parameters"))
    try:
        timeframe = resolve_builtin_strategy_timeframe(snapshot, config)
        rows = [dict(row) for row in load_klines(symbol, timeframe, 600)]
        candles = _candles(rows)
        envelopes = DEFAULT_STRATEGY_EVALUATOR.evaluate_envelopes(
            engine_key,
            candles,
            parameters,
            symbol=symbol,
            timeframe=timeframe,
            revision_fingerprint=_fingerprint(snapshot),
        )
    except (KeyError, StrategyEvaluationError, TypeError, ValueError):
        return _empty()
    if len(candles) < 3 or not rows or not envelopes:
        return _empty()
    envelope = envelopes[-1]
    atr = _atr(rows)
    evidence = {
        "engine_key": engine_key,
        "reason_codes": list(envelope.reason_codes),
    }
    if evidence_builder is not None:
        try:
            evidence.update(evidence_builder(engine_key, candles, parameters))
        except (KeyError, TypeError, ValueError):
            # Display-only evidence must not invalidate a strategy decision.
            # A short warm-up window can leave some indicators undefined.
            pass
    evidence.update(
        {
            "decision": envelope.decision.value,
            "decision_envelope": envelope.snapshot(),
            "reference_price": float(rows[-1]["close"]),
        }
    )
    basis = [
        f"策略：{snapshot.get('name') or engine_key}",
        f"引擎：{engine_key}",
        f"周期：{timeframe}",
    ]
    reason_codes = evidence.get("reason_codes")
    if isinstance(reason_codes, list) and reason_codes:
        basis.append(f"信号：{' / '.join(str(item) for item in reason_codes)}")
    if evidence.get("score") is not None:
        basis.append(
            f"实际评分：{evidence['score']:+g} / 阈值：{evidence.get('threshold', '--')}"
        )
    return EvaluatedStrategySignal(
        envelope.direction,
        atr,
        tuple(basis),
        int(rows[-1]["open_time"]),
        evidence,
        envelope,
    )


def _evaluate_full(
    snapshot: dict[str, Any],
    symbol: str,
    load_klines: KlineLoader,
    validator: Callable[[Any], dict[str, Any]],
    evaluator: Callable[[dict[str, Any], dict[str, Any]], Any],
) -> EvaluatedStrategySignal:
    raw_spec = snapshot.get("spec") or snapshot.get("spec_json")
    try:
        spec = validator(raw_spec)
        market = {
            timeframe: [dict(row) for row in load_klines(symbol, timeframe, 600)]
            for timeframe in set(spec["timeframes"].values())
        }
        decision = evaluator(spec, market)
    except (
        StrategySpecError,
        StrategyMarketDataError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return _empty(f"完整策略不可用：{type(exc).__name__}")
    try:
        envelope = _runtime_envelope(
            snapshot,
            symbol,
            spec["timeframes"]["trigger"],
            decision,
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return _empty("完整策略不可用：invalid_signal_time")
    atr = _decision_atr(decision)
    evidence = _decision_evidence(decision, envelope)
    reference_price = _latest_close(market.get(spec["timeframes"]["trigger"]))
    if reference_price is not None:
        evidence["reference_price"] = reference_price
    basis = [
        f"策略：{snapshot.get('name') or spec['strategy_type']}",
        "类型：完整策略",
        "周期："
        f"{spec['timeframes']['regime']}/{spec['timeframes']['setup']}/"
        f"{spec['timeframes']['trigger']}",
        f"决策：{decision.decision}",
        f"依据：{' / '.join(decision.reason_codes or ())}",
    ]
    if decision.confidence is not None:
        basis.append(f"置信度：{decision.confidence:.2%}")
    return EvaluatedStrategySignal(
        envelope.direction,
        atr,
        tuple(basis),
        decision.signal_time,
        evidence,
        envelope,
        decision,
        spec,
    )


def _evaluate_source(
    snapshot: dict[str, Any],
    symbol: str,
    load_klines: KlineLoader,
    clock: Callable[[], float],
    validator: Callable[[str, str], Any],
    evaluator: Callable[..., Any],
) -> EvaluatedStrategySignal:
    source_code = snapshot.get("source_code")
    language = str(snapshot.get("source_language") or "python")
    parameters = _object(snapshot.get("parameters"))
    try:
        if not isinstance(source_code, str) or not source_code.strip():
            raise StrategySourceError("策略源码不可用")
        metadata = validator(source_code, language)
        market = {
            timeframe: [
                {
                    "open_time": int(row["open_time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                for row in load_klines(symbol, timeframe, metadata.lookback_bars)
            ]
            for timeframe in metadata.timeframes
        }
        decision = evaluator(
            source_code,
            {
                "symbol": symbol,
                "decision_time": int(clock()),
                "bars": market,
            },
            parameters,
            language=language,
        )
    except (
        StrategySourceError,
        StrategySourceExecutionError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return _empty(f"源码策略不可用：{type(exc).__name__}")
    try:
        envelope = _runtime_envelope(
            snapshot,
            symbol,
            metadata.trigger_timeframe,
            decision,
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return _empty("源码策略不可用：invalid_signal_time")
    atr = _decision_atr(decision)
    evidence = {
        "source": "python_source_strategy_v1",
        "source_hash": metadata.source_hash,
        "runtime_version": metadata.runtime_version,
        **_decision_evidence(decision, envelope),
    }
    reference_price = _latest_close(market.get(metadata.trigger_timeframe))
    if reference_price is not None:
        evidence["reference_price"] = reference_price
    basis = [
        f"策略：{snapshot.get('name') or 'Python 源码策略'}",
        f"类型：源码策略 / {language}",
        f"周期：{'/'.join(metadata.timeframes)}（触发 {metadata.trigger_timeframe}）",
        f"决策：{decision.decision}",
    ]
    if decision.reason_codes:
        basis.append(f"依据：{' / '.join(decision.reason_codes)}")
    if decision.confidence is not None:
        basis.append(f"置信度：{decision.confidence:.2%}")
    return EvaluatedStrategySignal(
        envelope.direction,
        atr,
        tuple(basis),
        decision.signal_time,
        evidence,
        envelope,
        decision,
        {"timeframes": {"trigger": metadata.trigger_timeframe}},
    )


def _runtime_envelope(
    snapshot: Mapping[str, Any],
    symbol: str,
    timeframe: str,
    decision: Any,
) -> DecisionEnvelope:
    event_time = _timestamp(decision.signal_time)
    valid_until = (
        _timestamp(decision.valid_until)
        if decision.valid_until is not None
        else None
    )
    try:
        confidence = (
            Decimal(str(decision.confidence))
            if decision.confidence is not None
            else None
        )
    except (InvalidOperation, TypeError, ValueError):
        confidence = None
    return build_decision_envelope(
        revision_fingerprint=_fingerprint(snapshot),
        event_id=canonical_event_hash(
            {
                "type": "bar_closed",
                "symbol": symbol.strip().upper(),
                "timeframe": timeframe,
                "event_time": event_time.isoformat(),
            }
        ),
        symbol=symbol,
        timeframe=timeframe,
        event_time=event_time,
        decision=decision.decision,
        confidence=confidence,
        reason_codes=tuple(decision.reason_codes or ()),
        evidence=dict(decision.evidence or {}),
        risk_proposal=dict(decision.risk_proposal or {}),
        valid_until=valid_until,
    )


def _decision_evidence(
    decision: Any,
    envelope: DecisionEnvelope,
) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "confidence": decision.confidence,
        "valid_until": decision.valid_until,
        "reason_codes": list(decision.reason_codes),
        "evidence": decision.evidence,
        "risk_proposal": decision.risk_proposal,
        "decision_envelope": envelope.snapshot(),
    }


def _decision_atr(decision: Any) -> float | None:
    candidates: list[Any] = []
    setup = getattr(decision, "evidence", {}).get("setup")
    if isinstance(setup, Mapping):
        candidates.append(setup.get("atr"))
    candidates.extend(
        source.get("atr")
        for source in (
            getattr(decision, "evidence", {}),
            getattr(decision, "risk_proposal", {}),
        )
        if isinstance(source, Mapping)
    )
    for value in candidates:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return None


def _atr(rows: Sequence[Mapping[str, Any]]) -> float | None:
    if len(rows) <= 15:
        return None
    try:
        value = ind.atr(
            [float(row["high"]) for row in rows],
            [float(row["low"]) for row in rows],
            [float(row["close"]) for row in rows],
        )
    except (KeyError, TypeError, ValueError):
        return None
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _latest_close(rows: Any) -> float | None:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        return None
    latest = rows[-1]
    if not isinstance(latest, Mapping):
        return None
    try:
        price = float(latest["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _candles(rows: Sequence[Mapping[str, Any]]) -> list[StrategyCandle]:
    return [
        StrategyCandle(
            ts=_epoch_seconds(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for row in rows
    ]


def _epoch_seconds(value: Any) -> int:
    timestamp = int(value)
    while timestamp >= 100_000_000_000:
        timestamp //= 1_000
    return timestamp


def _timestamp(value: Any) -> datetime:
    return datetime.fromtimestamp(_epoch_seconds(value), tz=UTC)


def _fingerprint(snapshot: Mapping[str, Any]) -> str:
    return str(snapshot.get("source_hash") or snapshot.get("spec_hash") or "").strip() or (
        canonical_event_hash(
            {
                "public_id": snapshot.get("public_id"),
                "version": snapshot.get("version"),
                "strategy_kind": snapshot.get("strategy_kind"),
                "engine_key": snapshot.get("engine_key"),
                "parameters": snapshot.get("parameters"),
            }
        )
    )


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _empty(reason: str | None = None) -> EvaluatedStrategySignal:
    return EvaluatedStrategySignal(
        0,
        None,
        (reason,) if reason else (),
        None,
        {},
        None,
    )
