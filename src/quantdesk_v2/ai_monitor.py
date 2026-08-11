"""Tenant-scoped AI news analysis and technical-indicator opportunity scans."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AiModelConfig,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorRun,
    CompanyProfile,
    News,
    NewsAiBatch,
    NewsAiModelCall,
    NewsAiModelCallItem,
    Security,
    utcnow,
)
from .monitor import MonitorRepository, MonitorUnavailable
from .news_ai import CHUNK_SIZE, run_news_ai_batch
from .prediction_feature_indicators import evaluate_prediction_feature_indicators
from .strategy_indicators import BEARISH_STRATEGY_NAMES, evaluate_strategy_indicators

DEFAULT_INDICATOR_KEYS = ["moving_average_bull", "prediction_trend"]
NEWS_BATCH_SIZE = 10
ACTIVE_RUN_STATUSES = ("pending", "running")
_TIMEFRAME_SECONDS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
NEWS_CATCH_UP_THRESHOLD = NEWS_BATCH_SIZE * 3
NEWS_CATCH_UP_INTERVAL_SECONDS = 60
RUN_STALE_SECONDS = 5 * 60
PREDICTION_SETTLEMENT_RETRY_MINUTES = 5
PREDICTION_SETTLEMENT_GRACE_HOURS = 6
PREDICTION_SETTLEMENT_BACKFILL_DAYS = 7
PREDICTION_FEE_BPS_PER_SIDE = 5.0
PREDICTION_SLIPPAGE_BPS_PER_SIDE = 3.0
PREDICTION_FUNDING_BPS_PER_8H = 1.0
PREDICTION_SETTLEMENT_VERSION = "path_cost_v2"
INDICATOR_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "key": "trend_confirmation",
        "name": "趋势确认",
        "description": "均线趋势与周期预测方向同时成立。",
        "indicator_keys": ["moving_average_bull", "prediction_trend"],
    },
    {
        "key": "breakout_confirmation",
        "name": "放量突破",
        "description": "价格突破、量价配合并获得资金方向确认。",
        "indicator_keys": [
            "trend_breakout",
            "price_volume_rise",
            "prediction_book_imbalance_5",
            "prediction_trend",
        ],
    },
    {
        "key": "pullback_confirmation",
        "name": "趋势回踩",
        "description": "趋势保持、缩量回踩并获得资金方向确认。",
        "indicator_keys": [
            "moving_average_bull",
            "moving_average_pullback_bounce",
            "low_volume_pullback",
            "prediction_trend",
        ],
    },
    {
        "key": "reversal_confirmation",
        "name": "超跌反转",
        "description": "价格完成低位反转并获得周期方向确认。",
        "indicator_keys": [
            "new_low_reversal",
            "oversold_bounce",
            "prediction_trend",
        ],
    },
)
INDICATOR_CONFLICT_PAIRS: tuple[tuple[str, str], ...] = (
    ("low_volume_pullback", "trend_breakout"),
    ("low_volume_pullback", "price_volume_rise"),
    ("low_volume_pullback", "macd_golden_cross_volume"),
)
_worker_lock = threading.Lock()
_worker_started = False
_worker_wakeup = threading.Event()
_ingested_news_lock = threading.Lock()
_ingested_news_ids: dict[str, None] = {}


class AiMonitorError(RuntimeError):
    """Stable user-facing configuration or scheduling error."""


def enqueue_news_analysis(news_ids: Sequence[str]) -> int:
    """Wake the AI worker as soon as newly collected news is committed.

    The mapping preserves ingestion order while deduplicating repeated collector
    notifications. The persisted 15-minute scheduler remains the recovery path
    for process restarts and temporary model failures.
    """

    normalized_ids = [
        str(news_id).strip() for news_id in news_ids if str(news_id).strip()
    ]
    if not normalized_ids:
        return 0
    with _ingested_news_lock:
        before = len(_ingested_news_ids)
        for news_id in normalized_ids:
            _ingested_news_ids.setdefault(news_id, None)
        queued = len(_ingested_news_ids) - before
    _worker_wakeup.set()
    return queued


def _take_ingested_news(limit: int = NEWS_BATCH_SIZE) -> list[str]:
    if limit < 1:
        return []
    with _ingested_news_lock:
        selected = list(_ingested_news_ids)[:limit]
        for news_id in selected:
            _ingested_news_ids.pop(news_id, None)
        if _ingested_news_ids:
            _worker_wakeup.set()
    return selected


def _requeue_ingested_news(news_ids: Sequence[str]) -> None:
    with _ingested_news_lock:
        existing = list(_ingested_news_ids)
        _ingested_news_ids.clear()
        for news_id in news_ids:
            normalized = str(news_id).strip()
            if normalized:
                _ingested_news_ids.setdefault(normalized, None)
        for news_id in existing:
            _ingested_news_ids.setdefault(news_id, None)


def default_config_data() -> dict[str, Any]:
    return {
        "enabled": False,
        "news_interval_minutes": 15,
        "opportunity_interval_minutes": 15,
        "news_lookback_hours": 24,
        "timeframe": "1h",
        "indicator_keys": list(DEFAULT_INDICATOR_KEYS),
        "monitor_symbols": [],
        "minimum_news_confidence": 0.6,
        "minimum_news_mentions": 1,
        "minimum_indicator_score": 65.0,
        "minimum_combined_score": 70.0,
        "maximum_market_age_seconds": 120,
        "minimum_feature_quality": 0.7,
        "minimum_market_flow_quality": 0.5,
        "minimum_calibration_samples": 1000,
        "live_safety_margin_bps": 10.0,
        "news_score_weight": 45.0,
        "technical_score_weight": 35.0,
        "market_flow_score_weight": 20.0,
        "prediction_fee_enabled": True,
        "prediction_fee_bps_per_side": PREDICTION_FEE_BPS_PER_SIDE,
        "prediction_slippage_enabled": True,
        "prediction_slippage_bps_per_side": PREDICTION_SLIPPAGE_BPS_PER_SIDE,
        "prediction_funding_enabled": True,
        "prediction_funding_bps_per_8h": PREDICTION_FUNDING_BPS_PER_8H,
        "last_news_run_at": None,
        "last_opportunity_run_at": None,
        "created_at": None,
        "updated_at": None,
        "persisted": False,
        "match_policy": "all",
        "indicator_conflicts": [],
    }


def config_data(config: AiMonitorConfig | None) -> dict[str, Any]:
    if config is None:
        return default_config_data()
    indicator_keys = list(config.indicator_keys_json or DEFAULT_INDICATOR_KEYS)
    return {
        "enabled": bool(config.enabled),
        "news_interval_minutes": int(config.news_interval_minutes),
        "opportunity_interval_minutes": int(config.opportunity_interval_minutes),
        "news_lookback_hours": int(config.news_lookback_hours),
        "timeframe": config.timeframe,
        "indicator_keys": indicator_keys,
        "monitor_symbols": list(config.monitor_symbols_json or []),
        "minimum_news_confidence": float(config.minimum_news_confidence),
        "minimum_news_mentions": int(config.minimum_news_mentions),
        "minimum_indicator_score": float(config.minimum_indicator_score),
        "minimum_combined_score": float(config.minimum_combined_score),
        "maximum_market_age_seconds": int(config.maximum_market_age_seconds),
        "minimum_feature_quality": float(config.minimum_feature_quality),
        "minimum_market_flow_quality": float(config.minimum_market_flow_quality),
        "minimum_calibration_samples": int(config.minimum_calibration_samples),
        "live_safety_margin_bps": float(config.live_safety_margin_bps),
        "news_score_weight": float(config.news_score_weight),
        "technical_score_weight": float(config.technical_score_weight),
        "market_flow_score_weight": float(config.market_flow_score_weight),
        "prediction_fee_enabled": bool(config.prediction_fee_enabled),
        "prediction_fee_bps_per_side": float(config.prediction_fee_bps_per_side),
        "prediction_slippage_enabled": bool(config.prediction_slippage_enabled),
        "prediction_slippage_bps_per_side": float(
            config.prediction_slippage_bps_per_side
        ),
        "prediction_funding_enabled": bool(config.prediction_funding_enabled),
        "prediction_funding_bps_per_8h": float(config.prediction_funding_bps_per_8h),
        "last_news_run_at": config.last_news_run_at,
        "last_opportunity_run_at": config.last_opportunity_run_at,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
        "persisted": True,
        "match_policy": "all",
        "indicator_conflicts": indicator_conflicts(indicator_keys),
    }


def opportunity_score_weights(config: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Return normalized score weights, falling back safely for legacy inputs."""

    source = config or {}
    raw = {
        "news": float(source.get("news_score_weight", 45.0)),
        "technical": float(source.get("technical_score_weight", 35.0)),
        "market_flow": float(source.get("market_flow_score_weight", 20.0)),
    }
    total = sum(raw.values())
    if any(value < 0 or not math.isfinite(value) for value in raw.values()) or not math.isclose(
        total, 100.0, abs_tol=0.01
    ):
        raw = {"news": 45.0, "technical": 35.0, "market_flow": 20.0}
        total = 100.0
    return {key: round(value / total, 6) for key, value in raw.items()}


def weighted_opportunity_score(
    news_score: float,
    technical_score: float,
    market_flow_score: float,
    config: Mapping[str, Any] | None = None,
) -> float:
    weights = opportunity_score_weights(config)
    return round(
        float(news_score) * weights["news"]
        + float(technical_score) * weights["technical"]
        + float(market_flow_score) * weights["market_flow"],
        4,
    )


def indicator_templates() -> list[dict[str, Any]]:
    """Return bounded AND-compatible presets for the configuration UI."""

    return [dict(item) for item in INDICATOR_TEMPLATES]


def indicator_conflicts(indicator_keys: Sequence[str]) -> list[dict[str, str]]:
    """Detect mathematically incompatible latest-bar indicator requirements."""

    selected = {str(key).strip().lower() for key in indicator_keys}
    names = {item["key"]: item["name"] for item in indicator_catalog()}
    return [
        {
            "left": left,
            "right": right,
            "left_name": names.get(left, left),
            "right_name": names.get(right, right),
        }
        for left, right in INDICATOR_CONFLICT_PAIRS
        if left in selected and right in selected
    ]


def indicator_catalog(timeframe: str = "1h") -> list[dict[str, Any]]:
    """Return the same twenty indicator definitions used by contract research."""

    kline = evaluate_strategy_indicators([], timeframe)["items"]
    prediction = evaluate_prediction_feature_indicators(None, timeframe)["items"]
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "short_name": BEARISH_STRATEGY_NAMES.get(item["key"], item["name"]),
            "category": item["category"],
            "description": item["description"],
            "source": "prediction" if str(item["key"]).startswith("prediction_") else "kline",
        }
        for item in [*kline, *prediction]
    ]


