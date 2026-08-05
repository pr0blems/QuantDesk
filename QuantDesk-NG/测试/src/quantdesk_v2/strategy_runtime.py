"""Deterministic indicator kernel and constrained strategy evaluator.

This module is the shared calculation boundary for new, full strategies. Legacy
single-indicator engines remain available for compatibility, but new backtest,
paper and future live adapters must call this evaluator instead of reimplementing
entry logic.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal


class StrategySpecError(ValueError):
    """Raised when a full strategy specification is invalid or unsupported."""


class StrategyMarketDataError(ValueError):
    """Raised when strategy market data is malformed."""


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    decision: Literal["LONG_ENTRY", "SHORT_ENTRY", "HOLD", "SKIP"]
    signal_time: int | None
    valid_until: int | None
    confidence: float | None
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]
    risk_proposal: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


INDICATOR_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "key": "ema",
        "name": "EMA 指数移动平均",
        "category": "趋势",
        "outputs": ["value", "slope_pct", "distance_pct"],
        "parameters": [{"key": "period", "type": "integer", "min": 2, "max": 500}],
        "version": 1,
    },
    {
        "key": "atr",
        "name": "ATR 真实波幅",
        "category": "波动",
        "outputs": ["value", "percent"],
        "parameters": [{"key": "period", "type": "integer", "min": 2, "max": 100}],
        "version": 1,
    },
    {
        "key": "adx",
        "name": "ADX 趋势强度",
        "category": "趋势",
        "outputs": ["adx", "plus_di", "minus_di"],
        "parameters": [{"key": "period", "type": "integer", "min": 2, "max": 100}],
        "version": 1,
    },
    {
        "key": "donchian",
        "name": "Donchian 结构通道",
        "category": "突破",
        "outputs": ["prior_high", "prior_low"],
        "parameters": [{"key": "period", "type": "integer", "min": 2, "max": 300}],
        "version": 1,
    },
    {
        "key": "volume_ratio",
        "name": "成交量比",
        "category": "量价",
        "outputs": ["value", "average_volume"],
        "parameters": [{"key": "period", "type": "integer", "min": 2, "max": 300}],
        "version": 1,
    },
)


TREND_PULLBACK_SPEC_V1: dict[str, Any] = {
    "schema_version": 1,
    "strategy_type": "trend_pullback_continuation",
    "market": "BINANCE_TRADIFI_PERPETUAL",
    "directions": ["long", "short"],
    "timeframes": {"regime": "4h", "setup": "1h", "trigger": "15m"},
    "parameters": {
        "regime_fast_ema": 20,
        "regime_slow_ema": 50,
        "regime_adx_period": 14,
        "regime_min_adx": 18.0,
        "setup_ema_period": 20,
        "setup_atr_period": 14,
        "setup_lookback_bars": 4,
        "setup_max_distance_atr": 0.5,
        "trigger_donchian_period": 20,
        "trigger_volume_period": 20,
        "trigger_min_volume_ratio": 1.3,
        "signal_valid_bars": 2,
    },
    "exit": {
        "initial_stop_atr": 1.5,
        "take_profit_r": 2.5,
        "trailing_after_r": 1.0,
        "max_holding_bars": 48,
        "exit_on_regime_break": True,
    },
    "risk": {
        "risk_per_trade_pct": 0.5,
        "max_margin_pct": 10.0,
        "max_leverage": 5,
        "max_open_positions": 5,
        "max_correlated_positions": 2,
        "daily_loss_limit_pct": 3.0,
        "cooldown_bars": 4,
    },
    "execution": {
        "entry_order": "limit_or_cancel",
        "max_slippage_bps": 8.0,
        "order_timeout_seconds": 20,
        "closed_bar_only": True,
    },
}


def full_strategy_parameter_schema() -> list[dict[str, Any]]:
    """Return editable V1 parameters for the full trend-pullback strategy."""

    values = TREND_PULLBACK_SPEC_V1["parameters"]
    return [
        _field("regime_fast_ema", "4h 快速 EMA", "integer", values, 2, 200),
        _field("regime_slow_ema", "4h 慢速 EMA", "integer", values, 3, 500),
        _field("regime_adx_period", "4h ADX 周期", "integer", values, 2, 100),
        _field("regime_min_adx", "最小 ADX", "number", values, 0, 100),
        _field("setup_ema_period", "1h 回踩 EMA", "integer", values, 2, 200),
        _field("setup_atr_period", "1h ATR 周期", "integer", values, 2, 100),
        _field("setup_lookback_bars", "回踩有效 K 线", "integer", values, 1, 20),
        _field("setup_max_distance_atr", "最大回踩距离 ATR", "number", values, 0, 5),
        _field("trigger_donchian_period", "15m 突破周期", "integer", values, 2, 100),
        _field("trigger_volume_period", "15m 均量周期", "integer", values, 2, 100),
        _field("trigger_min_volume_ratio", "最小成交量比", "number", values, 0, 10),
        _field("signal_valid_bars", "信号有效 K 线", "integer", values, 1, 10),
    ]


def _field(
    key: str,
    label: str,
    kind: str,
    values: Mapping[str, Any],
    minimum: int | float,
    maximum: int | float,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "type": kind,
        "default": values[key],
        "min": minimum,
        "max": maximum,
    }


def strategy_spec_hash(spec: Mapping[str, Any]) -> str:
    normalized = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_trend_pullback_spec(
    parameters: Mapping[str, Any] | None = None,
    *,
    risk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = copy.deepcopy(TREND_PULLBACK_SPEC_V1)
    if parameters:
        spec["parameters"].update(parameters)
    if risk:
        spec["risk"].update(risk)
    return validate_strategy_spec(spec)


def validate_strategy_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StrategySpecError("策略定义必须是对象")
    spec = copy.deepcopy(dict(value))
    if spec.get("schema_version") != 1:
        raise StrategySpecError("仅支持策略 DSL v1")
    if spec.get("strategy_type") != "trend_pullback_continuation":
        raise StrategySpecError("不支持的真实策略类型")
    if spec.get("market") != "BINANCE_TRADIFI_PERPETUAL":
        raise StrategySpecError("V1 仅支持 Binance TradFi 永续合约")
    directions = spec.get("directions")
    if (
        not isinstance(directions, list)
        or not directions
        or not set(directions)
        <= {
            "long",
            "short",
        }
    ):
        raise StrategySpecError("策略方向必须是 long、short 或两者")
    timeframes = spec.get("timeframes")
    if not isinstance(timeframes, Mapping) or set(timeframes) != {"regime", "setup", "trigger"}:
        raise StrategySpecError("策略必须定义 regime、setup 和 trigger 周期")
    supported_timeframes = {"15m", "1h", "4h"}
    if any(item not in supported_timeframes for item in timeframes.values()):
        raise StrategySpecError("V1 策略周期仅支持 15m、1h 和 4h")

    parameters = spec.get("parameters")
    schema = {item["key"]: item for item in full_strategy_parameter_schema()}
    spec["parameters"] = _validate_numeric_object(parameters, schema, "策略参数")
    if spec["parameters"]["regime_fast_ema"] >= spec["parameters"]["regime_slow_ema"]:
        raise StrategySpecError("4h 快速 EMA 必须小于慢速 EMA")

    exit_config = spec.get("exit")
    exit_schema = {
        "initial_stop_atr": {"type": "number", "min": 0.1, "max": 10},
        "take_profit_r": {"type": "number", "min": 0.1, "max": 20},
        "trailing_after_r": {"type": "number", "min": 0, "max": 20},
        "max_holding_bars": {"type": "integer", "min": 1, "max": 10_000},
    }
    normalized_exit = _validate_numeric_object(
        exit_config, exit_schema, "退出参数", allow_extra={"exit_on_regime_break"}
    )
    if not isinstance(exit_config.get("exit_on_regime_break"), bool):
        raise StrategySpecError("exit_on_regime_break 必须是布尔值")
    normalized_exit["exit_on_regime_break"] = exit_config["exit_on_regime_break"]
    spec["exit"] = normalized_exit

    risk = spec.get("risk")
    risk_schema = {
        "risk_per_trade_pct": {"type": "number", "min": 0.01, "max": 5},
        "max_margin_pct": {"type": "number", "min": 0.01, "max": 100},
        "max_leverage": {"type": "integer", "min": 1, "max": 20},
        "max_open_positions": {"type": "integer", "min": 1, "max": 100},
        "max_correlated_positions": {"type": "integer", "min": 1, "max": 100},
        "daily_loss_limit_pct": {"type": "number", "min": 0.1, "max": 100},
        "cooldown_bars": {"type": "integer", "min": 0, "max": 10_000},
    }
    spec["risk"] = _validate_numeric_object(risk, risk_schema, "风险参数")

    execution = spec.get("execution")
    if not isinstance(execution, Mapping):
        raise StrategySpecError("执行参数必须是对象")
    if execution.get("entry_order") not in {"market", "limit_or_cancel"}:
        raise StrategySpecError("不支持的入场订单类型")
    if execution.get("closed_bar_only") is not True:
        raise StrategySpecError("真实策略必须只使用已收盘 K 线")
    max_slippage = _finite_number(execution.get("max_slippage_bps"), "最大滑点")
    timeout = _integer(execution.get("order_timeout_seconds"), "订单超时")
    if not 0 <= max_slippage <= 1_000 or not 1 <= timeout <= 600:
        raise StrategySpecError("执行参数超出允许范围")
    spec["execution"] = {
        "entry_order": execution["entry_order"],
        "max_slippage_bps": max_slippage,
        "order_timeout_seconds": timeout,
        "closed_bar_only": True,
    }
    return spec


def _validate_numeric_object(
    value: Any,
    schema: Mapping[str, Mapping[str, Any]],
    label: str,
    *,
    allow_extra: set[str] | None = None,
) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        raise StrategySpecError(f"{label}必须是对象")
    allowed = set(schema) | (allow_extra or set())
    unknown = set(value) - allowed
    missing = set(schema) - set(value)
    if unknown:
        raise StrategySpecError(f"{label}包含未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise StrategySpecError(f"{label}缺少字段：{', '.join(sorted(missing))}")
    output: dict[str, int | float] = {}
    for key, rule in schema.items():
        number = _finite_number(value[key], key)
        if rule["type"] == "integer":
            if not number.is_integer():
                raise StrategySpecError(f"{key} 必须是整数")
            normalized: int | float = int(number)
        else:
            normalized = int(number) if number.is_integer() else number
        if not float(rule["min"]) <= number <= float(rule["max"]):
            raise StrategySpecError(f"{key} 超出允许范围")
        output[key] = normalized
    return output


def evaluate_strategy(
    spec_value: Mapping[str, Any],
    bars_by_timeframe: Mapping[str, Sequence[Mapping[str, Any] | Candle]],
) -> StrategyDecision:
    spec = validate_strategy_spec(spec_value)
    timeframes = spec["timeframes"]
    market = {
        role: normalize_candles(bars_by_timeframe.get(timeframe, []))
        for role, timeframe in timeframes.items()
    }
    parameters = spec["parameters"]
    minimums = {
        "regime": max(
            int(parameters["regime_slow_ema"]) + 2,
            int(parameters["regime_adx_period"]) * 2 + 2,
        ),
        "setup": max(
            int(parameters["setup_ema_period"]) + int(parameters["setup_lookback_bars"]),
            int(parameters["setup_atr_period"]) + int(parameters["setup_lookback_bars"]) + 1,
        ),
        "trigger": max(
            int(parameters["trigger_donchian_period"]) + 1,
            int(parameters["trigger_volume_period"]) + 1,
        ),
    }
    shortages = {
        role: {"required": minimums[role], "actual": len(candles)}
        for role, candles in market.items()
        if len(candles) < minimums[role]
    }
    if shortages:
        return StrategyDecision(
            decision="SKIP",
            signal_time=None,
            valid_until=None,
            confidence=None,
            reason_codes=("INSUFFICIENT_DATA",),
            evidence={"data_requirements": shortages},
            risk_proposal={},
        )

    regime = _regime_features(market["regime"], parameters)
    setup = _setup_features(market["setup"], parameters)
    trigger = _trigger_features(market["trigger"], parameters)
    directions = set(spec["directions"])
    long_regime = (
        regime["fast_ema"] > regime["slow_ema"]
        and regime["fast_slope_pct"] > 0
        and regime["adx"] >= float(parameters["regime_min_adx"])
        and regime["plus_di"] > regime["minus_di"]
    )
    short_regime = (
        regime["fast_ema"] < regime["slow_ema"]
        and regime["fast_slope_pct"] < 0
        and regime["adx"] >= float(parameters["regime_min_adx"])
        and regime["minus_di"] > regime["plus_di"]
    )
    long_trigger = trigger["close"] > trigger["prior_high"] and trigger["volume_ratio"] >= float(
        parameters["trigger_min_volume_ratio"]
    )
    short_trigger = trigger["close"] < trigger["prior_low"] and trigger["volume_ratio"] >= float(
        parameters["trigger_min_volume_ratio"]
    )

    evidence = {"regime": regime, "setup": setup, "trigger": trigger}
    risk = _risk_proposal(spec, setup["atr"], trigger["close"])
    signal_time = market["trigger"][-1].open_time
    trigger_seconds = _timeframe_seconds(timeframes["trigger"])
    timestamp_scale = 1_000 if signal_time >= 100_000_000_000 else 1
    valid_until = signal_time + trigger_seconds * timestamp_scale * int(
        parameters["signal_valid_bars"]
    )

    if "long" in directions and long_regime and setup["long_pullback"] and long_trigger:
        confidence = _confidence(regime["adx"], trigger["volume_ratio"], parameters)
        return StrategyDecision(
            "LONG_ENTRY",
            signal_time,
            valid_until,
            confidence,
            ("REGIME_UP", "PULLBACK_LONG_CONFIRMED", "BREAKOUT_UP", "VOLUME_CONFIRMED"),
            evidence,
            {**risk, "side": "long"},
        )
    if "short" in directions and short_regime and setup["short_pullback"] and short_trigger:
        confidence = _confidence(regime["adx"], trigger["volume_ratio"], parameters)
        return StrategyDecision(
            "SHORT_ENTRY",
            signal_time,
            valid_until,
            confidence,
            ("REGIME_DOWN", "PULLBACK_SHORT_CONFIRMED", "BREAKOUT_DOWN", "VOLUME_CONFIRMED"),
            evidence,
            {**risk, "side": "short"},
        )

    reasons: list[str] = []
    if not long_regime and not short_regime:
        reasons.append("REGIME_NOT_ELIGIBLE")
    elif not setup["long_pullback"] and not setup["short_pullback"]:
        reasons.append("SETUP_NOT_READY")
    elif not long_trigger and not short_trigger:
        reasons.append("TRIGGER_NOT_CONFIRMED")
    return StrategyDecision(
        "HOLD",
        signal_time,
        valid_until,
        None,
        tuple(reasons or ["DIRECTION_DISABLED"]),
        evidence,
        {},
    )


def normalize_candles(rows: Sequence[Mapping[str, Any] | Candle]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if isinstance(row, Candle):
            candle = row
        elif isinstance(row, Mapping):
            try:
                candle = Candle(
                    open_time=int(row.get("open_time", row.get("ts"))),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise StrategyMarketDataError("K 线字段无效") from exc
        else:
            raise StrategyMarketDataError("K 线必须是对象")
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(item) for item in values):
            raise StrategyMarketDataError("K 线包含非有限数值")
        if candle.open_time < 0 or min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise StrategyMarketDataError("K 线时间或价格无效")
        if candle.high < max(candle.open, candle.close, candle.low):
            raise StrategyMarketDataError("K 线最高价无效")
        if candle.low > min(candle.open, candle.close, candle.high):
            raise StrategyMarketDataError("K 线最低价无效")
        if candle.volume < 0:
            raise StrategyMarketDataError("K 线成交量无效")
        candles.append(candle)
    candles.sort(key=lambda item: item.open_time)
    if len({item.open_time for item in candles}) != len(candles):
        raise StrategyMarketDataError("K 线时间重复")
    return candles


def _regime_features(candles: Sequence[Candle], parameters: Mapping[str, Any]) -> dict[str, float]:
    closes = [item.close for item in candles]
    highs = [item.high for item in candles]
    lows = [item.low for item in candles]
    fast = ema_series(closes, int(parameters["regime_fast_ema"]))
    slow = ema_series(closes, int(parameters["regime_slow_ema"]))
    adx, plus_di, minus_di = adx_series(highs, lows, closes, int(parameters["regime_adx_period"]))
    fast_now = _required(fast[-1], "fast EMA")
    fast_previous = _required(fast[-2], "previous fast EMA")
    slow_now = _required(slow[-1], "slow EMA")
    return {
        "close": closes[-1],
        "fast_ema": fast_now,
        "slow_ema": slow_now,
        "fast_slope_pct": (fast_now - fast_previous) / fast_previous * 100,
        "adx": _required(adx[-1], "ADX"),
        "plus_di": _required(plus_di[-1], "+DI"),
        "minus_di": _required(minus_di[-1], "-DI"),
    }


def _setup_features(candles: Sequence[Candle], parameters: Mapping[str, Any]) -> dict[str, Any]:
    closes = [item.close for item in candles]
    highs = [item.high for item in candles]
    lows = [item.low for item in candles]
    ema = ema_series(closes, int(parameters["setup_ema_period"]))
    atr = atr_series(highs, lows, closes, int(parameters["setup_atr_period"]))
    lookback = int(parameters["setup_lookback_bars"])
    max_distance = float(parameters["setup_max_distance_atr"])
    long_pullback = False
    short_pullback = False
    observations: list[dict[str, float]] = []
    for index in range(len(candles) - lookback, len(candles)):
        ema_value = ema[index]
        atr_value = atr[index]
        if ema_value is None or atr_value is None or atr_value <= 0:
            continue
        candle = candles[index]
        long_distance = (candle.low - ema_value) / atr_value
        short_distance = (ema_value - candle.high) / atr_value
        long_touch = (
            candle.low <= ema_value + max_distance * atr_value and candle.close >= ema_value
        )
        short_touch = (
            candle.high >= ema_value - max_distance * atr_value and candle.close <= ema_value
        )
        long_pullback = long_pullback or long_touch
        short_pullback = short_pullback or short_touch
        observations.append(
            {
                "open_time": candle.open_time,
                "ema": ema_value,
                "atr": atr_value,
                "long_distance_atr": long_distance,
                "short_distance_atr": short_distance,
            }
        )
    return {
        "close": closes[-1],
        "ema": _required(ema[-1], "setup EMA"),
        "atr": _required(atr[-1], "setup ATR"),
        "long_pullback": long_pullback,
        "short_pullback": short_pullback,
        "observations": observations,
    }


def _trigger_features(candles: Sequence[Candle], parameters: Mapping[str, Any]) -> dict[str, float]:
    period = int(parameters["trigger_donchian_period"])
    volume_period = int(parameters["trigger_volume_period"])
    previous = candles[-period - 1 : -1]
    prior_high = max(item.high for item in previous)
    prior_low = min(item.low for item in previous)
    volume_window = candles[-volume_period - 1 : -1]
    average_volume = sum(item.volume for item in volume_window) / len(volume_window)
    latest = candles[-1]
    volume_ratio = latest.volume / average_volume if average_volume > 0 else 0.0
    return {
        "open_time": latest.open_time,
        "close": latest.close,
        "prior_high": prior_high,
        "prior_low": prior_low,
        "volume": latest.volume,
        "average_volume": average_volume,
        "volume_ratio": volume_ratio,
    }


def _risk_proposal(spec: Mapping[str, Any], atr: float, price: float) -> dict[str, Any]:
    stop_distance = atr * float(spec["exit"]["initial_stop_atr"])
    return {
        "risk_per_trade_pct": spec["risk"]["risk_per_trade_pct"],
        "max_margin_pct": spec["risk"]["max_margin_pct"],
        "max_leverage": spec["risk"]["max_leverage"],
        "stop_distance": stop_distance,
        "stop_distance_pct": stop_distance / price * 100 if price > 0 else None,
        "take_profit_distance": stop_distance * float(spec["exit"]["take_profit_r"]),
        "max_holding_bars": spec["exit"]["max_holding_bars"],
    }


def _confidence(adx: float, volume_ratio: float, parameters: Mapping[str, Any]) -> float:
    adx_score = min(1.0, max(0.0, (adx - float(parameters["regime_min_adx"])) / 25))
    volume_score = min(
        1.0,
        max(0.0, (volume_ratio - float(parameters["trigger_min_volume_ratio"])) / 2),
    )
    return round(0.6 + 0.25 * adx_score + 0.15 * volume_score, 4)


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if period < 2 or len(values) < period:
        return output
    seed = sum(values[:period]) / period
    output[period - 1] = seed
    alpha = 2 / (period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1 - alpha)
        output[index] = previous
    return output


def atr_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> list[float | None]:
    _equal_lengths(highs, lows, closes)
    output: list[float | None] = [None] * len(closes)
    if period < 2 or len(closes) < period + 1:
        return output
    true_ranges = [0.0]
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    seed = sum(true_ranges[1 : period + 1]) / period
    output[period] = seed
    previous = seed
    for index in range(period + 1, len(closes)):
        previous = (previous * (period - 1) + true_ranges[index]) / period
        output[index] = previous
    return output


def adx_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    _equal_lengths(highs, lows, closes)
    length = len(closes)
    adx: list[float | None] = [None] * length
    plus_di: list[float | None] = [None] * length
    minus_di: list[float | None] = [None] * length
    if period < 2 or length < period * 2 + 1:
        return adx, plus_di, minus_di

    true_ranges = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length
    for index in range(1, length):
        upward = highs[index] - highs[index - 1]
        downward = lows[index - 1] - lows[index]
        plus_dm[index] = upward if upward > downward and upward > 0 else 0.0
        minus_dm[index] = downward if downward > upward and downward > 0 else 0.0
        true_ranges[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )

    tr_smooth = sum(true_ranges[1 : period + 1])
    plus_smooth = sum(plus_dm[1 : period + 1])
    minus_smooth = sum(minus_dm[1 : period + 1])
    dx: list[float | None] = [None] * length
    for index in range(period, length):
        if index > period:
            tr_smooth = tr_smooth - tr_smooth / period + true_ranges[index]
            plus_smooth = plus_smooth - plus_smooth / period + plus_dm[index]
            minus_smooth = minus_smooth - minus_smooth / period + minus_dm[index]
        if tr_smooth <= 0:
            plus_value = minus_value = 0.0
        else:
            plus_value = 100 * plus_smooth / tr_smooth
            minus_value = 100 * minus_smooth / tr_smooth
        plus_di[index] = plus_value
        minus_di[index] = minus_value
        denominator = plus_value + minus_value
        dx[index] = 100 * abs(plus_value - minus_value) / denominator if denominator else 0.0

    seed_index = period * 2 - 1
    seed_values = [item for item in dx[period : seed_index + 1] if item is not None]
    if len(seed_values) != period:
        return adx, plus_di, minus_di
    previous_adx = sum(seed_values) / period
    adx[seed_index] = previous_adx
    for index in range(seed_index + 1, length):
        current_dx = dx[index]
        if current_dx is None:
            continue
        previous_adx = (previous_adx * (period - 1) + current_dx) / period
        adx[index] = previous_adx
    return adx, plus_di, minus_di


def _equal_lengths(*values: Sequence[float]) -> None:
    if len({len(item) for item in values}) != 1:
        raise StrategyMarketDataError("指标输入序列长度不一致")


def _required(value: float | None, label: str) -> float:
    if value is None or not math.isfinite(value):
        raise StrategyMarketDataError(f"{label} 不可用")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategySpecError(f"{label}必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise StrategySpecError(f"{label}必须是有限数字")
    return number


def _integer(value: Any, label: str) -> int:
    number = _finite_number(value, label)
    if not number.is_integer():
        raise StrategySpecError(f"{label}必须是整数")
    return int(number)


def _timeframe_seconds(timeframe: str) -> int:
    values = {"15m": 900, "1h": 3_600, "4h": 14_400}
    try:
        return values[timeframe]
    except KeyError as exc:
        raise StrategySpecError("不支持的策略周期") from exc
