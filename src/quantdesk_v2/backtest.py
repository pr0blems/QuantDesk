from __future__ import annotations

import copy
import json
import math
from bisect import bisect_right
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .domain.exit_policy import DEFAULT_EXIT_POLICY
from .market_data_client import fetch_klines_range
from .strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    SUPPORTED_STRATEGY_TIMEFRAMES,
    StrategyCandle,
)
from .strategy_runtime import (
    StrategyMarketDataError,
    StrategySpecError,
    evaluate_strategy,
    full_strategy_parameter_schema,
    strategy_required_bars,
    validate_strategy_spec,
)
from .strategy_source_runtime import (
    StrategySourceError,
    StrategySourceExecutionError,
    evaluate_source_many,
    validate_source,
)


class BacktestUnavailable(RuntimeError):
    """Raised when a backtest cannot be validated or calculated."""


def _bind_params(sql: str, params: tuple[Any, ...]):
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise BacktestUnavailable("invalid market data query parameters")
    statement = "".join(
        part + (f":p{index}" if index < len(params) else "")
        for index, part in enumerate(parts)
    )
    return text(statement), {f"p{index}": value for index, value in enumerate(params)}


MAX_BARS = 50_000
MAX_EQUITY_POINTS = 1_500
MAX_RETURNED_TRADES = 5_000
MAINTENANCE_MARGIN_RATE = 0.005
SUPPORTED_BACKTEST_TIMEFRAMES = SUPPORTED_STRATEGY_TIMEFRAMES
SUPPORTED_MARKET_DATA_TIMEFRAMES = frozenset(
    {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
)


STRATEGY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "multi_factor",
        "name": "多因子评分",
        "category": "综合",
        "description": "综合趋势、MACD、RSI 与布林带，达到评分阈值后交易。",
        "params": [
            {
                "key": "fast_period",
                "label": "快线周期",
                "type": "integer",
                "default": 20,
                "min": 2,
                "max": 200,
            },
            {
                "key": "slow_period",
                "label": "慢线周期",
                "type": "integer",
                "default": 50,
                "min": 3,
                "max": 500,
            },
            {
                "key": "rsi_period",
                "label": "RSI 周期",
                "type": "integer",
                "default": 14,
                "min": 2,
                "max": 100,
            },
            {
                "key": "threshold",
                "label": "入场分数",
                "type": "number",
                "default": 2,
                "min": 1,
                "max": 4,
            },
        ],
    },
    {
        "id": "ma_cross",
        "name": "均线交叉",
        "category": "趋势",
        "description": "快均线上穿慢均线做多，下穿做空。",
        "params": [
            {
                "key": "fast_period",
                "label": "快线周期",
                "type": "integer",
                "default": 20,
                "min": 2,
                "max": 200,
            },
            {
                "key": "slow_period",
                "label": "慢线周期",
                "type": "integer",
                "default": 50,
                "min": 3,
                "max": 500,
            },
        ],
    },
    {
        "id": "macd_momentum",
        "name": "MACD 动量",
        "category": "动量",
        "description": "MACD 柱由负转正时做多，由正转负时做空。",
        "params": [
            {
                "key": "fast_period",
                "label": "快线周期",
                "type": "integer",
                "default": 12,
                "min": 2,
                "max": 100,
            },
            {
                "key": "slow_period",
                "label": "慢线周期",
                "type": "integer",
                "default": 26,
                "min": 3,
                "max": 200,
            },
            {
                "key": "signal_period",
                "label": "信号周期",
                "type": "integer",
                "default": 9,
                "min": 2,
                "max": 100,
            },
        ],
    },
    {
        "id": "rsi_reversal",
        "name": "RSI 反转",
        "category": "反转",
        "description": "RSI 离开超卖区时做多，离开超买区时做空。",
        "params": [
            {
                "key": "period",
                "label": "RSI 周期",
                "type": "integer",
                "default": 14,
                "min": 2,
                "max": 100,
            },
            {
                "key": "oversold",
                "label": "超卖线",
                "type": "number",
                "default": 30,
                "min": 1,
                "max": 49,
            },
            {
                "key": "overbought",
                "label": "超买线",
                "type": "number",
                "default": 70,
                "min": 51,
                "max": 99,
            },
        ],
    },
    {
        "id": "bollinger_reversion",
        "name": "布林回归",
        "category": "反转",
        "description": "价格从布林带外侧重新回到带内时进行均值回归交易。",
        "params": [
            {
                "key": "period",
                "label": "统计周期",
                "type": "integer",
                "default": 20,
                "min": 3,
                "max": 300,
            },
            {
                "key": "stddev",
                "label": "标准差倍数",
                "type": "number",
                "default": 2,
                "min": 0.5,
                "max": 5,
            },
        ],
    },
)

_STRATEGIES = {item["id"]: item for item in STRATEGY_TEMPLATES}
_STRATEGIES["strategy_dsl"] = {
    "id": "strategy_dsl",
    "name": "完整策略 DSL",
    "params": full_strategy_parameter_schema(),
}
_REQUIRED_CONFIG = {
    "strategy_id",
    "symbol",
    "timeframe",
    "start_ts",
    "end_ts",
    "initial_capital",
    "position_size_pct",
    "leverage",
    "fee_bps",
    "slippage_bps",
    "stop_loss_pct",
    "take_profit_pct",
    "max_holding_bars",
    "params",
}


_Candle = StrategyCandle