def valid_indicator_keys() -> set[str]:
    return {item["key"] for item in indicator_catalog()}


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _news_model_call_audit_index(
    db: Session,
    user_id: int,
    news_rows: Sequence[Any],
) -> dict[str, dict[str, Any]]:
    """Resolve each news item to its current, tenant-owned persisted model calls."""

    current_batches = {
        str(_row_value(row, "id") or ""): str(_row_value(row, "ai_batch_id") or "")
        for row in news_rows
        if _row_value(row, "id") and _row_value(row, "ai_batch_id")
    }
    if not current_batches:
        return {}
    rows = db.execute(
        select(
            NewsAiModelCallItem.news_id,
            NewsAiModelCall.id,
            NewsAiModelCall.batch_id,
        )
        .join(
            NewsAiModelCall,
            NewsAiModelCall.id == NewsAiModelCallItem.call_id,
        )
        .join(NewsAiBatch, NewsAiBatch.id == NewsAiModelCall.batch_id)
        .where(
            NewsAiBatch.started_by == user_id,
            NewsAiModelCallItem.news_id.in_(list(current_batches)),
            NewsAiModelCall.batch_id.in_(sorted(set(current_batches.values()))),
        )
        .order_by(NewsAiModelCall.id)
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for news_id, call_id, batch_id in rows:
        normalized_news_id = str(news_id)
        normalized_batch_id = str(batch_id)
        if current_batches.get(normalized_news_id) != normalized_batch_id:
            continue
        audit = result.setdefault(
            normalized_news_id,
            {"batch_id": normalized_batch_id, "call_ids": []},
        )
        audit["call_ids"].append(int(call_id))
    return result


def contract_symbol_map(repository: MonitorRepository) -> dict[str, str]:
    """Map normalized US tickers to the configured Binance TradFi contracts."""

    result: dict[str, str] = {}
    for item in repository.symbols_meta:
        contract = str(item.get("symbol") or "").strip().upper()
        if not contract or str(item.get("underlyingType") or "").upper() != "EQUITY":
            continue
        base = contract.removesuffix("USDT").removesuffix("USD1")
        result[base] = contract
    if "BRKB" in result:
        result["BRK.B"] = result["BRKB"]
        result["BRK-B"] = result["BRKB"]
    if "PAYP" in result:
        result["PYPL"] = result["PAYP"]
    return result


def monitor_symbol_catalog(repository: MonitorRepository) -> list[dict[str, str]]:
    """Return the equity contracts that can be selected for AI monitoring."""

    items: list[dict[str, str]] = []
    for metadata in repository.symbols_meta:
        contract = str(metadata.get("symbol") or "").strip().upper()
        if not contract or str(metadata.get("underlyingType") or "").upper() != "EQUITY":
            continue
        symbol = contract.removesuffix("USDT").removesuffix("USD1")
        if symbol == "PAYP":
            symbol = "PYPL"
        elif symbol == "BRKB":
            symbol = "BRK.B"
        items.append({"symbol": symbol, "contract_symbol": contract})
    return sorted(items, key=lambda item: (item["symbol"], item["contract_symbol"]))


def filter_monitored_candidates(
    candidates: Sequence[dict[str, Any]], monitor_symbols: Sequence[str]
) -> list[dict[str, Any]]:
    """Apply the user's contract-symbol allowlist; an empty list means all."""

    allowed = {str(symbol).strip().upper() for symbol in monitor_symbols if str(symbol).strip()}
    if not allowed:
        return [candidate for candidate in candidates if candidate.get("contract_symbol")]
    return [
        candidate
        for candidate in candidates
        if str(candidate.get("contract_symbol") or "").upper() in allowed
    ]


def prediction_outcome(
    entry_price: float, exit_price: float, direction: str
) -> dict[str, float | str]:
    """Calculate the same direction-aware result shape used by prediction ledgers."""

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


def prediction_cost_settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the user-selected virtual prediction cost assumptions."""

    values = config or {}
    return {
        "fee_enabled": bool(values.get("prediction_fee_enabled", True)),
        "fee_bps_per_side": max(
            float(values.get("prediction_fee_bps_per_side", PREDICTION_FEE_BPS_PER_SIDE)),
            0.0,
        ),
        "slippage_enabled": bool(values.get("prediction_slippage_enabled", True)),
        "slippage_bps_per_side": max(
            float(
                values.get(
                    "prediction_slippage_bps_per_side",
                    PREDICTION_SLIPPAGE_BPS_PER_SIDE,
                )
            ),
            0.0,
        ),
        "funding_enabled": bool(values.get("prediction_funding_enabled", True)),
        "funding_bps_per_8h": max(
            float(
                values.get(
                    "prediction_funding_bps_per_8h",
                    PREDICTION_FUNDING_BPS_PER_8H,
                )
            ),
            0.0,
        ),
    }


def prediction_cost_breakdown(
    predicted_at: datetime,
    due_at: datetime,
    config: Mapping[str, Any] | None = None,
) -> dict[str, float | bool]:
    """Calculate enabled fee, slippage and funding components in basis points."""

    settings = prediction_cost_settings(config)
    holding_seconds = max((due_at - predicted_at).total_seconds(), 0.0)
    fee_bps = (
        2 * float(settings["fee_bps_per_side"]) if settings["fee_enabled"] else 0.0
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
    """Estimate the enabled round-trip prediction costs in basis points."""

    return float(prediction_cost_breakdown(predicted_at, due_at, config)["total_cost_bps"])


def prediction_net_outcome(gross_return_bps: float, estimated_cost_bps: float) -> dict[str, Any]:
    """Convert a gross directional return into the conservative cost-adjusted result."""

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
    """Measure maximum favorable/adverse excursion over the prediction holding window."""

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


def edge_calibration_summary(
    returns_bps: Sequence[float], minimum_samples: int
) -> dict[str, Any]:
    """Return a conservative 95% lower confidence bound for historical gross edge."""

    values = [float(value) for value in returns_bps if math.isfinite(float(value))]
    sample_count = len(values)
    mean = sum(values) / sample_count if sample_count else None
    deviation: float | None = None
    lower_bound: float | None = None
    if sample_count >= 2 and mean is not None:
        deviation = math.sqrt(
            sum((value - mean) ** 2 for value in values) / (sample_count - 1)
        )
        lower_bound = mean - 1.96 * deviation / math.sqrt(sample_count)
    return {
        "sample_count": sample_count,
        "minimum_samples": int(minimum_samples),
        "mean_gross_edge_bps": round(mean, 8) if mean is not None else None,
        "sample_deviation_bps": round(deviation, 8) if deviation is not None else None,
        "lower_bound_bps": round(lower_bound, 8) if lower_bound is not None else None,
        "sufficient_samples": sample_count >= minimum_samples,
    }


def historical_edge_calibration(
    db: Session,
    user_id: int,
    direction: str,
    *,
    minimum_indicator_score: float,
    minimum_combined_score: float,
    minimum_samples: int,
) -> dict[str, Any]:
    """Calibrate only from prior settled predictions that meet the configured score gates."""

    returns = db.scalars(
        select(AiMonitorPrediction.directional_return_bps)
        .where(
            AiMonitorPrediction.user_id == user_id,
            AiMonitorPrediction.direction == direction,
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.directional_return_bps.is_not(None),
            AiMonitorPrediction.signal_indicator_score
            >= Decimal(str(minimum_indicator_score)),
            AiMonitorPrediction.confidence_score >= Decimal(str(minimum_combined_score)),
        )
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
        .limit(5000)
    ).all()
    return edge_calibration_summary(
        [float(value) for value in returns if value is not None], minimum_samples
    )


def signal_market_quality(
    scan: Mapping[str, Any],
    market: Mapping[str, Any],
    timeframe: str,
    now: datetime,
    *,
    maximum_market_age_seconds: int,
    minimum_feature_quality: float,
    requires_prediction_features: bool,
) -> dict[str, Any]:
    """Evaluate freshness and feature quality without fabricating missing market data."""

    now_ms = _datetime_ms(now)
    ticker_ts = int(market.get("ts") or 0)
    if 0 < ticker_ts < 1_000_000_000_000:
        ticker_ts *= 1_000
    evaluated_at = int(scan.get("evaluated_at") or 0)
    if 0 < evaluated_at < 1_000_000_000_000:
        evaluated_at *= 1_000
    bar_close_ms = evaluated_at + _TIMEFRAME_SECONDS.get(timeframe, 3600) * 1_000
    ticker_age_seconds = max(0.0, (now_ms - ticker_ts) / 1_000) if ticker_ts else None
    bar_age_seconds = max(0.0, (now_ms - bar_close_ms) / 1_000) if evaluated_at else None
    bar_age_limit = max(
        maximum_market_age_seconds,
        min(_TIMEFRAME_SECONDS.get(timeframe, 3600) // 4, 900),
    )
    prediction_features = scan.get("prediction_features") or {}
    feature_quality_value = prediction_features.get("quality_score")
    feature_quality = (
        float(feature_quality_value) if feature_quality_value is not None else None
    )
    ticker_fresh = (
        ticker_age_seconds is not None
        and ticker_age_seconds <= maximum_market_age_seconds
    )
    kline_fresh = bar_age_seconds is not None and bar_age_seconds <= bar_age_limit
    feature_quality_ok = not requires_prediction_features or (
        feature_quality is not None and feature_quality >= minimum_feature_quality
    )
    price_available = float(market.get("price") or 0) > 0
    checks = {
        "price_available": price_available,
        "ticker_fresh": ticker_fresh,
        "kline_fresh": kline_fresh,
        "feature_quality": feature_quality_ok,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "ticker_age_seconds": round(ticker_age_seconds, 3)
        if ticker_age_seconds is not None
        else None,
        "maximum_ticker_age_seconds": maximum_market_age_seconds,
        "bar_age_seconds": round(bar_age_seconds, 3) if bar_age_seconds is not None else None,
        "maximum_bar_age_seconds": bar_age_limit,
        "feature_quality": feature_quality,
        "minimum_feature_quality": minimum_feature_quality,
    }


def signal_readiness_snapshot(
    *,
    matched: bool,
    indicator_score: float,
    combined_score: float,
    estimated_cost_bps: float,
    market_quality: Mapping[str, Any],
    calibration: Mapping[str, Any],
    minimum_indicator_score: float,
    minimum_combined_score: float,
    safety_margin_bps: float,
    market_flow: Mapping[str, Any],
    market_flow_weight: float,
    minimum_market_flow_quality: float,
) -> dict[str, Any]:
    """Classify a virtual signal for research or shadow observation only."""

    lower_bound = calibration.get("lower_bound_bps")
    required_gross_edge = estimated_cost_bps + safety_margin_bps
    flow_required = market_flow_weight > 0
    try:
        flow_quality = float(market_flow.get("data_quality"))
    except (TypeError, ValueError, OverflowError):
        flow_quality = None
    flow_available = bool(market_flow.get("directional_data_available"))
    flow_fresh = bool(market_flow.get("fresh"))
    checks = {
        "all_indicators_matched": bool(matched),
        "indicator_strength": indicator_score >= minimum_indicator_score,
        "combined_score": combined_score >= minimum_combined_score,
        "market_quality": bool(market_quality.get("passed")),
        "market_flow_available": not flow_required or flow_available,
        "market_flow_freshness": not flow_required or flow_fresh,
        "market_flow_quality": not flow_required
        or (
            flow_quality is not None
            and math.isfinite(flow_quality)
            and flow_quality >= minimum_market_flow_quality
        ),
        "calibration_samples": bool(calibration.get("sufficient_samples")),
        "cost_stress_edge": (
            lower_bound is not None and float(lower_bound) > required_gross_edge
        ),
    }
    failed_labels = {
        "all_indicators_matched": "所选技术条件未全部满足",
        "indicator_strength": "技术强度未达到准入线",
        "combined_score": "组合评分未达到准入线",
        "market_quality": "实时行情或预测因子质量不足",
        "market_flow_available": "资金盘口缺少可验证的方向数据",
        "market_flow_freshness": "资金盘口方向数据已经过期",
        "market_flow_quality": "资金盘口数据质量未达到准入线",
        "calibration_samples": "历史校准样本不足",
        "cost_stress_edge": "毛优势置信下限未覆盖成本与安全边际",
    }
    status = "shadow_ready" if all(checks.values()) else "research_only"
    return {
        "status": status,
        "checks": checks,
        "failed_reasons": [
            failed_labels[key] for key, passed in checks.items() if not passed
        ],
        "minimum_indicator_score": minimum_indicator_score,
        "minimum_combined_score": minimum_combined_score,
        "estimated_cost_bps": estimated_cost_bps,
        "safety_margin_bps": safety_margin_bps,
        "required_gross_edge_bps": round(required_gross_edge, 8),
        "market_quality": dict(market_quality),
        "market_flow_quality": {
            "required": flow_required,
            "weight": market_flow_weight,
            "available": flow_available,
            "fresh": flow_fresh,
            "data_quality": flow_quality,
            "minimum_data_quality": minimum_market_flow_quality,
        },
        "calibration": dict(calibration),
        "note": "shadow_ready 仅表示可进入影子观察，仍不会触发真实订单。",
    }


def _optional_flow_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _market_flow_input_maps(
    db: Session,
    repository: MonitorRepository,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Bulk-load the latest market-flow inputs once for an opportunity scan."""

    def rows(sql: str) -> list[dict[str, Any]]:
        try:
            return [dict(item) for item in repository._query(sql)]
        except (MonitorUnavailable, KeyError, TypeError, ValueError):
            return []

    depth_rows = rows("SELECT * FROM market_microstructure")
    positioning_rows = rows(
        """SELECT p.* FROM market_positioning_snapshots p
           JOIN (
               SELECT symbol,MAX(snapshot_at_ms) AS latest
               FROM market_positioning_snapshots GROUP BY symbol
           ) current
             ON current.symbol=p.symbol AND current.latest=p.snapshot_at_ms"""
    )
    ticker_rows = rows("SELECT symbol,price,pct_24h,quote_volume,ts FROM ticker")
    underlying_rows = rows("SELECT * FROM underlying_market_quotes")
    profile_rows = db.execute(
        select(
            Security.symbol,
            CompanyProfile.market_cap,
            CompanyProfile.shares_outstanding,
            CompanyProfile.source,
        ).outerjoin(CompanyProfile, CompanyProfile.security_id == Security.id)
    ).all()
    return {
        "depth": {str(item.get("symbol") or "").upper(): item for item in depth_rows},
        "positioning": {
            str(item.get("symbol") or "").upper(): item for item in positioning_rows
        },
        "ticker": {str(item.get("symbol") or "").upper(): item for item in ticker_rows},
        "underlying": {
            str(item.get("contract_symbol") or "").upper(): item
            for item in underlying_rows
        },
        "profile": {
            str(symbol or "").upper(): {
                "market_cap": market_cap,
                "shares_outstanding": shares_outstanding,
                "source": source,
            }
            for symbol, market_cap, shares_outstanding, source in profile_rows
        },
    }


def market_flow_snapshot(
    inputs: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    symbol: str,
    contract_symbol: str,
    direction: str,
    now: datetime,
) -> dict[str, Any]:
    """Build an explainable flow score from depth, taker flow and turnover proxies.

    Binance publishes market-by-price depth, not individual order identities.  Bid and ask
    counts therefore mean visible price levels.  The main-force ratio is deliberately marked
    as a proxy and combines active taker flow, near-book pressure and depth acceleration.
    """

    contract = contract_symbol.upper()
    equity = symbol.upper()
    depth = dict(inputs.get("depth", {}).get(contract, {}))
    positioning = dict(inputs.get("positioning", {}).get(contract, {}))
    ticker = dict(inputs.get("ticker", {}).get(contract, {}))
    underlying = dict(inputs.get("underlying", {}).get(contract, {}))
    profile = dict(inputs.get("profile", {}).get(equity, {}))
    now_seconds = int(now.replace(tzinfo=UTC).timestamp())
    now_ms = now_seconds * 1_000

    depth_ts = int(depth.get("ts") or 0)
    depth_fresh = 0 <= now_seconds - depth_ts <= 30 if depth_ts else False
    if not depth_fresh:
        depth = {}
    positioning_ts = int(positioning.get("snapshot_at_ms") or 0)
    positioning_fresh = (
        0 <= now_ms - positioning_ts <= 20 * 60 * 1_000 if positioning_ts else False
    )
    if not positioning_fresh:
        positioning = {}

    bid_depth = _optional_flow_number(depth.get("bid_depth_notional"))
    ask_depth = _optional_flow_number(depth.get("ask_depth_notional"))
    bid_depth_5 = _optional_flow_number(depth.get("bid_depth_notional_5"))
    ask_depth_5 = _optional_flow_number(depth.get("ask_depth_notional_5"))
    book_imbalance = _optional_flow_number(depth.get("book_imbalance"))
    book_imbalance_5 = _optional_flow_number(depth.get("book_imbalance_5"))
    bid_growth_5s = _optional_flow_number(depth.get("bid_depth_change_5s_pct"))
    ask_growth_5s = _optional_flow_number(depth.get("ask_depth_change_5s_pct"))

    taker_buy = _optional_flow_number(positioning.get("taker_buy_volume"))
    taker_sell = _optional_flow_number(positioning.get("taker_sell_volume"))
    taker_ratio = _optional_flow_number(positioning.get("taker_buy_sell_ratio"))
    active_buy_ratio: float | None = None
    if taker_buy is not None and taker_sell is not None and taker_buy + taker_sell > 0:
        active_buy_ratio = taker_buy / (taker_buy + taker_sell)
    elif taker_ratio is not None and taker_ratio >= 0:
        active_buy_ratio = taker_ratio / (1 + taker_ratio)

    components: list[tuple[str, float, float]] = []
    if active_buy_ratio is not None:
        components.append(("taker_active_buy", active_buy_ratio, 0.50))
    if book_imbalance_5 is not None:
        components.append(("near_book_pressure", (book_imbalance_5 + 1) / 2, 0.30))
    elif book_imbalance is not None:
        components.append(("book_pressure", (book_imbalance + 1) / 2, 0.25))
    if bid_growth_5s is not None and ask_growth_5s is not None:
        growth_share = 0.5 + 0.5 * math.tanh((bid_growth_5s - ask_growth_5s) / 20)
        components.append(("depth_acceleration", growth_share, 0.20))
    component_weight = sum(weight for _, _, weight in components)
    main_force_ratio = (
        sum(value * weight for _, value, weight in components) / component_weight
        if component_weight
        else None
    )
    if main_force_ratio is not None:
        main_force_ratio = max(0.0, min(1.0, main_force_ratio))

    underlying_volume = _optional_flow_number(underlying.get("volume"))
    shares_outstanding = _optional_flow_number(profile.get("shares_outstanding"))
    turnover_rate_pct: float | None = None
    turnover_source = "unavailable"
    if underlying_volume is not None and shares_outstanding is not None and shares_outstanding > 0:
        # The research profile stores provider share counts in millions.  Keep a defensive
        # escape hatch for future imports that already contain an absolute share count.
        absolute_shares = (
            shares_outstanding * 1_000_000
            if shares_outstanding < 100_000_000
            else shares_outstanding
        )
        if absolute_shares > 0:
            turnover_rate_pct = underlying_volume / absolute_shares * 100
            turnover_source = "underlying_volume_over_shares"
    if turnover_rate_pct is None:
        quote_volume = _optional_flow_number(ticker.get("quote_volume"))
        market_cap = _optional_flow_number(profile.get("market_cap"))
        if quote_volume is not None and market_cap is not None and market_cap > 0:
            market_cap_usd = market_cap * 1_000_000
            turnover_rate_pct = quote_volume / market_cap_usd * 100
            turnover_source = "contract_value_over_market_cap_proxy"

    directional_force = (
        main_force_ratio if direction == "long" else 1 - main_force_ratio
        if main_force_ratio is not None
        else None
    )
    directional_score = directional_force * 100 if directional_force is not None else 50.0
    turnover_activity_score = (
        100 * (1 - math.exp(-max(turnover_rate_pct, 0.0)))
        if turnover_rate_pct is not None
        else None
    )
    flow_score = directional_score
    if turnover_activity_score is not None:
        flow_score = directional_score * 0.85 + turnover_activity_score * 0.15
    available_quality = min(component_weight, 1.0) * 0.65
    available_quality += 0.15 if turnover_rate_pct is not None else 0.0
    available_quality += 0.10 if _optional_flow_number(depth.get("spread_bps")) is not None else 0.0
    available_quality += (
        0.10
        if depth.get("bid_level_count") is not None and depth.get("ask_level_count") is not None
        else 0.0
    )
    data_quality = max(0.0, min(1.0, available_quality))
    hard_conflict = bool(
        main_force_ratio is not None
        and len(components) >= 2
        and directional_score < 35
    )
    confirms_direction = bool(main_force_ratio is not None and directional_score >= 55)
    directional_data_available = main_force_ratio is not None
    flow_fresh = directional_data_available and (depth_fresh or positioning_fresh)

    return {
        "version": "market_flow_proxy_v1",
        "score": round(max(0.0, min(100.0, flow_score)), 4),
        "directional_score": round(directional_score, 4),
        "data_quality": round(data_quality, 4),
        "directional_data_available": directional_data_available,
        "fresh": flow_fresh,
        "freshness": {
            "depth": depth_fresh,
            "positioning": positioning_fresh,
        },
        "confirms_direction": confirms_direction,
        "hard_conflict": hard_conflict,
        "main_force_ratio": round(main_force_ratio, 6)
        if main_force_ratio is not None
        else None,
        "main_force_method": "taker_50% + near_book_30% + depth_acceleration_20%",
        "main_force_components": [name for name, _, _ in components],
        "active_buy_ratio": round(active_buy_ratio, 6)
        if active_buy_ratio is not None
        else None,
        "taker_buy_sell_ratio": taker_ratio,
        "turnover_rate_pct": round(turnover_rate_pct, 6)
        if turnover_rate_pct is not None
        else None,
        "turnover_source": turnover_source,
        "underlying_volume": underlying_volume,
        "shares_outstanding": shares_outstanding,
        "bid_depth_notional": bid_depth,
        "ask_depth_notional": ask_depth,
        "bid_depth_notional_5": bid_depth_5,
        "ask_depth_notional_5": ask_depth_5,
        "book_imbalance": book_imbalance,
        "book_imbalance_5": book_imbalance_5,
        "bid_level_count": int(depth.get("bid_level_count") or 0),
        "ask_level_count": int(depth.get("ask_level_count") or 0),
        "spread_bps": _optional_flow_number(depth.get("spread_bps")),
        "bid_depth_change_5s_pct": bid_growth_5s,
        "ask_depth_change_5s_pct": ask_growth_5s,
        "bid_depth_change_30s_pct": _optional_flow_number(
            depth.get("bid_depth_change_30s_pct")
        ),
        "ask_depth_change_30s_pct": _optional_flow_number(
            depth.get("ask_depth_change_30s_pct")
        ),
        "imbalance_change_5s": _optional_flow_number(depth.get("imbalance_change_5s")),
        "price_change_pct_24h": _optional_flow_number(ticker.get("pct_24h")),
        "captured_at": depth_ts or int(ticker.get("ts") or 0),
        "sources": {
            "depth": "binance_futures_market_by_price" if depth else "unavailable",
            "active_flow": "binance_futures_taker" if positioning else "book_proxy",
            "turnover": turnover_source,
            "order_count": "visible_price_levels_proxy",
        },
        "note": "主力量比为主动成交、近端盘口和挂单增速的代理值；买卖单数为可见价格档位数，不冒充真实订单笔数。",
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


def summarize_historical_opportunities(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate observed outcomes for the historical-opportunity dashboard."""

    settled = [item for item in items if item.get("result") in {"win", "loss", "flat"}]
    win_count = sum(item.get("result") == "win" for item in settled)
    loss_count = sum(item.get("result") == "loss" for item in settled)
    flat_count = sum(item.get("result") == "flat" for item in settled)
    decisive_count = win_count + loss_count
    returns = [
        float(item["directional_return_bps"])
        for item in settled
        if item.get("directional_return_bps") is not None
    ]
    gross_returns = [
        float(item["gross_directional_return_bps"])
        for item in settled
        if item.get("gross_directional_return_bps") is not None
    ]
    costs = [
        float(item["estimated_cost_bps"])
        for item in settled
        if item.get("estimated_cost_bps") is not None
    ]
    fee_costs = [
        float(item["fee_cost_bps"])
        for item in settled
        if item.get("fee_cost_bps") is not None
    ]
    slippage_costs = [
        float(item["slippage_cost_bps"])
        for item in settled
        if item.get("slippage_cost_bps") is not None
    ]
    funding_costs = [
        float(item["funding_cost_bps"])
        for item in settled
        if item.get("funding_cost_bps") is not None
    ]
    favorable = [
        float(item["max_favorable_bps"])
        for item in settled
        if item.get("max_favorable_bps") is not None
    ]
    adverse = [
        float(item["max_adverse_bps"])
        for item in settled
        if item.get("max_adverse_bps") is not None
    ]
    total = len(items)
    return {
        "historical_count": total,
        "settled_count": len(settled),
        "unavailable_count": total - len(settled),
        "win_count": win_count,
        "loss_count": loss_count,
        "flat_count": flat_count,
        "decisive_count": decisive_count,
        "hit_rate": round(win_count / decisive_count * 100, 2) if decisive_count else None,
        "coverage_rate": round(len(settled) / total * 100, 2) if total else 0.0,
        "average_directional_return_bps": round(sum(returns) / len(returns), 4)
        if returns
        else None,
        "average_gross_return_bps": round(sum(gross_returns) / len(gross_returns), 4)
        if gross_returns
        else None,
        "average_estimated_cost_bps": round(sum(costs) / len(costs), 4) if costs else None,
        "average_fee_cost_bps": round(sum(fee_costs) / len(fee_costs), 4)
        if fee_costs
        else None,
        "average_slippage_cost_bps": round(sum(slippage_costs) / len(slippage_costs), 4)
        if slippage_costs
        else None,
        "average_funding_cost_bps": round(sum(funding_costs) / len(funding_costs), 4)
        if funding_costs
        else None,
        "average_max_favorable_bps": round(sum(favorable) / len(favorable), 4)
        if favorable
        else None,
        "average_max_adverse_bps": round(sum(adverse) / len(adverse), 4)
        if adverse
        else None,
        "best_directional_return_bps": round(max(returns), 4) if returns else None,
        "worst_directional_return_bps": round(min(returns), 4) if returns else None,
        "confirmed_count": sum(bool(item.get("technical_confirmed")) for item in items),
        "candidate_count": sum(not bool(item.get("technical_confirmed")) for item in items),
        "long_count": sum(item.get("direction") == "long" for item in items),
        "short_count": sum(item.get("direction") == "short" for item in items),
        "shadow_ready_count": sum(
            item.get("readiness_status") == "shadow_ready" for item in items
        ),
        "research_only_count": sum(
            item.get("readiness_status") != "shadow_ready" for item in items
        ),
    }


def settleable_historical_outcomes(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Exclude expired opportunities that cannot be priced at their settlement time."""

    return [dict(item) for item in items if item.get("result") != "unavailable"]


def strategy_readiness_report(
    db: Session,
    user_id: int,
    cost_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate quantitative gates required before shadow or micro-live validation."""

    predictions = list(
        db.scalars(
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.user_id == user_id,
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.directional_return_bps.is_not(None),
        )
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
        .limit(5000)
        ).all()
    )
    predictions.reverse()
    gross_returns = [float(item.directional_return_bps or 0) for item in predictions]
    costs = [
        prediction_estimated_cost_bps(item.predicted_at, item.due_at, cost_config)
        for item in predictions
    ]
    net_returns = [
        gross - cost for gross, cost in zip(gross_returns, costs, strict=True)
    ]
    sample_count = len(predictions)
    confidence = edge_calibration_summary(net_returns, 1000)
    confidence_lower = confidence["lower_bound_bps"]
    span_days = (
        (predictions[-1].predicted_at - predictions[0].predicted_at).total_seconds()
        / 86_400
        if sample_count >= 2
        else 0.0
    )
    gross_profit = sum(max(value, 0.0) for value in net_returns)
    gross_loss = abs(sum(min(value, 0.0) for value in net_returns))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    doubled_cost_returns = [
        gross - cost * 2 for gross, cost in zip(gross_returns, costs, strict=True)
    ]
    doubled_cost_average = (
        sum(doubled_cost_returns) / sample_count if sample_count else None
    )
    month_returns: dict[str, list[float]] = {}
    direction_returns: dict[str, list[float]] = {"long": [], "short": []}
    symbol_profit: dict[str, float] = {}
    for prediction, net_return in zip(predictions, net_returns, strict=True):
        month_returns.setdefault(prediction.predicted_at.strftime("%Y-%m"), []).append(
            net_return
        )
        direction_returns.setdefault(prediction.direction, []).append(net_return)
        symbol_profit[prediction.symbol] = symbol_profit.get(prediction.symbol, 0.0) + max(
            net_return, 0.0
        )
    positive_months = sum(
        sum(values) / len(values) > 0 for values in month_returns.values() if values
    )
    total_positive_profit = sum(symbol_profit.values())
    largest_symbol_share = (
        max(symbol_profit.values(), default=0.0) / total_positive_profit * 100
        if total_positive_profit > 0
        else None
    )
    long_count = len(direction_returns.get("long", []))
    short_count = len(direction_returns.get("short", []))
    criteria = [
        {
            "key": "sample_count",
            "label": "已结算去重样本",
            "passed": sample_count >= 1000,
            "current": sample_count,
            "required": "≥ 1,000 条",
        },
        {
            "key": "history_span",
            "label": "样本时间跨度",
            "passed": span_days >= 180,
            "current": round(span_days, 2),
            "required": "≥ 180 天",
        },
        {
            "key": "confidence_lower",
            "label": "净收益 95% 下限",
            "passed": confidence_lower is not None and confidence_lower > 0,
            "current": confidence_lower,
            "required": "> 0 bps",
        },
        {
            "key": "profit_factor",
            "label": "成本后利润因子",
            "passed": profit_factor is not None and profit_factor >= 1.2,
            "current": round(profit_factor, 4) if profit_factor is not None else None,
            "required": "≥ 1.20",
        },
        {
            "key": "double_cost",
            "label": "双倍成本压力收益",
            "passed": doubled_cost_average is not None and doubled_cost_average > 0,
            "current": round(doubled_cost_average, 4)
            if doubled_cost_average is not None
            else None,
            "required": "> 0 bps",
        },
        {
            "key": "positive_months",
            "label": "正收益月份",
            "passed": positive_months >= 5,
            "current": positive_months,
            "required": "≥ 5 个月",
        },
        {
            "key": "both_directions",
            "label": "多空样本覆盖",
            "passed": long_count >= 100 and short_count >= 100,
            "current": f"多 {long_count} / 空 {short_count}",
            "required": "各 ≥ 100 条",
        },
        {
            "key": "concentration",
            "label": "单品种利润集中度",
            "passed": largest_symbol_share is not None and largest_symbol_share <= 20,
            "current": round(largest_symbol_share, 2)
            if largest_symbol_share is not None
            else None,
            "required": "≤ 20%",
        },
    ]
    quantitative_ready = all(item["passed"] for item in criteria)
    return {
        "phase": "shadow_validation" if quantitative_ready else "research",
        "quantitative_ready": quantitative_ready,
        "live_trading_ready": False,
        "passed_count": sum(item["passed"] for item in criteria),
        "total_count": len(criteria),
        "criteria": criteria,
        "cost_settings": prediction_cost_settings(cost_config),
        "paper_and_shadow_requirements": [
            "同执行链模拟盘至少 100 笔",
            "影子运行不少于 4 周且保护事故为 0",
            "完成订单对账、部分成交、断线和保护单故障演练",
        ],
        "note": "量化门槛全部通过后也只能进入影子验证；微型实盘仍需人工审批。",
    }


def historical_opportunity_analytics(
    db: Session,
    _repository: MonitorRepository,
    user_id: int,
    *,
    limit: int = 300,
    news_score_min: float = 0.0,
    indicator_score_min: float = 0.0,
    direction: str = "all",
) -> dict[str, Any]:
    """Summarize completed virtual predictions without re-settling opportunities."""

    current_config = config_data(db.get(AiMonitorConfig, user_id))
    active_cost_settings = prediction_cost_settings(current_config)
    news_score_expression = func.coalesce(
        AiMonitorPrediction.signal_news_score,
        AiMonitorOpportunity.news_score,
    )
    indicator_score_expression = func.coalesce(
        AiMonitorPrediction.signal_indicator_score,
        AiMonitorOpportunity.indicator_score,
    )
    conditions = [
        AiMonitorPrediction.user_id == user_id,
        AiMonitorOpportunity.user_id == user_id,
        news_score_expression >= Decimal(str(news_score_min)),
        indicator_score_expression >= Decimal(str(indicator_score_min)),
    ]
    if direction in {"long", "short"}:
        conditions.append(AiMonitorPrediction.direction == direction)
    statement = (
        select(AiMonitorPrediction, AiMonitorOpportunity)
        .join(
            AiMonitorOpportunity,
            AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
        )
        .where(
            *conditions,
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.result.is_not(None),
            AiMonitorPrediction.entry_price.is_not(None),
            AiMonitorPrediction.exit_price.is_not(None),
        )
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
        .limit(limit)
    )
    rows = db.execute(statement).all()
    outcomes: list[dict[str, Any]] = []
    for prediction, opportunity in rows:
        gross_return = float(prediction.directional_return_bps or 0)
        cost_breakdown = prediction_cost_breakdown(
            prediction.predicted_at,
            prediction.due_at,
            current_config,
        )
        estimated_cost = float(cost_breakdown["total_cost_bps"])
        net_outcome = prediction_net_outcome(gross_return, estimated_cost)
        net_return = float(net_outcome["net_directional_return_bps"])
        net_result = str(net_outcome["net_result"])
        outcomes.append(
            {
            "id": opportunity.public_id,
            "prediction_id": prediction.public_id,
            "symbol": prediction.symbol,
            "contract_symbol": prediction.contract_symbol,
            "direction": prediction.direction,
            "timeframe": prediction.timeframe,
            "technical_confirmed": True,
            "news_score": float(
                prediction.signal_news_score
                if prediction.signal_news_score is not None
                else opportunity.news_score
            ),
            "indicator_score": float(
                prediction.signal_indicator_score
                if prediction.signal_indicator_score is not None
                else opportunity.indicator_score
            ),
            "combined_score": float(prediction.confidence_score),
            "entry_price": float(prediction.entry_price),
            "exit_price": float(prediction.exit_price),
            "settled_price_at": prediction.due_at,
            "raw_return_bps": float(prediction.raw_return_bps or 0),
            "gross_directional_return_bps": gross_return,
            "estimated_cost_bps": estimated_cost,
            "fee_cost_bps": float(cost_breakdown["fee_cost_bps"]),
            "slippage_cost_bps": float(cost_breakdown["slippage_cost_bps"]),
            "funding_cost_bps": float(cost_breakdown["funding_cost_bps"]),
            "recorded_estimated_cost_bps": float(prediction.estimated_cost_bps or 0),
            "directional_return_bps": net_return,
            "net_directional_return_bps": net_return,
            "gross_result": prediction.result,
            "result": net_result,
            "max_favorable_bps": (
                float(prediction.max_favorable_bps)
                if prediction.max_favorable_bps is not None
                else None
            ),
            "max_adverse_bps": (
                float(prediction.max_adverse_bps)
                if prediction.max_adverse_bps is not None
                else None
            ),
            "settlement_version": prediction.settlement_version,
            "readiness_status": prediction.readiness_status,
            "calibration_sample_count": int(prediction.calibration_sample_count),
            "expected_gross_edge_bps": (
                float(prediction.expected_gross_edge_bps)
                if prediction.expected_gross_edge_bps is not None
                else None
            ),
            "expected_edge_lower_bound_bps": (
                float(prediction.expected_edge_lower_bound_bps)
                if prediction.expected_edge_lower_bound_bps is not None
                else None
            ),
            "signal_time": prediction.predicted_at,
            "expires_at": prediction.due_at,
            }
        )
    status_rows = db.execute(
        select(AiMonitorPrediction.status, func.count(AiMonitorPrediction.id))
        .join(
            AiMonitorOpportunity,
            AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
        )
        .where(*conditions)
        .group_by(AiMonitorPrediction.status)
    ).all()
    status_counts = {str(status): int(count) for status, count in status_rows}
    summary = summarize_historical_opportunities(outcomes)
    summary["discarded_unavailable_count"] = status_counts.get("unavailable", 0)
    summary["pending_count"] = status_counts.get("pending", 0)
    summary["total_prediction_count"] = sum(status_counts.values())
    return {
        "summary": summary,
        "items": outcomes,
        "readiness": strategy_readiness_report(db, user_id, current_config),
        "cost_config": {
            **active_cost_settings,
            "example_one_hour_total_bps": prediction_estimated_cost_bps(
                datetime(2026, 1, 1),
                datetime(2026, 1, 1) + timedelta(hours=1),
                current_config,
            ),
        },
        "filters": {
            "news_score_min": float(news_score_min),
            "indicator_score_min": float(indicator_score_min),
            "direction": direction,
        },
        "note": "直接统计已经完成结算的虚拟预测；命中率和净收益按右侧当前启用的手续费、滑点与资金成本动态重算，不会执行任何交易。",
    }


def settle_due_predictions(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Settle due predictions, retrying delayed market data before giving up."""

    now = utcnow()
    retry_before = now - timedelta(minutes=PREDICTION_SETTLEMENT_RETRY_MINUTES)
    backfill_since = now - timedelta(days=PREDICTION_SETTLEMENT_BACKFILL_DAYS)
    grace_cutoff = now - timedelta(hours=PREDICTION_SETTLEMENT_GRACE_HOURS)
    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.due_at <= now,
            AiMonitorPrediction.updated_at <= retry_before,
            or_(
                AiMonitorPrediction.status == "pending",
                (
                    (AiMonitorPrediction.status == "unavailable")
                    & (AiMonitorPrediction.due_at >= backfill_since)
                ),
            ),
        )
        .order_by(AiMonitorPrediction.due_at, AiMonitorPrediction.id)
        .limit(500)
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    items = db.scalars(statement).all()
    window_ms = 45 * 60 * 1_000
    grouped: dict[str, list[AiMonitorPrediction]] = {}
    for item in items:
        grouped.setdefault(item.contract_symbol, []).append(item)
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        targets = [_datetime_ms(item.due_at) for item in symbol_items]
        starts = [
            _datetime_ms(
                getattr(
                    item,
                    "predicted_at",
                    item.due_at
                    - timedelta(
                        seconds=_TIMEFRAME_SECONDS.get(
                            getattr(item, "timeframe", "15m"), 900
                        )
                    ),
                )
            )
            for item in symbol_items
        ]
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(starts) - window_ms,
                max(targets) + window_ms,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []
    completed = 0
    recovered = 0
    deferred = 0
    unavailable = 0
    for item in items:
        previous_status = item.status
        settlement = historical_settlement_price(
            candles_by_symbol.get(item.contract_symbol, []),
            _datetime_ms(item.due_at),
        )
        exit_price = float(settlement["price"]) if settlement is not None else 0.0
        entry_price = float(item.entry_price or 0)
        if entry_price <= 0 or exit_price <= 0:
            item.updated_at = now
            if entry_price > 0 and item.due_at > grace_cutoff:
                item.status = "pending"
                item.completed_at = None
                deferred += 1
            else:
                item.status = "unavailable"
                item.completed_at = now
                unavailable += 1
            continue
        outcome = prediction_outcome(entry_price, exit_price, item.direction)
        predicted_at = getattr(
            item,
            "predicted_at",
            item.due_at
            - timedelta(
                seconds=_TIMEFRAME_SECONDS.get(getattr(item, "timeframe", "15m"), 900)
            ),
        )
        stored_estimated_cost = getattr(item, "estimated_cost_bps", None)
        estimated_cost = (
            float(stored_estimated_cost)
            if stored_estimated_cost is not None
            else prediction_estimated_cost_bps(predicted_at, item.due_at)
        )
        net_outcome = prediction_net_outcome(
            float(outcome["directional_return_bps"]), estimated_cost
        )
        path_metrics = prediction_path_metrics(
            candles_by_symbol.get(item.contract_symbol, []),
            entry_price,
            item.direction,
            _datetime_ms(predicted_at),
            _datetime_ms(item.due_at),
        )
        item.status = "completed"
        item.result = str(outcome["result"])
        item.exit_price = Decimal(str(exit_price))
        item.raw_return_bps = Decimal(str(outcome["raw_return_bps"]))
        item.directional_return_bps = Decimal(str(outcome["directional_return_bps"]))
        item.estimated_cost_bps = Decimal(str(net_outcome["estimated_cost_bps"]))
        item.net_directional_return_bps = Decimal(
            str(net_outcome["net_directional_return_bps"])
        )
        item.net_result = str(net_outcome["net_result"])
        item.max_favorable_bps = (
            Decimal(str(path_metrics["max_favorable_bps"]))
            if path_metrics["max_favorable_bps"] is not None
            else None
        )
        item.max_adverse_bps = (
            Decimal(str(path_metrics["max_adverse_bps"]))
            if path_metrics["max_adverse_bps"] is not None
            else None
        )
        item.settlement_version = PREDICTION_SETTLEMENT_VERSION
        item.completed_at = now
        item.updated_at = now
        completed += 1
        if previous_status == "unavailable":
            recovered += 1
    db.flush()
    return {
        "completed": completed,
        "recovered": recovered,
        "deferred": deferred,
        "unavailable": unavailable,
    }


def backfill_prediction_path_metrics(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Backfill MFE/MAE for recently completed predictions created before path tracking."""

    now = utcnow()
    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.entry_price.is_not(None),
            AiMonitorPrediction.due_at >= now
            - timedelta(days=PREDICTION_SETTLEMENT_BACKFILL_DAYS),
            or_(
                AiMonitorPrediction.max_favorable_bps.is_(None),
                AiMonitorPrediction.max_adverse_bps.is_(None),
            ),
        )
        .order_by(AiMonitorPrediction.predicted_at, AiMonitorPrediction.id)
        .limit(500)
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    items = db.scalars(statement).all()
    grouped: dict[str, list[AiMonitorPrediction]] = {}
    for item in items:
        grouped.setdefault(item.contract_symbol, []).append(item)
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(_datetime_ms(item.predicted_at) for item in symbol_items) - 15 * 60 * 1_000,
                max(_datetime_ms(item.due_at) for item in symbol_items) + 15 * 60 * 1_000,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []
    completed = 0
    unavailable = 0
    for item in items:
        path_metrics = prediction_path_metrics(
            candles_by_symbol.get(item.contract_symbol, []),
            float(item.entry_price or 0),
            item.direction,
            _datetime_ms(item.predicted_at),
            _datetime_ms(item.due_at),
        )
        if path_metrics["max_favorable_bps"] is None:
            unavailable += 1
            continue
        item.max_favorable_bps = Decimal(str(path_metrics["max_favorable_bps"]))
        item.max_adverse_bps = Decimal(str(path_metrics["max_adverse_bps"]))
        item.settlement_version = PREDICTION_SETTLEMENT_VERSION
        item.updated_at = now
        completed += 1
    db.flush()
    return {"scanned": len(items), "completed": completed, "unavailable": unavailable}


def aggregate_news_candidates(
    news_rows: Sequence[Any],
    symbol_map: Mapping[str, str],
    *,
    minimum_confidence: float,
    minimum_mentions: int,
) -> list[dict[str, Any]]:
    """Build direction-aware US-stock candidates, including bullish and bearish news."""

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in news_rows:
        confidence = float(_row_value(row, "ai_confidence", 0) or 0)
        if confidence <= 0:
            continue
        for related in _json_list(_row_value(row, "related_us_stocks")):
            if not isinstance(related, Mapping):
                continue
            raw_direction = str(related.get("direction") or "").strip().lower()
            if raw_direction in {"bull", "bullish", "long"}:
                direction = "long"
            elif raw_direction in {"bear", "bearish", "short"}:
                direction = "short"
            else:
                continue
            raw_symbol = str(related.get("symbol") or "").strip().upper()
            normalized = raw_symbol.removesuffix("USDT").removesuffix("USD1")
            if not normalized:
                continue
            contract = symbol_map.get(normalized, "")
            try:
                relevance = min(1.0, max(0.0, float(related.get("relevance") or 0)))
            except (TypeError, ValueError):
                continue
            score = confidence * relevance
            if score < minimum_confidence:
                continue
            candidate = candidates.setdefault(
                (normalized, direction),
                {
                    "symbol": normalized,
                    "contract_symbol": contract,
                    "market_available": bool(contract),
                    "direction": direction,
                    "scores": [],
                    "news": [],
                },
            )
            candidate["scores"].append(score)
            candidate["news"].append(
                {
                    "id": str(_row_value(row, "id") or ""),
                    "ts": int(_row_value(row, "ts", 0) or 0),
                    "source": _row_value(row, "source"),
                    "title": _row_value(row, "title_zh") or _row_value(row, "title") or "",
                    "reason": _row_value(row, "ai_reason"),
                    "confidence": round(confidence, 4),
                    "relevance": round(relevance, 4),
                    "score": round(score, 4),
                    "direction": direction,
                }
            )
    result: list[dict[str, Any]] = []
    for candidate in candidates.values():
        unique_news = {item["id"] for item in candidate["news"] if item["id"]}
        if len(unique_news) < minimum_mentions:
            continue
        scores = candidate.pop("scores")
        candidate["news_score"] = round(sum(scores) / len(scores) * 100, 4)
        candidate["news"].sort(key=lambda item: (item["score"], item["ts"]), reverse=True)
        result.append(candidate)
    return sorted(result, key=lambda item: item["news_score"], reverse=True)


def strongest_candidate_per_symbol(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one directional signal per instrument, choosing its strongest news side."""

    strongest: dict[str, dict[str, Any]] = {}
    strengths: dict[str, tuple[float, int, int]] = {}
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        key = str(candidate.get("contract_symbol") or candidate.get("symbol") or "").upper()
        if not key:
            continue
        news = list(candidate.get("news") or [])
        latest_news = max((int(item.get("ts") or 0) for item in news), default=0)
        strength = (
            float(candidate.get("news_score") or 0),
            len(news),
            latest_news,
        )
        if key not in strengths or strength > strengths[key]:
            strongest[key] = candidate
            strengths[key] = strength
    return sorted(
        strongest.values(),
        key=lambda item: float(item.get("news_score") or 0),
        reverse=True,
    )


def match_configured_indicators(
    scan: Mapping[str, Any], indicator_keys: Sequence[str], direction: str = "long"
) -> tuple[bool, list[dict[str, Any]]]:
    """Apply the all-selected policy using conditions aligned with opportunity direction."""

    expected_direction = "bearish" if direction == "short" else "bullish"

    items = [
        *list(scan.get("items") or []),
        *list((scan.get("prediction_features") or {}).get("items") or []),
    ]
    by_key = {str(item.get("key") or ""): item for item in items if isinstance(item, Mapping)}
    evidence: list[dict[str, Any]] = []
    for key in indicator_keys:
        item = by_key.get(key)
        matched = bool(
            item
            and (
                item.get("direction") == expected_direction
                if key.startswith("prediction_")
                else (
                    item.get("bearish_triggered") is True
                    if direction == "short"
                    else item.get("bullish_triggered", item.get("triggered")) is True
                )
            )
        )
        bearish_strategy = bool(item and direction == "short" and not key.startswith("prediction_"))
        strength_key = "bearish_strength" if direction == "short" else "bullish_strength"
        raw_strength = item.get(strength_key) if item else None
        strength = (
            float(raw_strength)
            if raw_strength is not None
            else 100.0
            if matched
            else 0.0
        )
        evidence.append(
            {
                "key": key,
                "name": (item.get("bearish_name") if bearish_strategy else item.get("name"))
                if item
                else key,
                "matched": matched,
                "strength": round(max(0.0, min(100.0, strength)), 4),
                "direction": direction,
                "status": (item.get("bearish_status") if bearish_strategy else item.get("status"))
                if item
                else "unavailable",
                "summary": (
                    item.get("bearish_summary") if bearish_strategy else item.get("summary")
                )
                if item
                else "指标数据不可用",
                "metrics": list(item.get("metrics") or []) if item else [],
            }
        )
    return bool(evidence) and all(item["matched"] for item in evidence), evidence


def create_run(db: Session, user_id: int, run_type: str) -> AiMonitorRun:
    if run_type not in {"news", "opportunity"}:
        raise AiMonitorError("不支持的 AI 监控任务类型")
    active = db.scalar(
        select(AiMonitorRun.id).where(
            AiMonitorRun.user_id == user_id,
            AiMonitorRun.run_type == run_type,
            AiMonitorRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active is not None:
        raise AiMonitorError("同类型任务正在运行，请稍后再试")

    now = utcnow()
    batch_id: str | None = None
    config = db.get(AiMonitorConfig, user_id)
    if run_type == "news":
        model = db.scalar(
            select(AiModelConfig.id).where(
                AiModelConfig.user_id == user_id,
                AiModelConfig.is_enabled.is_(True),
                AiModelConfig.is_default.is_(True),
            )
        )
        if model is None:
            raise AiMonitorError("请先在系统设置中配置并启用默认 AI 模型")
        lookback_hours = int(config.news_lookback_hours) if config is not None else 24
        minimum_news_ts = int(
            (datetime.now(UTC) - timedelta(hours=lookback_hours)).timestamp()
        )
        unanalyzed = int(
            db.scalar(
                select(func.count())
                .select_from(News)
                .where(News.ai_analyzed_at.is_(None), News.ts >= minimum_news_ts)
            )
            or 0
        )
        if unanalyzed <= 0:
            raise AiMonitorError("当前新闻回看范围内没有待 AI 分析的新新闻")
        batch_id = str(uuid.uuid4())
        db.add(
            NewsAiBatch(
                id=batch_id,
                started_by=user_id,
                status="pending",
                requested_count=NEWS_BATCH_SIZE,
                chunk_size=CHUNK_SIZE,
            )
        )
        # There is no ORM relationship between the batch and monitor run, so
        # explicitly persist the parent before inserting the foreign-key child.
        db.flush()

    run = AiMonitorRun(
        public_id=str(uuid.uuid4()),
        user_id=user_id,
        run_type=run_type,
        status="pending",
        news_batch_id=batch_id,
    )
    db.add(run)
    if config is not None:
        if run_type == "news":
            config.last_news_run_at = now
        else:
            config.last_opportunity_run_at = now
    db.flush()
    return run


def create_single_news_run(db: Session, user_id: int, news_id: str) -> AiMonitorRun:
    """Create a tenant-scoped run that analyzes exactly one selected news record."""

    return create_targeted_news_run(db, user_id, [news_id], trigger="manual")


def create_targeted_news_run(
    db: Session,
    user_id: int,
    news_ids: Sequence[str],
    *,
    trigger: str,
) -> AiMonitorRun:
    """Create one run for a committed, explicitly selected set of news rows."""

    selected_news_ids = list(
        dict.fromkeys(str(item).strip() for item in news_ids if str(item).strip())
    )
    if not selected_news_ids:
        raise AiMonitorError("新闻不存在或已被删除")
    selected_news_ids = selected_news_ids[:NEWS_BATCH_SIZE]

    if trigger != "collector":
        active = db.scalar(
            select(AiMonitorRun.id).where(
                AiMonitorRun.user_id == user_id,
                AiMonitorRun.run_type == "news",
                AiMonitorRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if active is not None:
            raise AiMonitorError("同类型任务正在运行，请稍后再试")
    existing_news_count = int(
        db.scalar(
            select(func.count()).select_from(News).where(News.id.in_(selected_news_ids))
        )
        or 0
    )
    if existing_news_count != len(selected_news_ids):
        raise AiMonitorError("新闻不存在或已被删除")
    model = db.scalar(
        select(AiModelConfig.id).where(
            AiModelConfig.user_id == user_id,
            AiModelConfig.is_enabled.is_(True),
            AiModelConfig.is_default.is_(True),
        )
    )
    if model is None:
        raise AiMonitorError("请先在系统设置中配置并启用默认 AI 模型")

    now = utcnow()
    batch = NewsAiBatch(
        id=str(uuid.uuid4()),
        started_by=user_id,
        status="pending",
        requested_count=NEWS_BATCH_SIZE,
        chunk_size=CHUNK_SIZE,
    )
    db.add(batch)
    db.flush()
    run = AiMonitorRun(
        public_id=str(uuid.uuid4()),
        user_id=user_id,
        run_type="news",
        status="pending",
        news_batch_id=batch.id,
        summary_json={"trigger": trigger, "news_ids": selected_news_ids},
    )
    db.add(run)
    config = db.get(AiMonitorConfig, user_id)
    if config is not None and trigger != "collector":
        config.last_news_run_at = now
    db.flush()
    return run


def execute_news_run(
    engine: Engine,
    run_public_id: str,
    master_key: str,
    news_ids: Sequence[str] | None = None,
    symbols_config: Path | None = None,
    trigger_opportunity: bool = False,
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    batch_id: str | None = None
    user_id: int | None = None
    processed_count = 0
    try:
        with factory() as db:
            run = db.scalar(select(AiMonitorRun).where(AiMonitorRun.public_id == run_public_id))
            if run is None or run.status not in ACTIVE_RUN_STATUSES:
                return
            run.status = "running"
            run.started_at = utcnow()
            batch_id = run.news_batch_id
            user_id = run.user_id
            config = db.get(AiMonitorConfig, run.user_id)
            lookback_hours = int(config.news_lookback_hours) if config is not None else 24
            minimum_news_ts = int(
                (datetime.now(UTC) - timedelta(hours=lookback_hours)).timestamp()
            )
            db.commit()
        if batch_id is None:
            raise AiMonitorError("新闻分析批次不存在")
        run_news_ai_batch(
            engine,
            batch_id,
            master_key,
            only_unanalyzed=not news_ids,
            news_ids=news_ids,
            minimum_news_ts=None if news_ids else minimum_news_ts,
            generate_model_summary=False,
        )
        with factory() as db:
            run = db.scalar(select(AiMonitorRun).where(AiMonitorRun.public_id == run_public_id))
            batch = db.get(NewsAiBatch, batch_id)
            if run is None or batch is None:
                return
            run.input_count = int(batch.selected_count)
            run.matched_count = int(batch.processed_count)
            processed_count = int(batch.processed_count)
            run.status = (
                batch.status if batch.status in {"completed", "partial", "failed"} else "failed"
            )
            run.error_message = batch.error_message
            run.summary_json = {
                **dict(run.summary_json or {}),
                "market_sentiment": batch.market_sentiment,
                "market_confidence": (
                    float(batch.market_confidence) if batch.market_confidence is not None else None
                ),
                "market_summary": batch.market_summary,
                "result": batch.result_json or {},
                "failed_count": int(batch.failed_count),
                "model_name": batch.model_name,
            }
            run.completed_at = utcnow()
            db.commit()
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"[ai-monitor] news run internal error: {error_type}")
        _fail_run(factory, run_public_id, f"AI 新闻分析执行失败（{error_type}）")
        return
    if not trigger_opportunity or processed_count <= 0 or user_id is None or symbols_config is None:
        return
    try:
        with factory() as db:
            opportunity_run = create_run(db, user_id, "opportunity")
            opportunity_run_public_id = opportunity_run.public_id
            db.commit()
        execute_opportunity_run(engine, opportunity_run_public_id, symbols_config)
    except (AiMonitorError, IntegrityError):
        return


def _scan_opportunities(
    db: Session,
    engine: Engine,
    run: AiMonitorRun,
    config: Mapping[str, Any],
    symbols_config: Path,
) -> dict[str, Any]:
    now = utcnow()
    db.execute(
        update(AiMonitorOpportunity)
        .where(
            AiMonitorOpportunity.user_id == run.user_id,
            AiMonitorOpportunity.status.in_(("candidate", "discovered")),
            AiMonitorOpportunity.expires_at <= now,
        )
        .values(status="expired", updated_at=now)
    )
    cutoff = int(
        (datetime.now(UTC) - timedelta(hours=int(config["news_lookback_hours"]))).timestamp()
    )
    news_rows = db.scalars(
        select(News)
        .where(
            News.ts >= cutoff,
            News.ai_analyzed_at.is_not(None),
            News.related_us_stocks.is_not(None),
        )
        .order_by(News.ts.desc(), News.id.desc())
        .limit(1000)
    ).all()
    model_call_audit = _news_model_call_audit_index(db, run.user_id, news_rows)
    repository = MonitorRepository(engine, symbols_config)
    symbol_map = contract_symbol_map(repository)
    supported_contracts = sorted(set(symbol_map.values()))
    if supported_contracts:
        db.execute(
            update(AiMonitorOpportunity)
            .where(
                AiMonitorOpportunity.user_id == run.user_id,
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.contract_symbol.not_in(supported_contracts),
            )
            .values(status="expired", expires_at=now, updated_at=now)
        )
    settlement = settle_due_predictions(db, repository, user_id=run.user_id)
    path_backfill = backfill_prediction_path_metrics(db, repository, user_id=run.user_id)
    directional_candidates = aggregate_news_candidates(
        news_rows,
        symbol_map,
        minimum_confidence=float(config["minimum_news_confidence"]),
        minimum_mentions=int(config["minimum_news_mentions"]),
    )
    all_candidates = strongest_candidate_per_symbol(directional_candidates)
    unmapped_candidates = [item for item in all_candidates if not item.get("contract_symbol")]
    monitor_symbols = list(config.get("monitor_symbols") or [])
    candidates = filter_monitored_candidates(all_candidates, monitor_symbols)
    run.input_count = len(candidates)
    stored = 0
    confirmed = 0
    shadow_ready = 0
    awaiting_indicators = 0
    predictions_created = 0
    duplicated = 0
    merged = 0
    failed_symbols: list[str] = []
    unmapped_symbols: list[str] = [str(item["symbol"]) for item in unmapped_candidates]
    indicator_keys = list(config["indicator_keys"])
    timeframe = str(config["timeframe"])
    minimum_indicator_score = float(config["minimum_indicator_score"])
    minimum_combined_score = float(config["minimum_combined_score"])
    minimum_calibration_samples = int(config["minimum_calibration_samples"])
    cost_settings = prediction_cost_settings(config)
    calibrations = {
        direction: historical_edge_calibration(
            db,
            run.user_id,
            direction,
            minimum_indicator_score=minimum_indicator_score,
            minimum_combined_score=minimum_combined_score,
            minimum_samples=minimum_calibration_samples,
        )
        for direction in ("long", "short")
    }
    requires_prediction_features = any(
        key.startswith("prediction_") for key in indicator_keys
    )
    market_flow_inputs = _market_flow_input_maps(db, repository)
    for candidate in candidates:
        contract_symbol = str(candidate.get("contract_symbol") or "")
        if not contract_symbol:
            unmapped_symbols.append(candidate["symbol"])
            scan = {"items": [], "prediction_features": {"items": []}, "evaluated_at": 0}
        else:
            try:
                scan = repository.strategy_indicators(contract_symbol, timeframe)
            except MonitorUnavailable:
                failed_symbols.append(candidate["symbol"])
                scan = {"items": [], "prediction_features": {"items": []}, "evaluated_at": 0}
        matched, indicator_evidence = match_configured_indicators(
            scan, indicator_keys, candidate["direction"]
        )
        matched_indicator_keys = [
            str(item["key"]) for item in indicator_evidence if item["matched"]
        ]
        indicator_score = (
            round(
                sum(float(item["strength"]) for item in indicator_evidence)
                / len(indicator_evidence),
                4,
            )
            if indicator_keys
            else 0.0
        )
        news_ids = sorted({item["id"] for item in candidate["news"] if item["id"]})
        evaluated_at = int(scan.get("evaluated_at") or 0)
        fingerprint = "|".join(
            [
                str(run.user_id),
                contract_symbol or candidate["symbol"],
                candidate["direction"],
                timeframe,
                str(evaluated_at),
                ",".join(indicator_keys),
                ",".join(news_ids),
            ]
        )
        dedup_key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        active_for_symbol = db.scalars(
            select(AiMonitorOpportunity)
            .where(
                AiMonitorOpportunity.user_id == run.user_id,
                AiMonitorOpportunity.symbol == candidate["symbol"],
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at > now,
            )
            .order_by(
                AiMonitorOpportunity.updated_at.desc(),
                AiMonitorOpportunity.id.desc(),
            )
        ).all()
        existing = next(
            (item for item in active_for_symbol if item.direction == candidate["direction"]),
            None,
        )
        if active_for_symbol and existing is None:
            for previous in active_for_symbol:
                previous.status = "expired"
                previous.expires_at = min(previous.expires_at, now)
                previous.updated_at = now
        if existing is None and db.scalar(
            select(AiMonitorOpportunity.id).where(AiMonitorOpportunity.dedup_key == dedup_key)
        ):
            duplicated += 1
            continue
        if existing is None:
            audit_rows = [model_call_audit[item] for item in news_ids if item in model_call_audit]
            frozen_batch_ids = sorted({str(item["batch_id"]) for item in audit_rows})
            frozen_model_call_ids = sorted(
                {
                    int(call_id)
                    for item in audit_rows
                    for call_id in item.get("call_ids", [])
                }
            )
        else:
            frozen_batch_ids = list(existing.news_ai_batch_ids_json or [])
            frozen_model_call_ids = [
                int(item) for item in (existing.news_ai_model_call_ids_json or [])
            ]
        market = dict(
            market_flow_inputs.get("ticker", {}).get(contract_symbol.upper(), {})
        )
        entry_price = float(market.get("price") or 0)
        if market.get("price") is not None:
            market["price"] = entry_price
        market_flow = market_flow_snapshot(
            market_flow_inputs,
            symbol=str(candidate["symbol"]),
            contract_symbol=contract_symbol,
            direction=str(candidate["direction"]),
            now=now,
        )
        flow_score = float(market_flow["score"])
        signal_confirmed = matched and not bool(market_flow["hard_conflict"])
        score_weights = opportunity_score_weights(config)
        combined_score = weighted_opportunity_score(
            candidate["news_score"], indicator_score, flow_score, config
        )
        expires_at = now + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe] * 2)
        due_at = now + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
        cost_breakdown = prediction_cost_breakdown(now, due_at, config)
        estimated_cost = float(cost_breakdown["total_cost_bps"])
        market_quality = signal_market_quality(
            scan,
            market,
            timeframe,
            now,
            maximum_market_age_seconds=int(config["maximum_market_age_seconds"]),
            minimum_feature_quality=float(config["minimum_feature_quality"]),
            requires_prediction_features=requires_prediction_features,
        )
        readiness = signal_readiness_snapshot(
            matched=signal_confirmed,
            indicator_score=indicator_score,
            combined_score=combined_score,
            estimated_cost_bps=estimated_cost,
            market_quality=market_quality,
            calibration=calibrations[candidate["direction"]],
            minimum_indicator_score=minimum_indicator_score,
            minimum_combined_score=minimum_combined_score,
            safety_margin_bps=float(config["live_safety_margin_bps"]),
            market_flow=market_flow,
            market_flow_weight=float(score_weights["market_flow"]),
            minimum_market_flow_quality=float(
                config["minimum_market_flow_quality"]
            ),
        )
        if readiness["status"] == "shadow_ready":
            shadow_ready += 1
        evidence = {
            "match_policy": "all",
            "indicator_scoring": "continuous_directional_v2",
            "direction": candidate["direction"],
            "confirmed": signal_confirmed,
            "technical_confirmed": matched,
            "market_available": bool(contract_symbol),
            "news": candidate["news"][:8],
            "indicators": indicator_evidence,
            "matched_indicator_count": len(matched_indicator_keys),
            "required_indicator_count": len(indicator_keys),
            "evaluated_bar_time": evaluated_at,
            "market": market,
            "market_flow": market_flow,
            "score_weights": score_weights,
            "market_quality": market_quality,
            "live_readiness": readiness,
            "model_audit": {
                "scope": "opportunity_created",
                "batch_ids": frozen_batch_ids,
                "call_ids": frozen_model_call_ids,
                "available": bool(frozen_model_call_ids),
            },
        }
        if existing is None:
            opportunity = AiMonitorOpportunity(
                public_id=str(uuid.uuid4()),
                user_id=run.user_id,
                analysis_run_id=run.id,
                symbol=candidate["symbol"],
                contract_symbol=contract_symbol or candidate["symbol"],
                direction=candidate["direction"],
                status="discovered" if signal_confirmed else "candidate",
                timeframe=timeframe,
                news_score=Decimal(str(candidate["news_score"])),
                indicator_score=Decimal(str(indicator_score)),
                combined_score=Decimal(str(combined_score)),
                matched_indicator_keys_json=matched_indicator_keys,
                news_ids_json=news_ids,
                news_ai_batch_ids_json=frozen_batch_ids,
                news_ai_model_call_ids_json=frozen_model_call_ids,
                evidence_json=evidence,
                dedup_key=dedup_key,
                discovered_at=now,
                expires_at=expires_at,
            )
            db.add(opportunity)
            stored += 1
        else:
            opportunity = existing
            opportunity.analysis_run_id = run.id
            opportunity.contract_symbol = contract_symbol or candidate["symbol"]
            opportunity.status = "discovered" if signal_confirmed else "candidate"
            opportunity.news_score = Decimal(str(candidate["news_score"]))
            opportunity.indicator_score = Decimal(str(indicator_score))
            opportunity.combined_score = Decimal(str(combined_score))
            opportunity.matched_indicator_keys_json = matched_indicator_keys
            opportunity.news_ids_json = news_ids
            opportunity.evidence_json = evidence
            opportunity.expires_at = max(opportunity.expires_at, expires_at)
            opportunity.updated_at = now
            merged += 1
        db.flush()
        prediction_exists = db.scalar(
            select(AiMonitorPrediction.id).where(
                AiMonitorPrediction.opportunity_id == opportunity.id
            )
        )
        if opportunity.status == "discovered":
            if prediction_exists is None:
                prediction_evidence = {
                    **evidence,
                    "signal_scores": {
                        "news": candidate["news_score"],
                        "indicator": indicator_score,
                        "combined": combined_score,
                    },
                    "cost_model": {
                        "version": PREDICTION_SETTLEMENT_VERSION,
                        **cost_settings,
                        "fee_cost_bps": cost_breakdown["fee_cost_bps"],
                        "slippage_cost_bps": cost_breakdown["slippage_cost_bps"],
                        "funding_cost_bps": cost_breakdown["funding_cost_bps"],
                        "estimated_total_bps": estimated_cost,
                    },
                }
                db.add(
                    AiMonitorPrediction(
                        public_id=str(uuid.uuid4()),
                        user_id=run.user_id,
                        opportunity_id=opportunity.id,
                        symbol=candidate["symbol"],
                        contract_symbol=contract_symbol,
                        direction=candidate["direction"],
                        timeframe=timeframe,
                        status="pending" if entry_price > 0 else "unavailable",
                        confidence_score=Decimal(str(combined_score)),
                        signal_news_score=Decimal(str(candidate["news_score"])),
                        signal_indicator_score=Decimal(str(indicator_score)),
                        estimated_cost_bps=Decimal(str(estimated_cost)),
                        settlement_version=PREDICTION_SETTLEMENT_VERSION,
                        readiness_status=str(readiness["status"]),
                        calibration_sample_count=int(
                            readiness["calibration"]["sample_count"]
                        ),
                        expected_gross_edge_bps=(
                            Decimal(
                                str(readiness["calibration"]["mean_gross_edge_bps"])
                            )
                            if readiness["calibration"]["mean_gross_edge_bps"] is not None
                            else None
                        ),
                        expected_edge_lower_bound_bps=(
                            Decimal(str(readiness["calibration"]["lower_bound_bps"]))
                            if readiness["calibration"]["lower_bound_bps"] is not None
                            else None
                        ),
                        entry_price=Decimal(str(entry_price)) if entry_price > 0 else None,
                        evidence_json=prediction_evidence,
                        predicted_at=now,
                        due_at=due_at,
                        completed_at=now if entry_price <= 0 else None,
                    )
                )
                predictions_created += 1
            confirmed += 1
        else:
            awaiting_indicators += 1
    run.matched_count = stored + merged
    return {
        "candidate_count": len(candidates),
        "long_candidate_count": sum(item["direction"] == "long" for item in candidates),
        "short_candidate_count": sum(item["direction"] == "short" for item in candidates),
        "news_candidate_count": len(all_candidates),
        "directional_candidate_count": len(directional_candidates),
        "stored_count": stored,
        "merged_count": merged,
        "discovered_count": confirmed,
        "shadow_ready_count": shadow_ready,
        "awaiting_indicator_count": awaiting_indicators,
        "prediction_count": predictions_created,
        "duplicate_count": duplicated,
        "failed_symbols": failed_symbols[:20],
        "unmapped_symbols": unmapped_symbols[:50],
        "settled_predictions": settlement,
        "path_metrics_backfill": path_backfill,
        "monitor_symbols": monitor_symbols,
        "monitor_scope": "selected" if monitor_symbols else "all",
        "indicator_keys": indicator_keys,
        "match_policy": "all",
        "indicator_scoring": "continuous_directional_v2",
        "minimum_indicator_score": minimum_indicator_score,
        "minimum_combined_score": minimum_combined_score,
        "timeframe": timeframe,
        "news_lookback_hours": int(config["news_lookback_hours"]),
    }


def execute_opportunity_run(
    engine: Engine,
    run_public_id: str,
    symbols_config: Path,
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with factory() as db:
            run = db.scalar(select(AiMonitorRun).where(AiMonitorRun.public_id == run_public_id))
            if run is None or run.status not in ACTIVE_RUN_STATUSES:
                return
            run.status = "running"
            run.started_at = utcnow()
            config = config_data(db.get(AiMonitorConfig, run.user_id))
            summary = _scan_opportunities(db, engine, run, config, symbols_config)
            run.summary_json = summary
            run.status = "partial" if summary["failed_symbols"] else "completed"
            run.completed_at = utcnow()
            db.commit()
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"[ai-monitor] opportunity run internal error: {error_type}")
        _fail_run(factory, run_public_id, f"机会扫描执行失败（{error_type}）")


def _fail_run(factory: sessionmaker[Session], run_public_id: str, message: str) -> None:
    try:
        with factory() as db:
            run = db.scalar(select(AiMonitorRun).where(AiMonitorRun.public_id == run_public_id))
            if run is None:
                return
            run.status = "failed"
            run.error_message = message
            run.completed_at = utcnow()
            db.commit()
    except Exception:
        return


def start(engine: Engine, master_key: str, symbols_config: Path) -> None:
    """Start one daemon scheduler for all persisted, enabled user configurations."""

    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(
        target=_worker_loop,
        args=(engine, master_key, symbols_config),
        daemon=True,
        name="ai-monitor",
    ).start()
    threading.Thread(
        target=_ingest_worker_loop,
        args=(engine, master_key),
        daemon=True,
        name="ai-news-immediate",
    ).start()


def recover_stale_runs(db: Session) -> dict[str, int]:
    """Release abandoned background jobs without waiting for another service restart."""

    now = utcnow()
    stale_cutoff = now - timedelta(seconds=RUN_STALE_SECONDS)
    stale_batch_ids = list(
        db.scalars(
            select(NewsAiBatch.id).where(
                NewsAiBatch.status.in_(ACTIVE_RUN_STATUSES),
                NewsAiBatch.updated_at < stale_cutoff,
            )
        ).all()
    )
    batch_result = db.execute(
        update(NewsAiBatch)
        .where(
            NewsAiBatch.status.in_(ACTIVE_RUN_STATUSES),
            NewsAiBatch.updated_at < stale_cutoff,
        )
        .values(
            status="failed",
            error_message="新闻分析任务超过运行时限，系统已自动释放",
            completed_at=now,
            updated_at=now,
        )
    )
    run_result = db.execute(
        update(AiMonitorRun)
        .where(
            AiMonitorRun.status.in_(ACTIVE_RUN_STATUSES),
            AiMonitorRun.updated_at < stale_cutoff,
        )
        .values(
            status="failed",
            error_message="后台任务超过运行时限，系统已自动释放",
            completed_at=now,
            updated_at=now,
        )
    )
    if stale_batch_ids:
        db.execute(
            update(News)
            .where(News.ai_claim_batch_id.in_(stale_batch_ids))
            .values(ai_claim_batch_id=None, ai_claimed_at=None)
        )
    return {
        "batches": int(batch_result.rowcount or 0),
        "runs": int(run_result.rowcount or 0),
    }


def _worker_loop(engine: Engine, master_key: str, symbols_config: Path) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    repository = MonitorRepository(engine, symbols_config)
    while True:
        try:
            with factory() as db:
                recover_stale_runs(db)
                settle_due_predictions(db, repository)
                db.commit()
                user_ids = list(
                    db.scalars(
                        select(AiMonitorConfig.user_id)
                        .where(AiMonitorConfig.enabled.is_(True))
                        .order_by(AiMonitorConfig.user_id)
                    ).all()
                )
            for user_id in user_ids:
                _run_due_user(factory, engine, master_key, symbols_config, user_id)
        except Exception as exc:
            print(f"[ai-monitor] scheduler error: {type(exc).__name__}")
        time.sleep(20)


def _ingest_worker_loop(engine: Engine, master_key: str) -> None:
    """Run collector-triggered model calls independently from backlog scans."""

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    while True:
        _worker_wakeup.wait()
        _worker_wakeup.clear()
        try:
            while _run_ingested_news(factory, engine, master_key):
                pass
        except Exception as exc:
            print(f"[ai-monitor] immediate news worker error: {type(exc).__name__}")


def _run_ingested_news(
    factory: sessionmaker[Session],
    engine: Engine,
    master_key: str,
) -> bool:
    """Immediately analyze one deduplicated batch emitted by the collector."""

    news_ids = _take_ingested_news()
    if not news_ids:
        return False
    with factory() as db:
        pending_ids = list(
            db.scalars(
                select(News.id)
                .where(
                    News.id.in_(news_ids),
                    News.ai_analyzed_at.is_(None),
                )
                .order_by(News.ts.asc(), News.id.asc())
            ).all()
        )
        if not pending_ids:
            return False
        user_id = db.scalar(
            select(AiMonitorConfig.user_id)
            .join(
                AiModelConfig,
                AiModelConfig.user_id == AiMonitorConfig.user_id,
            )
            .where(
                AiMonitorConfig.enabled.is_(True),
                AiModelConfig.is_enabled.is_(True),
                AiModelConfig.is_default.is_(True),
            )
            .order_by(AiMonitorConfig.user_id)
            .limit(1)
        )
        if user_id is None:
            return False
        try:
            run = create_targeted_news_run(
                db,
                int(user_id),
                pending_ids,
                trigger="collector",
            )
            run_public_id = run.public_id
            db.commit()
        except (AiMonitorError, IntegrityError):
            db.rollback()
            _requeue_ingested_news(pending_ids)
            return False
    execute_news_run(
        engine,
        run_public_id,
        master_key,
        news_ids=pending_ids,
    )
    return True


def _run_due_user(
    factory: sessionmaker[Session],
    engine: Engine,
    master_key: str,
    symbols_config: Path,
    user_id: int,
) -> None:
    now = utcnow()
    for run_type in ("news", "opportunity"):
        with factory() as db:
            config = db.get(AiMonitorConfig, user_id)
            if config is None or not config.enabled:
                continue
            interval = (
                config.news_interval_minutes
                if run_type == "news"
                else config.opportunity_interval_minutes
            )
            previous = (
                config.last_news_run_at if run_type == "news" else config.last_opportunity_run_at
            )
            if run_type == "news":
                minimum_news_ts = int(
                    (
                        datetime.now(UTC)
                        - timedelta(hours=int(config.news_lookback_hours))
                    ).timestamp()
                )
                pending = int(
                    db.scalar(
                        select(func.count())
                        .select_from(News)
                        .where(News.ai_analyzed_at.is_(None), News.ts >= minimum_news_ts)
                    )
                    or 0
                )
                if pending <= 0:
                    config.last_news_run_at = now
                    db.commit()
                    continue
                latest_news_run = db.execute(
                    select(AiMonitorRun.status, AiMonitorRun.matched_count)
                    .where(
                        AiMonitorRun.user_id == user_id,
                        AiMonitorRun.run_type == "news",
                    )
                    .order_by(AiMonitorRun.created_at.desc(), AiMonitorRun.id.desc())
                    .limit(1)
                ).first()
                catch_up_allowed = bool(
                    latest_news_run
                    and latest_news_run.status in {"completed", "partial"}
                    and int(latest_news_run.matched_count or 0) > 0
                )
                interval_delta = (
                    timedelta(seconds=NEWS_CATCH_UP_INTERVAL_SECONDS)
                    if pending >= NEWS_CATCH_UP_THRESHOLD and catch_up_allowed
                    else timedelta(minutes=interval)
                )
            else:
                interval_delta = timedelta(minutes=interval)
            if previous is not None and previous + interval_delta > now:
                continue
            try:
                run = create_run(db, user_id, run_type)
                run_public_id = run.public_id
                db.commit()
            except (AiMonitorError, IntegrityError):
                db.rollback()
                continue
        if run_type == "news":
            execute_news_run(engine, run_public_id, master_key)
        else:
            execute_opportunity_run(engine, run_public_id, symbols_config)