class BacktestRepository:
    """Read-only historical-data repository and deterministic backtest engine."""

    def __init__(
        self,
        engine: Engine,
        symbols_config: Path,
        *,
        kline_fetcher: Callable[[str, str, int, int], list[tuple]] | None = None,
    ):
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise BacktestUnavailable("backtest market data requires MySQL")
        self.engine = engine
        self.symbols_config = Path(symbols_config).expanduser().resolve()
        if not self.symbols_config.is_file():
            raise BacktestUnavailable("backtest symbols config is unavailable")
        try:
            raw_config = json.loads(self.symbols_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BacktestUnavailable("backtest symbols config is invalid") from exc
        raw_symbols = raw_config.get("symbols")
        if not isinstance(raw_symbols, list):
            raise BacktestUnavailable("backtest symbols config is invalid")
        self.symbols_meta: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_symbols:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                continue
            symbol = item["symbol"].strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            metadata = dict(item)
            metadata["symbol"] = symbol
            self.symbols_meta.append(metadata)
        if not self.symbols_meta:
            raise BacktestUnavailable("backtest symbols config contains no symbols")
        self.symbols = [item["symbol"] for item in self.symbols_meta]
        self.symbol_set = set(self.symbols)
        self.kline_fetcher = kline_fetcher or fetch_klines_range

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            statement, values = _bind_params(sql, params)
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(statement, values).mappings()]
        except SQLAlchemyError as exc:
            raise BacktestUnavailable("backtest market data query failed") from exc

    def catalog(self) -> dict[str, Any]:
        rows = self._query(
            """
            SELECT symbol, tf, COUNT(*) AS bars,
                   MIN(open_time) AS start_time, MAX(open_time) AS end_time
            FROM klines GROUP BY symbol, tf
            """
        )
        available: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row["symbol"] or "").upper()
            timeframe = str(row["tf"] or "")
            if symbol not in self.symbol_set or not timeframe:
                continue
            scale = _timestamp_scale(row["end_time"])
            available.setdefault(symbol, []).append(
                {
                    "timeframe": timeframe,
                    "start_ts": _to_unix_seconds(row["start_time"], scale),
                    "end_ts": _to_unix_seconds(row["end_time"], scale),
                    "bars": int(row["bars"]),
                }
            )

        symbols = []
        bounds: dict[str, dict[str, dict[str, int | str]]] = {}
        timeframe_names: set[str] = set(SUPPORTED_BACKTEST_TIMEFRAMES)
        for metadata in self.symbols_meta:
            symbol = metadata["symbol"]
            timeframes = sorted(
                available.get(symbol, []),
                key=lambda item: (
                    _timeframe_seconds(item["timeframe"]) or math.inf,
                    item["timeframe"],
                ),
            )
            if timeframes:
                bounds[symbol] = {}
                for item in timeframes:
                    timeframe_names.add(item["timeframe"])
                    bounds[symbol][item["timeframe"]] = dict(item)
            symbols.append(
                {
                    "symbol": symbol,
                    "pair": metadata.get("pair", symbol),
                    "underlying_type": metadata.get("underlyingType", ""),
                    "underlying_subtype": metadata.get("underlyingSubType", []),
                    "available": bool(timeframes),
                    "timeframes": timeframes,
                }
            )
        return {
            "strategies": copy.deepcopy(list(STRATEGY_TEMPLATES)),
            "symbols": symbols,
            "timeframes": sorted(
                timeframe_names,
                key=lambda item: (_timeframe_seconds(item) or math.inf, item),
            ),
            "bounds": bounds,
            "limits": {
                "max_bars": MAX_BARS,
                "max_equity_points": MAX_EQUITY_POINTS,
                "max_returned_trades": MAX_RETURNED_TRADES,
                "timestamp_unit": "seconds",
            },
        }

    def run(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = self._validate_config(config)
        if validated["strategy_id"] == "strategy_dsl":
            raise BacktestUnavailable("full strategy backtest requires an immutable strategy spec")
        fetched = self._ensure_klines_if_missing(
            validated["symbol"],
            validated["timeframe"],
            validated["start_ts"],
            validated["end_ts"],
        )
        series = self._series_info(validated["symbol"], validated["timeframe"])
        scale = series["scale"]
        start_raw = validated["start_ts"] * scale
        end_raw = validated["end_ts"] * scale + scale - 1
        count_rows = self._query(
            """
            SELECT COUNT(*) AS bars FROM klines
            WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
            """,
            (validated["symbol"], validated["timeframe"], start_raw, end_raw),
        )
        raw_count = int(count_rows[0]["bars"])
        if raw_count == 0:
            raise BacktestUnavailable("no klines are available for the requested range")
        if raw_count > MAX_BARS:
            raise BacktestUnavailable(f"backtest range exceeds the {MAX_BARS} bar limit")
        rows = self._query(
            """
            SELECT open_time, open, high, low, close, volume FROM klines
            WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
            ORDER BY open_time ASC
            """,
            (validated["symbol"], validated["timeframe"], start_raw, end_raw),
        )
        candles, quality = _clean_candles(rows, scale, validated["timeframe"])
        if len(candles) < 2:
            raise BacktestUnavailable("at least two valid klines are required")

        if fetched:
            quality["on_demand_fetches"] = [fetched]
        result = _run_engine(candles, validated)
        return _finalize_result(result, candles, validated, raw_count, quality)

    def load_market_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        *,
        max_bars: int = 20_000,
    ) -> tuple[list[_Candle], dict[str, Any]]:
        """Load closed Binance candles for a specialized research adapter.

        This deliberately bypasses the single-position strategy validator while
        retaining the same bounded fetch, persistence, cleaning and provenance
        rules.  Basket engines may use the additional 1m/5m/30m/1d intervals,
        but ordinary backtests keep their existing timeframe policy.
        """

        normalized_symbol = str(symbol).strip().upper()
        normalized_timeframe = str(timeframe).strip()
        if normalized_symbol not in self.symbol_set:
            raise BacktestUnavailable("unknown backtest symbol")
        if normalized_timeframe not in SUPPORTED_MARKET_DATA_TIMEFRAMES:
            raise BacktestUnavailable("unsupported specialized market-data timeframe")
        if start_ts < 0 or end_ts <= start_ts:
            raise BacktestUnavailable("invalid specialized market-data range")
        interval = _timeframe_seconds(normalized_timeframe)
        if interval is None:
            raise BacktestUnavailable("invalid specialized market-data timeframe")
        requested_bars = (end_ts - start_ts) // interval + 1
        if requested_bars > max_bars:
            raise BacktestUnavailable(
                f"specialized market-data range exceeds the {max_bars} bar limit"
            )

        fetched = self._ensure_klines_if_missing(
            normalized_symbol,
            normalized_timeframe,
            start_ts,
            end_ts,
        )
        series = self._series_info(normalized_symbol, normalized_timeframe)
        scale = series["scale"]
        rows = self._query(
            """
            SELECT open_time, open, high, low, close, volume FROM klines
            WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
            ORDER BY open_time ASC
            """,
            (
                normalized_symbol,
                normalized_timeframe,
                start_ts * scale,
                end_ts * scale + scale - 1,
            ),
        )
        if not rows:
            raise BacktestUnavailable("no closed klines are available for the requested range")
        if len(rows) > max_bars:
            raise BacktestUnavailable(
                f"specialized market-data range exceeds the {max_bars} bar limit"
            )
        candles, quality = _clean_candles(rows, scale, normalized_timeframe)
        if not candles:
            raise BacktestUnavailable("no valid closed klines are available for the requested range")
        quality.update(
            {
                "source": "binance_fapi",
                "symbol": normalized_symbol,
                "timeframe": normalized_timeframe,
                "actual_bars": len(candles),
                "raw_bars": len(rows),
            }
        )
        if fetched:
            quality["on_demand_fetch"] = fetched
        return candles, quality

    def run_full_strategy(
        self,
        config: dict[str, Any],
        spec_value: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay a full strategy using only bars closed at each trigger decision."""

        try:
            spec = validate_strategy_spec(spec_value)
        except (StrategySpecError, TypeError, ValueError) as exc:
            raise BacktestUnavailable(f"invalid full strategy spec: {exc}") from None
        config_with_engine = dict(config)
        config_with_engine["strategy_id"] = "strategy_dsl"
        dynamic_parameters = dict(config_with_engine.get("params", {}))
        if spec["strategy_type"] == "indicator_composite":
            # Generic account settings are still validated by the shared engine
            # boundary; composite parameters are validated against their DSL below.
            config_with_engine["params"] = {}
        validated = self._validate_config(config_with_engine)
        if spec["strategy_type"] == "indicator_composite":
            validated["params"] = dynamic_parameters
        spec["parameters"].update(validated["params"])
        try:
            spec = validate_strategy_spec(spec)
        except StrategySpecError as exc:
            raise BacktestUnavailable(f"invalid full strategy parameters: {exc}") from None

        trigger_timeframe = spec["timeframes"]["trigger"]
        if validated["timeframe"] != trigger_timeframe:
            raise BacktestUnavailable(
                f"完整策略触发周期必须是 {trigger_timeframe}"
            )
        if validated["leverage"] > int(spec["risk"]["max_leverage"]):
            raise BacktestUnavailable(
                f"leverage exceeds strategy maximum of {spec['risk']['max_leverage']}"
            )
        validated["max_holding_bars"] = int(spec["exit"]["max_holding_bars"])

        candles_by_timeframe: dict[str, list[_Candle]] = {}
        quality_by_timeframe: dict[str, dict[str, Any]] = {}
        raw_counts: dict[str, int] = {}
        window_sizes = _strategy_window_sizes(spec)
        on_demand_fetches: list[dict[str, Any]] = []
        for timeframe in set(spec["timeframes"].values()):
            interval = _timeframe_seconds(timeframe)
            if interval is None:
                raise BacktestUnavailable(f"unsupported strategy timeframe: {timeframe}")
            required_warmup = max(
                window_sizes[role]
                for role, role_timeframe in spec["timeframes"].items()
                if role_timeframe == timeframe
            )
            fetched = self._ensure_klines_if_missing(
                validated["symbol"],
                timeframe,
                max(0, validated["start_ts"] - interval * required_warmup),
                validated["end_ts"],
            )
            if fetched:
                on_demand_fetches.append(fetched)
        for role, timeframe in spec["timeframes"].items():
            series = self._series_info(validated["symbol"], timeframe)
            interval = _timeframe_seconds(timeframe)
            if interval is None:
                raise BacktestUnavailable(f"unsupported strategy timeframe: {timeframe}")
            warmup_start = max(
                series["start_ts"],
                validated["start_ts"] - interval * window_sizes[role],
            )
            start_raw = warmup_start * series["scale"]
            end_raw = validated["end_ts"] * series["scale"] + series["scale"] - 1
            rows = self._query(
                """
                SELECT open_time, open, high, low, close, volume FROM klines
                WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
                ORDER BY open_time ASC
                """,
                (validated["symbol"], timeframe, start_raw, end_raw),
            )
            candles, timeframe_quality = _clean_candles(
                rows, series["scale"], timeframe
            )
            candles_by_timeframe[timeframe] = candles
            quality_by_timeframe[timeframe] = timeframe_quality
            raw_counts[timeframe] = len(rows)

        trigger_all = candles_by_timeframe[trigger_timeframe]
        trigger_candles = [
            candle
            for candle in trigger_all
            if validated["start_ts"] <= candle.ts <= validated["end_ts"]
        ]
        if len(trigger_candles) < 2:
            raise BacktestUnavailable("at least two valid trigger klines are required")
        if len(trigger_candles) > MAX_BARS:
            raise BacktestUnavailable(f"backtest range exceeds the {MAX_BARS} bar limit")

        close_times = {
            timeframe: [
                candle.ts + int(_timeframe_seconds(timeframe) or 0)
                for candle in candles
            ]
            for timeframe, candles in candles_by_timeframe.items()
        }
        signals: list[int] = []
        risk_proposals: list[dict[str, Any] | None] = []
        signal_valid_until: list[int | None] = []
        decision_counts: dict[str, int] = {}
        for trigger in trigger_candles:
            decision_at = trigger.ts + int(_timeframe_seconds(trigger_timeframe) or 0)
            market: dict[str, list[dict[str, float | int]]] = {}
            for role, timeframe in spec["timeframes"].items():
                end_index = bisect_right(close_times[timeframe], decision_at)
                start_index = max(0, end_index - window_sizes[role])
                market[timeframe] = [
                    _runtime_candle(candle)
                    for candle in candles_by_timeframe[timeframe][start_index:end_index]
                ]
            try:
                decision = evaluate_strategy(spec, market)
            except (StrategySpecError, StrategyMarketDataError, ValueError) as exc:
                raise BacktestUnavailable(f"full strategy evaluation failed: {exc}") from None
            direction = {"LONG_ENTRY": 1, "SHORT_ENTRY": -1}.get(decision.decision, 0)
            signals.append(direction)
            risk_proposals.append(decision.risk_proposal if direction else None)
            signal_valid_until.append(decision.valid_until)
            decision_counts[decision.decision] = decision_counts.get(decision.decision, 0) + 1

        result = _run_engine(
            trigger_candles,
            validated,
            signals=signals,
            risk_proposals=risk_proposals,
            signal_valid_until=signal_valid_until,
        )
        quality = dict(quality_by_timeframe[trigger_timeframe])
        quality.update(
            {
                "strategy_kind": "full_strategy",
                "strategy_type": spec["strategy_type"],
                "timeframes": quality_by_timeframe,
                "raw_bars_by_timeframe": raw_counts,
                "decision_counts": decision_counts,
                "on_demand_fetches": on_demand_fetches,
            }
        )
        return _finalize_result(
            result,
            trigger_candles,
            validated,
            len(trigger_candles),
            quality,
            extra_assumptions=[
                "完整策略严格按 4h/1h/15m 各周期已收盘 K 线对齐求值",
                "完整策略入场后的止损和止盈使用信号时生成的 ATR 风险距离",
            ],
        )

    def run_source_strategy(
        self,
        config: dict[str, Any],
        source_code: str,
        *,
        language: str = "python",
    ) -> dict[str, Any]:
        """Replay one immutable source revision in the isolated Python worker."""

        try:
            metadata = validate_source(source_code, language)
        except StrategySourceError as exc:
            raise BacktestUnavailable(f"源码策略无效：{exc}") from None
        config_with_engine = dict(config)
        config_with_engine["strategy_id"] = "strategy_dsl"
        dynamic_parameters = dict(config_with_engine.get("params", {}))
        config_with_engine["params"] = {}
        validated = self._validate_config(config_with_engine)
        validated["params"] = dynamic_parameters
        if validated["timeframe"] != metadata.trigger_timeframe:
            raise BacktestUnavailable(
                f"源码策略触发周期必须是 {metadata.trigger_timeframe}"
            )

        candles_by_timeframe: dict[str, list[_Candle]] = {}
        quality_by_timeframe: dict[str, dict[str, Any]] = {}
        raw_counts: dict[str, int] = {}
        on_demand_fetches: list[dict[str, Any]] = []
        for timeframe in metadata.timeframes:
            interval = _timeframe_seconds(timeframe)
            if interval is None:
                raise BacktestUnavailable(f"unsupported strategy timeframe: {timeframe}")
            fetched = self._ensure_klines_if_missing(
                validated["symbol"],
                timeframe,
                max(0, validated["start_ts"] - interval * metadata.lookback_bars),
                validated["end_ts"],
            )
            if fetched:
                on_demand_fetches.append(fetched)
            series = self._series_info(validated["symbol"], timeframe)
            warmup_start = max(
                series["start_ts"],
                validated["start_ts"] - interval * metadata.lookback_bars,
            )
            rows = self._query(
                """
                SELECT open_time, open, high, low, close, volume FROM klines
                WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
                ORDER BY open_time ASC
                """,
                (
                    validated["symbol"],
                    timeframe,
                    warmup_start * series["scale"],
                    validated["end_ts"] * series["scale"] + series["scale"] - 1,
                ),
            )
            candles, quality = _clean_candles(rows, series["scale"], timeframe)
            candles_by_timeframe[timeframe] = candles
            quality_by_timeframe[timeframe] = quality
            raw_counts[timeframe] = len(rows)

        trigger_candles = [
            candle
            for candle in candles_by_timeframe[metadata.trigger_timeframe]
            if validated["start_ts"] <= candle.ts <= validated["end_ts"]
        ]
        if len(trigger_candles) < 2:
            raise BacktestUnavailable("at least two valid trigger klines are required")
        if len(trigger_candles) > MAX_BARS:
            raise BacktestUnavailable(f"backtest range exceeds the {MAX_BARS} bar limit")
        close_times = {
            timeframe: [
                candle.ts + int(_timeframe_seconds(timeframe) or 0) for candle in candles
            ]
            for timeframe, candles in candles_by_timeframe.items()
        }
        context_batch: list[dict[str, Any]] = []
        decisions = []

        def evaluate_batch() -> None:
            if not context_batch:
                return
            try:
                decisions.extend(
                    evaluate_source_many(
                        source_code,
                        context_batch,
                        validated["params"],
                        language=language,
                        timeout_seconds=12.0,
                    )
                )
            except (StrategySourceError, StrategySourceExecutionError) as exc:
                raise BacktestUnavailable(
                    f"源码策略执行失败：{exc}"
                ) from None
            context_batch.clear()

        for trigger in trigger_candles:
            decision_at = trigger.ts + int(
                _timeframe_seconds(metadata.trigger_timeframe) or 0
            )
            market: dict[str, list[dict[str, float | int]]] = {}
            for timeframe in metadata.timeframes:
                end_index = bisect_right(close_times[timeframe], decision_at)
                start_index = max(0, end_index - metadata.lookback_bars)
                market[timeframe] = [
                    _runtime_candle(candle)
                    for candle in candles_by_timeframe[timeframe][start_index:end_index]
                ]
            context_batch.append(
                {
                    "symbol": validated["symbol"],
                    "decision_time": decision_at,
                    "bars": market,
                }
            )
            if len(context_batch) >= 250:
                evaluate_batch()
        evaluate_batch()
        signals = [
            {"LONG_ENTRY": 1, "SHORT_ENTRY": -1}.get(decision.decision, 0)
            for decision in decisions
        ]
        risk_proposals = [
            decision.risk_proposal if direction else None
            for decision, direction in zip(decisions, signals, strict=True)
        ]
        decision_counts: dict[str, int] = {}
        for decision in decisions:
            decision_counts[decision.decision] = decision_counts.get(decision.decision, 0) + 1
        result = _run_engine(
            trigger_candles,
            validated,
            signals=signals,
            risk_proposals=risk_proposals,
            signal_valid_until=[decision.valid_until for decision in decisions],
        )
        quality = dict(quality_by_timeframe[metadata.trigger_timeframe])
        quality.update(
            {
                "strategy_kind": "source_strategy",
                "source_language": metadata.language,
                "source_hash": metadata.source_hash,
                "source_runtime_version": metadata.runtime_version,
                "timeframes": quality_by_timeframe,
                "raw_bars_by_timeframe": raw_counts,
                "decision_counts": decision_counts,
                "on_demand_fetches": on_demand_fetches,
            }
        )
        return _finalize_result(
            result,
            trigger_candles,
            validated,
            len(trigger_candles),
            quality,
            extra_assumptions=[
                "Python 源码在隔离进程中按已收盘 K 线批量求值",
                "回测、模拟和实盘使用同一源码修订与参数快照",
            ],
        )

    def _ensure_klines_if_missing(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any] | None:
        """Fetch and persist Binance closed bars only when the local range is empty."""

        if symbol not in self.symbol_set or timeframe not in SUPPORTED_MARKET_DATA_TIMEFRAMES:
            raise BacktestUnavailable("unsupported on-demand market data request")
        series_rows = self._query(
            """
            SELECT COUNT(*) AS bars, MAX(open_time) AS end_time
            FROM klines WHERE symbol=? AND tf=?
            """,
            (symbol, timeframe),
        )
        scale = (
            _timestamp_scale(series_rows[0]["end_time"])
            if series_rows and series_rows[0].get("end_time") is not None
            else 1_000
        )
        local_rows = self._query(
            """
            SELECT COUNT(*) AS bars FROM klines
            WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
            """,
            (symbol, timeframe, start_ts * scale, end_ts * scale + scale - 1),
        )
        if local_rows and int(local_rows[0]["bars"] or 0) > 0:
            return None
        try:
            rows = self.kline_fetcher(symbol, timeframe, start_ts * 1_000, end_ts * 1_000)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            raise BacktestUnavailable(
                f"Binance historical data fetch failed for {symbol} {timeframe}: {exc}"
            ) from None
        if not rows:
            raise BacktestUnavailable(
                f"Binance returned no closed klines for {symbol} {timeframe} in the requested range"
            )
        self._upsert_binance_klines(symbol, timeframe, rows)
        return {
            "source": "binance_fapi",
            "symbol": symbol,
            "timeframe": timeframe,
            "requested_start_ts": start_ts,
            "requested_end_ts": end_ts,
            "bars_fetched": len(rows),
        }

    def _upsert_binance_klines(
        self, symbol: str, timeframe: str, rows: list[tuple]
    ) -> None:
        statement = text(
            """
            INSERT INTO klines(symbol,tf,open_time,open,high,low,close,volume)
            VALUES(:symbol,:tf,:open_time,:open,:high,:low,:close,:volume)
            ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
                                    close=VALUES(close),volume=VALUES(volume)
            """
        )
        values = [
            {
                "symbol": symbol,
                "tf": timeframe,
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
        ]
        try:
            with self.engine.begin() as connection:
                connection.execute(statement, values)
        except SQLAlchemyError as exc:
            raise BacktestUnavailable(
                "Binance historical data could not be saved to MySQL"
            ) from exc

    def _series_info(self, symbol: str, timeframe: str) -> dict[str, int]:
        rows = self._query(
            """
            SELECT COUNT(*) AS bars, MIN(open_time) AS start_time, MAX(open_time) AS end_time
            FROM klines WHERE symbol=? AND tf=?
            """,
            (symbol, timeframe),
        )
        if not rows or not rows[0]["bars"]:
            raise BacktestUnavailable("symbol and timeframe have no historical data")
        scale = _timestamp_scale(rows[0]["end_time"])
        return {
            "bars": int(rows[0]["bars"]),
            "start_ts": _to_unix_seconds(rows[0]["start_time"], scale),
            "end_ts": _to_unix_seconds(rows[0]["end_time"], scale),
            "scale": scale,
        }

    def _validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise BacktestUnavailable("backtest config must be an object")
        missing = sorted(_REQUIRED_CONFIG - config.keys())
        unknown = sorted(config.keys() - _REQUIRED_CONFIG)
        if missing:
            raise BacktestUnavailable(f"missing backtest config fields: {', '.join(missing)}")
        if unknown:
            raise BacktestUnavailable(f"unknown backtest config fields: {', '.join(unknown)}")

        strategy_id = config["strategy_id"]
        if not isinstance(strategy_id, str) or strategy_id not in _STRATEGIES:
            raise BacktestUnavailable("unknown backtest strategy")
        symbol = config["symbol"]
        if not isinstance(symbol, str) or symbol.strip().upper() not in self.symbol_set:
            raise BacktestUnavailable("unknown backtest symbol")
        symbol = symbol.strip().upper()
        timeframe = config["timeframe"]
        if not isinstance(timeframe, str) or not timeframe.strip():
            raise BacktestUnavailable("回测周期格式无效")
        timeframe = timeframe.strip()
        if _timeframe_seconds(timeframe) is None:
            raise BacktestUnavailable("暂不支持该回测周期")

        start_ts = _strict_integer(config["start_ts"], "start_ts", minimum=0)
        end_ts = _strict_integer(config["end_ts"], "end_ts", minimum=0)
        if start_ts >= end_ts:
            raise BacktestUnavailable("start_ts must be earlier than end_ts")
        if end_ts > 32_503_680_000:
            raise BacktestUnavailable("end_ts is outside the supported Unix timestamp range")

        initial_capital = _strict_number(
            config["initial_capital"], "initial_capital", minimum=1, maximum=1_000_000_000_000
        )
        position_size_pct = _strict_number(
            config["position_size_pct"], "position_size_pct", minimum=0.01, maximum=100
        )
        leverage = _strict_integer(config["leverage"], "leverage", minimum=1, maximum=20)
        fee_bps = _strict_number(config["fee_bps"], "fee_bps", minimum=0, maximum=1_000)
        slippage_bps = _strict_number(
            config["slippage_bps"], "slippage_bps", minimum=0, maximum=1_000
        )
        stop_loss_pct = _strict_number(
            config["stop_loss_pct"], "stop_loss_pct", minimum=0, maximum=99.9
        )
        take_profit_pct = _strict_number(
            config["take_profit_pct"], "take_profit_pct", minimum=0, maximum=99.9
        )
        max_holding_bars = _strict_integer(
            config["max_holding_bars"], "max_holding_bars", minimum=0, maximum=MAX_BARS
        )
        params = _validate_strategy_params(strategy_id, config["params"])
        return {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "initial_capital": initial_capital,
            "position_size_pct": position_size_pct,
            "leverage": leverage,
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "max_holding_bars": max_holding_bars,
            "params": params,
        }


def _timestamp_scale(value: Any) -> int:
    try:
        timestamp = abs(int(value))
    except (TypeError, ValueError) as exc:
        raise BacktestUnavailable("historical data contains an invalid timestamp") from exc
    if timestamp >= 100_000_000_000_000_000:
        return 1_000_000_000
    if timestamp >= 100_000_000_000_000:
        return 1_000_000
    if timestamp >= 100_000_000_000:
        return 1_000
    return 1


def _to_unix_seconds(value: Any, scale: int) -> int:
    return int(value) // scale


def _timeframe_seconds(timeframe: str) -> int | None:
    if not isinstance(timeframe, str) or len(timeframe) < 2:
        return None
    unit = timeframe[-1]
    amount_text = timeframe[:-1]
    if not amount_text.isdigit() or int(amount_text) <= 0:
        return None
    multipliers = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    multiplier = multipliers.get(unit)
    return int(amount_text) * multiplier if multiplier else None


def _strict_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BacktestUnavailable(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BacktestUnavailable(f"{name} must be between {minimum} and {maximum}")
    return result


def _strict_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise BacktestUnavailable(f"{name} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        # Pydantic may normalize a numeric strategy-parameter map to floats.
        result = int(value)
    else:
        raise BacktestUnavailable(f"{name} must be an integer")
    if result < minimum or (maximum is not None and result > maximum):
        upper = f" and {maximum}" if maximum is not None else ""
        raise BacktestUnavailable(f"{name} must be between {minimum}{upper}")
    return result


def _validate_strategy_params(strategy_id: str, values: Any) -> dict[str, int | float]:
    if not isinstance(values, dict):
        raise BacktestUnavailable("params must be an object")
    definitions = {item["key"]: item for item in _STRATEGIES[strategy_id]["params"]}
    unknown = sorted(values.keys() - definitions.keys())
    if unknown:
        raise BacktestUnavailable(f"unknown strategy params: {', '.join(unknown)}")
    params: dict[str, int | float] = {}
    for key, definition in definitions.items():
        value = values.get(key, definition["default"])
        if definition["type"] == "integer":
            params[key] = _strict_integer(
                value,
                f"params.{key}",
                minimum=int(definition["min"]),
                maximum=int(definition["max"]),
            )
        else:
            params[key] = _strict_number(
                value,
                f"params.{key}",
                minimum=float(definition["min"]),
                maximum=float(definition["max"]),
            )
    if strategy_id in {"multi_factor", "ma_cross", "macd_momentum"}:
        if params["fast_period"] >= params["slow_period"]:
            raise BacktestUnavailable("fast_period must be smaller than slow_period")
    if strategy_id == "rsi_reversal" and params["oversold"] >= params["overbought"]:
        raise BacktestUnavailable("oversold must be smaller than overbought")
    return params


def _clean_candles(
    rows: list[dict[str, Any]], scale: int, timeframe: str
) -> tuple[list[_Candle], dict[str, Any]]:
    candles: list[_Candle] = []
    seen: set[int] = set()
    invalid = 0
    duplicates = 0
    for row in rows:
        try:
            ts = _to_unix_seconds(row["open_time"], scale)
            values = [float(row[key]) for key in ("open", "high", "low", "close", "volume")]
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        open_price, high, low, close, volume = values
        if ts in seen:
            duplicates += 1
            continue
        if (
            not all(math.isfinite(value) for value in values)
            or min(open_price, high, low, close) <= 0
            or volume < 0
            or high < max(open_price, close, low)
            or low > min(open_price, close, high)
        ):
            invalid += 1
            continue
        seen.add(ts)
        candles.append(_Candle(ts, open_price, high, low, close, volume))
    candles.sort(key=lambda item: item.ts)

    interval = _timeframe_seconds(timeframe)
    missing = 0
    if interval:
        for previous, current in zip(candles, candles[1:], strict=False):
            delta = current.ts - previous.ts
            if delta > interval:
                missing += max(0, round(delta / interval) - 1)
    expected = len(candles) + missing
    warnings = []
    if invalid:
        warnings.append(f"已忽略 {invalid} 根无效 K 线")
    if duplicates:
        warnings.append(f"已忽略 {duplicates} 个重复时间戳")
    if missing:
        warnings.append(f"检测到约 {missing} 根缺失 K 线")
    return candles, {
        "invalid_bars": invalid,
        "duplicate_timestamps": duplicates,
        "missing_bars": missing,
        "coverage_pct": _round_number(len(candles) / expected * 100) if expected else 0.0,
        "warnings": warnings,
    }


def _strategy_window_sizes(spec: dict[str, Any]) -> dict[str, int]:
    if spec["strategy_type"] == "indicator_composite":
        return {
            role: required + 10
            for role, required in strategy_required_bars(spec).items()
        }
    parameters = spec["parameters"]
    return {
        "regime": max(
            int(parameters["regime_slow_ema"]) + 10,
            int(parameters["regime_adx_period"]) * 3 + 10,
        ),
        "setup": max(
            int(parameters["setup_ema_period"])
            + int(parameters["setup_lookback_bars"])
            + 10,
            int(parameters["setup_atr_period"])
            + int(parameters["setup_lookback_bars"])
            + 10,
        ),
        "trigger": max(
            int(parameters["trigger_donchian_period"]) + 10,
            int(parameters["trigger_volume_period"]) + 10,
        ),
    }


def _runtime_candle(candle: _Candle) -> dict[str, float | int]:
    return {
        "open_time": candle.ts,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def _finalize_result(
    result: dict[str, Any],
    candles: list[_Candle],
    config: dict[str, Any],
    raw_count: int,
    quality: dict[str, Any],
    *,
    extra_assumptions: list[str] | None = None,
) -> dict[str, Any]:
    full_curve = result.pop("_full_equity_curve")
    all_trades = result["trades"]
    equity_curve = _even_sample(full_curve, MAX_EQUITY_POINTS)
    returned_trades = all_trades[-MAX_RETURNED_TRADES:]
    assumptions = [
        "信号在当前 K 线收盘确认，并于下一根 K 线开盘成交",
        "同一根 K 线同时触发有利和不利退出价位时，保守地按不利退出优先",
        "每次仅交易一个标的并持有一个方向的单一仓位",
        "手续费和滑点均在开仓、平仓两端计入",
        (
            "强平采用固定 0.5% 维持保证金率的简化逐仓模型；逐 K 线检查，"
            "跳空越过强平价时按更差的开盘价成交"
        ),
        (
            "止损与强平同时触发时，按从开盘价向不利方向最先遇到的价位执行；"
            "该模型不包含阶梯维持保证金、保险基金或自动减仓"
        ),
        "收益计算未覆盖资金费率与借贷成本，长时间持仓的结果可能偏乐观",
    ]
    if extra_assumptions:
        assumptions.extend(extra_assumptions)
    quality.update(
        {
            "requested_start_ts": config["start_ts"],
            "requested_end_ts": config["end_ts"],
            "actual_start_ts": candles[0].ts,
            "actual_end_ts": candles[-1].ts,
            "bars_loaded": raw_count,
            "bars_used": len(candles),
            "equity_points_total": len(full_curve),
            "equity_points_returned": len(equity_curve),
            "equity_curve_truncated": len(equity_curve) < len(full_curve),
            "trades_total": len(all_trades),
            "trades_returned": len(returned_trades),
            "trades_truncated": len(returned_trades) < len(all_trades),
            "timestamp_unit": "seconds",
            "assumptions": assumptions,
        }
    )
    result["equity_curve"] = equity_curve
    result["trades"] = returned_trades
    if len(returned_trades) < len(all_trades):
        result["_all_trades"] = all_trades
    result["data_quality"] = quality
    return result


def _run_engine(
    candles: list[_Candle],
    config: dict[str, Any],
    *,
    signals: list[int] | None = None,
    risk_proposals: list[dict[str, Any] | None] | None = None,
    signal_valid_until: list[int | None] | None = None,
) -> dict[str, Any]:
    if signals is None:
        envelopes = DEFAULT_STRATEGY_EVALUATOR.evaluate_envelopes(
            config["strategy_id"],
            candles,
            config["params"],
            symbol=config["symbol"],
            timeframe=config["timeframe"],
        )
        signals = [item.direction for item in envelopes]
        signal_valid_until = [
            int(item.valid_until.timestamp()) if item.valid_until is not None else None
            for item in envelopes
        ]
    if len(signals) != len(candles):
        raise BacktestUnavailable("strategy signal count does not match trigger candles")
    if risk_proposals is not None and len(risk_proposals) != len(candles):
        raise BacktestUnavailable("strategy risk proposal count does not match trigger candles")
    if signal_valid_until is not None and len(signal_valid_until) != len(candles):
        raise BacktestUnavailable("strategy signal validity count does not match trigger candles")
    initial_capital = config["initial_capital"]
    balance = initial_capital
    fee_rate = config["fee_bps"] / 10_000
    slippage_rate = config["slippage_bps"] / 10_000
    position: dict[str, Any] | None = None
    pending_signal = 0
    pending_risk: dict[str, Any] | None = None
    pending_valid_until: int | None = None
    trades: list[dict[str, Any]] = []
    total_fees = 0.0
    exposed_bars = 0
    equity_values = [initial_capital]
    running_peak = initial_capital
    running_max_drawdown_pct = 0.0
    curve: list[dict[str, Any]] = []

    def close_position(
        candle: _Candle,
        base_price: float,
        reason: str,
    ) -> None:
        nonlocal balance, position, total_fees
        if position is None:
            return
        direction = position["direction"]
        exit_price = base_price * (1 - direction * slippage_rate)
        exit_notional = position["quantity"] * exit_price
        exit_fee = exit_notional * fee_rate
        gross_pnl = (exit_price - position["entry_price"]) * direction * position["quantity"]
        balance += gross_pnl - exit_fee
        total_fees += exit_fee
        fees = position["entry_fee"] + exit_fee
        net_pnl = gross_pnl - fees
        margin = position["notional"] / config["leverage"]
        exit_decision = DEFAULT_EXIT_POLICY.decision_for_reason(
            reason, base_price, observed_at=candle.ts
        )
        trades.append(
            {
                "symbol": config["symbol"],
                "timeframe": config["timeframe"],
                "side": "long" if direction == 1 else "short",
                "entry_ts": position["entry_ts"],
                "exit_ts": candle.ts,
                # Prices and quantities are persistence-sensitive: keep the
                # finite float instead of applying display rounding here.
                "entry_price": _finite_or_zero(position["entry_price"]),
                "exit_price": _finite_or_zero(exit_price),
                "quantity": _finite_or_zero(position["quantity"]),
                "notional": _result_number(position["notional"], 8),
                "leverage": config["leverage"],
                "liquidation_price": _finite_or_zero(position["liquidation_price"]),
                "gross_pnl": _result_number(gross_pnl, 8),
                "net_pnl": _result_number(net_pnl, 8),
                "fees": _result_number(fees, 8),
                "return_pct": _result_number(net_pnl / margin * 100, 8) if margin else None,
                "holding_bars": max(1, int(position["holding_bars"])),
                "exit_reason": reason,
                "exit_decision": (
                    exit_decision.snapshot(
                        mode="backtest", execution_price=exit_price
                    )
                    if exit_decision is not None
                    else None
                ),
            }
        )
        position = None

    def open_position(
        candle: _Candle,
        direction: int,
        risk_proposal: dict[str, Any] | None,
    ) -> None:
        nonlocal balance, position, total_fees
        if balance <= 0:
            return
        entry_price = candle.open * (1 + direction * slippage_rate)
        margin = balance * config["position_size_pct"] / 100
        notional = margin * config["leverage"]
        if notional <= 0 or entry_price <= 0:
            return
        level_plan = None
        if isinstance(risk_proposal, dict) or (
            config["stop_loss_pct"] > 0 and config["take_profit_pct"] > 0
        ):
            level_plan = DEFAULT_EXIT_POLICY.resolve_levels(
                entry_price,
                direction,
                stop_loss_pct=config["stop_loss_pct"],
                take_profit_pct=config["take_profit_pct"],
                risk_proposal=(risk_proposal if isinstance(risk_proposal, dict) else None),
            )
        if isinstance(risk_proposal, dict) and level_plan is None:
            return
        quantity = notional / entry_price
        entry_fee = notional * fee_rate
        balance -= entry_fee
        total_fees += entry_fee
        stop_price = level_plan.stop if level_plan is not None else None
        take_price = level_plan.target if level_plan is not None else None
        position = {
            "direction": direction,
            "entry_ts": candle.ts,
            "entry_price": entry_price,
            "quantity": quantity,
            "notional": notional,
            "liquidation_price": _liquidation_price(
                entry_price,
                direction,
                config["leverage"],
            ),
            "entry_fee": entry_fee,
            "holding_bars": 0,
            "stop_price": stop_price,
            "take_price": take_price,
        }

    for index, candle in enumerate(candles):
        was_exposed = position is not None
        # Exchange risk controls act before a strategy can reverse a position at
        # the same open. A gap through the liquidation level therefore remains a
        # liquidation rather than being relabelled as a strategy exit.
        if position is not None:
            direction = position["direction"]
            liquidation_price = position["liquidation_price"]
            liquidation_gap = (
                candle.open <= liquidation_price
                if direction == 1
                else candle.open >= liquidation_price
            )
            if liquidation_gap:
                close_position(candle, candle.open, "liquidation")

        pending_is_valid = (
            pending_valid_until is None or candle.ts <= pending_valid_until
        )
        if pending_signal in {-1, 1} and pending_is_valid:
            if position is not None and position["direction"] != pending_signal:
                close_position(candle, candle.open, "strategy_reversal")
            if position is None:
                open_position(candle, pending_signal, pending_risk)

        if position is not None:
            position["holding_bars"] += 1
            was_exposed = True
            direction = position["direction"]
            entry_price = position["entry_price"]
            stop_pct = config["stop_loss_pct"] / 100
            take_pct = config["take_profit_pct"] / 100
            stop_price = position.get("stop_price")
            take_price = position.get("take_price")
            if stop_price is None and stop_pct:
                stop_price = entry_price * (1 - direction * stop_pct)
            if take_price is None and take_pct:
                take_price = entry_price * (1 + direction * take_pct)
            liquidation_price = position["liquidation_price"]
            exit_decision = DEFAULT_EXIT_POLICY.evaluate_bar(
                open_price=candle.open,
                high=candle.high,
                low=candle.low,
                direction=direction,
                stop=stop_price,
                target=take_price,
                liquidation=liquidation_price,
                observed_at=candle.ts,
            )
            selected_exit = DEFAULT_EXIT_POLICY.select(
                price=candle.close,
                observed_at=candle.ts,
                market_decision=exit_decision,
                holding_period_expired=bool(
                    config["max_holding_bars"]
                    and position["holding_bars"] >= config["max_holding_bars"]
                ),
            )
            if selected_exit is not None:
                close_position(
                    candle, selected_exit.price, selected_exit.reason
                )

        if index == len(candles) - 1 and position is not None:
            close_position(candle, candle.close, "end_of_data")

        if was_exposed:
            exposed_bars += 1
        if position is None:
            equity = balance
        else:
            unrealized = (
                (candle.close - position["entry_price"])
                * position["direction"]
                * position["quantity"]
            )
            equity = balance + unrealized
        equity = _finite_or_zero(equity)
        equity_values.append(equity)
        running_peak = max(running_peak, equity)
        drawdown_pct = (running_peak - equity) / running_peak * 100 if running_peak > 0 else 0.0
        running_max_drawdown_pct = max(running_max_drawdown_pct, drawdown_pct)
        curve.append(
            {
                "ts": candle.ts,
                "equity": _result_number(equity, 8),
                "drawdown_pct": _result_number(drawdown_pct, 8),
            }
        )
        pending_signal = signals[index]
        pending_risk = risk_proposals[index] if risk_proposals is not None else None
        pending_valid_until = (
            signal_valid_until[index] if signal_valid_until is not None else None
        )

    final_equity = balance
    net_profit = final_equity - initial_capital
    peak_equity = running_peak
    max_drawdown_pct = running_max_drawdown_pct
    interval_seconds = _timeframe_seconds(config["timeframe"])
    duration_seconds = candles[-1].ts + interval_seconds - candles[0].ts if interval_seconds else 0
    net_results = [float(trade["net_pnl"]) for trade in trades]
    wins = [value for value in net_results if value > 0]
    losses = [value for value in net_results if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss else None
    average_win = gross_profit / len(wins) if wins else 0.0
    average_loss = abs(gross_loss) / len(losses) if losses else 0.0
    profit_loss_ratio = average_win / average_loss if average_loss else None
    liquidation_count = sum(trade["exit_reason"] == "liquidation" for trade in trades)
    metrics = {
        "net_profit": _result_number(net_profit, 8),
        "total_return_pct": _result_number(net_profit / initial_capital * 100, 8),
        "annualized_return_pct": _annualized_return(
            initial_capital,
            final_equity,
            duration_seconds,
        ),
        "max_drawdown_pct": _result_number(max_drawdown_pct, 8),
        "win_rate_pct": _result_number(len(wins) / len(trades) * 100, 8) if trades else 0.0,
        "profit_factor": _round_number(profit_factor) if profit_factor is not None else None,
        "profit_loss_ratio": (
            _round_number(profit_loss_ratio) if profit_loss_ratio is not None else None
        ),
        "sharpe_ratio": _sharpe_ratio(equity_values, config["timeframe"]),
        "trade_count": len(trades),
        "liquidation_count": liquidation_count,
        "maintenance_margin_rate_pct": MAINTENANCE_MARGIN_RATE * 100,
        "liquidation_model": "isolated_fixed_mmr_ohlc",
        "exposure_pct": _result_number(exposed_bars / len(candles) * 100, 8),
        "gross_profit": _result_number(gross_profit, 8),
        "gross_loss": _result_number(gross_loss, 8),
        "average_trade": (
            _result_number(sum(net_results) / len(net_results), 8) if trades else 0.0
        ),
    }
    return {
        "account": {
            "initial_capital": _result_number(initial_capital, 8),
            "final_balance": _result_number(balance, 8),
            "final_equity": _result_number(final_equity, 8),
            "net_profit": _result_number(net_profit, 8),
            "total_fees": _result_number(total_fees, 8),
            "peak_equity": _result_number(peak_equity, 8),
        },
        "metrics": metrics,
        "_full_equity_curve": curve,
        "trades": trades,
    }


def _build_signals(
    strategy_id: str, candles: list[_Candle], params: dict[str, int | float]
) -> list[int]:
    """Compatibility wrapper for callers that used the former private helper."""

    return DEFAULT_STRATEGY_EVALUATOR.evaluate(strategy_id, candles, params)


def _liquidation_price(entry_price: float, direction: int, leverage: int) -> float:
    """Return the fixed-MMR isolated liquidation threshold for one position."""
    if direction == 1:
        price = entry_price * (1 - 1 / leverage) / (1 - MAINTENANCE_MARGIN_RATE)
        return max(0.0, price)
    return entry_price * (1 + 1 / leverage) / (1 + MAINTENANCE_MARGIN_RATE)


def _annualized_return(initial: float, final: float, duration_seconds: int) -> float | None:
    if initial <= 0 or duration_seconds <= 0:
        return None
    if final <= 0:
        return -100.0
    exponent = math.log(final / initial) * (365 * 86_400 / duration_seconds)
    # A very short profitable sample can mathematically annualize to infinity.
    # Keep the metric JSON-safe and visibly capped rather than returning Infinity.
    maximum_pct = 1_000_000_000.0
    if exponent >= math.log1p(maximum_pct / 100):
        return maximum_pct
    if exponent <= -50:
        return -100.0
    return _round_number(math.expm1(exponent) * 100)


def _sharpe_ratio(equity_values: list[float], timeframe: str) -> float | None:
    returns = []
    for previous, current in zip(equity_values, equity_values[1:], strict=False):
        if previous > 0:
            value = current / previous - 1
            if math.isfinite(value):
                returns.append(value)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    interval = _timeframe_seconds(timeframe)
    if not interval:
        return None
    periods_per_year = 365 * 86_400 / interval
    sharpe = mean / math.sqrt(variance) * math.sqrt(periods_per_year)
    return _round_number(sharpe) if math.isfinite(sharpe) else None


def _even_sample(points: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(points) <= maximum:
        return points
    if maximum < 2:
        return points[-maximum:]
    indexes = [round(index * (len(points) - 1) / (maximum - 1)) for index in range(maximum)]
    return [points[index] for index in indexes]


def _finite_or_zero(value: float) -> float:
    return value if math.isfinite(value) else 0.0


def _round_number(value: float, digits: int = 4) -> float:
    return round(_finite_or_zero(float(value)), digits)


def _result_number(value: float, digits: int) -> float:
    """Round normal outputs while preserving a finite, non-zero sub-unit value."""
    number = _finite_or_zero(float(value))
    rounded = round(number, digits)
    if rounded == 0.0 and number != 0.0:
        return number
    return rounded
