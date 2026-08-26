"""Tenant-scoped AI news analysis and technical-indicator opportunity scans."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import macro_market
from .ai_model_config import global_ai_model_configured
from .ai_monitor_read_models import (
    PREDICTION_FACT_PROJECTION_VERSION,
    read_models_available,
    refresh_ai_monitor_read_models,
)
from .market_microstructure import order_book_gate_snapshot
from .models import (
    AdminSetting,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorPredictionFact,
    AiMonitorRun,
    CompanyProfile,
    FinnhubQuoteSnapshot,
    MarketRiskEvent,
    MarketStreamEvent,
    News,
    NewsAiBatch,
    NewsAiModelCall,
    NewsAiModelCallItem,
    OpportunityGateDecision,
    OpportunityMarketSnapshot,
    RealtimeMarketFeatureSnapshot,
    Security,
    utcnow,
)
from .monitor import MonitorRepository, MonitorUnavailable
from .news_ai import (
    CHUNK_SIZE,
    DEFAULT_NEWS_ANALYSIS_SYSTEM_PROMPT,
    effective_news_analysis_system_prompt,
    news_stock_relation_supported,
    run_news_ai_batch,
)
from .prediction_feature_indicators import evaluate_prediction_feature_indicators
from .strategy_indicators import BEARISH_STRATEGY_NAMES, evaluate_strategy_indicators

DEFAULT_INDICATOR_KEYS = ["moving_average_bull", "prediction_trend"]
NEWS_BATCH_SIZE = 10
ACTIVE_RUN_STATUSES = ("pending", "running")
_TIMEFRAME_SECONDS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
NEWS_CATCH_UP_THRESHOLD = NEWS_BATCH_SIZE * 3
NEWS_CATCH_UP_INTERVAL_SECONDS = 60
LIVE_NEWS_MAX_AGE_SECONDS = 24 * 60 * 60
RUN_STALE_SECONDS = 5 * 60
PREDICTION_SETTLEMENT_RETRY_MINUTES = 5
PREDICTION_SETTLEMENT_GRACE_HOURS = 6
PREDICTION_SETTLEMENT_BACKFILL_DAYS = 7
PREDICTION_SETTLEMENT_POLL_SECONDS = 20
PREDICTION_SETTLEMENT_BATCH_SIZE = 25
PREDICTION_FEE_BPS_PER_SIDE = 5.0
PREDICTION_SLIPPAGE_BPS_PER_SIDE = 3.0
PREDICTION_FUNDING_BPS_PER_8H = 1.0
PREVIOUS_PREDICTION_SETTLEMENT_VERSION = "cost_consistent_exit_v7"
PREDICTION_SETTLEMENT_VERSION = "cost_consistent_exit_v8"
COST_CONSISTENT_SETTLEMENT_VERSIONS = {
    "cost_consistent_exit_v6",
    PREVIOUS_PREDICTION_SETTLEMENT_VERSION,
    PREDICTION_SETTLEMENT_VERSION,
}
PREDICTION_PROFIT_PROTECTION_TRIGGER_BPS = 20.0
PREDICTION_PROFIT_PROTECTION_MIN_BPS = 20.0
PREDICTION_TRAILING_TRIGGER_BPS = 50.0
PREDICTION_TRAILING_GIVEBACK_BPS = 30.0
PREDICTION_RISK_UNIT_PROTECTION_VERSION = "risk_unit_profit_guard_v2"
PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R = 0.5
PREDICTION_RISK_UNIT_TRAILING_ACTIVATION_R = 1.0
PREDICTION_RISK_UNIT_TRAILING_GIVEBACK_R = 0.5
PREDICTION_RISK_UNIT_EARLY_GIVEBACK_R = 0.35
PREDICTION_RISK_UNIT_MINIMUM_PROTECTED_R = 0.0
PREDICTION_RISK_UNIT_MINIMUM_NET_PROTECTED_R = 0.25
PREDICTION_FOLLOW_THROUGH_BARS = 3
PREDICTION_FOLLOW_THROUGH_LOSS_BPS = -15.0
PREDICTION_FOLLOW_THROUGH_MAXIMUM_ADVERSE_R = 0.20
PREDICTION_FOLLOW_THROUGH_CONFIRMATION_CLOSES = 2
PREDICTION_SCORE_EXIT_BAR_MS = 15 * 60 * 1_000
PREDICTION_SCORE_EXIT_MIN_HOLD_MS = 30 * 60 * 1_000
PREDICTION_SCORE_EXIT_CONFIRMATION_BARS = 2
PREDICTION_SCORE_EXIT_POLICY_VERSION = "horizon_aligned_closed_bar_v3"
PREDICTION_SOFT_EXIT_MIN_HORIZON_FRACTION = 0.5
MARKET_FEATURE_VERSION = "uw_features_v2"
OPPORTUNITY_WEIGHTS_VERSION = "opportunity_weights_v3_six_domain"
OPPORTUNITY_DECISION_VERSION = "actionable_entry_v9"
OPPORTUNITY_API_VERSION = "ai_opportunity.v3"
MULTI_TIMEFRAME_TECHNICAL_VERSION = "directional_mtf_15m_1h_4h_v1"
MULTI_TIMEFRAME_TECHNICAL_WEIGHTS = {"15m": 0.25, "1h": 0.45, "4h": 0.30}
UNUSUAL_WHALES_SIGNAL_SETTING_KEY = "market_data:unusual_whales:v1"
UNUSUAL_WHALES_SIGNAL_POLICY_VERSION = "uw_signal_policy_v2"
FINNHUB_SIGNAL_QUOTE_MAX_AGE_SECONDS = 15 * 60


def prediction_soft_exit_policy(
    timeframe: str,
    *,
    start_ms: int,
    due_ms: int,
) -> dict[str, Any]:
    """Return the frozen, horizon-aligned policy for non-emergency exits.

    A prediction evaluated on hourly bars must not be closed by a few
    scheduler refreshes or 15-minute noise.  Soft exits therefore wait for at
    least two native bars and half of the planned horizon.  The hard stop and
    explicit risk/reward target remain independent price barriers.
    """

    native_bar_ms = max(
        PREDICTION_SCORE_EXIT_BAR_MS,
        int(_TIMEFRAME_SECONDS.get(str(timeframe), 15 * 60)) * 1_000,
    )
    planned_horizon_ms = max(native_bar_ms, int(due_ms) - int(start_ms))
    minimum_hold_ms = min(
        planned_horizon_ms,
        max(
            PREDICTION_SCORE_EXIT_MIN_HOLD_MS,
            native_bar_ms * PREDICTION_SCORE_EXIT_CONFIRMATION_BARS,
            int(
                planned_horizon_ms
                * PREDICTION_SOFT_EXIT_MIN_HORIZON_FRACTION
            ),
        ),
    )
    timeframe_label = str(timeframe or "15m").lower()
    return {
        "bar_ms": native_bar_ms,
        "minimum_hold_ms": minimum_hold_ms,
        "confirmation_bars": PREDICTION_SCORE_EXIT_CONFIRMATION_BARS,
        "confirmation_unit": f"closed_{timeframe_label}_bar",
        "planned_horizon_ms": planned_horizon_ms,
    }
DEFAULT_UNUSUAL_WHALES_THRESHOLDS: dict[str, float | int] = {
    "quote_age_regular_ms": 2_000,
    "quote_age_extended_ms": 10_000,
    "spread_hard_max_bps": 80.0,
    "source_divergence_max_bps": 35.0,
    "min_data_coverage": 0.8,
    "event_block_before_minutes": 30,
    "event_block_after_minutes": 15,
    "halt_cooldown_minutes": 15,
}
DEFAULT_UNUSUAL_WHALES_WEIGHTS: dict[str, float] = {
    "news": 0.20,
    "technical": 0.30,
    "market_context": 0.10,
    "options_flow": 0.20,
    "gex": 0.10,
    "institutional_flow": 0.10,
}
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
INDICATOR_MATCH_POLICY = "grouped_weighted_v1"
INDICATOR_GROUPS: dict[str, frozenset[str]] = {
    "trend": frozenset(
        {
            "moving_average_bull",
            "ma_golden_cross",
            "prediction_trend",
        }
    ),
    "breakout": frozenset(
        {
            "bollinger_breakout",
            "trend_breakout",
            "price_volume_rise",
            "strong_gap_open",
            "macd_golden_cross_volume",
        }
    ),
    "pullback": frozenset(
        {
            "moving_average_pullback_bounce",
            "low_volume_pullback",
        }
    ),
    "reversal": frozenset(
        {
            "new_low_reversal",
            "oversold_bounce",
            "oversold_reversal",
        }
    ),
    "market_flow": frozenset(
        {
            "prediction_aggressive_flow",
            "prediction_book_imbalance",
            "prediction_book_imbalance_5",
            "prediction_velocity",
            "prediction_flash_imbalance",
            "prediction_taker_flow",
            "prediction_price_oi_impulse",
        }
    ),
}
CORE_INDICATOR_GROUPS = frozenset({"trend", "breakout", "pullback", "reversal"})
NON_BLOCKING_INDICATOR_KEYS = frozenset(
    {
        # The current depth stream does not provide signed trade prints or a
        # one-minute trade-price series.  Keep these visible as observations,
        # but never turn their neutral fallback into a failed hard condition.
        "prediction_aggressive_flow",
        "prediction_velocity",
    }
)
_worker_lock = threading.Lock()
_worker_started = False
_worker_wakeup = threading.Event()
_ingested_news_lock = threading.Lock()
_ingested_news_ids: dict[str, None] = {}
_legacy_rescue_lock = threading.Lock()
_legacy_rescued_batches: dict[str, datetime] = {}


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


def _enqueue_failed_legacy_news(db: Session) -> int:
    """Rescue failed work emitted by another instance still using V4 thinking mode."""

    now = utcnow()
    cutoff = now - timedelta(minutes=15)
    rows = db.execute(
        select(
            NewsAiBatch.id,
            NewsAiModelCall.news_ids_json,
            NewsAiModelCall.request_json,
        )
        .join(NewsAiModelCall, NewsAiModelCall.batch_id == NewsAiBatch.id)
        .where(
            NewsAiBatch.status.in_(("failed", "partial")),
            NewsAiBatch.created_at >= cutoff,
            NewsAiModelCall.call_type == "analysis",
            NewsAiModelCall.status == "failed",
        )
        .order_by(NewsAiBatch.created_at.desc(), NewsAiModelCall.id.desc())
        .limit(100)
    ).all()
    candidates_by_batch: dict[str, list[str]] = {}
    for batch_id, news_ids, request_json in rows:
        request = dict(request_json or {})
        thinking = request.get("thinking")
        if isinstance(thinking, Mapping) and thinking.get("type") == "disabled":
            continue
        candidates_by_batch.setdefault(str(batch_id), []).extend(
            str(news_id).strip()
            for news_id in list(news_ids or [])
            if str(news_id).strip()
        )
    if not candidates_by_batch:
        return 0
    with _legacy_rescue_lock:
        expired = [
            batch_id
            for batch_id, rescued_at in _legacy_rescued_batches.items()
            if rescued_at < cutoff
        ]
        for batch_id in expired:
            _legacy_rescued_batches.pop(batch_id, None)
        fresh_batches = {
            batch_id: news_ids
            for batch_id, news_ids in candidates_by_batch.items()
            if batch_id not in _legacy_rescued_batches
        }
        for batch_id in fresh_batches:
            _legacy_rescued_batches[batch_id] = now
    candidate_ids = list(
        dict.fromkeys(
            news_id
            for news_ids in fresh_batches.values()
            for news_id in news_ids
        )
    )
    if not candidate_ids:
        return 0
    pending_ids = list(
        db.scalars(
            select(News.id)
            .where(
                News.id.in_(candidate_ids),
                News.ai_analyzed_at.is_(None),
                News.ts >= int(time.time()) - LIVE_NEWS_MAX_AGE_SECONDS,
            )
            .order_by(News.ts.desc(), News.id.desc())
        ).all()
    )
    return enqueue_news_analysis(pending_ids)


def default_config_data() -> dict[str, Any]:
    return {
        "enabled": False,
        "news_interval_minutes": 15,
        "opportunity_interval_minutes": 15,
        "news_lookback_hours": 168,
        "news_trigger_window_hours": 4,
        "require_new_news_trigger": True,
        "require_market_quality_for_prediction": True,
        "timeframe": "1h",
        "prediction_max_holding_bars": 4,
        "indicator_keys": list(DEFAULT_INDICATOR_KEYS),
        "monitor_symbols": [],
        "minimum_news_confidence": 0.6,
        "minimum_news_mentions": 1,
        "minimum_indicator_score": 65.0,
        "minimum_combined_score": 75.0,
        "maximum_market_age_seconds": 120,
        "minimum_feature_quality": 0.7,
        "minimum_market_flow_quality": 0.5,
        "minimum_calibration_samples": 1000,
        "live_safety_margin_bps": 10.0,
        "news_score_weight": 45.0,
        "technical_score_weight": 35.0,
        "market_flow_score_weight": 20.0,
        "news_system_prompt": DEFAULT_NEWS_ANALYSIS_SYSTEM_PROMPT,
        "news_system_prompt_is_custom": False,
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
        "match_policy": INDICATOR_MATCH_POLICY,
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
        "news_trigger_window_hours": 4,
        "require_new_news_trigger": True,
        "require_market_quality_for_prediction": True,
        "timeframe": config.timeframe,
        "prediction_max_holding_bars": int(config.prediction_max_holding_bars),
        "indicator_keys": indicator_keys,
        "monitor_symbols": list(config.monitor_symbols_json or []),
        "minimum_news_confidence": float(config.minimum_news_confidence),
        "minimum_news_mentions": int(config.minimum_news_mentions),
        "minimum_indicator_score": float(config.minimum_indicator_score),
        "minimum_combined_score": max(75.0, float(config.minimum_combined_score)),
        "maximum_market_age_seconds": int(config.maximum_market_age_seconds),
        "minimum_feature_quality": float(config.minimum_feature_quality),
        "minimum_market_flow_quality": float(config.minimum_market_flow_quality),
        "minimum_calibration_samples": int(config.minimum_calibration_samples),
        "live_safety_margin_bps": float(config.live_safety_margin_bps),
        "news_score_weight": float(config.news_score_weight),
        "technical_score_weight": float(config.technical_score_weight),
        "market_flow_score_weight": float(config.market_flow_score_weight),
        "news_system_prompt": effective_news_analysis_system_prompt(
            config.news_system_prompt
        ),
        "news_system_prompt_is_custom": bool(
            str(config.news_system_prompt or "").strip()
        ),
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
        "match_policy": INDICATOR_MATCH_POLICY,
        "indicator_conflicts": [],
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
    market_flow: Mapping[str, Any] | None = None,
) -> float:
    weights = effective_opportunity_score_weights(config, market_flow)
    return round(
        float(news_score) * weights["news"]
        + float(technical_score) * weights["technical"]
        + float(market_flow_score) * weights["market_flow"],
        4,
    )


def effective_opportunity_score_weights(
    config: Mapping[str, Any] | None = None,
    market_flow: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Reduce the flow weight when its directional evidence is missing or weak.

    Calls without a market-flow snapshot retain the configured weights for
    backwards-compatible calculations and tests.  Live scans pass a snapshot,
    so an unavailable flow feed can no longer contribute a fabricated neutral 50.
    """

    configured = opportunity_score_weights(config)
    if market_flow is None:
        return configured
    try:
        quality = float(market_flow.get("data_quality") or 0)
    except (TypeError, ValueError, OverflowError):
        quality = 0.0
    if not math.isfinite(quality):
        quality = 0.0
    if not bool(market_flow.get("directional_data_available")) or not bool(
        market_flow.get("fresh")
    ):
        quality = 0.0
    quality = max(0.0, min(1.0, quality))
    flow_weight = configured["market_flow"] * quality
    non_flow_total = configured["news"] + configured["technical"]
    if non_flow_total <= 0:
        return {
            "news": round((1.0 - flow_weight) / 2, 6),
            "technical": round((1.0 - flow_weight) / 2, 6),
            "market_flow": round(flow_weight, 6),
        }
    remaining = 1.0 - flow_weight
    return {
        "news": round(remaining * configured["news"] / non_flow_total, 6),
        "technical": round(remaining * configured["technical"] / non_flow_total, 6),
        "market_flow": round(flow_weight, 6),
    }


def unusual_whales_signal_policy(db: Session) -> dict[str, Any]:
    """Load the published platform policy with a safe record-only fallback.

    The admin API validates writes, but this reader also treats a partially
    migrated or manually damaged row as untrusted input.  An invalid policy can
    therefore never turn on a hard gate by accident.
    """

    setting = db.get(AdminSetting, UNUSUAL_WHALES_SIGNAL_SETTING_KEY)
    raw = (
        dict(setting.value_json)
        if setting is not None and isinstance(setting.value_json, Mapping)
        else {}
    )
    mode = str(raw.get("mode") or "record").strip().lower()
    if mode not in {"record", "score", "gate"}:
        mode = "record"
    enabled = bool(raw.get("enabled", True))
    effective_mode = mode if enabled else "disabled"

    raw_thresholds = raw.get("thresholds")
    raw_thresholds = dict(raw_thresholds) if isinstance(raw_thresholds, Mapping) else {}
    thresholds: dict[str, float | int] = dict(DEFAULT_UNUSUAL_WHALES_THRESHOLDS)
    for key, default in DEFAULT_UNUSUAL_WHALES_THRESHOLDS.items():
        candidate = _finite_number(raw_thresholds.get(key))
        if candidate is None or candidate < 0:
            continue
        thresholds[key] = int(candidate) if isinstance(default, int) else float(candidate)
    thresholds["min_data_coverage"] = max(
        0.0, min(1.0, float(thresholds["min_data_coverage"]))
    )
    raw_channels = raw.get("channels")
    channels = dict(raw_channels) if isinstance(raw_channels, Mapping) else {}

    raw_weights = raw.get("weights")
    raw_weights = dict(raw_weights) if isinstance(raw_weights, Mapping) else {}
    weights = {
        key: _finite_number(raw_weights.get(key))
        for key in DEFAULT_UNUSUAL_WHALES_WEIGHTS
    }
    valid_weights = all(value is not None and value >= 0 for value in weights.values())
    weight_total = sum(float(value or 0) for value in weights.values())
    if not valid_weights or not math.isclose(weight_total, 1.0, abs_tol=1e-6):
        normalized_weights = dict(DEFAULT_UNUSUAL_WHALES_WEIGHTS)
    else:
        normalized_weights = {
            key: round(float(value), 8) for key, value in weights.items() if value is not None
        }

    published_version = max(0, int(setting.version or 0)) if setting is not None else 0
    return {
        "enabled": enabled,
        "mode": mode,
        "effective_mode": effective_mode,
        "thresholds": thresholds,
        "channels": {str(key): bool(value) for key, value in channels.items()},
        "weights": normalized_weights,
        "published_version": published_version,
        "policy_version": UNUSUAL_WHALES_SIGNAL_POLICY_VERSION,
        "weights_version": f"uw_weights_v{published_version}",
        "decision_version": f"uw_{effective_mode}_v{published_version}",
        "score_enabled": enabled and mode in {"score", "gate"},
        "hard_gate_enabled": enabled and mode == "gate",
    }


def enhanced_opportunity_domain_score(
    *,
    news_score: float,
    technical_score: float,
    market_environment: Mapping[str, Any],
    market_flow: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Map the six published domains and renormalize only over real evidence.

    Missing UW feeds stay unavailable. They neither contribute a neutral 50 nor
    consume configured weight. Data quality reduces the effective weight of the
    three UW-only domains before the remaining weights are renormalized.
    """

    configured = dict(policy.get("weights") or DEFAULT_UNUSUAL_WHALES_WEIGHTS)
    flow_domains = dict(market_flow.get("domains") or {})
    macro_available = bool(market_environment.get("available"))
    macro_adjustment = _finite_number(market_environment.get("adjustment"))
    context_score = (
        max(0.0, min(100.0, 50.0 + float(macro_adjustment) * 2.5))
        if macro_available and macro_adjustment is not None
        else None
    )

    domains: dict[str, dict[str, Any]] = {
        "news": {
            "available": math.isfinite(float(news_score)),
            "fresh": True,
            "score": max(0.0, min(100.0, float(news_score))),
            "data_quality": 1.0,
            "source": "news_ai",
        },
        "technical": {
            "available": math.isfinite(float(technical_score)),
            "fresh": True,
            "score": max(0.0, min(100.0, float(technical_score))),
            "data_quality": 1.0,
            "source": "strategy_indicators",
        },
        "market_context": {
            "available": context_score is not None,
            "fresh": context_score is not None,
            "score": round(context_score, 4) if context_score is not None else None,
            "data_quality": 1.0 if context_score is not None else 0.0,
            "source": "macro_directional_adjustment",
            "mapping": "neutral_50_plus_adjustment_x2_5",
        },
    }
    for published_key, source_key in (
        ("options_flow", "option_flow"),
        ("gex", "gex"),
        ("institutional_flow", "institutional_flow"),
    ):
        source = dict(flow_domains.get(source_key) or {})
        score = _finite_number(source.get("score"))
        quality = _finite_number(source.get("data_quality"))
        quality = max(0.0, min(1.0, quality if quality is not None else 0.0))
        available = bool(source.get("available")) and bool(source.get("fresh")) and score is not None
        domains[published_key] = {
            "available": available,
            "fresh": bool(source.get("fresh")),
            "score": round(max(0.0, min(100.0, score)), 4) if score is not None else None,
            "data_quality": round(quality, 4),
            "source": f"unusual_whales.{source_key}",
        }

    weighted: dict[str, float] = {}
    for key, domain in domains.items():
        if not bool(domain["available"]):
            continue
        quality_multiplier = (
            float(domain["data_quality"])
            if key in {"options_flow", "gex", "institutional_flow"}
            else 1.0
        )
        effective = max(0.0, float(configured.get(key) or 0.0)) * quality_multiplier
        if effective > 0:
            weighted[key] = effective
    total = sum(weighted.values())
    effective_weights = (
        {key: round(weight / total, 8) for key, weight in weighted.items()}
        if total > 0
        else {}
    )
    score = (
        sum(float(domains[key]["score"]) * weight for key, weight in effective_weights.items())
        if effective_weights
        else None
    )
    return {
        "available": score is not None,
        "score": round(float(score), 4) if score is not None else None,
        "domains": domains,
        "configured_weights": {
            key: round(float(value), 8) for key, value in configured.items()
        },
        "effective_weights": effective_weights,
        "missing_domains": [key for key, item in domains.items() if not item["available"]],
        "weights_version": str(policy.get("weights_version") or OPPORTUNITY_WEIGHTS_VERSION),
        "mode": str(policy.get("mode") or "record"),
    }


def indicator_templates() -> list[dict[str, Any]]:
    """Return coherent market-regime presets for the configuration UI."""

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


def indicator_group(key: str) -> str:
    normalized = str(key).strip().lower()
    for group, keys in INDICATOR_GROUPS.items():
        if normalized in keys:
            return group
    return "other"


def configured_indicator_policy(
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate selected indicators as alternative market-regime groups.

    Breakout, pullback and reversal are alternative entry setups, so requiring
    every selected indicator on one bar closes the signal gate.  This policy
    requires at least two available core indicators and lets any coherent core
    group qualify.  Market-flow observations remain score/context inputs and
    are protected by the separate hard-conflict check.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in evidence:
        grouped.setdefault(str(item.get("group") or "other"), []).append(item)

    group_results: list[dict[str, Any]] = []
    core_available_count = 0
    core_matched_count = 0
    for group, items in grouped.items():
        available = [item for item in items if bool(item.get("available"))]
        matched = [item for item in available if bool(item.get("matched"))]
        is_core = group in CORE_INDICATOR_GROUPS or group == "other"
        if is_core:
            core_available_count += len(available)
            core_matched_count += len(matched)
        required = min(2, len(available)) if is_core else 0
        strength_source = matched if is_core and required and len(matched) >= required else available
        ranked_strengths = sorted(
            (float(item.get("strength") or 0.0) for item in strength_source),
            reverse=True,
        )
        score_width = required or min(2, len(ranked_strengths))
        score = (
            round(sum(ranked_strengths[:score_width]) / score_width, 4)
            if score_width
            else None
        )
        group_results.append(
            {
                "key": group,
                "selected_count": len(items),
                "available_count": len(available),
                "matched_count": len(matched),
                "required_count": required,
                "passed": bool(is_core and required and len(matched) >= required),
                "score": score,
                "blocking": is_core,
            }
        )

    minimum_core_matches = min(2, core_available_count) if core_available_count else 0
    passed_groups = [
        item for item in group_results if bool(item["blocking"]) and bool(item["passed"])
    ]
    passed = bool(
        minimum_core_matches
        and core_matched_count >= minimum_core_matches
        and passed_groups
    )
    scored_groups = [
        item for item in (passed_groups or group_results)
        if bool(item["blocking"]) and item["score"] is not None
    ]
    technical_score = max(
        (float(item["score"]) for item in scored_groups),
        default=0.0,
    )
    return {
        "version": INDICATOR_MATCH_POLICY,
        "passed": passed,
        "technical_score": round(technical_score, 4),
        "minimum_core_matches": minimum_core_matches,
        "core_available_count": core_available_count,
        "core_matched_count": core_matched_count,
        "passed_groups": [str(item["key"]) for item in passed_groups],
        "groups": group_results,
        "non_blocking_keys": sorted(NON_BLOCKING_INDICATOR_KEYS),
    }


def cleanup_unpredicted_opportunities(db: Session, user_id: int) -> dict[str, int]:
    """Remove non-auditable candidates while preserving every prediction ledger row.

    Expired candidates that never crossed the prediction gate are transient
    scan state, not historical predictions.  Active candidates from an older
    rule version are also invalid after deployment and must be rebuilt using
    the current policy.  Rows linked to a prediction are never deleted.
    """

    rows = db.scalars(
        select(AiMonitorOpportunity).where(AiMonitorOpportunity.user_id == user_id)
    ).all()
    if not rows:
        return {"expired": 0, "stale_policy": 0, "total": 0}
    prediction_ids = set(
        db.scalars(
            select(AiMonitorPrediction.opportunity_id).where(
                AiMonitorPrediction.user_id == user_id,
                AiMonitorPrediction.opportunity_id.in_([row.id for row in rows]),
            )
        ).all()
    )
    counts = {"expired": 0, "stale_policy": 0, "total": 0}
    for row in rows:
        if row.id in prediction_ids:
            continue
        policy = str((row.evidence_json or {}).get("match_policy") or "")
        reason = (
            "expired"
            if row.status == "expired"
            else "stale_policy"
            if policy != INDICATOR_MATCH_POLICY
            else ""
        )
        if not reason:
            continue
        db.delete(row)
        counts[reason] += 1
        counts["total"] += 1
    if counts["total"]:
        db.flush()
    return counts


def merged_opportunity_expiration(
    current: datetime,
    proposed: datetime,
    *,
    has_prediction: bool,
    has_new_material_news: bool,
    newly_confirmed: bool,
) -> datetime:
    """Keep live rescoring separate from an auditable signal lifetime."""

    if has_prediction:
        return current
    if newly_confirmed:
        return proposed
    if has_new_material_news:
        return max(current, proposed)
    return current


def append_score_history(
    evidence: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    *,
    seed_snapshots: Sequence[Mapping[str, Any]] = (),
    limit: int = 96,
) -> list[dict[str, Any]]:
    """Append one bounded live-score observation without losing frozen baselines."""

    source = evidence or {}
    raw_history = source.get("score_history")
    stored_history = (
        [dict(item) for item in raw_history if isinstance(item, Mapping)]
        if isinstance(raw_history, list)
        else []
    )
    previous_snapshot = source.get("score_snapshot")
    candidates = [dict(item) for item in seed_snapshots if isinstance(item, Mapping)]
    if not stored_history and isinstance(previous_snapshot, Mapping):
        candidates.append(dict(previous_snapshot))
    candidates.extend(stored_history)
    candidates.append(dict(snapshot))

    # A legacy scanner used to replace evidence_json wholesale.  Re-introduce
    # immutable prediction-time points and de-duplicate observations globally,
    # rather than only comparing the last item in the current JSON document.
    by_timestamp: dict[str, dict[str, Any]] = {}
    without_timestamp: list[dict[str, Any]] = []
    for item in candidates:
        calculated_at = item.get("calculated_at")
        if calculated_at:
            by_timestamp[str(calculated_at)] = item
        else:
            without_timestamp.append(item)
    history = without_timestamp + [
        by_timestamp[key] for key in sorted(by_timestamp)
    ]
    return history[-max(2, int(limit)) :]


def market_flow_history_snapshot(flow: Mapping[str, Any] | None) -> dict[str, float]:
    """Keep the auditable market-flow inputs needed for the UI trend view."""

    source = flow or {}
    keys = (
        "score",
        "main_force_ratio",
        "active_buy_ratio",
        "book_imbalance",
        "book_imbalance_5",
        "bid_depth_notional",
        "ask_depth_notional",
        "bid_depth_notional_5",
        "ask_depth_notional_5",
        "bid_depth_change_5s_pct",
        "ask_depth_change_5s_pct",
        "bid_depth_change_30s_pct",
        "ask_depth_change_30s_pct",
        "spread_bps",
        "data_quality",
    )
    snapshot: dict[str, float] = {}
    for key in keys:
        value = _finite_number(source.get(key))
        if value is not None:
            snapshot[key] = value
    return snapshot


def prediction_live_score_snapshot(
    prediction: AiMonitorPrediction,
    candidate: Mapping[str, Any],
    repository: MonitorRepository,
    market_flow_inputs: Mapping[str, Mapping[str, Any]],
    now: datetime,
    macro_market_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Rescore a pending prediction using only its frozen strategy identity."""

    evidence = dict(prediction.evidence_json or {})
    configured_keys = evidence.get("configured_indicator_keys")
    if not isinstance(configured_keys, list):
        frozen_indicators = evidence.get("indicators")
        configured_keys = (
            [
                str(item.get("key"))
                for item in frozen_indicators
                if isinstance(item, Mapping) and item.get("key")
            ]
            if isinstance(frozen_indicators, list)
            else []
        )
    configured_keys = [str(key) for key in configured_keys if str(key)]
    if not configured_keys:
        return None

    direction = str(candidate.get("direction") or prediction.direction)
    if direction not in {"long", "short"}:
        return None
    try:
        scan = repository.strategy_indicators(
            prediction.contract_symbol,
            prediction.timeframe,
        )
    except MonitorUnavailable:
        return None
    _, indicator_evidence = match_configured_indicators(
        scan,
        configured_keys,
        direction,
    )
    indicator_policy = configured_indicator_policy(indicator_evidence)
    if int(indicator_policy.get("core_available_count") or 0) <= 0:
        return None
    technical_score = float(indicator_policy["technical_score"])

    stored_weights = evidence.get("score_weights")
    if isinstance(stored_weights, Mapping):
        try:
            raw_weights = {
                key: float(stored_weights[key])
                for key in ("news", "technical", "market_flow")
            }
            total_weight = sum(raw_weights.values())
            if (
                total_weight <= 0
                or any(value < 0 or not math.isfinite(value) for value in raw_weights.values())
            ):
                raise ValueError("invalid frozen score weights")
            weights = {
                key: value / total_weight for key, value in raw_weights.items()
            }
        except (KeyError, TypeError, ValueError):
            weights = opportunity_score_weights()
    else:
        weights = opportunity_score_weights()

    flow = market_flow_snapshot(
        market_flow_inputs,
        symbol=prediction.symbol,
        contract_symbol=prediction.contract_symbol,
        direction=direction,
        now=now,
    )
    news_score = float(candidate.get("news_score") or 0)
    flow_score = float(flow["score"])
    effective_weights = effective_opportunity_score_weights(
        {
            "news_score_weight": weights["news"] * 100,
            "technical_score_weight": weights["technical"] * 100,
            "market_flow_score_weight": weights["market_flow"] * 100,
        },
        flow,
    )
    base_combined_score = round(
        news_score * effective_weights["news"]
        + technical_score * effective_weights["technical"]
        + flow_score * effective_weights["market_flow"],
        4,
    )
    company_profile = dict(
        market_flow_inputs.get("profile", {}).get(prediction.symbol.upper(), {})
    )
    market_environment = macro_market.opportunity_market_context(
        macro_market_snapshot,
        direction=direction,
        symbol=prediction.symbol,
        sector=company_profile.get("sector"),
        industry=company_profile.get("industry"),
    )
    combined_score = macro_market.apply_market_adjustment(
        base_combined_score,
        market_environment,
    )
    market = dict(
        market_flow_inputs.get("ticker", {}).get(prediction.contract_symbol.upper(), {})
    )
    reference_price = float(market.get("price") or 0)
    reference_time_ms = int(market.get("ts") or 0)
    if 0 < reference_time_ms < 1_000_000_000_000:
        reference_time_ms *= 1_000
    if not (0 < reference_time_ms <= _datetime_ms(now)):
        reference_time_ms = 0
    frozen_readiness = evidence.get("live_readiness")
    frozen_readiness = (
        dict(frozen_readiness) if isinstance(frozen_readiness, Mapping) else {}
    )
    minimum_indicator_score = float(
        frozen_readiness.get("minimum_indicator_score", 65.0)
    )
    minimum_combined_score = float(
        frozen_readiness.get(
            "base_minimum_combined_score",
            frozen_readiness.get("minimum_combined_score", 70.0),
        )
    )
    entry_policy = dict(market_environment.get("entry_policy") or {})
    threshold_delta = float(entry_policy.get("threshold_delta") or 0)
    effective_minimum_combined_score = min(
        100.0, minimum_combined_score + max(0.0, threshold_delta)
    )
    macro_entry_allowed = bool(entry_policy.get("entry_allowed", True))
    entry_confirmed = bool(
        indicator_policy.get("passed")
        and technical_score >= minimum_indicator_score
        and combined_score >= effective_minimum_combined_score
        and not bool(flow.get("hard_conflict"))
        and macro_entry_allowed
    )
    return {
        "news": news_score,
        "technical": technical_score,
        "market_flow": flow_score,
        "market_flow_snapshot": market_flow_history_snapshot(flow),
        "base_combined": base_combined_score,
        "macro_adjustment": float(market_environment.get("adjustment") or 0),
        "macro_market": market_environment,
        "combined": combined_score,
        "direction": direction,
        "calculated_at": now.isoformat(),
        "reference_price": (
            reference_price if reference_price > 0 and reference_time_ms else None
        ),
        "reference_price_time_ms": reference_time_ms or None,
        "entry_confirmed": entry_confirmed,
        "strategy_identity": {
            "timeframe": prediction.timeframe,
            "indicator_keys": configured_keys,
            "score_weights": {
                key: round(value, 8) for key, value in weights.items()
            },
            "effective_score_weights": {
                key: round(value, 8) for key, value in effective_weights.items()
            },
            "minimum_indicator_score": minimum_indicator_score,
            "minimum_combined_score": minimum_combined_score,
        },
        "effective_minimum_combined_score": effective_minimum_combined_score,
        "macro_entry_allowed": macro_entry_allowed,
        "macro_policy": entry_policy,
    }


def refresh_pending_prediction_scores(
    db: Session,
    *,
    user_id: int,
    news_rows: Sequence[News],
    symbol_map: Mapping[str, str],
    repository: MonitorRepository,
    market_flow_inputs: Mapping[str, Mapping[str, Any]],
    now: datetime,
    macro_market_snapshot: Mapping[str, Any] | None = None,
) -> int:
    """Refresh pending ledgers from raw news using each prediction's frozen policy."""

    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.user_id == user_id,
            AiMonitorPrediction.status == "pending",
        )
        .order_by(AiMonitorPrediction.id)
        .limit(500)
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    )
    predictions = list(db.scalars(statement).all())
    candidate_cache: dict[
        tuple[float, int, int], dict[tuple[str, str], dict[str, Any]]
    ] = {}
    updated = 0
    for prediction in predictions:
        evidence = dict(prediction.evidence_json or {})
        try:
            minimum_confidence = max(
                0.0,
                min(1.0, float(evidence.get("minimum_news_score", 60.0)) / 100.0),
            )
            minimum_mentions = max(
                1,
                int(evidence.get("minimum_news_mentions", 1)),
            )
            news_lookback_hours = max(
                1,
                min(168, int(evidence.get("news_lookback_hours", 24))),
            )
        except (TypeError, ValueError):
            minimum_confidence, minimum_mentions, news_lookback_hours = 0.6, 1, 24
        policy_key = (minimum_confidence, minimum_mentions, news_lookback_hours)
        if policy_key not in candidate_cache:
            news_cutoff = int(
                (now.replace(tzinfo=UTC) - timedelta(hours=news_lookback_hours)).timestamp()
            )
            directional = aggregate_news_candidates(
                [
                    row
                    for row in news_rows
                    if int(_row_value(row, "ts", 0) or 0) >= news_cutoff
                ],
                symbol_map,
                minimum_confidence=minimum_confidence,
                minimum_mentions=minimum_mentions,
            )
            candidate_cache[policy_key] = {
                (str(item.get("symbol") or ""), str(item.get("direction") or "")): item
                for item in directional
            }
        candidates = candidate_cache[policy_key]
        held_candidate = candidates.get((prediction.symbol, prediction.direction)) or {
            "symbol": prediction.symbol,
            "contract_symbol": prediction.contract_symbol,
            "direction": prediction.direction,
            "news_score": 0.0,
            "news": [],
        }
        opposite_direction = "short" if prediction.direction == "long" else "long"
        opposite_candidate = candidates.get((prediction.symbol, opposite_direction))
        held_snapshot = prediction_live_score_snapshot(
            prediction,
            held_candidate,
            repository,
            market_flow_inputs,
            now,
            macro_market_snapshot,
        )
        opposite_snapshot = (
            prediction_live_score_snapshot(
                prediction,
                opposite_candidate,
                repository,
                market_flow_inputs,
                now,
                macro_market_snapshot,
            )
            if opposite_candidate is not None
            else None
        )
        snapshot = (
            opposite_snapshot
            if opposite_snapshot is not None
            and bool(opposite_snapshot.get("entry_confirmed"))
            else held_snapshot
        )
        if snapshot is None:
            continue
        frozen_seed = {
            "news": float(prediction.signal_news_score or 0),
            "technical": float(prediction.signal_indicator_score or 0),
            "market_flow": float(
                (evidence.get("signal_scores") or {}).get("market_flow") or 0
            ),
            "combined": float(prediction.confidence_score),
            "direction": prediction.direction,
            "calculated_at": prediction.predicted_at.isoformat(),
        }
        evidence["live_score_history"] = append_score_history(
            {"score_history": evidence.get("live_score_history")},
            snapshot,
            seed_snapshots=[frozen_seed],
        )
        evidence["latest_live_score"] = snapshot
        policy_due_at = getattr(
            prediction,
            "due_at",
            prediction.predicted_at
            + timedelta(
                seconds=_TIMEFRAME_SECONDS.get(prediction.timeframe, 15 * 60) * 4
            ),
        )
        score_policy = prediction_soft_exit_policy(
            prediction.timeframe,
            start_ms=_datetime_ms(prediction.predicted_at),
            due_ms=_datetime_ms(policy_due_at),
        )
        evidence["score_exit_policy"] = {
            "version": PREDICTION_SCORE_EXIT_POLICY_VERSION,
            "direction_reversal_points": score_policy["confirmation_bars"],
            "score_breakdown_points": score_policy["confirmation_bars"],
            "score_breakdown_bars": score_policy["confirmation_bars"],
            "confirmation_unit": score_policy["confirmation_unit"],
            "bar_interval_minutes": score_policy["bar_ms"] // 60_000,
            "minimum_hold_minutes": score_policy["minimum_hold_ms"] // 60_000,
            "planned_horizon_minutes": score_policy["planned_horizon_ms"] // 60_000,
            "combined_hysteresis_points": 5.0,
            "news_policy": {
                "minimum_confidence": minimum_confidence,
                "minimum_mentions": minimum_mentions,
                "lookback_hours": news_lookback_hours,
            },
        }
        prediction.evidence_json = evidence
        updated += 1
    return updated


def virtual_entry_gate_snapshot(
    *,
    direction: str,
    news_score: float,
    news_mention_count: int,
    minimum_news_score: float,
    minimum_news_mentions: int,
    indicator_policy_passed: bool,
    indicator_score: float,
    minimum_indicator_score: float,
    combined_score: float,
    minimum_combined_score: float,
    market_flow_hard_conflict: bool,
    entry_price: float,
    checked_at: datetime | str,
    has_new_trigger_news: bool = True,
    require_new_trigger_news: bool = False,
    market_quality_passed: bool = True,
    require_market_quality: bool = False,
    macro_entry_allowed: bool = True,
    macro_policy: Mapping[str, Any] | None = None,
    order_book_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an explicit, auditable gate for research-only virtual entries."""

    checked_at_text = checked_at.isoformat() if isinstance(checked_at, datetime) else str(checked_at)
    frozen_order_book = (
        dict(order_book_gate) if isinstance(order_book_gate, Mapping) else {}
    )
    checks = [
        *(
            [
                {
                    "key": "new_news_trigger",
                    "label": "新事件",
                    "passed": bool(has_new_trigger_news),
                    "current": bool(has_new_trigger_news),
                    "required": True,
                    "detail": "至少一条触发窗口内、尚未用于预测的新新闻",
                }
            ]
            if require_new_trigger_news
            else []
        ),
        {
            "key": "news_candidate",
            "label": "新闻候选",
            "passed": bool(
                news_score >= minimum_news_score
                and news_mention_count >= minimum_news_mentions
            ),
            "current": round(float(news_score), 4),
            "required": round(float(minimum_news_score), 4),
            "detail": f"{int(news_mention_count)} 条关联新闻",
        },
        {
            "key": "indicator_policy",
            "label": "策略组",
            "passed": bool(indicator_policy_passed),
            "current": bool(indicator_policy_passed),
            "required": True,
            "detail": "至少一个核心技术策略组通过",
        },
        {
            "key": "indicator_score",
            "label": "技术评分",
            "passed": bool(indicator_score >= minimum_indicator_score),
            "current": round(float(indicator_score), 4),
            "required": round(float(minimum_indicator_score), 4),
            "detail": "方向一致的连续技术强度",
        },
        {
            "key": "combined_score",
            "label": "组合评分",
            "passed": bool(combined_score >= minimum_combined_score),
            "current": round(float(combined_score), 4),
            "required": round(float(minimum_combined_score), 4),
            "detail": "新闻、技术与资金盘口加权评分",
        },
        {
            "key": "macro_entry_policy",
            "label": "宏观准入",
            "passed": bool(macro_entry_allowed),
            "current": str(dict(macro_policy or {}).get("label") or "正常环境"),
            "required": "当前方向允许新增仓位",
            "detail": str(
                dict(macro_policy or {}).get("blocked_reason")
                or "宏观状态通过提高门槛与仓位系数控制风险"
            ),
        },
        {
            "key": "market_flow_conflict",
            "label": "盘口冲突",
            "passed": not bool(market_flow_hard_conflict),
            "current": bool(market_flow_hard_conflict),
            "required": False,
            "detail": "候选方向资金评分不得形成强冲突",
        },
        *(
            [
                {
                    "key": "market_quality",
                    "label": "行情质量",
                    "passed": bool(market_quality_passed),
                    "current": bool(market_quality_passed),
                    "required": True,
                    "detail": "实时价格、已收盘 K 线与所需预测因子必须新鲜可用",
                }
            ]
            if require_market_quality
            else []
        ),
        *(
            [
                {
                    "key": "order_book_quality",
                    "label": "实时盘口质量",
                    "passed": bool(frozen_order_book.get("quality_passed")),
                    "blocking": False,
                    "current": str(frozen_order_book.get("status") or "missing"),
                    "required": "研究信号仅作质量提示",
                    "detail": "盘口缺失或过期会降低研究信号质量，但不会单独阻止模拟记录",
                },
                {
                    "key": "order_book_direction",
                    "label": "盘口强冲突否决",
                    "passed": not bool(
                        frozen_order_book.get("quality_passed")
                        and frozen_order_book.get("direction_conflict")
                    ),
                    "current": frozen_order_book.get("directional_pressure"),
                    "required": "不得存在新鲜盘口的强反向冲突",
                    "detail": "中性盘口允许生成研究预测；只有新鲜且明确反向的盘口才否决",
                },
            ]
            if frozen_order_book
            else []
        ),
        {
            "key": "entry_price",
            "label": "入场价格",
            "passed": bool(entry_price > 0),
            "current": round(float(entry_price), 12) if entry_price > 0 else None,
            "required": "> 0",
            "detail": "必须取得真实扫描参考价后才能冻结入场",
        },
    ]
    signal_confirmed = all(
        bool(item["passed"]) or item.get("blocking") is False
        for item in checks[:-1]
    )
    entry_ready = signal_confirmed and bool(checks[-1]["passed"])
    return {
        "version": "research_entry_quality_v6",
        "execution_mode": "virtual_prediction_only",
        "real_order_enabled": False,
        "direction": "short" if direction == "short" else "long",
        "signal_confirmed": signal_confirmed,
        "entry_ready": entry_ready,
        "status": (
            "ready"
            if entry_ready
            else "price_unavailable"
            if signal_confirmed
            else "waiting_conditions"
        ),
        "reference_price": round(float(entry_price), 12) if entry_price > 0 else None,
        "checked_at": checked_at_text,
        "checks": checks,
        "macro_policy": dict(macro_policy or {}),
        "order_book_gate": frozen_order_book or None,
        "note": "仅生成预测记录，不会调用模拟盘或实盘下单接口。",
    }


def prediction_entry_gate_snapshot(
    prediction: AiMonitorPrediction,
) -> dict[str, Any]:
    """Return the frozen virtual-entry gate, deriving it for legacy predictions."""

    evidence = dict(prediction.evidence_json or {})
    stored = evidence.get("virtual_entry_gate")
    if isinstance(stored, Mapping):
        return dict(stored)
    readiness = evidence.get("live_readiness")
    readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
    indicator_policy = evidence.get("indicator_policy")
    indicator_policy = dict(indicator_policy) if isinstance(indicator_policy, Mapping) else {}
    news = evidence.get("news")
    news_items = list(news) if isinstance(news, list) else []
    market_flow = evidence.get("market_flow")
    market_flow = dict(market_flow) if isinstance(market_flow, Mapping) else {}
    signal_scores = evidence.get("signal_scores")
    signal_scores = dict(signal_scores) if isinstance(signal_scores, Mapping) else {}
    market_environment = evidence.get("market_environment")
    market_environment = (
        dict(market_environment) if isinstance(market_environment, Mapping) else {}
    )
    macro_policy = market_environment.get("entry_policy")
    macro_policy = dict(macro_policy) if isinstance(macro_policy, Mapping) else {}
    news_score = float(
        prediction.signal_news_score
        if prediction.signal_news_score is not None
        else signal_scores.get("news", 0)
    )
    indicator_score = float(
        prediction.signal_indicator_score
        if prediction.signal_indicator_score is not None
        else signal_scores.get("indicator", 0)
    )
    return virtual_entry_gate_snapshot(
        direction=prediction.direction,
        news_score=news_score,
        news_mention_count=len(news_items),
        minimum_news_score=float(evidence.get("minimum_news_score", 60.0)),
        minimum_news_mentions=int(evidence.get("minimum_news_mentions", 1)),
        indicator_policy_passed=bool(
            indicator_policy.get("passed", evidence.get("technical_confirmed", True))
        ),
        indicator_score=indicator_score,
        minimum_indicator_score=float(readiness.get("minimum_indicator_score", 65.0)),
        combined_score=float(prediction.confidence_score),
        minimum_combined_score=float(readiness.get("minimum_combined_score", 70.0)),
        market_flow_hard_conflict=bool(market_flow.get("hard_conflict")),
        entry_price=float(prediction.entry_price or 0),
        checked_at=prediction.predicted_at,
        has_new_trigger_news=bool(
            (evidence.get("news_trigger") or {}).get("has_new_news", True)
        ),
        require_new_trigger_news=bool(
            (evidence.get("news_trigger") or {}).get("required", False)
        ),
        market_quality_passed=bool((evidence.get("market_quality") or {}).get("passed", True)),
        require_market_quality=bool(evidence.get("require_market_quality_for_prediction", False)),
        macro_entry_allowed=bool(macro_policy.get("entry_allowed", True)),
        macro_policy=macro_policy,
        order_book_gate=(
            dict(market_flow.get("order_book_gate") or {})
            if isinstance(market_flow.get("order_book_gate"), Mapping)
            else None
        ),
    )


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
            "minimum_net_protected_r": (
                PREDICTION_RISK_UNIT_MINIMUM_NET_PROTECTED_R
            ),
            "early_maximum_giveback_r": PREDICTION_RISK_UNIT_EARLY_GIVEBACK_R,
            "trailing_activation_r": (
                PREDICTION_RISK_UNIT_TRAILING_ACTIVATION_R
            ),
            "maximum_giveback_r": PREDICTION_RISK_UNIT_TRAILING_GIVEBACK_R,
            "activation_boundary": "prior_closed_15m_bar",
            "minimum_hold_policy": "immediate_after_activation",
        },
        "failed_follow_through": {
            "closed_bars": PREDICTION_FOLLOW_THROUGH_BARS,
            "maximum_favorable_r": (
                PREDICTION_RISK_UNIT_PROTECTION_ACTIVATION_R
            ),
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


def virtual_position_snapshot(
    prediction: AiMonitorPrediction,
    live_market: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Calculate a direction-aware, read-only mark-to-market position snapshot."""

    evidence = dict(prediction.evidence_json or {})
    stored_plan = evidence.get("risk_plan")
    risk_plan = (
        dict(stored_plan)
        if isinstance(stored_plan, Mapping)
        else virtual_risk_plan_snapshot(
            entry_price=float(prediction.entry_price or 0),
            direction=prediction.direction,
            timeframe=prediction.timeframe,
        )
    )
    market = dict(live_market or {})
    entry_price = float(prediction.entry_price or 0)
    live_price = float(market.get("price") or 0)
    settled = prediction.status == "completed" and prediction.exit_price is not None
    current_price = float(prediction.exit_price) if settled else live_price
    market_timestamp = int(market.get("ts") or 0)
    market_at = None
    market_age_seconds = None
    if market_timestamp > 0:
        if market_timestamp > 10_000_000_000:
            market_timestamp //= 1000
        market_datetime = datetime.fromtimestamp(market_timestamp, UTC)
        market_at = market_datetime.isoformat()
        market_age_seconds = max(
            0.0, (datetime.now(UTC) - market_datetime).total_seconds()
        )
    if settled:
        settled_at = getattr(prediction, "exit_at", None) or prediction.due_at
        market_at = settled_at.replace(tzinfo=UTC).isoformat()
        market_age_seconds = None
    if entry_price <= 0 or current_price <= 0:
        return {
            "available": False,
            "entry_price": entry_price if entry_price > 0 else None,
            "current_price": current_price if current_price > 0 else None,
            "market_at": market_at,
            "valuation_state": "settled" if settled else "live",
            "market_age_seconds": market_age_seconds,
            "market_stale": bool(
                not settled
                and (market_age_seconds is None or market_age_seconds > 120)
            ),
            "risk_plan": risk_plan,
        }
    gross = prediction_outcome(entry_price, current_price, prediction.direction)
    gross_bps = float(gross["directional_return_bps"])
    estimated_cost_bps = float(prediction.estimated_cost_bps or 0)
    net_bps = gross_bps - estimated_cost_bps
    per_unit_gross = (
        current_price - entry_price
        if prediction.direction == "long"
        else entry_price - current_price
    )
    per_unit_net = per_unit_gross - entry_price * estimated_cost_bps / 10_000
    stop_loss_price = float(risk_plan.get("stop_loss_price") or 0)
    take_profit_price = float(risk_plan.get("take_profit_price") or 0)
    if prediction.direction == "short":
        target_state = (
            "take_profit_reached"
            if take_profit_price > 0 and current_price <= take_profit_price
            else "stop_loss_reached"
            if stop_loss_price > 0 and current_price >= stop_loss_price
            else "active"
        )
    else:
        target_state = (
            "take_profit_reached"
            if take_profit_price > 0 and current_price >= take_profit_price
            else "stop_loss_reached"
            if stop_loss_price > 0 and current_price <= stop_loss_price
            else "active"
        )
    return {
        "available": True,
        "entry_price": round(entry_price, 12),
        "current_price": round(current_price, 12),
        "market_at": market_at,
        "valuation_state": "settled" if settled else "live",
        "market_age_seconds": (
            round(market_age_seconds, 3) if market_age_seconds is not None else None
        ),
        "market_stale": bool(
            not settled and (market_age_seconds is None or market_age_seconds > 120)
        ),
        "gross_return_bps": round(gross_bps, 8),
        "gross_return_pct": round(gross_bps / 100, 8),
        "estimated_cost_bps": round(estimated_cost_bps, 8),
        "net_return_bps": round(net_bps, 8),
        "net_return_pct": round(net_bps / 100, 8),
        "gross_pnl_per_unit": round(per_unit_gross, 12),
        "net_pnl_per_unit": round(per_unit_net, 12),
        "net_pnl_per_10000": round(net_bps, 8),
        "profit_state": "profit" if net_bps > 0 else "loss" if net_bps < 0 else "flat",
        "target_state": target_state,
        "risk_plan": risk_plan,
        "exit_reason": getattr(prediction, "exit_reason", None) if settled else None,
        "exit_at": (
            (getattr(prediction, "exit_at", None) or prediction.due_at)
            .replace(tzinfo=UTC)
            .isoformat()
            if settled
            else None
        ),
        "note": "浮盈亏按预测方向和最新合约行情计算；每 10,000 U 为标准化名义本金，不代表真实持仓。",
    }


def backfill_prediction_risk_plans(db: Session, user_id: int) -> int:
    """Freeze deterministic fallback levels for active legacy predictions."""

    items = db.scalars(
        select(AiMonitorPrediction).where(
            AiMonitorPrediction.user_id == user_id,
            AiMonitorPrediction.status == "pending",
        )
    ).all()
    updated = 0
    for item in items:
        evidence = dict(item.evidence_json or {})
        if isinstance(evidence.get("risk_plan"), Mapping):
            continue
        risk_plan = virtual_risk_plan_snapshot(
            entry_price=float(item.entry_price or 0),
            direction=item.direction,
            timeframe=item.timeframe,
        )
        repair = evidence.get("settlement_repair")
        repair = dict(repair) if isinstance(repair, Mapping) else {}
        frozen_settlement_version = str(
            repair.get("target_version")
            or item.settlement_version
            or PREVIOUS_PREDICTION_SETTLEMENT_VERSION
        )
        risk_plan["settlement_version"] = frozen_settlement_version
        evidence["risk_plan"] = risk_plan
        item.evidence_json = evidence
        updated += 1
    return updated


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


def readiness_cost_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Force conservative costs for admission metrics even when UI costs are disabled."""

    settings = prediction_cost_settings(config)
    return {
        "prediction_fee_enabled": True,
        "prediction_fee_bps_per_side": max(
            float(settings["fee_bps_per_side"]), PREDICTION_FEE_BPS_PER_SIDE
        ),
        "prediction_slippage_enabled": True,
        "prediction_slippage_bps_per_side": max(
            float(settings["slippage_bps_per_side"]), PREDICTION_SLIPPAGE_BPS_PER_SIDE
        ),
        "prediction_funding_enabled": True,
        "prediction_funding_bps_per_8h": max(
            float(settings["funding_bps_per_8h"]), PREDICTION_FUNDING_BPS_PER_8H
        ),
        "forced_for_readiness": True,
    }


def prediction_settlement_cost_config(
    cost_model: Mapping[str, Any] | None,
    *,
    settlement_version: str,
) -> dict[str, Any]:
    """Return the frozen cost model, enforcing a floor for the current policy.

    Older policy rows retain their original assumptions for auditability.  New
    policy rows can never turn fees, slippage, or funding off: doing so made a
    gross price hit look profitable in storage while readiness correctly showed
    the same trade as a cost-adjusted loss.
    """

    frozen = dict(cost_model or {})
    config = {
        "prediction_fee_enabled": bool(frozen.get("fee_enabled", True)),
        "prediction_fee_bps_per_side": float(
            frozen.get("fee_bps_per_side", PREDICTION_FEE_BPS_PER_SIDE)
        ),
        "prediction_slippage_enabled": bool(
            frozen.get("slippage_enabled", True)
        ),
        "prediction_slippage_bps_per_side": float(
            frozen.get(
                "slippage_bps_per_side", PREDICTION_SLIPPAGE_BPS_PER_SIDE
            )
        ),
        "prediction_funding_enabled": bool(frozen.get("funding_enabled", True)),
        "prediction_funding_bps_per_8h": float(
            frozen.get("funding_bps_per_8h", PREDICTION_FUNDING_BPS_PER_8H)
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
    """Return the first stop/target touched by the observed price path.

    When one candle touches both barriers, intrabar ordering is unknowable from
    OHLC data.  The research ledger deliberately assumes the stop was hit
    first, avoiding optimistic look-ahead bias.
    """

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
        # OHLC high/low values are only causal after the candle has closed.
        # In particular, a candle opening exactly at the hard holding cap must
        # never contribute its future range to an earlier virtual exit.
        close_time = open_time + timeframe_ms
        if not (start_ms <= open_time and close_time <= end_ms):
            continue
        if open_price <= 0 or high_price <= 0 or low_price <= 0:
            continue
        normalized.append(
            (open_time, close_time, open_price, high_price, low_price)
        )
    for open_time, close_time, open_price, high_price, low_price in sorted(
        normalized
    ):
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
            AiMonitorPrediction.settlement_version == PREDICTION_SETTLEMENT_VERSION,
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


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _stream_event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _stream_event_datetime(event: Any, key: str, fallback: datetime) -> datetime:
    value = _stream_event_value(event, key)
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).replace(tzinfo=None)


def _stream_domain_payload(event: Any) -> dict[str, Any]:
    """Keep an auditable domain sample without manufacturing a direction score."""

    values = dict(_stream_event_value(event, "values", {}) or {})
    raw = dict(_stream_event_value(event, "raw", {}) or {})
    quality = dict(_stream_event_value(event, "quality", {}) or {})
    payload: dict[str, Any] = {
        "available": True,
        "fresh": not bool(quality.get("stale")),
        "data_quality": 1.0 if bool(quality.get("valid", True)) else 0.35,
        "event_id": str(_stream_event_value(event, "event_id", "") or ""),
        "event_type": str(_stream_event_value(event, "event_type", "") or ""),
        "event_time_ms": _stream_event_value(event, "event_time_ms"),
        "received_at_ms": _stream_event_value(event, "received_at_ms"),
        "values": values,
    }
    # Only propagate an upstream/normalizer score when it is explicit.  A missing
    # score must remain missing so downstream weight renormalisation can degrade
    # safely instead of treating an unknown feed as neutral 50.
    for key in (
        "long_score",
        "short_score",
        "directional_score",
        "score",
        "direction",
        "hard_conflict",
    ):
        candidate = values.get(key, raw.get(key))
        if candidate is not None:
            payload[key] = candidate
    for key in (
        "net_call_premium",
        "net_put_premium",
        "net_volume",
        "gamma",
        "delta",
        "premium",
        "notional",
        "venue",
        "side",
    ):
        candidate = values.get(key, raw.get(key))
        if candidate is not None:
            payload[key] = candidate
    return payload


def _stream_trade_direction(
    values: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    option_contract_required: bool = False,
) -> str | None:
    explicit = str(
        values.get("direction")
        or raw.get("direction")
        or raw.get("sentiment")
        or ""
    ).strip().lower()
    if explicit in {"long", "bull", "bullish", "buy"}:
        return "long"
    if explicit in {"short", "bear", "bearish", "sell"}:
        return "short"
    option_type = str(
        raw.get("option_type")
        or raw.get("put_call")
        or raw.get("contract_type")
        or ""
    ).strip().lower()
    side = str(
        raw.get("side")
        or raw.get("trade_side")
        or raw.get("execution_side")
        or ""
    ).strip().lower()
    buyer_initiated = side in {"ask", "at_ask", "above_ask", "buy", "bought"}
    seller_initiated = side in {"bid", "at_bid", "below_bid", "sell", "sold"}
    if option_type in {"call", "c"} and buyer_initiated:
        return "long"
    if option_type in {"call", "c"} and seller_initiated:
        return "short"
    if option_type in {"put", "p"} and buyer_initiated:
        return "short"
    if option_type in {"put", "p"} and seller_initiated:
        return "long"
    if not option_contract_required and not option_type and buyer_initiated:
        return "long"
    if not option_contract_required and not option_type and seller_initiated:
        return "short"
    return None


def _explicit_bool(*values: Any) -> bool | None:
    """Return an explicitly encoded boolean without treating absence as false."""

    for value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value in {0, 1}:
                return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    return None


def _option_trade_attribute(
    values: Mapping[str, Any], raw: Mapping[str, Any], key: str
) -> bool | None:
    aliases = {
        "opening": ("is_opening", "opening", "is_open", "open_trade"),
        "sweep": ("is_sweep", "sweep", "sweep_order"),
        "multileg": ("is_multileg", "is_multi_leg", "multi_leg", "multileg"),
    }
    explicit = _explicit_bool(
        *(values.get(name) for name in aliases[key]),
        *(raw.get(name) for name in aliases[key]),
    )
    if explicit is not None:
        return explicit
    if key == "opening":
        open_close = str(
            values.get("open_close")
            or raw.get("open_close")
            or raw.get("opening_side")
            or ""
        ).strip().lower()
        if open_close in {"open", "opening", "to_open", "bto", "sto"}:
            return True
        if open_close in {"close", "closing", "to_close", "btc", "stc"}:
            return False
    elif key == "sweep":
        order_type = str(
            values.get("order_type")
            or raw.get("order_type")
            or raw.get("trade_type")
            or ""
        ).strip().lower()
        if order_type:
            return "sweep" in order_type
    else:
        leg_count = _finite_number(values.get("leg_count", raw.get("leg_count")))
        if leg_count is not None:
            return leg_count > 1
        trade_type = str(raw.get("trade_type") or raw.get("strategy") or "").lower()
        if trade_type:
            if any(token in trade_type for token in ("multi", "complex", "spread")):
                return True
            if any(token in trade_type for token in ("single", "outright")):
                return False
    return None


def _option_trade_dte(
    values: Mapping[str, Any], raw: Mapping[str, Any], event: Any
) -> float | None:
    direct = _finite_number(
        values.get(
            "dte",
            raw.get("dte", raw.get("days_to_expiration", raw.get("days_to_expiry"))),
        )
    )
    if direct is not None and direct >= 0:
        return direct
    expiry_value = next(
        (
            candidate
            for candidate in (
                values.get("expiration"),
                values.get("expiry"),
                raw.get("expiration"),
                raw.get("expiry"),
                raw.get("expiration_date"),
            )
            if candidate not in (None, "")
        ),
        None,
    )
    if expiry_value is None:
        return None
    try:
        if isinstance(expiry_value, (int, float)):
            expiry = datetime.fromtimestamp(
                float(expiry_value) / (1_000 if float(expiry_value) > 10**11 else 1),
                tz=UTC,
            ).replace(tzinfo=None)
        else:
            expiry = datetime.fromisoformat(str(expiry_value).replace("Z", "+00:00"))
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(UTC).replace(tzinfo=None)
        event_time = _stream_event_datetime(event, "event_time_ms", utcnow())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return max(0.0, (expiry - event_time).total_seconds() / 86_400)


def _linear_slope(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    mean_x = sum(item[0] for item in points) / len(points)
    mean_y = sum(item[1] for item in points) / len(points)
    denominator = sum((item[0] - mean_x) ** 2 for item in points)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def _option_flow_window_summary(
    series: Sequence[Mapping[str, Any]], minutes: int
) -> dict[str, Any]:
    if not series:
        return {
            "window_minutes": minutes,
            "sample_count": 0,
            "direction": "unknown",
            "insufficient_data": True,
            "same_direction_points": 0,
            "slope_last_6": None,
            "acceleration_last_6": None,
        }
    latest_ms = int(_finite_number(series[-1].get("bucket_time_ms")) or 0)
    cutoff = latest_ms - minutes * 60_000
    window = [
        item
        for item in series
        if int(_finite_number(item.get("bucket_time_ms")) or 0) >= cutoff
    ]
    bullish = sum(float(_finite_number(item.get("bullish_premium")) or 0) for item in window)
    bearish = sum(float(_finite_number(item.get("bearish_premium")) or 0) for item in window)
    total = bullish + bearish
    balance = (bullish - bearish) / total if total > 0 else None
    direction = (
        "long"
        if balance is not None and balance > 0
        else "short"
        if balance is not None and balance < 0
        else "neutral"
        if balance == 0
        else "unknown"
    )
    consecutive = 0
    for item in reversed(window):
        item_direction = str(item.get("direction") or "unknown")
        if direction not in {"long", "short"} or item_direction != direction:
            break
        consecutive += 1
    recent = window[-6:]
    trend_points = [
        (
            (int(_finite_number(item.get("bucket_time_ms")) or 0) - latest_ms) / 60_000,
            float(value),
        )
        for item in recent
        if (value := _finite_number(item.get("directional_balance"))) is not None
    ]
    slope = _linear_slope(trend_points)
    differences = [
        (trend_points[index][0], trend_points[index][1] - trend_points[index - 1][1])
        for index in range(1, len(trend_points))
    ]
    acceleration = _linear_slope(differences)
    return {
        "window_minutes": minutes,
        "sample_count": len(window),
        "classified_sample_count": sum(
            bool(_finite_number(item.get("directional_balance")) is not None)
            for item in window
        ),
        "direction": direction,
        "directional_balance": round(balance, 6) if balance is not None else None,
        "same_direction_points": consecutive,
        "slope_last_6": round(slope, 8) if slope is not None else None,
        "acceleration_last_6": (
            round(acceleration, 8) if acceleration is not None else None
        ),
        "insufficient_data": len(trend_points) < 2,
    }


def _refresh_option_flow_series(result: dict[str, Any], event: Any) -> None:
    event_ms = int(
        _finite_number(_stream_event_value(event, "event_time_ms"))
        or _finite_number(_stream_event_value(event, "received_at_ms"))
        or 0
    )
    bucket_ms = event_ms - event_ms % 60_000 if event_ms else 0
    bullish = float(_finite_number(result.get("bullish_premium")) or 0)
    bearish = float(_finite_number(result.get("bearish_premium")) or 0)
    total = bullish + bearish
    balance = (bullish - bearish) / total if total > 0 else None
    direction = (
        "long"
        if balance is not None and balance > 0
        else "short"
        if balance is not None and balance < 0
        else "neutral"
        if balance == 0
        else "unknown"
    )
    current = {
        "bucket_time_ms": bucket_ms,
        "event_count": int(result.get("event_count") or 0),
        "classified_event_count": int(result.get("classified_event_count") or 0),
        "bullish_premium": round(bullish, 4),
        "bearish_premium": round(bearish, 4),
        "directional_balance": round(balance, 8) if balance is not None else None,
        "direction": direction,
        "ask_premium_share": result.get("ask_premium_share"),
        "bid_premium_share": result.get("bid_premium_share"),
        "opening_share": result.get("opening_share"),
        "sweep_share": result.get("sweep_share"),
        "event_volume_oi_ratio_mean": result.get("event_volume_oi_ratio_mean"),
        "dte_mean": result.get("dte_mean"),
        "data_quality": result.get("data_quality"),
    }
    prior = [
        dict(item)
        for item in (result.get("minute_series") or [])
        if isinstance(item, Mapping)
        and int(_finite_number(item.get("bucket_time_ms")) or -1) != bucket_ms
    ]
    series = sorted(
        [*prior, current],
        key=lambda item: int(_finite_number(item.get("bucket_time_ms")) or 0),
    )[-31:]
    result["minute_series"] = series
    result["window_metrics"] = {
        f"{minutes}m": _option_flow_window_summary(series, minutes)
        for minutes in (5, 15, 30)
    }


def _merge_stream_domain(
    previous: Mapping[str, Any] | None,
    event: Any,
    *,
    domain_name: str,
) -> dict[str, Any]:
    """Aggregate a minute bucket using only directionally defensible evidence."""

    result = dict(previous or {})
    sample = _stream_domain_payload(event)
    values = dict(_stream_event_value(event, "values", {}) or {})
    raw = dict(_stream_event_value(event, "raw", {}) or {})
    result.update(
        {
            "available": True,
            "fresh": sample["fresh"],
            "event_id": sample["event_id"],
            "event_type": sample["event_type"],
            "event_time_ms": sample["event_time_ms"],
            "received_at_ms": sample["received_at_ms"],
            "values": values,
            "event_count": int(result.get("event_count") or 0) + 1,
        }
    )
    if domain_name == "option_flow":
        bullish = float(_finite_number(result.get("bullish_premium")) or 0.0)
        bearish = float(_finite_number(result.get("bearish_premium")) or 0.0)
        raw_bullish = float(
            _finite_number(result.get("raw_bullish_premium")) or bullish
        )
        raw_bearish = float(
            _finite_number(result.get("raw_bearish_premium")) or bearish
        )
        classified = int(result.get("classified_event_count") or 0)
        premium = _finite_number(values.get("premium", raw.get("premium")))
        direction = _stream_trade_direction(
            values,
            raw,
            option_contract_required=True,
        )
        multileg = _option_trade_attribute(values, raw, "multileg")
        direction_weight = 0.35 if multileg is True else 1.0
        if premium is not None and premium > 0 and direction is not None:
            if direction == "long":
                raw_bullish += premium
                bullish += premium * direction_weight
            else:
                raw_bearish += premium
                bearish += premium * direction_weight
            classified += 1

        # Net call/put totals are retained for audit only.  Without execution
        # side they do not establish whether contracts opened/closed or whether
        # the customer bought/sold them, so they must never cast a long/short vote.
        for source_key, target_key in (
            ("net_call_premium", "net_call_premium_latest"),
            ("net_put_premium", "net_put_premium_latest"),
            ("net_volume", "net_volume_latest"),
        ):
            candidate = _finite_number(values.get(source_key, raw.get(source_key)))
            if candidate is not None:
                result[target_key] = candidate

        side = str(
            values.get("side")
            or raw.get("side")
            or raw.get("trade_side")
            or raw.get("execution_side")
            or ""
        ).strip().lower()
        ask_side = side in {"ask", "at_ask", "above_ask", "buy", "bought"}
        bid_side = side in {"bid", "at_bid", "below_bid", "sell", "sold"}
        if ask_side or bid_side:
            count_key = "ask_execution_count" if ask_side else "bid_execution_count"
            result[count_key] = int(result.get(count_key) or 0) + 1
            execution_count = int(result.get("ask_execution_count") or 0) + int(
                result.get("bid_execution_count") or 0
            )
            result["ask_execution_share"] = round(
                int(result.get("ask_execution_count") or 0) / execution_count,
                6,
            )
            result["bid_execution_share"] = round(
                int(result.get("bid_execution_count") or 0) / execution_count,
                6,
            )
        if premium is not None and premium > 0:
            premium_bucket = (
                "ask_premium"
                if ask_side
                else "bid_premium"
                if bid_side
                else "unknown_side_premium"
            )
            result[premium_bucket] = round(
                float(_finite_number(result.get(premium_bucket)) or 0) + premium,
                4,
            )
        ask_premium = float(_finite_number(result.get("ask_premium")) or 0)
        bid_premium = float(_finite_number(result.get("bid_premium")) or 0)
        aggressive_total = ask_premium + bid_premium
        if aggressive_total > 0:
            result["ask_premium_share"] = round(ask_premium / aggressive_total, 6)
            result["bid_premium_share"] = round(bid_premium / aggressive_total, 6)

        for attribute in ("opening", "sweep", "multileg"):
            observed = _option_trade_attribute(values, raw, attribute)
            if observed is None:
                continue
            observed_key = f"{attribute}_observed_count"
            event_key = f"{attribute}_event_count"
            result[observed_key] = int(result.get(observed_key) or 0) + 1
            if premium is not None and premium > 0:
                observed_premium_key = f"{attribute}_observed_premium"
                result[observed_premium_key] = round(
                    float(_finite_number(result.get(observed_premium_key)) or 0)
                    + premium,
                    4,
                )
            if observed:
                result[event_key] = int(result.get(event_key) or 0) + 1
                if premium is not None and premium > 0:
                    premium_key = f"{attribute}_premium"
                    result[premium_key] = round(
                        float(_finite_number(result.get(premium_key)) or 0) + premium,
                        4,
                    )
            result[f"{attribute}_share"] = round(
                int(result.get(event_key) or 0) / int(result[observed_key]),
                6,
            )
            attribute_premium = float(
                _finite_number(result.get(f"{attribute}_premium")) or 0
            )
            observed_premium = float(
                _finite_number(result.get(f"{attribute}_observed_premium")) or 0
            )
            if observed_premium > 0:
                result[f"{attribute}_premium_share"] = round(
                    attribute_premium / observed_premium,
                    6,
                )
        if multileg is not None:
            result["multileg_direction_discount"] = 0.35
            result["last_direction_evidence_weight"] = direction_weight
            result["last_multileg_status"] = "multileg" if multileg else "single_leg"
        else:
            result["last_multileg_status"] = "unknown"

        volume = _finite_number(
            values.get(
                "volume",
                raw.get("volume", raw.get("trade_volume", raw.get("size"))),
            )
        )
        open_interest = _finite_number(
            values.get("open_interest", raw.get("open_interest", raw.get("oi")))
        )
        if volume is not None and volume >= 0:
            result["explicit_volume_total"] = round(
                float(_finite_number(result.get("explicit_volume_total")) or 0)
                + volume,
                4,
            )
        if open_interest is not None and open_interest >= 0:
            result["open_interest_latest"] = open_interest
        if volume is not None and volume >= 0 and open_interest not in (None, 0):
            ratio = volume / open_interest
            ratio_sum = float(_finite_number(result.get("volume_oi_ratio_sum")) or 0)
            ratio_count = int(result.get("volume_oi_ratio_sample_count") or 0) + 1
            result["volume_oi_ratio_sum"] = round(ratio_sum + ratio, 8)
            result["volume_oi_ratio_sample_count"] = ratio_count
            result["event_volume_oi_ratio_mean"] = round(
                (ratio_sum + ratio) / ratio_count,
                8,
            )

        dte = _option_trade_dte(values, raw, event)
        if dte is not None:
            dte_sum = float(_finite_number(result.get("dte_sum")) or 0) + dte
            dte_count = int(result.get("dte_sample_count") or 0) + 1
            result["dte_sum"] = round(dte_sum, 6)
            result["dte_sample_count"] = dte_count
            result["dte_mean"] = round(dte_sum / dte_count, 4)
            previous_min = _finite_number(result.get("dte_min"))
            previous_max = _finite_number(result.get("dte_max"))
            result["dte_min"] = round(
                min(previous_min if previous_min is not None else dte, dte), 4
            )
            result["dte_max"] = round(
                max(previous_max if previous_max is not None else dte, dte), 4
            )

        result["bullish_premium"] = round(bullish, 4)
        result["bearish_premium"] = round(bearish, 4)
        result["raw_bullish_premium"] = round(raw_bullish, 4)
        result["raw_bearish_premium"] = round(raw_bearish, 4)
        result["classified_event_count"] = classified
        total = bullish + bearish
        if total > 0:
            long_score = max(0.0, min(100.0, bullish / total * 100))
            result["long_score"] = round(long_score, 4)
            result["short_score"] = round(100.0 - long_score, 4)
        else:
            result.pop("long_score", None)
            result.pop("short_score", None)
        result["data_quality"] = round(
            (bullish + bearish)
            / max(raw_bullish + raw_bearish, 1.0)
            * classified
            / max(1, int(result["event_count"])),
            4,
        )
        _refresh_option_flow_series(result, event)
    elif domain_name == "institutional_flow":
        snapshot_kind = str(raw.get("snapshot_kind") or "").strip().lower()
        if snapshot_kind == "off_lit_price_levels":
            for key in (
                "total_lit_volume",
                "total_off_lit_volume",
                "off_lit_ratio",
            ):
                candidate = _finite_number(values.get(key, raw.get(key)))
                if candidate is not None:
                    result[key] = candidate
            levels = raw.get("levels")
            if isinstance(levels, list):
                # Price levels are attention/liquidity context, never a vote.
                result["off_lit_price_levels"] = [
                    dict(item) for item in levels[:20] if isinstance(item, Mapping)
                ]
            result["price_levels_available"] = bool(
                result.get("off_lit_price_levels")
                or _finite_number(result.get("off_lit_ratio")) is not None
            )
            result["price_levels_data_quality"] = sample["data_quality"]
            return result
        lit_notional = float(_finite_number(result.get("lit_notional")) or 0.0)
        off_lit_notional = float(
            _finite_number(result.get("off_lit_notional")) or 0.0
        )
        bullish = float(_finite_number(result.get("bullish_notional")) or 0.0)
        bearish = float(_finite_number(result.get("bearish_notional")) or 0.0)
        notional = _finite_number(values.get("notional", sample.get("notional")))
        venue = str(values.get("venue") or sample.get("venue") or "")
        if notional is not None and notional >= 0:
            if venue == "off_lit":
                off_lit_notional += notional
            else:
                lit_notional += notional
            direction = _stream_trade_direction(values, raw)
            if direction == "long":
                bullish += notional
            elif direction == "short":
                bearish += notional
        result.update(
            {
                "lit_notional": round(lit_notional, 4),
                "off_lit_notional": round(off_lit_notional, 4),
                "bullish_notional": round(bullish, 4),
                "bearish_notional": round(bearish, 4),
            }
        )
        directional_total = bullish + bearish
        if directional_total > 0:
            long_score = bullish / directional_total * 100
            result["long_score"] = round(long_score, 4)
            result["short_score"] = round(100.0 - long_score, 4)
            result["data_quality"] = round(
                directional_total / max(lit_notional + off_lit_notional, 1.0), 4
            )
        else:
            # Dark-pool size by itself is attention, not a directional vote.
            result.pop("long_score", None)
            result.pop("short_score", None)
            result["data_quality"] = 0.0
    else:
        # GEX without call-wall/put-wall/gamma-flip context is a volatility
        # structure observation, not a directional long/short score.
        for key in (
            "gamma",
            "delta",
            "strike",
            "open_interest",
            "call_wall",
            "put_wall",
            "gamma_flip",
            "gamma_magnet",
        ):
            candidate = values.get(key, raw.get(key))
            if candidate is not None:
                result[key] = candidate
        if str(raw.get("snapshot_kind") or "") == "gex_levels":
            result["levels_available"] = any(
                result.get(key) is not None
                for key in ("call_wall", "put_wall", "gamma_flip", "gamma_magnet")
            )
        result["data_quality"] = sample["data_quality"]
    return result


def _prior_option_flow_series(
    db: Session,
    *,
    symbol: str,
    bucket_at: datetime,
    feature_version: str,
) -> list[dict[str, Any]]:
    """Best-effort seed for rolling windows after a process/minute boundary."""

    try:
        snapshots = db.scalars(
            select(RealtimeMarketFeatureSnapshot)
            .where(
                RealtimeMarketFeatureSnapshot.symbol == symbol,
                RealtimeMarketFeatureSnapshot.bucket_at < bucket_at,
                RealtimeMarketFeatureSnapshot.bucket_at
                >= bucket_at - timedelta(minutes=30),
                RealtimeMarketFeatureSnapshot.feature_version == feature_version,
            )
            .order_by(RealtimeMarketFeatureSnapshot.bucket_at.desc())
            .limit(30)
        ).all()
    except (AttributeError, TypeError):
        # Lightweight test doubles and older repository adapters need not expose
        # ``scalars``; rolling enrichment is optional and the current minute
        # remains fully auditable without it.
        return []
    by_bucket: dict[int, dict[str, Any]] = {}
    for snapshot in reversed(snapshots):
        flow = dict(snapshot.option_flow_snapshot_json or {})
        nested = flow.get("minute_series") or []
        if nested:
            for item in nested:
                if isinstance(item, Mapping):
                    bucket_ms = int(_finite_number(item.get("bucket_time_ms")) or 0)
                    if bucket_ms:
                        by_bucket[bucket_ms] = dict(item)
            continue
        timestamp_ms = int(
            snapshot.bucket_at.replace(tzinfo=UTC).timestamp() * 1_000
        )
        bullish = float(_finite_number(flow.get("bullish_premium")) or 0)
        bearish = float(_finite_number(flow.get("bearish_premium")) or 0)
        total = bullish + bearish
        balance = (bullish - bearish) / total if total > 0 else None
        by_bucket[timestamp_ms] = {
            "bucket_time_ms": timestamp_ms,
            "event_count": int(flow.get("event_count") or 0),
            "classified_event_count": int(flow.get("classified_event_count") or 0),
            "bullish_premium": bullish,
            "bearish_premium": bearish,
            "directional_balance": balance,
            "direction": (
                "long"
                if balance is not None and balance > 0
                else "short"
                if balance is not None and balance < 0
                else "neutral"
                if balance == 0
                else "unknown"
            ),
            "data_quality": flow.get("data_quality"),
        }
    return [by_bucket[key] for key in sorted(by_bucket)][-30:]


def upsert_realtime_market_feature_from_stream_event(
    db: Session,
    event: Any,
    *,
    feature_version: str = MARKET_FEATURE_VERSION,
) -> RealtimeMarketFeatureSnapshot | None:
    """Merge one canonical UW stream event into its UTC minute feature bucket.

    The caller owns the transaction.  Non-symbol channels (for example the
    market-wide tide) are intentionally retained only in ``market_stream_events``.
    """

    symbol = str(_stream_event_value(event, "symbol", "") or "").strip().upper()
    if not symbol:
        return None
    now = utcnow()
    received_at = _stream_event_datetime(event, "received_at_ms", now)
    event_time = _stream_event_datetime(event, "event_time_ms", received_at)
    bucket_at = event_time.replace(second=0, microsecond=0)
    channel = str(_stream_event_value(event, "channel", "") or "")
    channel_base = channel.partition(":")[0]
    values = dict(_stream_event_value(event, "values", {}) or {})
    raw = dict(_stream_event_value(event, "raw", {}) or {})
    event_quality = dict(_stream_event_value(event, "quality", {}) or {})

    snapshot = db.scalar(
        select(RealtimeMarketFeatureSnapshot).where(
            RealtimeMarketFeatureSnapshot.symbol == symbol,
            RealtimeMarketFeatureSnapshot.bucket_at == bucket_at,
            RealtimeMarketFeatureSnapshot.feature_version == feature_version,
        )
    )
    if snapshot is None:
        session_key = str(macro_market.us_market_session(event_time).get("key") or "unknown")
        if session_key not in {"premarket", "regular", "postmarket", "closed"}:
            session_key = "unknown"
        snapshot = RealtimeMarketFeatureSnapshot(
            symbol=symbol,
            bucket_at=bucket_at,
            market_session=session_key,
            halt_status="unknown",
            data_coverage=Decimal("0.0000"),
            stale_fields_json=[],
            quality_json={},
            feature_version=feature_version,
            captured_at=received_at,
        )
        db.add(snapshot)

    quote = dict(snapshot.quote_snapshot_json or {})
    price = _finite_number(values.get("price"))
    bid = _finite_number(values.get("bid"))
    ask = _finite_number(values.get("ask"))
    has_bid_update = bid is not None and bid > 0
    has_ask_update = ask is not None and ask > 0
    # Quote freshness belongs to one coherent NBBO observation.  A trade-only
    # price update (or a single quote leg) must never make an older bid/ask pair
    # appear executable again.
    has_nbbo_update = has_bid_update and has_ask_update and ask >= bid
    if price is not None and price > 0:
        snapshot.last_price = Decimal(str(price))
        quote["last_price"] = price
        quote["last_trade_received_at_ms"] = _stream_event_value(
            event, "received_at_ms"
        )
        quote["last_trade_event_time_ms"] = _stream_event_value(
            event, "event_time_ms"
        )
    if has_bid_update:
        snapshot.bid = Decimal(str(bid))
        quote["bid"] = bid
    if has_ask_update:
        snapshot.ask = Decimal(str(ask))
        quote["ask"] = ask
    current_bid = _finite_number(snapshot.bid)
    current_ask = _finite_number(snapshot.ask)
    if current_bid is not None and current_ask is not None and current_ask >= current_bid > 0:
        midpoint = (current_bid + current_ask) / 2
        spread_bps = ((current_ask - current_bid) / midpoint) * 10_000 if midpoint else None
        if spread_bps is not None:
            snapshot.spread_bps = Decimal(str(round(spread_bps, 8)))
            quote["spread_bps"] = round(spread_bps, 8)
    age_ms = _finite_number(event_quality.get("age_ms"))
    if has_nbbo_update and age_ms is not None:
        snapshot.quote_age_ms = max(0, int(age_ms))
        quote["quote_age_ms"] = max(0, int(age_ms))
        quote["quote_received_at_ms"] = _stream_event_value(
            event, "received_at_ms"
        )
        quote["quote_event_time_ms"] = _stream_event_value(event, "event_time_ms")
        # Retain the legacy field for older readers, but update it only with a
        # complete NBBO so price ticks cannot reset quote age.
        quote["received_at_ms"] = _stream_event_value(event, "received_at_ms")
        quote["fresh"] = not bool(event_quality.get("stale"))
    if channel_base == "price":
        for key in ("volume", "open", "high", "low", "change", "change_percent"):
            candidate = values.get(key, raw.get(key))
            if candidate is not None:
                quote[key] = candidate
        quote["event_id"] = str(_stream_event_value(event, "event_id", "") or "")
        quote["last_trade_fresh"] = not bool(event_quality.get("stale"))

    bid_size = _finite_number(raw.get("bid_size", raw.get("nbbo_bid_size")))
    ask_size = _finite_number(raw.get("ask_size", raw.get("nbbo_ask_size")))
    if bid_size is not None and ask_size is not None and bid_size + ask_size > 0:
        imbalance = (bid_size - ask_size) / (bid_size + ask_size)
        snapshot.size_imbalance = Decimal(str(round(imbalance, 8)))
        quote["bid_size"] = bid_size
        quote["ask_size"] = ask_size
        quote["size_imbalance"] = round(imbalance, 8)
    snapshot.quote_snapshot_json = quote or None

    if channel_base in {"flow-alerts", "interval_flow", "net_flow", "option_trades"}:
        prior_flow = dict(snapshot.option_flow_snapshot_json or {})
        if not prior_flow.get("minute_series"):
            prior_flow["minute_series"] = _prior_option_flow_series(
                db,
                symbol=symbol,
                bucket_at=bucket_at,
                feature_version=feature_version,
            )
        snapshot.option_flow_snapshot_json = _merge_stream_domain(
            prior_flow,
            event,
            domain_name="option_flow",
        )
    elif channel_base in {"gex", "gex_strike", "gex_strike_expiry"}:
        snapshot.gex_snapshot_json = _merge_stream_domain(
            snapshot.gex_snapshot_json,
            event,
            domain_name="gex",
        )
    elif channel_base in {
        "lit_trades",
        "off_lit_trades",
        "periscope",
        "off_lit_price_levels",
    }:
        snapshot.institutional_flow_snapshot_json = _merge_stream_domain(
            snapshot.institutional_flow_snapshot_json,
            event,
            domain_name="institutional_flow",
        )
    elif channel_base == "trading_halts":
        state = str(values.get("state") or raw.get("state") or "").strip().lower()
        if state in {"halt", "halted", "paused", "pause"}:
            snapshot.halt_status = "halted"
        elif state in {"resume", "resumed", "open", "trading", "clear", "cleared"}:
            snapshot.halt_status = "clear"

    stale_fields = set(snapshot.stale_fields_json or [])
    if bool(event_quality.get("stale")):
        stale_fields.add(channel_base or "unknown")
    else:
        stale_fields.discard(channel_base or "unknown")
    snapshot.stale_fields_json = sorted(stale_fields)

    # A fresh last trade is the minimum viable real-time coverage because the UW
    # price channel does not guarantee NBBO fields.  Bid/ask and optional domains
    # add confidence; they are never fabricated when absent.
    coverage = 0.0
    if snapshot.last_price is not None:
        coverage += 0.55
    if snapshot.bid is not None and snapshot.ask is not None and snapshot.quote_age_ms is not None:
        coverage += 0.15
    if snapshot.option_flow_snapshot_json:
        coverage += 0.12
    if snapshot.gex_snapshot_json:
        coverage += 0.09
    if snapshot.institutional_flow_snapshot_json:
        coverage += 0.09
    snapshot.data_coverage = Decimal(str(round(min(1.0, coverage), 4)))
    quality = dict(snapshot.quality_json or {})
    if channel_base == "trading_halts":
        if state in {"halt", "halted", "paused", "pause"}:
            quality["halted_at"] = received_at.isoformat()
            quality.pop("halt_resumed_at", None)
        elif state in {"resume", "resumed", "open", "trading", "clear", "cleared"}:
            quality["halt_resumed_at"] = received_at.isoformat()
    channels = set(quality.get("channels") or [])
    channels.add(channel_base or channel)
    quality.update(
        {
            "provider": "unusual_whales",
            "channels": sorted(channels),
            "last_event_id": str(_stream_event_value(event, "event_id", "") or ""),
            "last_event_time_ms": _stream_event_value(event, "event_time_ms"),
            "last_received_at_ms": _stream_event_value(event, "received_at_ms"),
            "last_event_valid": bool(event_quality.get("valid", True)),
            "coverage_method": "realtime_price_quote_optional_domains_v2",
        }
    )
    snapshot.quality_json = quality
    snapshot.captured_at = received_at
    return snapshot


def ingest_market_stream_event(
    db: Session,
    event: Any,
    *,
    provider: str = "unusual_whales",
    schema_version: str = "uw_stream_v1",
    update_feature: bool = True,
) -> MarketStreamEvent | None:
    """Idempotently stage a canonical stream event and its feature update.

    This function intentionally does not commit.  The streaming service can call
    it for a batch of events and commit once, avoiding one database transaction
    per websocket frame.  ``None`` means the provider/channel/event identity was
    already persisted.
    """

    channel = str(_stream_event_value(event, "channel", "") or "").strip()
    event_id = str(_stream_event_value(event, "event_id", "") or "").strip()
    if not channel or not event_id:
        raise ValueError("canonical stream event requires channel and event_id")
    dedup_key = event_id[:191]
    exists = db.scalar(
        select(MarketStreamEvent.id).where(
            MarketStreamEvent.provider == provider,
            MarketStreamEvent.channel == channel,
            MarketStreamEvent.dedup_key == dedup_key,
        )
    )
    if exists is not None:
        return None
    received_at = _stream_event_datetime(event, "received_at_ms", utcnow())
    event_time = _stream_event_datetime(event, "event_time_ms", received_at)
    quality = dict(_stream_event_value(event, "quality", {}) or {})
    if bool(quality.get("stale")):
        quality_status = "stale"
    elif not bool(quality.get("valid", True)):
        quality_status = "invalid"
    else:
        quality_status = "valid"
    raw = dict(_stream_event_value(event, "raw", {}) or {})
    sequence = next(
        (
            str(raw[key])
            for key in ("sequence", "sequence_id", "seq", "trade_id", "id")
            if raw.get(key) is not None
        ),
        None,
    )
    row = MarketStreamEvent(
        provider=provider,
        channel=channel,
        symbol=(str(_stream_event_value(event, "symbol", "") or "").strip().upper() or None),
        event_time=event_time,
        received_at=received_at,
        sequence_key=sequence[:96] if sequence else None,
        dedup_key=dedup_key,
        payload_json={
            "event_type": str(_stream_event_value(event, "event_type", "") or ""),
            "values": dict(_stream_event_value(event, "values", {}) or {}),
            "raw": raw,
            "quality": quality,
        },
        schema_version=schema_version,
        quality_status=quality_status,
    )
    db.add(row)
    if update_feature:
        if channel.partition(":")[0] == "market_tide":
            macro_market.record_market_tide_event(event)
        upsert_realtime_market_feature_from_stream_event(db, event)
    return row


def ingest_market_stream_events(
    db: Session,
    events: Sequence[Any],
    *,
    provider: str = "unusual_whales",
) -> dict[str, int]:
    """Stage a bounded event batch; transaction/commit remains caller-owned."""

    accepted = 0
    duplicates = 0
    for event in events:
        if ingest_market_stream_event(db, event, provider=provider) is None:
            duplicates += 1
        else:
            accepted += 1
    return {"accepted": accepted, "duplicates": duplicates}


def realtime_feature_payload(
    snapshot: RealtimeMarketFeatureSnapshot | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize persisted market features without inventing absent domains."""

    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    quote = dict(snapshot.quote_snapshot_json or {})
    typed_quote = {
        "last_price": _finite_number(snapshot.last_price),
        "bid": _finite_number(snapshot.bid),
        "ask": _finite_number(snapshot.ask),
        "spread_bps": _finite_number(snapshot.spread_bps),
        "quote_age_ms": int(snapshot.quote_age_ms)
        if snapshot.quote_age_ms is not None
        else None,
        "size_imbalance": _finite_number(snapshot.size_imbalance),
        "market_session": snapshot.market_session,
    }
    quote.update({key: value for key, value in typed_quote.items() if value is not None})
    return {
        "id": snapshot.id,
        "symbol": snapshot.symbol,
        "bucket_at": snapshot.bucket_at.isoformat(),
        "captured_at": snapshot.captured_at.isoformat(),
        "quote": quote,
        "option_flow": dict(snapshot.option_flow_snapshot_json or {}),
        "gex": dict(snapshot.gex_snapshot_json or {}),
        "institutional_flow": dict(
            snapshot.institutional_flow_snapshot_json or {}
        ),
        "halt_status": snapshot.halt_status,
        "data_coverage": _finite_number(snapshot.data_coverage) or 0.0,
        "stale_fields": list(snapshot.stale_fields_json or []),
        "quality": dict(snapshot.quality_json or {}),
        "feature_version": snapshot.feature_version,
    }


def latest_realtime_feature_snapshots(
    db: Session,
    symbols: Sequence[str],
) -> dict[str, RealtimeMarketFeatureSnapshot]:
    """Load the newest feature bucket per requested symbol in one bounded query."""

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not normalized:
        return {}
    rows = db.scalars(
        select(RealtimeMarketFeatureSnapshot)
        .where(RealtimeMarketFeatureSnapshot.symbol.in_(normalized))
        .order_by(
            RealtimeMarketFeatureSnapshot.bucket_at.desc(),
            RealtimeMarketFeatureSnapshot.id.desc(),
        )
        .limit(max(100, len(normalized) * 4))
    ).all()
    result: dict[str, RealtimeMarketFeatureSnapshot] = {}
    for row in rows:
        result.setdefault(row.symbol.strip().upper(), row)
    return result


def active_market_risk_events(
    db: Session,
    *,
    now: datetime,
    symbols: Sequence[str],
    blocking_before_minutes: int | None = None,
    blocking_after_minutes: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return only events whose configured block window contains ``now``."""

    normalized = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    rows = db.scalars(
        select(MarketRiskEvent)
        .where(
            MarketRiskEvent.status.in_(("scheduled", "active")),
            or_(
                MarketRiskEvent.symbol.is_(None),
                MarketRiskEvent.symbol.in_(normalized),
            ),
            or_(
                MarketRiskEvent.status == "active",
                and_(
                    MarketRiskEvent.scheduled_at >= now - timedelta(days=7),
                    MarketRiskEvent.scheduled_at <= now + timedelta(days=7),
                ),
            ),
        )
        .order_by(MarketRiskEvent.scheduled_at, MarketRiskEvent.id)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {"*": []}
    for row in rows:
        anchor = row.actual_at or row.scheduled_at
        before_seconds = (
            max(0, int(blocking_before_minutes)) * 60
            if blocking_before_minutes is not None
            else max(0, row.blocking_before_seconds)
        )
        after_seconds = (
            max(0, int(blocking_after_minutes)) * 60
            if blocking_after_minutes is not None
            else max(0, row.blocking_after_seconds)
        )
        starts_at = anchor - timedelta(seconds=before_seconds)
        ends_at = anchor + timedelta(seconds=after_seconds)
        active = row.status == "active" or starts_at <= now <= ends_at
        if not active or row.risk_level not in {"high", "critical"}:
            continue
        item = {
            "id": row.public_id,
            "event_type": row.event_type,
            "event_name": row.event_name,
            "symbol": row.symbol,
            "risk_level": row.risk_level,
            "status": row.status,
            "scheduled_at": row.scheduled_at.isoformat(),
            "actual_at": row.actual_at.isoformat() if row.actual_at else None,
            "blocking_starts_at": starts_at.isoformat(),
            "blocking_ends_at": ends_at.isoformat(),
            "provider": row.provider,
        }
        result.setdefault((row.symbol or "*").strip().upper(), []).append(item)
    return result


def _market_domain(
    payload: Mapping[str, Any] | None,
    *,
    direction: str,
) -> dict[str, Any]:
    source = dict(payload or {})
    direction_key = "long" if direction == "long" else "short"
    score = next(
        (
            number
            for key in (
                f"{direction_key}_score",
                f"{direction_key}_directional_score",
                "directional_score",
                "score",
            )
            if (number := _finite_number(source.get(key))) is not None
        ),
        None,
    )
    payload_direction = str(source.get("direction") or "").strip().lower()
    if score is not None and payload_direction in {"long", "short"} and payload_direction != direction:
        score = 100.0 - score
    quality = _finite_number(source.get("data_quality"))
    if quality is None:
        quality = _finite_number(source.get("quality"))
    quality = max(0.0, min(1.0, quality if quality is not None else 0.0))
    available = score is not None and bool(source.get("available", True))
    return {
        "available": available,
        "fresh": bool(source.get("fresh", available)),
        "score": round(max(0.0, min(100.0, score)), 4) if score is not None else None,
        "data_quality": round(quality, 4),
        "hard_conflict": bool(source.get("hard_conflict", False)),
        "source": source,
    }


def apply_enhanced_market_domains(
    legacy_market_flow: Mapping[str, Any],
    feature: RealtimeMarketFeatureSnapshot | Mapping[str, Any] | None,
    *,
    direction: str,
    blend_into_legacy: bool = True,
) -> dict[str, Any]:
    """Blend available domains and renormalize weights when a feed is missing.

    Missing option-flow, GEX, or venue data never becomes a fabricated neutral
    score. The established order-book score remains unchanged until a new domain
    has a finite directional score.
    """

    result = dict(legacy_market_flow or {})
    legacy_hard_conflict = bool(result.get("hard_conflict"))
    normalized = realtime_feature_payload(feature)
    domains = {
        "option_flow": _market_domain(normalized.get("option_flow"), direction=direction),
        "gex": _market_domain(normalized.get("gex"), direction=direction),
        "institutional_flow": _market_domain(
            normalized.get("institutional_flow"), direction=direction
        ),
    }
    legacy_score = _finite_number(result.get("score"))
    legacy_quality = _finite_number(result.get("data_quality")) or 0.0
    legacy_available = bool(result.get("directional_data_available")) and legacy_score is not None
    weighted_domains: list[tuple[float, float, float]] = []
    if legacy_available:
        weighted_domains.append((legacy_score, 0.40, max(0.0, min(1.0, legacy_quality))))
    for key, weight in (("option_flow", 0.35), ("gex", 0.15), ("institutional_flow", 0.10)):
        domain = domains[key]
        if domain["available"]:
            weighted_domains.append(
                (float(domain["score"]), weight, float(domain["data_quality"]))
            )
    if weighted_domains and blend_into_legacy:
        total_weight = sum(weight for _, weight, _ in weighted_domains)
        result["score"] = round(
            sum(score * weight for score, weight, _ in weighted_domains) / total_weight,
            4,
        )
        result["data_quality"] = round(
            sum(quality * weight for _, weight, quality in weighted_domains) / total_weight,
            4,
        )
        result["directional_data_available"] = True
        result["fresh"] = bool(result.get("fresh")) or any(
            domain["available"] and domain["fresh"] for domain in domains.values()
        )
    result["legacy_hard_conflict"] = legacy_hard_conflict
    result["enhanced_hard_conflict"] = any(
        domain["hard_conflict"] for domain in domains.values()
    )
    if blend_into_legacy:
        result["hard_conflict"] = legacy_hard_conflict or bool(
            result["enhanced_hard_conflict"]
        )
    result["domains"] = domains
    result["option_flow"] = dict(normalized.get("option_flow") or {})
    result["gex"] = dict(normalized.get("gex") or {})
    result["institutional_flow"] = dict(normalized.get("institutional_flow") or {})
    result["feature_version"] = normalized.get("feature_version") or MARKET_FEATURE_VERSION
    result["data_coverage"] = _finite_number(normalized.get("data_coverage"))
    return result


def signal_market_quality(
    scan: Mapping[str, Any],
    market: Mapping[str, Any],
    timeframe: str,
    now: datetime,
    *,
    maximum_market_age_seconds: int,
    minimum_feature_quality: float,
    requires_prediction_features: bool,
    enhanced_feature: RealtimeMarketFeatureSnapshot | Mapping[str, Any] | None = None,
    risk_events: Sequence[Mapping[str, Any]] = (),
    maximum_quote_age_ms: int = 2_000,
    maximum_spread_bps: float = 25.0,
    minimum_data_coverage: float = 0.50,
    maximum_source_divergence_bps: float | None = None,
    halt_cooldown_seconds: int = 0,
) -> dict[str, Any]:
    """Evaluate freshness and feature quality without fabricating missing market data."""

    now_ms = _datetime_ms(now)
    ticker_ts = int(market.get("ts") or 0)
    if 0 < ticker_ts < 1_000_000_000_000:
        ticker_ts *= 1_000
    evaluated_at = int(scan.get("evaluated_at") or 0)
    if 0 < evaluated_at < 1_000_000_000_000:
        evaluated_at *= 1_000
    timeframe_seconds = _TIMEFRAME_SECONDS.get(timeframe, 3600)
    bar_close_ms = evaluated_at + timeframe_seconds * 1_000
    ticker_age_seconds = max(0.0, (now_ms - ticker_ts) / 1_000) if ticker_ts else None
    bar_age_seconds = max(0.0, (now_ms - bar_close_ms) / 1_000) if evaluated_at else None
    # The most recent closed bar remains the valid technical input until the
    # next bar closes. The former 15-minute cap incorrectly blocked 1h/4h
    # signals long before a replacement bar could exist.
    bar_age_limit = max(
        maximum_market_age_seconds,
        timeframe_seconds + min(300, max(60, timeframe_seconds // 12)),
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
    normalized_feature = realtime_feature_payload(enhanced_feature)
    quote = dict(normalized_feature.get("quote") or {})
    snapshot_age_ms: float | None = None
    # Prefer the quote's own receive time, then the feature capture time.  The
    # minute bucket is only a conservative legacy fallback; using it for every
    # quote would make a quote received at xx:xx:59 appear almost a minute old.
    received_at_ms = _finite_number(
        quote.get("quote_received_at_ms", quote.get("received_at_ms"))
    )
    evaluated_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if received_at_ms is not None and received_at_ms > 0:
        if received_at_ms < 1_000_000_000_000:
            received_at_ms *= 1_000
        snapshot_age_ms = max(0.0, _datetime_ms(evaluated_now) - received_at_ms)
    snapshot_time_value = (
        normalized_feature.get("captured_at") or normalized_feature.get("bucket_at")
    )
    if snapshot_age_ms is None and snapshot_time_value:
        try:
            snapshot_at = datetime.fromisoformat(
                str(snapshot_time_value).replace("Z", "+00:00")
            )
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=UTC)
            snapshot_age_ms = max(
                0.0,
                (
                    evaluated_now.astimezone(UTC) - snapshot_at.astimezone(UTC)
                ).total_seconds()
                * 1_000,
            )
        except (TypeError, ValueError, OverflowError):
            snapshot_age_ms = None
    # A last trade is useful for charting, but is not an executable reference.
    # Entry decisions require a complete NBBO (bid + ask), a measurable age and
    # a bounded spread.  Keep every execution check present even when the feed
    # is missing so other high-quality domains cannot accidentally satisfy the
    # aggregate coverage threshold and bypass the quote gate.
    bid = _finite_number(quote.get("bid"))
    ask = _finite_number(quote.get("ask"))
    quote_age_ms = _finite_number(quote.get("quote_age_ms"))
    if quote_age_ms is not None and snapshot_age_ms is not None:
        quote_age_ms += snapshot_age_ms
    spread_bps = _finite_number(quote.get("spread_bps"))
    quote_available = bid is not None and ask is not None
    quote_sane = bool(
        quote_available
        and bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
    )
    if spread_bps is None and quote_sane and bid is not None and ask is not None:
        midpoint = (bid + ask) / 2
        if midpoint > 0:
            spread_bps = ((ask - bid) / midpoint) * 10_000
            quote["spread_bps"] = round(spread_bps, 8)
    checks.update(
        {
            "reference_quote_available": quote_available,
            "quote_fresh": bool(
                quote_available
                and quote_age_ms is not None
                and 0 <= quote_age_ms <= maximum_quote_age_ms
            ),
            "spread_acceptable": bool(
                quote_available
                and spread_bps is not None
                and 0 <= spread_bps <= maximum_spread_bps
            ),
            "quote_sane": quote_sane,
        }
    )
    halt_status = str(normalized_feature.get("halt_status") or "unknown").lower()
    halt_cooldown_active = False
    feature_quality_meta = dict(normalized_feature.get("quality") or {})
    resumed_at_value = feature_quality_meta.get("halt_resumed_at")
    if resumed_at_value and halt_cooldown_seconds > 0:
        try:
            resumed_at = datetime.fromisoformat(
                str(resumed_at_value).replace("Z", "+00:00")
            )
            if resumed_at.tzinfo is None:
                resumed_at = resumed_at.replace(tzinfo=UTC)
            evaluated_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            halt_cooldown_active = (
                evaluated_now.astimezone(UTC) - resumed_at.astimezone(UTC)
            ).total_seconds() < halt_cooldown_seconds
        except (TypeError, ValueError, OverflowError):
            halt_cooldown_active = False
    if normalized_feature:
        checks["not_halted"] = (
            halt_status not in {"halted", "cooldown"} and not halt_cooldown_active
        )
        coverage = _finite_number(normalized_feature.get("data_coverage"))
        if coverage is not None:
            checks["data_coverage"] = coverage >= minimum_data_coverage
    else:
        coverage = None
    source_divergence_bps: float | None = None
    enhanced_last = _finite_number(quote.get("last_price"))
    legacy_last = _finite_number(market.get("price"))
    if (
        maximum_source_divergence_bps is not None
        and enhanced_last is not None
        and legacy_last is not None
        and enhanced_last > 0
        and legacy_last > 0
    ):
        source_divergence_bps = abs(enhanced_last / legacy_last - 1.0) * 10_000
        checks["source_price_consistent"] = (
            source_divergence_bps <= maximum_source_divergence_bps
        )
    checks["event_window_clear"] = not bool(risk_events)
    stale_fields = list(normalized_feature.get("stale_fields") or [])
    if not normalized_feature:
        data_status = "unavailable"
    elif not all(checks.values()):
        data_status = "blocked"
    elif stale_fields:
        data_status = "degraded"
    else:
        data_status = "live"
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
        "quote": quote,
        "quote_available": quote_available,
        "quote_rejection_reason": (
            None
            if quote_available
            and checks["quote_fresh"]
            and checks["spread_acceptable"]
            and checks["quote_sane"]
            else "REFERENCE_QUOTE_UNAVAILABLE"
            if not quote_available
            else "REFERENCE_QUOTE_STALE"
            if not checks["quote_fresh"]
            else "REFERENCE_SPREAD_TOO_WIDE"
            if not checks["spread_acceptable"]
            else "REFERENCE_QUOTE_INVALID"
        ),
        "snapshot_age_ms": round(snapshot_age_ms, 3)
        if snapshot_age_ms is not None
        else None,
        "maximum_quote_age_ms": maximum_quote_age_ms,
        "maximum_spread_bps": maximum_spread_bps,
        "halt_status": halt_status,
        "halt_cooldown_active": halt_cooldown_active,
        "halt_cooldown_seconds": max(0, int(halt_cooldown_seconds)),
        "risk_events": [dict(item) for item in risk_events],
        "source_divergence_bps": (
            round(source_divergence_bps, 4)
            if source_divergence_bps is not None
            else None
        ),
        "maximum_source_divergence_bps": maximum_source_divergence_bps,
        "data_coverage": coverage,
        "minimum_data_coverage": minimum_data_coverage,
        "stale_fields": stale_fields,
        "data_status": data_status,
        "feature_version": normalized_feature.get("feature_version")
        or MARKET_FEATURE_VERSION,
    }


def stable_gate_summary(
    market_quality: Mapping[str, Any],
    market_flow: Mapping[str, Any],
    *,
    evaluated_at: datetime,
    policy_mode: str = "gate",
) -> dict[str, Any]:
    """Expose stable gate codes independently from translated UI labels."""

    checks = {
        str(key): bool(value)
        for key, value in dict(market_quality.get("checks") or {}).items()
    }
    # Defensive normalization: current quality payloads always contain these
    # checks, but callers loading transitional rows may only expose the summary
    # fields.  Explicitly unavailable NBBO or an observed halt must never be
    # interpreted as a passing gate because the detailed map was incomplete.
    if "quote_available" in market_quality:
        checks["reference_quote_available"] = bool(
            market_quality.get("quote_available")
        )
    halt_status = str(market_quality.get("halt_status") or "").strip().lower()
    if halt_status in {"halt", "halted", "paused", "pause", "cooldown"} or bool(
        market_quality.get("halt_cooldown_active")
    ):
        checks["not_halted"] = False
    normalized_mode = (
        policy_mode
        if policy_mode in {"disabled", "record", "score", "gate"}
        else "record"
    )
    order_book_gate = market_flow.get("order_book_gate")
    order_book_gate = (
        dict(order_book_gate) if isinstance(order_book_gate, Mapping) else {}
    )
    if order_book_gate:
        checks["order_book_usable"] = bool(order_book_gate.get("quality_passed"))
    checks["directional_conflict_clear"] = not bool(market_flow.get("hard_conflict"))
    failure_codes = {
        "price_available": "QUOTE_PRICE_MISSING",
        "ticker_fresh": "EXECUTION_PRICE_STALE",
        "kline_fresh": "TECHNICAL_BAR_STALE",
        "feature_quality": "TECHNICAL_FEATURE_QUALITY_LOW",
        "reference_quote_available": "REFERENCE_QUOTE_UNAVAILABLE",
        "quote_fresh": "REFERENCE_QUOTE_STALE",
        "spread_acceptable": "REFERENCE_SPREAD_TOO_WIDE",
        "quote_sane": "REFERENCE_QUOTE_INVALID",
        "not_halted": "SYMBOL_HALTED_OR_COOLDOWN",
        "data_coverage": "MARKET_DATA_COVERAGE_LOW",
        "event_window_clear": "HIGH_IMPACT_EVENT_WINDOW",
        "source_price_consistent": "REFERENCE_SOURCE_DIVERGENCE_HIGH",
        "order_book_usable": "BINANCE_ORDER_BOOK_NOT_USABLE",
        "directional_conflict_clear": "MARKET_FLOW_DIRECTION_CONFLICT",
    }
    def failed_codes(values: Mapping[str, bool]) -> list[str]:
        # When NBBO is absent, stale/spread/sanity are unknowable rather than
        # three additional independent failures.  Surface one stable reason.
        quote_missing = values.get("reference_quote_available") is False
        quote_dependent = {"quote_fresh", "spread_acceptable", "quote_sane"}
        return [
            failure_codes.get(key, f"GATE_{key.upper()}_FAILED")
            for key, passed in values.items()
            if not passed and not (quote_missing and key in quote_dependent)
        ]

    observed_blocking_reasons = failed_codes(checks)
    legacy_quality_keys = {
        "price_available",
        "ticker_fresh",
        "kline_fresh",
        "feature_quality",
    }
    # A depth snapshot is an execution aid, not a signal source. Missing or
    # stale depth therefore degrades a research/simulation decision instead of
    # suppressing it. A fresh, strong directional conflict remains a hard veto
    # through ``directional_conflict_clear``; automatic live copy applies the
    # stricter fresh-and-confirming rule immediately before order placement.
    # Binance is the execution and valuation venue for mapped contracts.
    # Finnhub/UW cash-market quotes are cross-venue references: in record and
    # score modes they can adjust confidence and raise warnings, but a closed or
    # delayed US cash feed must not make a fresh Binance contract untradable.
    # Deployments that intentionally require synchronized cash NBBO must opt in
    # to ``gate`` mode.
    # The mapped Binance contract is the execution venue for the core strategy.
    # Cash-market NBBO/flow providers are asynchronous references and can be
    # closed while Binance remains live.  Keep those domains out of the hard
    # execution gate in every policy mode; ``gate`` still applies the strict
    # directional-flow conflict below, while cross-venue basis has its own
    # synchronized-quote eligibility state.
    decision_checks = {
        key: passed
        for key, passed in checks.items()
        if key in legacy_quality_keys
    }
    decision_checks["directional_conflict_clear"] = (
        True
        if normalized_mode == "disabled"
        else not bool(
            market_flow.get("hard_conflict")
            if normalized_mode == "gate"
            else market_flow.get(
                "legacy_hard_conflict", market_flow.get("hard_conflict")
            )
        )
    )
    blocking_reasons = failed_codes(decision_checks)
    warnings: list[str] = []
    if normalized_mode != "disabled" and not bool(market_quality.get("quote_available")) and (
        "REFERENCE_QUOTE_UNAVAILABLE" not in blocking_reasons
    ):
        warnings.append("REFERENCE_QUOTE_UNAVAILABLE")
    if normalized_mode != "disabled":
        for field in market_quality.get("stale_fields") or []:
            warnings.append(f"STALE_FIELD:{field}")
        flow_domains = dict(market_flow.get("domains") or {})
        for key in ("option_flow", "gex", "institutional_flow"):
            if not bool(dict(flow_domains.get(key) or {}).get("available")):
                warnings.append(f"{key.upper()}_UNAVAILABLE")
    if normalized_mode != "disabled":
        warnings.extend(
            f"OBSERVED_ONLY:{code}"
            for code in observed_blocking_reasons
            if code not in blocking_reasons
        )
    passed = not blocking_reasons
    data_status = str(market_quality.get("data_status") or "unavailable")
    status = "blocked" if not passed else "degraded" if warnings or data_status != "live" else "passed"
    return {
        "status": status,
        "passed": passed,
        "checks": checks,
        "decision_checks": decision_checks,
        "blocking_reasons": blocking_reasons,
        "observed_blocking_reasons": observed_blocking_reasons,
        "warnings": warnings,
        "coverage": market_quality.get("data_coverage"),
        "market_quality_passed": all(
            passed
            for key, passed in decision_checks.items()
            if key != "directional_conflict_clear"
        ),
        "policy_mode": normalized_mode,
        "hard_gate_applied": normalized_mode == "gate",
        "execution_safety_gate_applied": bool(order_book_gate),
        "execution_price_source": "binance",
        "reference_quote_role": "observe_and_score",
        "evaluated_at": evaluated_at.isoformat(),
        "decision_version": f"binance_primary_{normalized_mode}_v3",
    }


def prediction_actionability_gate_summary(
    stable_summary: Mapping[str, Any],
    *,
    market_quality: Mapping[str, Any],
    market_environment: Mapping[str, Any],
    news_trigger: Mapping[str, Any],
    order_book_gate: Mapping[str, Any] | None,
    require_new_news: bool,
) -> dict[str, Any]:
    """Add the canonical simulated-entry gates to the execution summary.

    ``market_quality['passed']`` also contains cross-venue cash NBBO checks.
    Those inputs are useful observations, but the simulated Binance entry must
    not be stopped merely because Finnhub/UW is delayed or the cash quote is
    unavailable.  ``stable_gate_summary`` already separates Binance execution
    quality from those reference observations, so this layer must consume that
    exact decision instead of reintroducing the raw aggregate as a hard gate.
    """

    result = dict(stable_summary or {})
    decision_checks = dict(result.get("decision_checks") or {})
    session = dict(market_environment.get("market_session") or {})
    session_key = str(session.get("key") or "unknown").lower()
    event_cluster = dict(news_trigger.get("event_cluster") or {})
    execution_market_quality = bool(
        result.get("market_quality_passed", market_quality.get("passed"))
    )
    raw_market_quality = bool(market_quality.get("passed"))
    macro_direction_aligned = str(
        market_environment.get("resonance") or "unknown"
    ).lower() != "divergent"
    regular_us_session = bool(
        session_key == "regular" and session.get("allows_new_entries") is not False
    )
    actionable_checks = {
        "execution_market_quality": execution_market_quality,
        "actionable_news_trigger": bool(
            not require_new_news or news_trigger.get("has_actionable_new_news")
        ),
        "event_cluster_selected": bool(event_cluster.get("selected", True)),
    }
    frozen_order_book = (
        dict(order_book_gate) if isinstance(order_book_gate, Mapping) else {}
    )
    if frozen_order_book and frozen_order_book.get("quality_passed"):
        actionable_checks["order_book_no_strong_conflict"] = not bool(
            frozen_order_book.get("direction_conflict")
        )
    decision_checks.update(actionable_checks)
    decision_checks["regular_us_session"] = regular_us_session
    decision_checks["macro_direction_aligned"] = macro_direction_aligned
    failure_codes = {
        "execution_market_quality": "EXECUTION_MARKET_QUALITY_BLOCKED",
        "actionable_news_trigger": "NEWS_NOT_ACTIONABLE",
        "event_cluster_selected": "CORRELATED_EVENT_ALREADY_SELECTED",
        "order_book_no_strong_conflict": "MARKET_FLOW_DIRECTION_CONFLICT",
    }
    new_reasons = [
        failure_codes[key]
        for key, passed in actionable_checks.items()
        if not passed
    ]
    blocking_reasons = list(
        dict.fromkeys(
            [*list(result.get("blocking_reasons") or []), *new_reasons]
        )
    )
    passed = not blocking_reasons
    warnings = list(result.get("warnings") or [])
    if execution_market_quality and not raw_market_quality:
        warnings.append("OBSERVED_ONLY:RAW_MARKET_QUALITY_BLOCKED")
    if not regular_us_session:
        warnings.append("OBSERVED_ONLY:NON_REGULAR_US_SESSION")
    if not macro_direction_aligned:
        warnings.append("OBSERVED_ONLY:MACRO_DIRECTION_DIVERGENT")
    warnings = list(dict.fromkeys(warnings))
    result.update(
        {
            "status": "blocked" if not passed else "degraded" if warnings else "passed",
            "passed": passed,
            "decision_checks": decision_checks,
            "blocking_reasons": blocking_reasons,
            "warnings": warnings,
            "market_quality_passed": execution_market_quality,
            "raw_market_quality_passed": raw_market_quality,
            "prediction_actionability_checks": actionable_checks,
            "hard_gate_applied": True,
            "decision_version": OPPORTUNITY_DECISION_VERSION,
        }
    )
    return result


def opportunity_score_components(
    *,
    news_score: float,
    technical_score: float,
    market_flow: Mapping[str, Any],
    base_combined_score: float,
    market_environment: Mapping[str, Any],
    combined_score: float,
    configured_weights: Mapping[str, Any],
    effective_weights: Mapping[str, Any],
    enhanced_domain_scoring: Mapping[str, Any] | None = None,
    signal_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    domains = dict(market_flow.get("domains") or {})
    result = {
        "news": round(float(news_score), 4),
        "technical": round(float(technical_score), 4),
        "market_flow": round(float(market_flow.get("score") or 0), 4),
        "option_flow": dict(domains.get("option_flow") or {}).get("score"),
        "gex": dict(domains.get("gex") or {}).get("score"),
        "institutional_flow": dict(domains.get("institutional_flow") or {}).get("score"),
        "base_combined": round(float(base_combined_score), 4),
        "macro_adjustment": round(float(market_environment.get("adjustment") or 0), 4),
        "combined": round(float(combined_score), 4),
        "configured_weights": {
            str(key): round(float(value), 8) for key, value in configured_weights.items()
        },
        "effective_weights": {
            str(key): round(float(value), 8) for key, value in effective_weights.items()
        },
        "weights_version": OPPORTUNITY_WEIGHTS_VERSION,
    }
    if enhanced_domain_scoring is not None:
        enhanced = dict(enhanced_domain_scoring)
        result["enhanced_domains"] = dict(enhanced.get("domains") or {})
        result["enhanced_configured_weights"] = dict(
            enhanced.get("configured_weights") or {}
        )
        result["enhanced_effective_weights"] = dict(
            enhanced.get("effective_weights") or {}
        )
        result["enhanced_score"] = enhanced.get("score")
        result["missing_enhanced_domains"] = list(
            enhanced.get("missing_domains") or []
        )
        result["weights_version"] = str(
            enhanced.get("weights_version") or OPPORTUNITY_WEIGHTS_VERSION
        )
    if signal_policy is not None:
        result["policy_mode"] = str(signal_policy.get("mode") or "record")
        result["policy_version"] = str(
            signal_policy.get("policy_version") or UNUSUAL_WHALES_SIGNAL_POLICY_VERSION
        )
        result["published_config_version"] = int(
            signal_policy.get("published_version") or 0
        )
    return result


def freeze_opportunity_market_snapshot(
    db: Session,
    opportunity: AiMonitorOpportunity,
    *,
    evidence: Mapping[str, Any],
    feature: RealtimeMarketFeatureSnapshot | Mapping[str, Any] | None,
) -> OpportunityMarketSnapshot:
    """Create the one allowed signal-time snapshot; never update it later."""

    existing = db.scalar(
        select(OpportunityMarketSnapshot).where(
            OpportunityMarketSnapshot.opportunity_id == opportunity.id
        )
    )
    if existing is not None:
        return existing
    normalized = realtime_feature_payload(feature)
    market_flow = dict(evidence.get("market_flow") or {})
    snapshot = OpportunityMarketSnapshot(
        opportunity_id=opportunity.id,
        user_id=opportunity.user_id,
        market_feature_snapshot_id=(
            int(normalized["id"]) if normalized.get("id") is not None else None
        ),
        captured_at=opportunity.discovered_at,
        quote_snapshot_json=dict(
            evidence.get("quote") or normalized.get("quote") or {}
        ),
        option_flow_snapshot_json=dict(
            evidence.get("option_flow")
            or market_flow.get("option_flow")
            or normalized.get("option_flow")
            or {}
        ),
        gex_snapshot_json=dict(
            evidence.get("gex")
            or market_flow.get("gex")
            or normalized.get("gex")
            or {}
        ),
        institutional_flow_snapshot_json=dict(
            evidence.get("institutional_flow")
            or market_flow.get("institutional_flow")
            or normalized.get("institutional_flow")
            or {}
        ),
        macro_snapshot_json=dict(evidence.get("macro_market_snapshot") or {}),
        risk_gate_snapshot_json=dict(evidence.get("gate_summary") or {}),
        score_components_json=dict(evidence.get("score_components") or {}),
        data_quality_json=dict(evidence.get("data_quality") or {}),
        weights_version=str(
            dict(evidence.get("version") or {}).get("weights")
            or OPPORTUNITY_WEIGHTS_VERSION
        ),
        feature_version=str(
            dict(evidence.get("version") or {}).get("feature")
            or normalized.get("feature_version")
            or MARKET_FEATURE_VERSION
        ),
        decision_version=str(
            dict(evidence.get("version") or {}).get("decision")
            or OPPORTUNITY_DECISION_VERSION
        ),
    )
    db.add(snapshot)
    return snapshot


def _frozen_feature_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def record_opportunity_gate_decision(
    db: Session,
    opportunity: AiMonitorOpportunity,
    *,
    analysis_run_id: int,
    decision_at: datetime,
    evidence: Mapping[str, Any],
    feature: RealtimeMarketFeatureSnapshot | Mapping[str, Any] | None,
) -> OpportunityGateDecision:
    """Persist one immutable, idempotent decision per scan and candidate."""

    frozen = dict(evidence or {})
    gate = dict(frozen.get("gate_summary") or {})
    versions = dict(frozen.get("version") or {})
    decision_version = str(
        versions.get("decision")
        or gate.get("decision_version")
        or OPPORTUNITY_DECISION_VERSION
    )
    identity = (
        f"gate:{opportunity.user_id}:{analysis_run_id}:"
        f"{opportunity.id}:{decision_version}"
    )
    dedup_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    for pending in db.new:
        if (
            isinstance(pending, OpportunityGateDecision)
            and pending.dedup_key == dedup_key
        ):
            return pending
    existing = db.scalar(
        select(OpportunityGateDecision).where(
            OpportunityGateDecision.dedup_key == dedup_key
        )
    )
    if existing is not None:
        return existing

    normalized = realtime_feature_payload(feature)
    raw_status = str(gate.get("status") or "").lower()
    if raw_status not in {"passed", "blocked", "degraded", "unavailable"}:
        raw_status = (
            "passed"
            if bool(gate.get("passed"))
            else "blocked"
            if gate
            else "unavailable"
        )
    feature_captured_at = _frozen_feature_timestamp(
        normalized.get("captured_at") or normalized.get("bucket_at")
    )
    event = OpportunityGateDecision(
        opportunity_id=opportunity.id,
        user_id=opportunity.user_id,
        analysis_run_id=analysis_run_id,
        market_feature_snapshot_id=(
            int(normalized["id"]) if normalized.get("id") is not None else None
        ),
        symbol=opportunity.symbol,
        contract_symbol=opportunity.contract_symbol,
        direction=opportunity.direction,
        gate_status=raw_status,
        selected=bool(frozen.get("confirmed")),
        decision_at=(
            decision_at.astimezone(UTC).replace(tzinfo=None)
            if decision_at.tzinfo is not None
            else decision_at
        ),
        feature_captured_at=feature_captured_at,
        blocking_reasons_json=sorted(
            {
                str(reason).strip().upper()
                for reason in gate.get("blocking_reasons", [])
                if str(reason).strip()
            }
        ),
        warnings_json=sorted(
            {
                str(reason).strip().upper()
                for reason in gate.get("warnings", [])
                if str(reason).strip()
            }
        ),
        risk_gate_snapshot_json=gate,
        quote_snapshot_json=dict(
            frozen.get("quote") or normalized.get("quote") or {}
        ),
        market_flow_snapshot_json=dict(frozen.get("market_flow") or {}),
        score_components_json=dict(frozen.get("score_components") or {}),
        data_quality_json=dict(frozen.get("data_quality") or {}),
        feature_version=str(
            versions.get("feature")
            or normalized.get("feature_version")
            or MARKET_FEATURE_VERSION
        ),
        weights_version=str(
            versions.get("weights") or OPPORTUNITY_WEIGHTS_VERSION
        ),
        decision_version=decision_version,
        dedup_key=dedup_key,
    )
    db.add(event)
    return event


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
    macro_entry_allowed: bool = True,
    macro_policy: Mapping[str, Any] | None = None,
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
        "indicator_policy_passed": bool(matched),
        "indicator_strength": indicator_score >= minimum_indicator_score,
        "combined_score": combined_score >= minimum_combined_score,
        "macro_entry_policy": bool(macro_entry_allowed),
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
        "indicator_policy_passed": "技术指标策略组未达到确认门槛",
        "indicator_strength": "技术强度未达到准入线",
        "combined_score": "组合评分未达到准入线",
        "macro_entry_policy": "宏观状态暂停当前方向新增仓位",
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
        "macro_policy": dict(macro_policy or {}),
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
            CompanyProfile.sector,
            CompanyProfile.industry,
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
                "sector": sector,
                "industry": industry,
            }
            for symbol, market_cap, shares_outstanding, source, sector, industry in profile_rows
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
    raw_depth = dict(depth)
    positioning = dict(inputs.get("positioning", {}).get(contract, {}))
    ticker = dict(inputs.get("ticker", {}).get(contract, {}))
    underlying = dict(inputs.get("underlying", {}).get(contract, {}))
    profile = dict(inputs.get("profile", {}).get(equity, {}))
    now_seconds = int(now.replace(tzinfo=UTC).timestamp())
    now_ms = now_seconds * 1_000

    order_book_gate = order_book_gate_snapshot(
        raw_depth,
        direction=direction,
        now_seconds=now_seconds,
    )
    depth_ts = int(raw_depth.get("ts") or 0)
    depth_age_seconds = order_book_gate.get("age_seconds")
    depth_status = str(order_book_gate.get("snapshot_status") or "missing")
    depth_fresh = depth_status == "fresh"
    if not raw_depth:
        depth_unavailable_reason = "BINANCE_DEPTH_SNAPSHOT_MISSING"
    elif not depth_ts:
        depth_unavailable_reason = "BINANCE_DEPTH_TIMESTAMP_MISSING"
    elif depth_status == "invalid":
        depth_unavailable_reason = "BINANCE_DEPTH_CLOCK_SKEW"
    elif not depth_fresh:
        depth_unavailable_reason = "BINANCE_DEPTH_SNAPSHOT_STALE"
    else:
        depth_unavailable_reason = None
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
    legacy_flow_conflict = bool(
        main_force_ratio is not None
        and len(components) >= 2
        and directional_score < 35
    )
    hard_conflict = bool(
        legacy_flow_conflict or order_book_gate.get("direction_conflict")
    )
    confirms_direction = bool(main_force_ratio is not None and directional_score >= 55)
    directional_data_available = main_force_ratio is not None
    flow_fresh = directional_data_available and (depth_fresh or positioning_fresh)

    return {
        "version": "market_flow_proxy_v2",
        "score": round(max(0.0, min(100.0, flow_score)), 4),
        "directional_score": round(directional_score, 4),
        "data_quality": round(data_quality, 4),
        "directional_data_available": directional_data_available,
        "fresh": flow_fresh,
        "freshness": {
            "depth": depth_fresh,
            "positioning": positioning_fresh,
        },
        "depth_status": depth_status,
        "depth_age_seconds": depth_age_seconds,
        "depth_freshness_limit_seconds": 30,
        "depth_unavailable_reason": depth_unavailable_reason,
        "order_book_gate": order_book_gate,
        "order_book_quality_passed": bool(order_book_gate.get("quality_passed")),
        "order_book_direction_clear": bool(order_book_gate.get("direction_clear")),
        "order_book_confirms_direction": bool(
            order_book_gate.get("confirms_direction")
        ),
        "confirms_direction": confirms_direction,
        "legacy_flow_conflict": legacy_flow_conflict,
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


def historical_closed_settlement_price(
    candles: Sequence[Mapping[str, Any]],
    settles_at_ms: int,
    *,
    timeframe_ms: int = 15 * 60 * 1_000,
    not_before_ms: int | None = None,
) -> dict[str, Any] | None:
    """Return the first causally executable candle open at/after a hard cap.

    Using the final closed candle *before* the cap shortened positions by up to
    one full bar and could even select a price from before very short-lived
    predictions.  Waiting for the first bar open at or after the cap makes the
    timestamp executable and guarantees that a hard-cap settlement never
    predates its signal.
    """

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


def _compact_historical_outcome(
    row: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the fields required by list summaries without replaying market evidence."""

    actual_exit_at = row.get("exit_at") or row["due_at"]
    gross_return = float(row.get("directional_return_bps") or 0)
    conservative_cost_config = readiness_cost_config(current_config)
    cost_breakdown = prediction_cost_breakdown(
        row["predicted_at"],
        actual_exit_at,
        conservative_cost_config,
    )
    net_outcome = prediction_net_outcome(
        gross_return,
        float(cost_breakdown["total_cost_bps"]),
    )
    return {
        "direction": row["direction"],
        "technical_confirmed": True,
        "result": str(net_outcome["net_result"]),
        "directional_return_bps": float(
            net_outcome["net_directional_return_bps"]
        ),
        "gross_directional_return_bps": gross_return,
        "estimated_cost_bps": float(cost_breakdown["total_cost_bps"]),
        "fee_cost_bps": float(cost_breakdown["fee_cost_bps"]),
        "slippage_cost_bps": float(cost_breakdown["slippage_cost_bps"]),
        "funding_cost_bps": float(cost_breakdown["funding_cost_bps"]),
        "max_favorable_bps": (
            float(row["max_favorable_bps"])
            if row.get("max_favorable_bps") is not None
            else None
        ),
        "max_adverse_bps": (
            float(row["max_adverse_bps"])
            if row.get("max_adverse_bps") is not None
            else None
        ),
        "readiness_status": row.get("readiness_status"),
        "exit_reason": row.get("exit_reason") or "legacy_horizon_close",
        "exit_detail": row.get("exit_subreason"),
        "exit_protection": {
            "peak_favorable_bps": (
                float(row["peak_favorable_bps_at_exit"])
                if row.get("peak_favorable_bps_at_exit") is not None
                else None
            ),
            "protected_bps": (
                float(row["protected_bps_at_exit"])
                if row.get("protected_bps_at_exit") is not None
                else None
            ),
        },
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
            AiMonitorPrediction.settlement_version == PREDICTION_SETTLEMENT_VERSION,
            AiMonitorPrediction.directional_return_bps.is_not(None),
        )
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
        .limit(5000)
        ).all()
    )
    predictions.reverse()
    gross_returns = [float(item.directional_return_bps or 0) for item in predictions]
    qualifying_cost_config = readiness_cost_config(cost_config)
    costs = [
        prediction_estimated_cost_bps(
            item.predicted_at, item.exit_at or item.due_at, qualifying_cost_config
        )
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
        "cost_settings": {
            **prediction_cost_settings(qualifying_cost_config),
            "forced_for_readiness": True,
        },
        "paper_and_shadow_requirements": [
            "同执行链模拟盘至少 100 笔",
            "影子运行不少于 4 周且保护事故为 0",
            "完成订单对账、部分成交、断线和保护单故障演练",
        ],
        "note": "量化门槛全部通过后也只能进入影子验证；微型实盘仍需人工审批。",
    }


_ABLATION_VARIANT_LABELS = {
    "baseline": "基线",
    "quote_halt": "基线 + Quote/Halt",
    "option_flow": "基线 + Quote/Halt + Option Flow",
    "full": "完整策略",
}
_QUOTE_HALT_CHECK_CODES = {
    "price_available": "QUOTE_PRICE_MISSING",
    "quote_fresh": "REFERENCE_QUOTE_STALE",
    "spread_acceptable": "REFERENCE_SPREAD_TOO_WIDE",
    "quote_sane": "REFERENCE_QUOTE_INVALID",
    "not_halted": "SYMBOL_HALTED_OR_COOLDOWN",
}


def _finnhub_signal_quote_payload(
    row: FinnhubQuoteSnapshot,
    *,
    signal_at: datetime,
) -> dict[str, Any]:
    """Return an audit-safe, last-trade-only Finnhub signal-time quote.

    Finnhub's stored cash-equity snapshot is useful for the session and spot
    context, but it is not an executable NBBO.  Keep that distinction explicit
    so analytics can display the observation without ever treating it as a
    passed bid/ask quality gate.
    """

    reference_at = signal_at
    if reference_at.tzinfo is not None:
        reference_at = reference_at.astimezone(UTC).replace(tzinfo=None)
    fetched_at = row.fetched_at
    if fetched_at.tzinfo is not None:
        fetched_at = fetched_at.astimezone(UTC).replace(tzinfo=None)
    age_ms = max(0, int((reference_at - fetched_at).total_seconds() * 1_000))
    session_key = str(
        macro_market.us_market_session(reference_at.replace(tzinfo=UTC)).get("key")
        or "unknown"
    )
    return {
        "available": True,
        "provider": "finnhub",
        "source": "finnhub_quote_snapshots",
        "price": float(row.price),
        "last_price": float(row.price),
        "change": float(row.change) if row.change is not None else None,
        "change_percent": (
            float(row.change_percent) if row.change_percent is not None else None
        ),
        "day_high": float(row.day_high) if row.day_high is not None else None,
        "day_low": float(row.day_low) if row.day_low is not None else None,
        "day_open": float(row.day_open) if row.day_open is not None else None,
        "previous_close": (
            float(row.previous_close) if row.previous_close is not None else None
        ),
        "volume": float(row.volume) if row.volume is not None else None,
        "source_timestamp": int(row.source_timestamp),
        "fetched_at": fetched_at.replace(tzinfo=UTC).isoformat(),
        "last_trade_age_ms": age_ms,
        "market_session": session_key,
        "live": bool(row.live),
        "last_trade_only": True,
        "nbbo_available": False,
        "quality_status": "last_trade_only",
    }


def _latest_finnhub_signal_quotes(
    db: Session,
    symbols: Sequence[str],
    *,
    signal_at: datetime,
    maximum_age_seconds: int = FINNHUB_SIGNAL_QUOTE_MAX_AGE_SECONDS,
) -> dict[str, FinnhubQuoteSnapshot]:
    """Load the latest persisted Finnhub observation available at signal time."""

    normalized_symbols = sorted(
        {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
    )
    if not normalized_symbols:
        return {}
    reference_at = signal_at
    if reference_at.tzinfo is not None:
        reference_at = reference_at.astimezone(UTC).replace(tzinfo=None)
    minimum_at = reference_at - timedelta(seconds=max(1, int(maximum_age_seconds)))
    rows = db.scalars(
        select(FinnhubQuoteSnapshot)
        .where(
            FinnhubQuoteSnapshot.symbol.in_(normalized_symbols),
            FinnhubQuoteSnapshot.fetched_at <= reference_at,
            FinnhubQuoteSnapshot.fetched_at >= minimum_at,
        )
        .order_by(
            FinnhubQuoteSnapshot.symbol,
            FinnhubQuoteSnapshot.fetched_at.desc(),
            FinnhubQuoteSnapshot.id.desc(),
        )
    ).all()
    latest: dict[str, FinnhubQuoteSnapshot] = {}
    for row in rows:
        latest.setdefault(str(row.symbol).upper(), row)
    return latest


def _historical_finnhub_quote_index(
    db: Session,
    rows: Sequence[tuple[Any, ...]],
    *,
    maximum_age_seconds: int = FINNHUB_SIGNAL_QUOTE_MAX_AGE_SECONDS,
) -> dict[str, list[FinnhubQuoteSnapshot]]:
    """Batch-load point-in-time Finnhub observations for analytics rows."""

    if not rows:
        return {}
    predictions = [row[0] for row in rows]
    symbols = sorted(
        {str(item.symbol or "").strip().upper() for item in predictions if str(item.symbol or "").strip()}
    )
    if not symbols:
        return {}
    signal_times = [
        item.predicted_at.astimezone(UTC).replace(tzinfo=None)
        if item.predicted_at.tzinfo is not None
        else item.predicted_at
        for item in predictions
    ]
    earliest = min(signal_times) - timedelta(seconds=max(1, int(maximum_age_seconds)))
    latest = max(signal_times)
    snapshots = db.scalars(
        select(FinnhubQuoteSnapshot)
        .where(
            FinnhubQuoteSnapshot.symbol.in_(symbols),
            FinnhubQuoteSnapshot.fetched_at >= earliest,
            FinnhubQuoteSnapshot.fetched_at <= latest,
        )
        .order_by(
            FinnhubQuoteSnapshot.symbol,
            FinnhubQuoteSnapshot.fetched_at,
            FinnhubQuoteSnapshot.id,
        )
    ).all()
    indexed: dict[str, list[FinnhubQuoteSnapshot]] = {}
    for row in snapshots:
        indexed.setdefault(str(row.symbol).upper(), []).append(row)
    return indexed


def _point_in_time_finnhub_quote(
    index: Mapping[str, Sequence[FinnhubQuoteSnapshot]],
    *,
    symbol: str,
    signal_at: datetime,
    maximum_age_seconds: int = FINNHUB_SIGNAL_QUOTE_MAX_AGE_SECONDS,
) -> FinnhubQuoteSnapshot | None:
    """Select only observations fetched no later than the historical signal."""

    reference_at = signal_at
    if reference_at.tzinfo is not None:
        reference_at = reference_at.astimezone(UTC).replace(tzinfo=None)
    for row in reversed(index.get(str(symbol or "").strip().upper(), ())):
        fetched_at = row.fetched_at
        if fetched_at.tzinfo is not None:
            fetched_at = fetched_at.astimezone(UTC).replace(tzinfo=None)
        if fetched_at > reference_at:
            continue
        if (reference_at - fetched_at).total_seconds() <= max(1, int(maximum_age_seconds)):
            return row
        break
    return None


def _frozen_analytics_evidence(
    prediction_evidence: Mapping[str, Any],
    snapshot: OpportunityMarketSnapshot | None,
    feature: RealtimeMarketFeatureSnapshot | None = None,
    gate_decision: OpportunityGateDecision | None = None,
) -> dict[str, Any]:
    """Normalize only immutable signal-time evidence for historical analytics."""

    evidence = dict(prediction_evidence or {})
    market_flow = dict(evidence.get("market_flow") or {})
    if snapshot is None:
        return {
            "quote": dict(evidence.get("quote") or {}),
            "flow": market_flow,
            "gex": dict(evidence.get("gex") or market_flow.get("gex") or {}),
            "gate_summary": dict(evidence.get("gate_summary") or {}),
            "score_components": dict(
                evidence.get("score_components")
                or evidence.get("signal_scores")
                or {}
            ),
            "data_quality": dict(evidence.get("data_quality") or {}),
            "version": dict(evidence.get("version") or {}),
            "snapshot_source": "prediction_evidence",
            "point_in_time_replay": {},
        }

    option_flow = dict(snapshot.option_flow_snapshot_json or {})
    gex = dict(snapshot.gex_snapshot_json or {})
    institutional_flow = dict(snapshot.institutional_flow_snapshot_json or {})
    domains = dict(market_flow.get("domains") or {})
    if option_flow:
        domains["option_flow"] = {
            **dict(domains.get("option_flow") or {}),
            **option_flow,
        }
    if gex:
        domains["gex"] = {**dict(domains.get("gex") or {}), **gex}
    if institutional_flow:
        domains["institutional_flow"] = {
            **dict(domains.get("institutional_flow") or {}),
            **institutional_flow,
        }
    market_flow.update(
        {
            "domains": domains,
            "option_flow": option_flow,
            "gex": gex,
            "institutional_flow": institutional_flow,
        }
    )
    gate_payload = (
        {
            "id": gate_decision.public_id,
            "market_feature_snapshot_id": gate_decision.market_feature_snapshot_id,
            "selected": bool(gate_decision.selected),
            "gate_status": gate_decision.gate_status,
            "decision_at": gate_decision.decision_at.isoformat(),
            "feature_captured_at": (
                gate_decision.feature_captured_at.isoformat()
                if gate_decision.feature_captured_at is not None
                else None
            ),
            "risk_gate_snapshot": dict(
                gate_decision.risk_gate_snapshot_json or {}
            ),
            "feature_version": gate_decision.feature_version,
            "weights_version": gate_decision.weights_version,
            "decision_version": gate_decision.decision_version,
        }
        if gate_decision is not None
        else {}
    )
    return {
        "quote": dict(snapshot.quote_snapshot_json or {}),
        "flow": market_flow,
        "gex": gex,
        "gate_summary": dict(snapshot.risk_gate_snapshot_json or {}),
        "score_components": dict(snapshot.score_components_json or {}),
        "data_quality": dict(snapshot.data_quality_json or {}),
        "version": {
            "api": OPPORTUNITY_API_VERSION,
            "feature": snapshot.feature_version,
            "weights": snapshot.weights_version,
            "decision": snapshot.decision_version,
        },
        "snapshot_source": "opportunity_market_snapshot",
        "point_in_time_replay": {
            "snapshot_captured_at": (
                getattr(snapshot, "captured_at", None).isoformat()
                if getattr(snapshot, "captured_at", None) is not None
                else None
            ),
            "market_feature_snapshot_id": getattr(
                snapshot, "market_feature_snapshot_id", None
            ),
            "macro_snapshot": dict(
                getattr(snapshot, "macro_snapshot_json", None) or {}
            ),
            "feature_snapshot": realtime_feature_payload(feature),
            "gate_decision": gate_payload,
        },
    }


def _ablation_domain(
    item: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    flow = dict(item.get("flow") or {})
    domains = dict(flow.get("domains") or {})
    source = dict(domains.get(key) or flow.get(key) or {})
    score_components = dict(item.get("score_components") or {})
    score = _finite_number(source.get("score"))
    if score is None:
        score = _finite_number(score_components.get(key))
    available = bool(source.get("available", score is not None)) and score is not None
    return {
        "available": available,
        "fresh": bool(source.get("fresh", available)),
        "score": score,
        "hard_conflict": bool(source.get("hard_conflict", False)),
    }


def _ablation_signal_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one settled signal without consulting post-signal feature values."""

    gate = dict(item.get("gate_summary") or {})
    checks = {
        str(key): bool(value) for key, value in dict(gate.get("checks") or {}).items()
    }
    quote = dict(item.get("quote") or {})
    quality = dict(item.get("data_quality") or {})
    quote_fields_available = (
        _finite_number(quote.get("quote_age_ms")) is not None
        and (
            (
                _finite_number(quote.get("bid")) is not None
                and _finite_number(quote.get("ask")) is not None
            )
            or _finite_number(quote.get("spread_bps")) is not None
        )
    )
    quote_available = bool(quality.get("quote_available", quote_fields_available))
    quote_check_keys = set(_QUOTE_HALT_CHECK_CODES)
    quote_halt_available = quote_available and quote_check_keys <= set(checks)
    quote_reasons = [
        code
        for key, code in _QUOTE_HALT_CHECK_CODES.items()
        if key in checks and not checks[key]
    ]
    quote_halt_selected = quote_halt_available and not quote_reasons

    option_flow = _ablation_domain(item, "option_flow")
    gex = _ablation_domain(item, "gex")
    institutional_flow = _ablation_domain(item, "institutional_flow")
    option_available = quote_halt_available and option_flow["available"]
    option_reasons = list(quote_reasons)
    if option_flow["available"] and option_flow["hard_conflict"]:
        option_reasons.append("OPTION_FLOW_DIRECTION_CONFLICT")
    option_selected = option_available and quote_halt_selected and not option_reasons

    event_check_available = "event_window_clear" in checks
    full_available = bool(
        option_available
        and gex["available"]
        and institutional_flow["available"]
        and event_check_available
    )
    full_reasons = [
        str(reason).strip().upper()
        for reason in gate.get("blocking_reasons", [])
        if str(reason).strip()
    ]
    if gex["available"] and gex["hard_conflict"]:
        full_reasons.append("GEX_DIRECTION_CONFLICT")
    if institutional_flow["available"] and institutional_flow["hard_conflict"]:
        full_reasons.append("INSTITUTIONAL_FLOW_DIRECTION_CONFLICT")
    full_selected = full_available and bool(gate.get("passed")) and not full_reasons

    return {
        "baseline": {"available": True, "selected": True, "reasons": []},
        "quote_halt": {
            "available": quote_halt_available,
            "selected": quote_halt_selected,
            "reasons": sorted(set(quote_reasons)),
        },
        "option_flow": {
            "available": option_available,
            "selected": option_selected,
            "reasons": sorted(set(option_reasons)),
        },
        "full": {
            "available": full_available,
            "selected": full_selected,
            "reasons": sorted(set(full_reasons)),
        },
        "modules": {
            "quote_halt": quote_halt_available,
            "option_flow": option_flow["available"],
            "gex": gex["available"],
            "institutional_flow": institutional_flow["available"],
            "event_window": event_check_available,
        },
    }


def _ablation_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    returns = [
        float(item["net_directional_return_bps"])
        for item in items
        if _finite_number(item.get("net_directional_return_bps")) is not None
    ]
    wins = sum(str(item.get("result")) == "win" for item in items)
    losses = sum(str(item.get("result")) == "loss" for item in items)
    flats = sum(str(item.get("result")) == "flat" for item in items)
    decisive = wins + losses
    gross_profit = sum(max(value, 0.0) for value in returns)
    gross_loss = abs(sum(min(value, 0.0) for value in returns))
    ordered = sorted(items, key=lambda item: item.get("signal_time") or datetime.min)
    equity = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for item in ordered:
        value = _finite_number(item.get("net_directional_return_bps"))
        if value is None:
            continue
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    ordered_returns = sorted(returns)
    middle = len(ordered_returns) // 2
    median_return = None
    if ordered_returns:
        median_return = (
            ordered_returns[middle]
            if len(ordered_returns) % 2
            else (ordered_returns[middle - 1] + ordered_returns[middle]) / 2
        )
    return {
        "sample_count": len(items),
        "win_count": wins,
        "loss_count": losses,
        "flat_count": flats,
        "hit_rate": round(wins / decisive * 100, 2) if decisive else None,
        "average_net_return_bps": round(sum(returns) / len(returns), 4)
        if returns
        else None,
        "median_net_return_bps": round(median_return, 4)
        if median_return is not None
        else None,
        "profit_factor": round(gross_profit / gross_loss, 4)
        if gross_loss > 0
        else None,
        "maximum_drawdown_bps": round(maximum_drawdown, 4) if returns else None,
    }


def frozen_market_ablation_summary(
    items: Sequence[Mapping[str, Any]],
    *,
    rejection_sample_limit: int = 20,
) -> dict[str, Any]:
    """Compare nested strategies using immutable signal-time evidence.

    A result is marked causal only when every settled item can reproduce its
    score and gate from the exact referenced feature/snapshot/decision rows.
    Otherwise the retained-signal comparison remains observational and the
    causal replay status is explicitly unavailable.
    """

    frozen_items = [dict(item) for item in items]
    # Imported lazily because the historical replay module already imports the
    # real-time scoring module.  The call happens only after both modules are
    # fully initialized and keeps one implementation of score/gate semantics.
    from . import historical_replay

    replay_results = [
        historical_replay.replay_frozen_market_signal(item)
        for item in frozen_items
    ]
    causal_replay = bool(frozen_items) and all(
        result.get("status") == "available" and bool(result.get("causal"))
        for result in replay_results
    )
    states = (
        [dict(result["variant_states"]) for result in replay_results]
        if causal_replay
        else [_ablation_signal_state(item) for item in frozen_items]
    )
    total = len(frozen_items)
    replay_available_count = sum(
        result.get("status") == "available" and bool(result.get("causal"))
        for result in replay_results
    )
    replay_reason_counts: dict[str, int] = {}
    for result in replay_results:
        for reason in result.get("reasons") or []:
            code = str(reason).strip().upper()
            if code:
                replay_reason_counts[code] = replay_reason_counts.get(code, 0) + 1
    variants: list[dict[str, Any]] = []
    rejected_samples: list[dict[str, Any]] = []
    module_available = {
        key: sum(bool(state["modules"][key]) for state in states)
        for key in (
            "quote_halt",
            "option_flow",
            "gex",
            "institutional_flow",
            "event_window",
        )
    }
    for key, label in _ABLATION_VARIANT_LABELS.items():
        available_pairs = [
            (item, state[key])
            for item, state in zip(frozen_items, states, strict=True)
            if state[key]["available"]
        ]
        selected = [item for item, state in available_pairs if state["selected"]]
        rejected = [(item, state) for item, state in available_pairs if not state["selected"]]
        reason_counts: dict[str, int] = {}
        for item, state in rejected:
            reasons = list(state["reasons"] or ["FROZEN_GATE_REJECTED"])
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if len(rejected_samples) < max(0, rejection_sample_limit):
                rejected_samples.append(
                    {
                        "variant": key,
                        "prediction_id": item.get("prediction_id"),
                        "opportunity_id": item.get("id"),
                        "symbol": item.get("symbol"),
                        "direction": item.get("direction"),
                        "signal_time": item.get("signal_time"),
                        "reasons": reasons,
                        "feature_version": dict(item.get("version") or {}).get(
                            "feature"
                        ),
                        "decision_version": dict(item.get("version") or {}).get(
                            "decision"
                        ),
                    }
                )
        available_count = len(available_pairs)
        status = "available" if available_count else "unavailable"
        metrics = _ablation_metrics(selected) if available_count else {
            "sample_count": 0,
            "win_count": None,
            "loss_count": None,
            "flat_count": None,
            "hit_rate": None,
            "average_net_return_bps": None,
            "median_net_return_bps": None,
            "profit_factor": None,
            "maximum_drawdown_bps": None,
        }
        variants.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "total_settled_count": total,
                "available_count": available_count,
                "unavailable_count": total - available_count,
                "data_coverage_rate": round(available_count / total * 100, 2)
                if total
                else 0.0,
                "rejected_count": len(rejected),
                "signal_retention_rate": round(len(selected) / total * 100, 2)
                if total
                else 0.0,
                "rejection_reason_counts": reason_counts,
                **metrics,
            }
        )
    return {
        "schema_version": "frozen_market_ablation.v2",
        "status": "available" if total else "unavailable",
        "method": (
            "point_in_time_frozen_signal_replay"
            if causal_replay
            else "nested_filter_on_frozen_signal_evidence"
        ),
        "evidence_policy": (
            "Causal replay requires the immutable opportunity snapshot, its exact "
            "referenced feature row, and a matching gate decision captured no later "
            "than the signal with identical feature/weights/decision versions."
        ),
        "causal_replay": causal_replay,
        "causal_replay_status": "available" if causal_replay else "unavailable",
        "causal_replay_scope": "generated_predictions_only",
        "causal_replay_coverage": {
            "available_count": replay_available_count,
            "missing_count": total - replay_available_count,
            "coverage_rate": round(replay_available_count / total * 100, 2)
            if total
            else 0.0,
            "unavailable_reason_counts": replay_reason_counts,
        },
        "total_settled_count": total,
        "variants": variants,
        "data_coverage": {
            key: {
                "status": "available" if count else "unavailable",
                "available_count": count,
                "missing_count": total - count,
                "coverage_rate": round(count / total * 100, 2) if total else 0.0,
            }
            for key, count in module_available.items()
        },
        "gate_rejections": {
            "sample_limit": max(0, rejection_sample_limit),
            "returned_count": len(rejected_samples),
            "items": rejected_samples,
        },
        "limitations": [
            "Even causal signal-time replay covers generated predictions only; it never fabricates outcomes for candidates that did not generate a prediction.",
            "A module with no frozen signal-time data is unavailable and is never imputed as a neutral score.",
        ],
    }


def frozen_gate_rejection_summary(
    decisions: Sequence[OpportunityGateDecision],
    *,
    total_count: int | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Summarize immutable blocked gate events without current-market joins."""

    reason_counts: dict[str, int] = {}
    quote_available = 0
    feature_time_available = 0
    module_counts = {
        "option_flow": 0,
        "gex": 0,
        "institutional_flow": 0,
    }
    samples: list[dict[str, Any]] = []
    for decision in decisions:
        reasons = [
            str(reason).strip().upper()
            for reason in (decision.blocking_reasons_json or [])
            if str(reason).strip()
        ]
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        quote = dict(decision.quote_snapshot_json or {})
        if (
            _finite_number(quote.get("quote_age_ms")) is not None
            and (
                (
                    _finite_number(quote.get("bid")) is not None
                    and _finite_number(quote.get("ask")) is not None
                )
                or _finite_number(quote.get("spread_bps")) is not None
            )
        ):
            quote_available += 1
        if decision.feature_captured_at is not None:
            feature_time_available += 1
        flow = dict(decision.market_flow_snapshot_json or {})
        domains = dict(flow.get("domains") or {})
        for key in module_counts:
            domain = dict(domains.get(key) or flow.get(key) or {})
            score = _finite_number(domain.get("score"))
            if bool(domain.get("available", score is not None)) and score is not None:
                module_counts[key] += 1
        if len(samples) < max(0, sample_limit):
            samples.append(
                {
                    "id": decision.public_id,
                    "symbol": decision.symbol,
                    "contract_symbol": decision.contract_symbol,
                    "direction": decision.direction,
                    "gate_status": decision.gate_status,
                    "selected": bool(decision.selected),
                    "decision_at": decision.decision_at,
                    "feature_captured_at": decision.feature_captured_at,
                    "blocking_reasons": reasons,
                    "warnings": list(decision.warnings_json or []),
                    "feature_version": decision.feature_version,
                    "weights_version": decision.weights_version,
                    "decision_version": decision.decision_version,
                }
            )
    loaded = len(decisions)
    total = max(loaded, int(total_count or 0))
    coverage_counts = {
        "quote_halt": quote_available,
        "feature_timestamp": feature_time_available,
        **module_counts,
    }
    return {
        "schema_version": "opportunity_gate_rejections.v1",
        "status": "available" if total else "unavailable",
        "total_count": total,
        "loaded_count": loaded,
        "truncated": loaded < total,
        "reason_counts": reason_counts,
        "data_coverage": {
            key: {
                "available_count": count,
                "missing_count": loaded - count,
                "coverage_rate": round(count / loaded * 100, 2) if loaded else 0.0,
            }
            for key, count in coverage_counts.items()
        },
        "sample_limit": max(0, sample_limit),
        "returned_count": len(samples),
        "items": samples,
        "evidence_policy": "Immutable opportunity_gate_decisions rows only.",
    }


def _prediction_fact_read_model_current(db: Session, user_id: int) -> bool:
    """Verify that the disposable fact projection covers the source ledger."""

    if not read_models_available(db):
        return False
    source_count, source_updated_at = db.execute(
        select(
            func.count(AiMonitorPrediction.id),
            func.max(AiMonitorPrediction.updated_at),
        ).where(AiMonitorPrediction.user_id == user_id)
    ).one()
    fact_count, projected_source_updated_at = db.execute(
        select(
            func.count(AiMonitorPredictionFact.id),
            func.max(AiMonitorPredictionFact.source_updated_at),
        ).where(
            AiMonitorPredictionFact.user_id == user_id,
            AiMonitorPredictionFact.projection_version
            == PREDICTION_FACT_PROJECTION_VERSION,
        )
    ).one()
    if int(source_count or 0) != int(fact_count or 0):
        return False
    if source_updated_at is None:
        return True
    return projected_source_updated_at == source_updated_at


def _prediction_fact_outcome(
    fact: AiMonitorPredictionFact,
    current_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the analytics contract without replaying source evidence JSON."""

    actual_exit_at = fact.exit_at or fact.due_at
    gross_return = float(fact.gross_return_bps or 0)
    conservative_cost_config = readiness_cost_config(current_config)
    cost_breakdown = prediction_cost_breakdown(
        fact.signal_at,
        actual_exit_at,
        conservative_cost_config,
    )
    estimated_cost = float(cost_breakdown["total_cost_bps"])
    net_outcome = prediction_net_outcome(gross_return, estimated_cost)
    net_return = float(net_outcome["net_directional_return_bps"])
    coverage = float(fact.data_coverage) if fact.data_coverage is not None else None
    option_flow_score = (
        float(fact.option_flow_score) if fact.option_flow_score is not None else None
    )
    gex_score = float(fact.gex_score) if fact.gex_score is not None else None
    institutional_score = (
        float(fact.institutional_flow_score)
        if fact.institutional_flow_score is not None
        else None
    )
    option_status = str(
        getattr(fact, "option_flow_status", "not_captured_at_signal")
        or "not_captured_at_signal"
    )
    gex_status = str(
        getattr(fact, "gex_status", "not_captured_at_signal")
        or "not_captured_at_signal"
    )
    institutional_status = str(
        getattr(
            fact,
            "institutional_flow_status",
            "not_captured_at_signal",
        )
        or "not_captured_at_signal"
    )
    quote_age_ms = getattr(fact, "quote_age_ms", None)
    quote_spread_bps = getattr(fact, "quote_spread_bps", None)
    quote_reason = (
        "available"
        if fact.quote_quality == "passed"
        else "reference_quote_stale"
        if fact.quote_quality == "partial" and quote_age_ms is not None
        else "last_trade_only"
        if fact.quote_quality == "partial"
        else "reference_quote_blocked"
        if fact.quote_quality == "blocked"
        else "no_signal_time_quote"
    )
    feature_availability = {
        "unusual_whales_enabled_at_signal": any(
            status
            not in {
                "channel_disabled",
                "uw_disabled_at_signal",
                "not_captured_at_signal",
                "legacy_snapshot_missing",
                "market_feature_not_linked",
            }
            for status in (option_status, gex_status, institutional_status)
        ),
        "snapshot_recorded": bool(fact.snapshot_complete),
        "market_feature_linked": bool(fact.snapshot_complete),
        "quote": {
            "available": fact.quote_quality not in {"missing", "unknown"},
            "nbbo_available": fact.quote_quality == "passed",
            "quality": fact.quote_quality,
            "source": str(getattr(fact, "quote_source", "unknown") or "unknown"),
            "reason": quote_reason,
        },
        "option_flow": {
            "available": option_status == "available",
            "reason": option_status,
        },
        "gex": {
            "available": gex_status == "available",
            "reason": gex_status,
        },
        "institutional_flow": {
            "available": institutional_status == "available",
            "reason": institutional_status,
        },
    }
    return {
        "id": str(fact.opportunity_id),
        "prediction_id": str(fact.prediction_id),
        "symbol": fact.symbol,
        "contract_symbol": fact.contract_symbol,
        "direction": fact.direction,
        "timeframe": fact.timeframe,
        "technical_confirmed": True,
        "api_version": OPPORTUNITY_API_VERSION,
        "gate_summary": {
            "execution_price_source": "binance",
            "status": "historical",
            "passed": True,
            "blocking_reasons": [],
        },
        "score_components": {
            "news": float(fact.news_score) if fact.news_score is not None else None,
            "technical": (
                float(fact.technical_score) if fact.technical_score is not None else None
            ),
            "market_flow": (
                float(fact.market_flow_score)
                if fact.market_flow_score is not None
                else None
            ),
            "combined": (
                float(fact.combined_score) if fact.combined_score is not None else None
            ),
        },
        "quote": {
            "source": str(getattr(fact, "quote_source", "unknown") or "unknown"),
            "market_session": fact.market_session,
            "age_ms": int(quote_age_ms) if quote_age_ms is not None else None,
            "spread_bps": (
                float(quote_spread_bps) if quote_spread_bps is not None else None
            ),
        },
        "flow": {
            "score": (
                float(fact.market_flow_score)
                if fact.market_flow_score is not None
                else None
            ),
            "option_flow": {
                "score": option_flow_score,
                "available": option_status == "available",
            },
            "institutional_flow": {
                "score": institutional_score,
                "available": institutional_status == "available",
            },
        },
        "gex": {"score": gex_score, "available": gex_status == "available"},
        "data_quality": {
            "coverage": coverage,
            "quote_quality": fact.quote_quality,
        },
        "market_snapshot_source": "prediction_fact_read_model",
        "point_in_time_replay": {"status": "projected"},
        "version": {
            "api": OPPORTUNITY_API_VERSION,
            "feature": fact.feature_version,
            "weights": fact.weights_version,
            "decision": fact.decision_version,
        },
        "news_score": float(fact.news_score) if fact.news_score is not None else None,
        "indicator_score": (
            float(fact.technical_score) if fact.technical_score is not None else None
        ),
        "combined_score": (
            float(fact.combined_score) if fact.combined_score is not None else None
        ),
        "option_flow_score": option_flow_score,
        "gex_score": gex_score,
        "institutional_score": institutional_score,
        "market_session": fact.market_session,
        "quote_quality": fact.quote_quality,
        "event_risk": fact.event_risk,
        "data_coverage_pct": coverage,
        "feature_availability": feature_availability,
        "price_source": "binance",
        "entry_price": float(fact.entry_price) if fact.entry_price is not None else None,
        "exit_price": float(fact.exit_price) if fact.exit_price is not None else None,
        "settled_price_at": actual_exit_at,
        "exit_reason": fact.exit_reason or "legacy_horizon_close",
        "exit_detail": fact.exit_subreason,
        "exit_protection": {
            "peak_favorable_bps": (
                float(fact.peak_favorable_bps_at_exit)
                if fact.peak_favorable_bps_at_exit is not None
                else None
            ),
            "protected_bps": (
                float(fact.protected_bps_at_exit)
                if fact.protected_bps_at_exit is not None
                else None
            ),
        },
        "raw_return_bps": gross_return,
        "gross_directional_return_bps": gross_return,
        "estimated_cost_bps": estimated_cost,
        "fee_cost_bps": float(cost_breakdown["fee_cost_bps"]),
        "slippage_cost_bps": float(cost_breakdown["slippage_cost_bps"]),
        "funding_cost_bps": float(cost_breakdown["funding_cost_bps"]),
        "recorded_estimated_cost_bps": float(fact.estimated_cost_bps or 0),
        "directional_return_bps": net_return,
        "net_directional_return_bps": net_return,
        "gross_result": fact.result,
        "result": str(net_outcome["net_result"]),
        "max_favorable_bps": float(fact.mfe_bps) if fact.mfe_bps is not None else None,
        "max_adverse_bps": float(fact.mae_bps) if fact.mae_bps is not None else None,
        "settlement_version": fact.settlement_version,
        "readiness_status": fact.readiness_status,
        "calibration_sample_count": int(fact.calibration_sample_count or 0),
        "expected_gross_edge_bps": (
            float(fact.expected_gross_edge_bps)
            if fact.expected_gross_edge_bps is not None
            else None
        ),
        "expected_edge_lower_bound_bps": (
            float(fact.expected_edge_lower_bound_bps)
            if fact.expected_edge_lower_bound_bps is not None
            else None
        ),
        "signal_time": fact.signal_at,
        "expires_at": fact.due_at,
        "exit_at": actual_exit_at,
        "max_holding_minutes": round(
            max(0.0, (fact.due_at - fact.signal_at).total_seconds() / 60), 2
        ),
        "actual_holding_minutes": round(
            max(0.0, (actual_exit_at - fact.signal_at).total_seconds() / 60), 2
        ),
        "max_holding_bars": max(
            1,
            round(
                (fact.due_at - fact.signal_at).total_seconds()
                / _TIMEFRAME_SECONDS.get(fact.timeframe, 3600)
            ),
        ),
    }


_SETTLEMENT_VERSION_LABELS = {
    "cost_consistent_exit_v8": "V8 · 入场质量与R确认退出",
    "cost_consistent_exit_v7": "V7 · R单位递进锁盈",
    "cost_consistent_exit_v6": "V6 · 成本保护线",
    "horizon_aligned_exit_v5": "V5 · 原移动保护",
}


def normalized_settlement_version(value: str) -> str:
    normalized = str(value or "current").strip().lower()
    if normalized in {"", "current"}:
        return PREDICTION_SETTLEMENT_VERSION
    if normalized == "all":
        return "all"
    return str(value).strip()[:32]


def prediction_settlement_version_catalog(
    db: Session,
    user_id: int,
) -> list[dict[str, Any]]:
    counts = {
        str(version): int(count)
        for version, count in db.execute(
            select(
                AiMonitorPrediction.settlement_version,
                func.count(AiMonitorPrediction.id),
            )
            .where(AiMonitorPrediction.user_id == user_id)
            .group_by(AiMonitorPrediction.settlement_version)
        ).all()
        if version and not str(version).startswith("repair_pending")
    }
    counts.setdefault(PREDICTION_SETTLEMENT_VERSION, 0)

    def sort_key(version: str) -> tuple[int, str]:
        match = re.search(r"_v(\d+)$", version)
        return (int(match.group(1)) if match else -1, version)

    return [
        {
            "value": version,
            "label": _SETTLEMENT_VERSION_LABELS.get(version, version),
            "count": counts[version],
            "current": version == PREDICTION_SETTLEMENT_VERSION,
        }
        for version in sorted(counts, key=sort_key, reverse=True)
    ]


def historical_opportunity_fact_analytics(
    db: Session,
    user_id: int,
    *,
    limit: int,
    page: int,
    date_from: datetime | None,
    date_to: datetime | None,
    timezone_offset_minutes: int,
    symbol: str,
    news_score_min: float,
    indicator_score_min: float,
    combined_score_min: float,
    option_flow_score_min: float,
    gex_score_min: float,
    min_data_coverage: float,
    feature_version: str,
    decision_version: str,
    settlement_version: str,
    direction: str,
    market_session: str,
    quote_quality: str,
    event_risk: str,
    exit_reason: str,
    include_readiness: bool,
) -> dict[str, Any] | None:
    """Serve historical analytics from flat facts, or return None when stale."""

    if not _prediction_fact_read_model_current(db, user_id):
        return None
    current_config = config_data(db.get(AiMonitorConfig, user_id))
    local_date_offset = timedelta(minutes=timezone_offset_minutes)
    normalized_symbol = str(symbol or "").strip().upper()
    selected_settlement_version = normalized_settlement_version(settlement_version)
    conditions = [
        AiMonitorPredictionFact.user_id == user_id,
        AiMonitorPredictionFact.news_score >= Decimal(str(news_score_min)),
        AiMonitorPredictionFact.technical_score >= Decimal(str(indicator_score_min)),
        AiMonitorPredictionFact.combined_score >= Decimal(str(combined_score_min)),
    ]
    if selected_settlement_version != "all":
        conditions.append(
            AiMonitorPredictionFact.settlement_version
            == selected_settlement_version
        )
    if date_from is not None:
        conditions.append(AiMonitorPredictionFact.signal_at >= date_from)
    if date_to is not None:
        conditions.append(AiMonitorPredictionFact.signal_at < date_to)
    if normalized_symbol:
        conditions.append(
            or_(
                func.upper(AiMonitorPredictionFact.symbol) == normalized_symbol,
                func.upper(AiMonitorPredictionFact.contract_symbol) == normalized_symbol,
            )
        )
    if direction in {"long", "short"}:
        conditions.append(AiMonitorPredictionFact.direction == direction)
    if option_flow_score_min > 0:
        conditions.append(
            AiMonitorPredictionFact.option_flow_score
            >= Decimal(str(option_flow_score_min))
        )
    if gex_score_min > 0:
        conditions.append(
            AiMonitorPredictionFact.gex_score >= Decimal(str(gex_score_min))
        )
    if min_data_coverage > 0:
        conditions.append(
            AiMonitorPredictionFact.data_coverage
            >= Decimal(str(min_data_coverage))
        )
    if feature_version:
        conditions.append(AiMonitorPredictionFact.feature_version == feature_version)
    if decision_version:
        conditions.append(AiMonitorPredictionFact.decision_version == decision_version)
    if market_session != "all":
        conditions.append(AiMonitorPredictionFact.market_session == market_session)
    if quote_quality != "all":
        conditions.append(AiMonitorPredictionFact.quote_quality == quote_quality)
    if event_risk != "all":
        conditions.append(AiMonitorPredictionFact.event_risk == event_risk)
    if exit_reason != "all":
        conditions.append(
            or_(
                func.lower(AiMonitorPredictionFact.exit_reason).contains(
                    exit_reason.lower()
                ),
                func.lower(AiMonitorPredictionFact.exit_subreason).contains(
                    exit_reason.lower()
                ),
            )
        )
    completed_conditions = [
        *conditions,
        AiMonitorPredictionFact.prediction_status == "completed",
        AiMonitorPredictionFact.result.is_not(None),
        AiMonitorPredictionFact.entry_price.is_not(None),
        AiMonitorPredictionFact.exit_price.is_not(None),
    ]
    facts = list(
        db.scalars(
            select(AiMonitorPredictionFact)
            .where(*completed_conditions)
            .order_by(
                AiMonitorPredictionFact.signal_at.desc(),
                AiMonitorPredictionFact.prediction_id.desc(),
            )
        ).all()
    )
    outcomes = [_prediction_fact_outcome(fact, current_config) for fact in facts]
    summary = summarize_historical_opportunities(outcomes)
    exit_reason_counts: dict[str, int] = {}
    for outcome in outcomes:
        key = str(outcome.get("exit_detail") or outcome.get("exit_reason") or "unknown")
        exit_reason_counts[key] = exit_reason_counts.get(key, 0) + 1
    status_counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(
                AiMonitorPredictionFact.prediction_status,
                func.count(AiMonitorPredictionFact.id),
            )
            .where(*conditions)
            .group_by(AiMonitorPredictionFact.prediction_status)
        ).all()
    }
    summary.update(
        {
            "exit_reason_counts": exit_reason_counts,
            "settlement_policy_version": selected_settlement_version,
            "discarded_unavailable_count": status_counts.get("unavailable", 0),
            "pending_count": status_counts.get("pending", 0),
            "total_prediction_count": sum(status_counts.values()),
        }
    )
    page_size = max(1, int(limit))
    total_items = len(outcomes)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    current_page = min(max(1, int(page)), total_pages)
    page_start = (current_page - 1) * page_size
    configured_cost_settings = prediction_cost_settings(current_config)
    conservative_cost_config = readiness_cost_config(current_config)
    active_cost_settings = prediction_cost_settings(conservative_cost_config)
    result: dict[str, Any] = {
        "summary": summary,
        "ablation": {
            "schema_version": "frozen_market_ablation.v1",
            "status": "deferred",
            "method": "separate_read_model",
            "total_settled_count": total_items,
            "variants": [],
            "gate_rejections": {},
        },
        "items": outcomes[page_start : page_start + page_size],
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total": total_items,
            "total_pages": total_pages,
            "has_previous": current_page > 1,
            "has_next": current_page < total_pages,
        },
        "cost_config": {
            **active_cost_settings,
            "configured": configured_cost_settings,
            "forced_conservative_floor": True,
            "example_one_hour_total_bps": prediction_estimated_cost_bps(
                datetime(2026, 1, 1),
                datetime(2026, 1, 1) + timedelta(hours=1),
                conservative_cost_config,
            ),
        },
        "settlement_versions": prediction_settlement_version_catalog(db, user_id),
        "filters": {
            "date_from": (
                (date_from + local_date_offset).date().isoformat() if date_from else None
            ),
            "date_to": (
                (date_to + local_date_offset - timedelta(days=1)).date().isoformat()
                if date_to
                else None
            ),
            "timezone_offset_minutes": timezone_offset_minutes,
            "symbol": normalized_symbol,
            "news_score_min": float(news_score_min),
            "indicator_score_min": float(indicator_score_min),
            "combined_score_min": float(combined_score_min),
            "option_flow_score_min": float(option_flow_score_min),
            "gex_score_min": float(gex_score_min),
            "min_data_coverage": float(min_data_coverage),
            "feature_version": feature_version,
            "decision_version": decision_version,
            "settlement_version": selected_settlement_version,
            "direction": direction,
            "market_session": market_session,
            "quote_quality": quote_quality,
            "event_risk": event_risk,
            "exit_reason": exit_reason,
        },
        "query_mode": "prediction_fact_read_model",
        "note": "按 Binance 映射合约入场、退出及结算价格统计；外部行情只作为特征证据。",
    }
    if include_readiness:
        result["readiness"] = strategy_readiness_report(db, user_id, current_config)
    return result


def historical_opportunity_analytics(
    db: Session,
    _repository: MonitorRepository,
    user_id: int,
    *,
    limit: int = 300,
    page: int = 1,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    timezone_offset_minutes: int = 0,
    symbol: str = "",
    news_score_min: float = 0.0,
    indicator_score_min: float = 0.0,
    combined_score_min: float = 0.0,
    option_flow_score_min: float = 0.0,
    gex_score_min: float = 0.0,
    min_data_coverage: float = 0.0,
    feature_version: str = "",
    decision_version: str = "",
    settlement_version: str = "current",
    direction: str = "all",
    market_session: str = "all",
    quote_quality: str = "all",
    event_risk: str = "all",
    exit_reason: str = "all",
    include_readiness: bool = True,
    include_ablation: bool = True,
) -> dict[str, Any]:
    """Summarize completed virtual predictions without re-settling opportunities."""

    current_config = config_data(db.get(AiMonitorConfig, user_id))
    local_date_offset = timedelta(minutes=timezone_offset_minutes)
    configured_cost_settings = prediction_cost_settings(current_config)
    conservative_cost_config = readiness_cost_config(current_config)
    active_cost_settings = prediction_cost_settings(conservative_cost_config)
    selected_settlement_version = normalized_settlement_version(settlement_version)
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
    if selected_settlement_version != "all":
        conditions.append(
            AiMonitorPrediction.settlement_version
            == selected_settlement_version
        )
    if date_from is not None:
        conditions.append(AiMonitorPrediction.predicted_at >= date_from)
    if date_to is not None:
        conditions.append(AiMonitorPrediction.predicted_at < date_to)
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol:
        conditions.append(
            or_(
                func.upper(AiMonitorPrediction.symbol) == normalized_symbol,
                func.upper(AiMonitorPrediction.contract_symbol) == normalized_symbol,
            )
        )
    if direction in {"long", "short"}:
        conditions.append(AiMonitorPrediction.direction == direction)
    conditions.append(
        AiMonitorPrediction.confidence_score >= Decimal(str(combined_score_min))
    )
    requires_evidence_scan = bool(
        include_ablation
        or option_flow_score_min > 0
        or gex_score_min > 0
        or min_data_coverage > 0
        or feature_version
        or decision_version
        or market_session != "all"
        or quote_quality != "all"
        or event_risk != "all"
        or exit_reason != "all"
    )
    completed_conditions = [
        *conditions,
        AiMonitorPrediction.status == "completed",
        AiMonitorPrediction.result.is_not(None),
        AiMonitorPrediction.entry_price.is_not(None),
        AiMonitorPrediction.exit_price.is_not(None),
    ]
    compact_rows: list[Mapping[str, Any]] = []
    page_size = max(1, int(limit))
    requested_page = max(1, int(page))
    total_items: int | None = None
    if not requires_evidence_scan:
        compact_statement = (
            select(
                AiMonitorPrediction.id.label("prediction_id"),
                AiMonitorPrediction.direction,
                AiMonitorPrediction.directional_return_bps,
                AiMonitorPrediction.predicted_at,
                AiMonitorPrediction.exit_at,
                AiMonitorPrediction.due_at,
                AiMonitorPrediction.max_favorable_bps,
                AiMonitorPrediction.max_adverse_bps,
                AiMonitorPrediction.readiness_status,
                AiMonitorPrediction.exit_reason,
                AiMonitorPrediction.exit_subreason,
                AiMonitorPrediction.peak_favorable_bps_at_exit,
                AiMonitorPrediction.protected_bps_at_exit,
            )
            .join(
                AiMonitorOpportunity,
                AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
            )
            .where(*completed_conditions)
            .order_by(
                AiMonitorPrediction.predicted_at.desc(),
                AiMonitorPrediction.id.desc(),
            )
        )
        compact_rows = list(db.execute(compact_statement).mappings().all())
        total_items = len(compact_rows)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        current_page = min(requested_page, total_pages)
        page_start = (current_page - 1) * page_size
        page_prediction_ids = [
            int(row["prediction_id"])
            for row in compact_rows[page_start : page_start + page_size]
        ]
    statement = (
        select(
            AiMonitorPrediction,
            AiMonitorOpportunity,
            OpportunityMarketSnapshot,
        )
        .join(
            AiMonitorOpportunity,
            AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
        )
        .outerjoin(
            OpportunityMarketSnapshot,
            OpportunityMarketSnapshot.opportunity_id == AiMonitorOpportunity.id,
        )
        .where(
            *completed_conditions,
        )
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
    )
    if not requires_evidence_scan:
        if not page_prediction_ids:
            rows = []
        else:
            evidence_keys = (
                "settlement",
                "market_flow",
                "quote",
                "gex",
                "gate_summary",
                "score_components",
                "signal_scores",
                "data_quality",
                "version",
                "market_environment",
                "score_snapshot",
                "risk_events",
                "max_holding",
                "unusual_whales_policy",
            )
            page_statement = (
                select(
                    AiMonitorPrediction.id.label("prediction_id"),
                    AiMonitorPrediction.public_id.label("prediction_public_id"),
                    AiMonitorPrediction.symbol,
                    AiMonitorPrediction.contract_symbol,
                    AiMonitorPrediction.direction,
                    AiMonitorPrediction.timeframe,
                    AiMonitorPrediction.signal_news_score,
                    AiMonitorPrediction.signal_indicator_score,
                    AiMonitorPrediction.confidence_score,
                    AiMonitorPrediction.entry_price,
                    AiMonitorPrediction.exit_price,
                    AiMonitorPrediction.predicted_at,
                    AiMonitorPrediction.exit_at,
                    AiMonitorPrediction.due_at,
                    AiMonitorPrediction.raw_return_bps,
                    AiMonitorPrediction.directional_return_bps,
                    AiMonitorPrediction.estimated_cost_bps,
                    AiMonitorPrediction.max_favorable_bps,
                    AiMonitorPrediction.max_adverse_bps,
                    AiMonitorPrediction.result,
                    AiMonitorPrediction.settlement_version,
                    AiMonitorPrediction.readiness_status,
                    AiMonitorPrediction.calibration_sample_count,
                    AiMonitorPrediction.expected_gross_edge_bps,
                    AiMonitorPrediction.expected_edge_lower_bound_bps,
                    AiMonitorPrediction.exit_reason,
                    AiMonitorPrediction.exit_subreason,
                    AiMonitorPrediction.peak_favorable_bps_at_exit,
                    AiMonitorPrediction.protected_bps_at_exit,
                    AiMonitorOpportunity.id.label("opportunity_id"),
                    AiMonitorOpportunity.public_id.label("opportunity_public_id"),
                    AiMonitorOpportunity.news_score.label("opportunity_news_score"),
                    AiMonitorOpportunity.indicator_score.label(
                        "opportunity_indicator_score"
                    ),
                    OpportunityMarketSnapshot.id.label("snapshot_id"),
                    OpportunityMarketSnapshot.market_feature_snapshot_id.label(
                        "snapshot_feature_id"
                    ),
                    OpportunityMarketSnapshot.feature_version.label(
                        "snapshot_feature_version"
                    ),
                    OpportunityMarketSnapshot.weights_version.label(
                        "snapshot_weights_version"
                    ),
                    OpportunityMarketSnapshot.decision_version.label(
                        "snapshot_decision_version"
                    ),
                    OpportunityMarketSnapshot.quote_snapshot_json.label(
                        "snapshot_quote"
                    ),
                    OpportunityMarketSnapshot.option_flow_snapshot_json.label(
                        "snapshot_option_flow"
                    ),
                    OpportunityMarketSnapshot.gex_snapshot_json.label(
                        "snapshot_gex"
                    ),
                    OpportunityMarketSnapshot.institutional_flow_snapshot_json.label(
                        "snapshot_institutional_flow"
                    ),
                    OpportunityMarketSnapshot.risk_gate_snapshot_json.label(
                        "snapshot_risk_gate"
                    ),
                    OpportunityMarketSnapshot.score_components_json.label(
                        "snapshot_score_components"
                    ),
                    OpportunityMarketSnapshot.data_quality_json.label(
                        "snapshot_data_quality"
                    ),
                    OpportunityMarketSnapshot.macro_snapshot_json.label(
                        "snapshot_macro"
                    ),
                    OpportunityMarketSnapshot.captured_at.label("snapshot_captured_at"),
                    *[
                        func.json_extract(
                            AiMonitorPrediction.evidence_json,
                            f"$.{key}",
                        ).label(f"evidence_{key}")
                        for key in evidence_keys
                    ],
                )
                .join(
                    AiMonitorOpportunity,
                    AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
                )
                .outerjoin(
                    OpportunityMarketSnapshot,
                    OpportunityMarketSnapshot.opportunity_id
                    == AiMonitorOpportunity.id,
                )
                .where(AiMonitorPrediction.id.in_(page_prediction_ids))
                .order_by(
                    AiMonitorPrediction.predicted_at.desc(),
                    AiMonitorPrediction.id.desc(),
                )
            )
            page_rows = list(db.execute(page_statement).mappings().all())

            def decoded_json(value: Any, fallback: Any) -> Any:
                if value is None:
                    return fallback
                if isinstance(value, (dict, list)):
                    return value
                try:
                    return json.loads(str(value))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return fallback

            rows = []
            for row in page_rows:
                evidence = {
                    key: decoded_json(row.get(f"evidence_{key}"), None)
                    for key in evidence_keys
                    if row.get(f"evidence_{key}") is not None
                }
                prediction = SimpleNamespace(
                    id=row["prediction_id"],
                    public_id=row["prediction_public_id"],
                    symbol=row["symbol"],
                    contract_symbol=row["contract_symbol"],
                    direction=row["direction"],
                    timeframe=row["timeframe"],
                    signal_news_score=row["signal_news_score"],
                    signal_indicator_score=row["signal_indicator_score"],
                    confidence_score=row["confidence_score"],
                    entry_price=row["entry_price"],
                    exit_price=row["exit_price"],
                    predicted_at=row["predicted_at"],
                    exit_at=row["exit_at"],
                    due_at=row["due_at"],
                    raw_return_bps=row["raw_return_bps"],
                    directional_return_bps=row["directional_return_bps"],
                    estimated_cost_bps=row["estimated_cost_bps"],
                    max_favorable_bps=row["max_favorable_bps"],
                    max_adverse_bps=row["max_adverse_bps"],
                    result=row["result"],
                    settlement_version=row["settlement_version"],
                    readiness_status=row["readiness_status"],
                    calibration_sample_count=row["calibration_sample_count"],
                    expected_gross_edge_bps=row["expected_gross_edge_bps"],
                    expected_edge_lower_bound_bps=row[
                        "expected_edge_lower_bound_bps"
                    ],
                    exit_reason=row["exit_reason"],
                    exit_subreason=row["exit_subreason"],
                    peak_favorable_bps_at_exit=row[
                        "peak_favorable_bps_at_exit"
                    ],
                    protected_bps_at_exit=row["protected_bps_at_exit"],
                    evidence_json=evidence,
                )
                opportunity = SimpleNamespace(
                    id=row["opportunity_id"],
                    public_id=row["opportunity_public_id"],
                    news_score=row["opportunity_news_score"],
                    indicator_score=row["opportunity_indicator_score"],
                )
                market_snapshot = (
                    SimpleNamespace(
                        id=row["snapshot_id"],
                        opportunity_id=row["opportunity_id"],
                        market_feature_snapshot_id=row["snapshot_feature_id"],
                        feature_version=row["snapshot_feature_version"],
                        weights_version=row["snapshot_weights_version"],
                        decision_version=row["snapshot_decision_version"],
                        quote_snapshot_json=row["snapshot_quote"],
                        option_flow_snapshot_json=row["snapshot_option_flow"],
                        gex_snapshot_json=row["snapshot_gex"],
                        institutional_flow_snapshot_json=row[
                            "snapshot_institutional_flow"
                        ],
                        risk_gate_snapshot_json=row["snapshot_risk_gate"],
                        score_components_json=row["snapshot_score_components"],
                        data_quality_json=row["snapshot_data_quality"],
                        macro_snapshot_json=row["snapshot_macro"],
                        captured_at=row["snapshot_captured_at"],
                    )
                    if row["snapshot_id"] is not None
                    else None
                )
                rows.append((prediction, opportunity, market_snapshot))
    else:
        rows = db.execute(statement).all()
    finnhub_quote_index = _historical_finnhub_quote_index(db, rows)
    opportunity_ids = sorted(
        {
            int(opportunity.id)
            for _prediction, opportunity, _snapshot in rows
            if opportunity.id is not None
        }
    )
    gate_decisions = (
        list(
            db.scalars(
                select(OpportunityGateDecision)
                .where(OpportunityGateDecision.opportunity_id.in_(opportunity_ids))
                .order_by(
                    OpportunityGateDecision.opportunity_id,
                    OpportunityGateDecision.decision_at,
                    OpportunityGateDecision.id,
                )
            ).all()
        )
        if opportunity_ids and requires_evidence_scan
        else []
    )
    decisions_by_opportunity: dict[int, list[OpportunityGateDecision]] = {}
    for decision in gate_decisions:
        decisions_by_opportunity.setdefault(decision.opportunity_id, []).append(
            decision
        )
    feature_ids = {
        int(snapshot.market_feature_snapshot_id)
        for _prediction, _opportunity, snapshot in rows
        if snapshot is not None and snapshot.market_feature_snapshot_id is not None
    }
    feature_ids.update(
        int(decision.market_feature_snapshot_id)
        for decision in gate_decisions
        if decision.market_feature_snapshot_id is not None
    )
    features_by_id = (
        {
            int(feature.id): feature
            for feature in db.scalars(
                select(RealtimeMarketFeatureSnapshot).where(
                    RealtimeMarketFeatureSnapshot.id.in_(sorted(feature_ids))
                )
            ).all()
        }
        if feature_ids
        else {}
    )
    outcomes: list[dict[str, Any]] = []
    for prediction, opportunity, market_snapshot in rows:
        prediction_evidence = dict(prediction.evidence_json or {})
        eligible_decisions = [
            decision
            for decision in decisions_by_opportunity.get(opportunity.id, [])
            if decision.decision_at <= prediction.predicted_at
        ]
        if market_snapshot is not None:
            version_matched_decisions = [
                decision
                for decision in eligible_decisions
                if decision.feature_version == market_snapshot.feature_version
                and decision.weights_version == market_snapshot.weights_version
                and decision.decision_version == market_snapshot.decision_version
            ]
            if version_matched_decisions:
                eligible_decisions = version_matched_decisions
        gate_decision = (
            max(eligible_decisions, key=lambda decision: (decision.decision_at, decision.id))
            if eligible_decisions
            else None
        )
        feature_id = (
            market_snapshot.market_feature_snapshot_id
            if market_snapshot is not None
            else None
        )
        market_feature = (
            features_by_id.get(int(feature_id)) if feature_id is not None else None
        )
        frozen_market = _frozen_analytics_evidence(
            prediction_evidence,
            market_snapshot,
            market_feature,
            gate_decision,
        )
        frozen_quote = dict(frozen_market.get("quote") or {})
        finnhub_quote_row = None
        if not any(
            _finite_number(frozen_quote.get(key)) is not None
            for key in ("last_price", "price", "bid", "ask", "midpoint")
        ):
            finnhub_quote_row = _point_in_time_finnhub_quote(
                finnhub_quote_index,
                symbol=prediction.symbol,
                signal_at=prediction.predicted_at,
            )
        if finnhub_quote_row is not None:
            frozen_quote = _finnhub_signal_quote_payload(
                finnhub_quote_row,
                signal_at=prediction.predicted_at,
            )
            frozen_market["quote"] = frozen_quote
            frozen_market["snapshot_source"] = (
                f"{frozen_market['snapshot_source']}+finnhub_quote_snapshot"
            )
            frozen_quality = dict(frozen_market.get("data_quality") or {})
            frozen_quality.setdefault("quote_source", "finnhub_quote_snapshots")
            frozen_quality.setdefault("last_trade_only", True)
            frozen_quality.setdefault("quote_status", "last_trade_only")
            frozen_market["data_quality"] = frozen_quality
        settlement_evidence = prediction_evidence.get("settlement")
        settlement_evidence = (
            dict(settlement_evidence)
            if isinstance(settlement_evidence, Mapping)
            else {}
        )
        gross_return = float(prediction.directional_return_bps or 0)
        actual_exit_at = prediction.exit_at or prediction.due_at
        cost_breakdown = prediction_cost_breakdown(
            prediction.predicted_at,
            actual_exit_at,
            conservative_cost_config,
        )
        estimated_cost = float(cost_breakdown["total_cost_bps"])
        net_outcome = prediction_net_outcome(gross_return, estimated_cost)
        net_return = float(net_outcome["net_directional_return_bps"])
        net_result = str(net_outcome["net_result"])
        outcome = {
            "id": opportunity.public_id,
            "prediction_id": prediction.public_id,
            "symbol": prediction.symbol,
            "contract_symbol": prediction.contract_symbol,
            "direction": prediction.direction,
            "timeframe": prediction.timeframe,
            "technical_confirmed": True,
            "api_version": OPPORTUNITY_API_VERSION,
            "gate_summary": dict(frozen_market["gate_summary"] or {}),
            "score_components": dict(frozen_market["score_components"] or {}),
            "quote": dict(frozen_market["quote"] or {}),
            "flow": dict(frozen_market["flow"] or {}),
            "gex": dict(frozen_market["gex"] or {}),
            "data_quality": dict(frozen_market["data_quality"] or {}),
            "market_snapshot_source": frozen_market["snapshot_source"],
            "point_in_time_replay": dict(
                frozen_market.get("point_in_time_replay") or {}
            ),
            "version": dict(
                frozen_market["version"]
                or {
                    "api": OPPORTUNITY_API_VERSION,
                    "feature": "legacy",
                    "weights": "legacy",
                    "decision": "legacy",
                }
            ),
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
            "settled_price_at": actual_exit_at,
            "exit_reason": prediction.exit_reason or "legacy_horizon_close",
            "exit_detail": (
                prediction.exit_subreason
                or settlement_evidence.get("exit_subreason")
            ),
            "exit_protection": {
                "peak_favorable_bps": (
                    float(prediction.peak_favorable_bps_at_exit)
                    if prediction.peak_favorable_bps_at_exit is not None
                    else settlement_evidence.get(
                        "peak_favorable_bps_at_decision"
                    )
                ),
                "protected_bps": (
                    float(prediction.protected_bps_at_exit)
                    if prediction.protected_bps_at_exit is not None
                    else settlement_evidence.get("protected_bps")
                ),
                "observed_bar_count": settlement_evidence.get(
                    "observed_bar_count"
                ),
            },
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
            "exit_at": actual_exit_at,
            "max_holding_minutes": round(
                max(
                    0.0,
                    (prediction.due_at - prediction.predicted_at).total_seconds()
                    / 60,
                ),
                2,
            ),
            "actual_holding_minutes": round(
                max(
                    0.0,
                    (actual_exit_at - prediction.predicted_at).total_seconds()
                    / 60,
                ),
                2,
            ),
            "max_holding_bars": int(
                (
                    prediction_evidence.get("max_holding")
                    if isinstance(prediction_evidence.get("max_holding"), Mapping)
                    else {}
                ).get("bars")
                or max(
                    1,
                    round(
                        (prediction.due_at - prediction.predicted_at).total_seconds()
                        / _TIMEFRAME_SECONDS.get(prediction.timeframe, 3600)
                    ),
                )
            ),
            }
        gate_summary = dict(outcome["gate_summary"] or {})
        blocking_reasons = {
            str(item).strip().upper()
            for item in gate_summary.get("blocking_reasons", [])
        }
        warnings = {
            str(item).strip().upper() for item in gate_summary.get("warnings", [])
        }
        quote_payload = dict(outcome["quote"] or {})
        quote_has_price = any(
            _finite_number(quote_payload.get(key)) is not None
            for key in ("last_price", "price", "bid", "ask", "midpoint")
        )
        quote_bid = _finite_number(quote_payload.get("bid"))
        quote_ask = _finite_number(quote_payload.get("ask"))
        quote_has_nbbo = bool(
            quote_bid is not None
            and quote_ask is not None
            and quote_bid > 0
            and quote_ask >= quote_bid
        )
        quote_blocked = any(
            "QUOTE" in reason or "SPREAD" in reason or "HALT" in reason
            for reason in blocking_reasons
        )
        outcome_quote_quality = (
            "missing"
            if not quote_has_price
            else "blocked"
            if quote_blocked and quote_has_nbbo
            else "passed"
            if quote_has_nbbo
            else "partial"
        )
        market_environment = dict(prediction_evidence.get("market_environment") or {})
        score_snapshot = dict(prediction_evidence.get("score_snapshot") or {})
        snapshot_macro = dict(score_snapshot.get("macro_market") or {})
        computed_session = str(
            macro_market.us_market_session(
                prediction.predicted_at.replace(tzinfo=UTC)
                if prediction.predicted_at.tzinfo is None
                else prediction.predicted_at.astimezone(UTC)
            ).get("key")
            or "unknown"
        )
        outcome_market_session = str(
            quote_payload.get("market_session")
            or prediction_evidence.get("market_session")
            or dict(market_environment.get("market_session") or {}).get("key")
            or dict(snapshot_macro.get("market_session") or {}).get("key")
            or computed_session
            or "unknown"
        ).lower()
        risk_events = prediction_evidence.get("risk_events")
        risk_events = list(risk_events) if isinstance(risk_events, list) else []
        risk_levels = {
            str(event.get("risk_level") or event.get("severity") or "").lower()
            for event in risk_events
            if isinstance(event, Mapping)
        }
        if any("EVENT" in reason for reason in blocking_reasons) or risk_levels.intersection(
            {"critical", "blocked"}
        ):
            outcome_event_risk = "blocked"
        elif any("EVENT" in reason for reason in warnings) or risk_levels.intersection(
            {"high", "medium", "warning"}
        ):
            outcome_event_risk = "warning"
        else:
            outcome_event_risk = "clear"

        market_flow_payload = dict(outcome["flow"] or {})
        domains = market_flow_payload.get("domains")
        domains = dict(domains) if isinstance(domains, Mapping) else {}
        option_payload = market_flow_payload.get("option_flow") or domains.get(
            "option_flow"
        )
        option_payload = dict(option_payload) if isinstance(option_payload, Mapping) else {}
        gex_payload = outcome.get("gex") or domains.get("gex")
        gex_payload = dict(gex_payload) if isinstance(gex_payload, Mapping) else {}
        institutional_payload = market_flow_payload.get(
            "institutional_flow"
        ) or domains.get("institutional_flow")
        institutional_payload = (
            dict(institutional_payload)
            if isinstance(institutional_payload, Mapping)
            else {}
        )
        score_components = dict(outcome["score_components"] or {})
        data_quality_payload = dict(outcome["data_quality"] or {})
        gate_coverage = _finite_number(gate_summary.get("coverage"))
        data_coverage = next(
            (
                value
                for value in (
                    _finite_number(data_quality_payload.get("coverage")),
                    _finite_number(data_quality_payload.get("data_coverage")),
                    _finite_number(data_quality_payload.get("coverage_rate")),
                    gate_coverage,
                )
                if value is not None
            ),
            None,
        )
        data_coverage_pct = (
            None
            if data_coverage is None
            else round(
                max(0.0, min(100.0, data_coverage * 100 if data_coverage <= 1 else data_coverage)),
                2,
            )
        )
        option_flow_score = _finite_number(
            option_payload.get("score", score_components.get("option_flow"))
        )
        gex_score = _finite_number(gex_payload.get("score", score_components.get("gex")))
        institutional_score = _finite_number(
            institutional_payload.get(
                "score", score_components.get("institutional_flow")
            )
        )
        version_payload = dict(outcome.get("version") or {})
        decision_at_signal = str(version_payload.get("decision") or "")
        uw_policy_payload = dict(
            prediction_evidence.get("unusual_whales_policy") or {}
        )
        uw_enabled_at_signal = bool(
            uw_policy_payload.get(
                "enabled", "uw_disabled" not in decision_at_signal.lower()
            )
        )

        def feature_reason(
            payload: Mapping[str, Any],
            *,
            enabled_at_signal: bool = uw_enabled_at_signal,
            snapshot: OpportunityMarketSnapshot | None = market_snapshot,
        ) -> str:
            if payload and payload.get("available") is not False:
                return "available"
            if not enabled_at_signal:
                return "uw_disabled_at_signal"
            if snapshot is None:
                return "legacy_snapshot_missing"
            if snapshot.market_feature_snapshot_id is None:
                return "market_feature_not_linked"
            return "not_captured_at_signal"

        quote_reason = (
            "available"
            if quote_has_nbbo
            else "finnhub_last_trade_only"
            if quote_has_price and quote_payload.get("provider") == "finnhub"
            else "last_trade_only"
            if quote_has_price
            else "legacy_snapshot_missing"
            if market_snapshot is None
            else "no_signal_time_quote"
        )
        feature_availability = {
            "unusual_whales_enabled_at_signal": uw_enabled_at_signal,
            "snapshot_recorded": market_snapshot is not None,
            "market_feature_linked": bool(
                market_snapshot is not None
                and market_snapshot.market_feature_snapshot_id is not None
            ),
            "quote": {
                "available": quote_has_price,
                "nbbo_available": quote_has_nbbo,
                "quality": outcome_quote_quality,
                "source": quote_payload.get("source")
                or quote_payload.get("provider"),
                "reason": quote_reason,
            },
            "option_flow": {
                "available": feature_reason(option_payload) == "available",
                "reason": feature_reason(option_payload),
            },
            "gex": {
                "available": feature_reason(gex_payload) == "available",
                "reason": feature_reason(gex_payload),
            },
            "institutional_flow": {
                "available": feature_reason(institutional_payload) == "available",
                "reason": feature_reason(institutional_payload),
            },
        }
        outcome.update(
            {
                "option_flow_score": option_flow_score,
                "gex_score": gex_score,
                "institutional_score": institutional_score,
                "market_session": outcome_market_session,
                "quote_quality": outcome_quote_quality,
                "event_risk": outcome_event_risk,
                "data_coverage_pct": data_coverage_pct,
                "feature_availability": feature_availability,
            }
        )
        outcome_exit_reason = str(
            outcome.get("exit_detail") or outcome.get("exit_reason") or "unknown"
        ).lower()
        if option_flow_score_min > 0 and (
            option_flow_score is None or option_flow_score < option_flow_score_min
        ):
            continue
        if gex_score_min > 0 and (gex_score is None or gex_score < gex_score_min):
            continue
        if min_data_coverage > 0 and (
            data_coverage_pct is None or data_coverage_pct < min_data_coverage
        ):
            continue
        outcome_version = dict(outcome.get("version") or {})
        if feature_version and str(outcome_version.get("feature") or "") != feature_version:
            continue
        if decision_version and str(outcome_version.get("decision") or "") != decision_version:
            continue
        if market_session != "all" and outcome_market_session != market_session:
            continue
        if quote_quality != "all" and outcome_quote_quality != quote_quality:
            continue
        if event_risk != "all" and outcome_event_risk != event_risk:
            continue
        if exit_reason != "all" and exit_reason.lower() not in outcome_exit_reason:
            continue
        outcomes.append(outcome)
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
    summary_outcomes = (
        [
            _compact_historical_outcome(row, current_config)
            for row in compact_rows
        ]
        if not requires_evidence_scan
        else outcomes
    )
    summary = summarize_historical_opportunities(summary_outcomes)
    exit_reason_counts: dict[str, int] = {}
    for outcome in summary_outcomes:
        exit_key = str(outcome.get("exit_detail") or outcome.get("exit_reason") or "unknown")
        exit_reason_counts[exit_key] = exit_reason_counts.get(exit_key, 0) + 1
    summary["exit_reason_counts"] = exit_reason_counts
    summary["settlement_policy_version"] = selected_settlement_version
    summary["discarded_unavailable_count"] = status_counts.get("unavailable", 0)
    summary["pending_count"] = status_counts.get("pending", 0)
    summary["total_prediction_count"] = sum(status_counts.values())
    ablation = (
        frozen_market_ablation_summary(summary_outcomes)
        if include_ablation
        else {
            "schema_version": "frozen_market_ablation.v1",
            "status": "deferred",
            "method": "separate_read_model",
            "total_settled_count": len(summary_outcomes),
            "variants": [],
            "gate_rejections": {},
        }
    )
    gate_rejection_conditions = [
        OpportunityGateDecision.user_id == user_id,
        OpportunityGateDecision.gate_status == "blocked",
        OpportunityGateDecision.selected.is_(False),
    ]
    if direction in {"long", "short"}:
        gate_rejection_conditions.append(
            OpportunityGateDecision.direction == direction
        )
    if date_from is not None:
        gate_rejection_conditions.append(OpportunityGateDecision.decision_at >= date_from)
    if date_to is not None:
        gate_rejection_conditions.append(OpportunityGateDecision.decision_at < date_to)
    if normalized_symbol:
        gate_rejection_conditions.append(
            or_(
                func.upper(OpportunityGateDecision.symbol) == normalized_symbol,
                func.upper(OpportunityGateDecision.contract_symbol) == normalized_symbol,
            )
        )
    if feature_version:
        gate_rejection_conditions.append(
            OpportunityGateDecision.feature_version == feature_version
        )
    if decision_version:
        gate_rejection_conditions.append(
            OpportunityGateDecision.decision_version == decision_version
        )
    if include_ablation:
        observed_gate_rejection_count = int(
            db.scalar(
                select(func.count(OpportunityGateDecision.id)).where(
                    *gate_rejection_conditions
                )
            )
            or 0
        )
        observed_gate_rejections = list(
            db.scalars(
                select(OpportunityGateDecision)
                .where(*gate_rejection_conditions)
                .order_by(
                    OpportunityGateDecision.decision_at.desc(),
                    OpportunityGateDecision.id.desc(),
                )
                .limit(5000)
            ).all()
        )
        ablation["gate_rejections"]["observed_gate_decisions"] = (
            frozen_gate_rejection_summary(
                observed_gate_rejections,
                total_count=observed_gate_rejection_count,
            )
        )
    if requires_evidence_scan:
        total_items = len(outcomes)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        current_page = min(requested_page, total_pages)
        page_start = (current_page - 1) * page_size
        page_items = outcomes[page_start : page_start + page_size]
    else:
        page_items = outcomes
    result = {
        "summary": summary,
        "ablation": ablation,
        "items": page_items,
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total": total_items,
            "total_pages": total_pages,
            "has_previous": current_page > 1,
            "has_next": current_page < total_pages,
        },
        "cost_config": {
            **active_cost_settings,
            "configured": configured_cost_settings,
            "forced_conservative_floor": True,
            "example_one_hour_total_bps": prediction_estimated_cost_bps(
                datetime(2026, 1, 1),
                datetime(2026, 1, 1) + timedelta(hours=1),
                conservative_cost_config,
            ),
        },
        "settlement_versions": prediction_settlement_version_catalog(db, user_id),
        "filters": {
            "date_from": (
                (date_from + local_date_offset).date().isoformat()
                if date_from
                else None
            ),
            "date_to": (
                (date_to + local_date_offset - timedelta(days=1)).date().isoformat()
                if date_to
                else None
            ),
            "timezone_offset_minutes": timezone_offset_minutes,
            "symbol": normalized_symbol,
            "news_score_min": float(news_score_min),
            "indicator_score_min": float(indicator_score_min),
            "combined_score_min": float(combined_score_min),
            "option_flow_score_min": float(option_flow_score_min),
            "gex_score_min": float(gex_score_min),
            "min_data_coverage": float(min_data_coverage),
            "feature_version": feature_version,
            "decision_version": decision_version,
            "settlement_version": selected_settlement_version,
            "direction": direction,
            "market_session": market_session,
            "quote_quality": quote_quality,
            "event_risk": event_risk,
            "exit_reason": exit_reason,
        },
        "query_mode": "evidence_scan" if requires_evidence_scan else "paged",
        "note": "直接统计已经完成结算的预测；命中率和净收益按右侧当前启用的手续费、滑点与资金成本动态重算，不会执行任何交易。",
    }
    if include_readiness:
        result["readiness"] = strategy_readiness_report(
            db,
            user_id,
            current_config,
        )
    return result


def settle_due_predictions(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Manage virtual exits using barriers, score decay, then a hard time cap."""

    now = utcnow()
    retry_before = now - timedelta(minutes=PREDICTION_SETTLEMENT_RETRY_MINUTES)
    backfill_since = now - timedelta(days=PREDICTION_SETTLEMENT_BACKFILL_DAYS)
    grace_cutoff = now - timedelta(hours=PREDICTION_SETTLEMENT_GRACE_HOURS)
    # Opportunity rescans may refresh a pending prediction's generic
    # ``updated_at`` while its frozen entry and risk plan remain unchanged.
    # Hard stops therefore stay eligible on every poll; the retry cooldown is
    # only for rows whose settlement data was actually unavailable.
    statement = (
        select(AiMonitorPrediction)
        .where(
            or_(
                AiMonitorPrediction.status == "pending",
                (
                    (AiMonitorPrediction.status == "unavailable")
                    & (AiMonitorPrediction.updated_at <= retry_before)
                    & (AiMonitorPrediction.due_at <= now)
                    & (AiMonitorPrediction.due_at >= backfill_since)
                ),
            ),
        )
        .order_by(AiMonitorPrediction.due_at, AiMonitorPrediction.id)
        .limit(PREDICTION_SETTLEMENT_BATCH_SIZE)
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    # Hold each candidate row until the surrounding transaction commits.  A
    # second scheduler/process skips rows already owned by another worker
    # instead of calculating the same settlement from stale evidence.  SQLite
    # safely omits the unsupported locking clause; production MySQL emits
    # ``FOR UPDATE SKIP LOCKED``.
    statement = statement.execution_options(populate_existing=True).with_for_update(
        skip_locked=True
    )
    items = db.scalars(statement).all()
    window_ms = 45 * 60 * 1_000
    grouped: dict[str, list[AiMonitorPrediction]] = {}
    for item in items:
        grouped.setdefault(item.contract_symbol, []).append(item)
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        targets = [_datetime_ms(min(item.due_at, now)) for item in symbol_items]
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
    take_profit = 0
    stop_loss = 0
    score_exit = 0
    max_holding = 0
    profit_protection = 0
    failed_follow_through = 0
    for item in items:
        previous_status = item.status
        entry_price = float(item.entry_price or 0)
        predicted_at = getattr(
            item,
            "predicted_at",
            item.due_at
            - timedelta(
                seconds=_TIMEFRAME_SECONDS.get(getattr(item, "timeframe", "15m"), 900)
            ),
        )
        start_ms = _datetime_ms(predicted_at)
        observed_until = min(item.due_at, now)
        observed_until_ms = _datetime_ms(observed_until)
        candles = candles_by_symbol.get(item.contract_symbol, [])
        evidence = dict(getattr(item, "evidence_json", None) or {})
        stored_risk_plan = evidence.get("risk_plan")
        has_frozen_risk_plan = isinstance(stored_risk_plan, Mapping)
        risk_plan = (
            dict(stored_risk_plan)
            if has_frozen_risk_plan
            else virtual_risk_plan_snapshot(
                entry_price=entry_price,
                direction=item.direction,
                timeframe=getattr(item, "timeframe", "15m"),
            )
        )
        # A policy version belongs to the risk plan frozen at signal time.  Do
        # not relabel an old pending signal merely because a newer worker later
        # settles it.  Legacy rows without a frozen plan stay in the previous
        # cohort and therefore cannot contaminate current readiness statistics.
        settlement_version = str(
            (
                risk_plan.get("settlement_version")
                or getattr(item, "settlement_version", None)
            )
            if has_frozen_risk_plan
            else PREVIOUS_PREDICTION_SETTLEMENT_VERSION
        )
        risk_plan["settlement_version"] = settlement_version
        cost_model = evidence.get("cost_model")
        actual_cost_config = prediction_settlement_cost_config(
            cost_model if isinstance(cost_model, Mapping) else None,
            settlement_version=settlement_version,
        )
        guard_cost_estimate = prediction_estimated_cost_bps(
            predicted_at,
            observed_until,
            actual_cost_config,
        )
        soft_exit_policy = prediction_soft_exit_policy(
            getattr(item, "timeframe", "15m"),
            start_ms=start_ms,
            due_ms=_datetime_ms(item.due_at),
        )
        barrier_exit = prediction_price_barrier_exit(
            candles,
            entry_price,
            item.direction,
            risk_plan,
            start_ms,
            observed_until_ms,
        )
        adaptive_exit = prediction_adaptive_path_exit(
            candles,
            entry_price,
            item.direction,
            start_ms,
            observed_until_ms,
            estimated_cost_bps=guard_cost_estimate,
            minimum_soft_exit_ms=int(soft_exit_policy["minimum_hold_ms"]),
            minimum_profit_protection_ms=(
                0
                if str(
                    dict(risk_plan.get("profit_protection") or {}).get("mode")
                    or ""
                )
                == "risk_unit"
                else int(soft_exit_policy["minimum_hold_ms"])
            ),
            minimum_failed_follow_through_ms=int(
                soft_exit_policy["minimum_hold_ms"]
            ),
            profit_protection=(
                dict(risk_plan.get("profit_protection") or {})
                if isinstance(risk_plan.get("profit_protection"), Mapping)
                else None
            ),
            failed_follow_through=(
                dict(risk_plan.get("failed_follow_through") or {})
                if isinstance(risk_plan.get("failed_follow_through"), Mapping)
                else None
            ),
        )
        score_signal = prediction_score_exit_signal(
            evidence,
            item.direction,
            start_ms=start_ms,
            end_ms=observed_until_ms,
            confirmation_bar_ms=int(soft_exit_policy["bar_ms"]),
            minimum_hold_ms=int(soft_exit_policy["minimum_hold_ms"]),
            confirmation_bars=int(soft_exit_policy["confirmation_bars"]),
            confirmation_unit=str(soft_exit_policy["confirmation_unit"]),
        )
        score_settlement = (
            prediction_score_exit_price(
                candles,
                score_signal,
                end_ms=observed_until_ms,
            )
            if score_signal is not None
            else None
        )
        exit_decision: dict[str, Any] | None = barrier_exit
        if score_signal is not None and score_settlement is not None:
            score_decision = {
                **score_signal,
                "price": float(score_settlement["price"]),
                "price_time_ms": int(score_settlement["price_time_ms"]),
            }
            if (
                exit_decision is None
                or int(score_decision["price_time_ms"])
                < int(exit_decision["price_time_ms"])
            ):
                exit_decision = score_decision
        if adaptive_exit is not None:
            # A frozen protective stop is an executable price barrier, but OHLC
            # cannot reveal whether it or the original stop was touched first.
            # Keep the loss-side barrier on an equal timestamp to avoid
            # overstating research returns.  Profit protection may still beat
            # an equal-time target or close-time score decision conservatively.
            if adaptive_exit_precedes(exit_decision, adaptive_exit):
                exit_decision = adaptive_exit
        if exit_decision is None and item.due_at <= now:
            settlement = historical_closed_settlement_price(
                candles,
                _datetime_ms(item.due_at),
                not_before_ms=start_ms,
            )
            if settlement is not None:
                exit_decision = {
                    "reason": "max_holding_time",
                    "price": float(settlement["price"]),
                    "price_time_ms": int(settlement["price_time_ms"]),
                    "same_bar_conflict": False,
                    "gap_execution": False,
                }
        if (
            exit_decision is not None
            and int(exit_decision.get("price_time_ms") or 0) < start_ms
        ):
            # Never persist an exit from before the virtual entry.  Leave the
            # prediction pending so the retry path can obtain causal data.
            exit_decision = None
        if exit_decision is None and item.due_at > now:
            continue
        exit_price = float(exit_decision["price"]) if exit_decision is not None else 0.0
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
        exit_at = datetime.fromtimestamp(
            int(exit_decision["price_time_ms"]) / 1_000,
            UTC,
        ).replace(tzinfo=None)
        estimated_cost = prediction_estimated_cost_bps(
            predicted_at,
            exit_at,
            actual_cost_config,
        )
        net_outcome = prediction_net_outcome(
            float(outcome["directional_return_bps"]), estimated_cost
        )
        path_metrics = prediction_path_metrics(
            candles,
            entry_price,
            item.direction,
            start_ms,
            int(exit_decision["price_time_ms"]),
        )
        exit_reason = str(exit_decision["reason"])
        exit_subreason = settlement_exit_subreason(
            exit_decision,
            net_result=str(net_outcome.get("net_result") or ""),
        )
        peak_favorable_bps_at_exit = (
            Decimal(str(path_metrics["max_favorable_bps"]))
            if path_metrics["max_favorable_bps"] is not None
            else None
        )
        protected_bps_at_exit = (
            Decimal(str(exit_decision["protected_bps"]))
            if exit_decision.get("protected_bps") is not None
            else None
        )
        item.status = "completed"
        item.result = str(outcome["result"])
        item.exit_price = Decimal(str(exit_price))
        item.exit_at = exit_at
        item.exit_reason = exit_reason
        item.exit_subreason = exit_subreason
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
        item.peak_favorable_bps_at_exit = peak_favorable_bps_at_exit
        item.protected_bps_at_exit = protected_bps_at_exit
        item.settlement_version = settlement_version
        evidence["settlement"] = {
            "version": settlement_version,
            "score_exit_policy_version": PREDICTION_SCORE_EXIT_POLICY_VERSION,
            "exit_reason": exit_reason,
            "exit_subreason": exit_subreason,
            "exit_at": exit_at.replace(tzinfo=UTC).isoformat(),
            "exit_price": exit_price,
            "same_bar_conflict": bool(exit_decision.get("same_bar_conflict")),
            "gap_execution": bool(exit_decision.get("gap_execution")),
            "price_source": exit_decision.get("price_source") or "closed_candle_path",
            "reference_price_time_ms": exit_decision.get("reference_price_time_ms"),
            "peak_favorable_bps_at_decision": (
                float(peak_favorable_bps_at_exit)
                if peak_favorable_bps_at_exit is not None
                else None
            ),
            "protected_bps": exit_decision.get("protected_bps"),
            "observed_bar_count": exit_decision.get("observed_bar_count"),
            "risk_plan": risk_plan,
            "score_signal": (
                {
                    key: exit_decision.get(key)
                    for key in (
                        "combined",
                        "technical",
                        "exit_threshold",
                        "confirmation_points",
                        "confirmation_unit",
                        "confirmation_bar_times_ms",
                        "confirmation_scores",
                        "failed_loss_threshold_bps",
                        "failed_threshold_mode",
                        "minimum_hold_ms",
                        "ignored_duplicate_points",
                        "calculated_at",
                        "closed_bar_time_ms",
                        "reference_price_time_ms",
                    )
                    if exit_decision.get(key) is not None
                }
                if exit_reason in {"score_breakdown", "score_reversal"}
                else None
            ),
            "cost_breakdown": prediction_cost_breakdown(
                predicted_at, exit_at, actual_cost_config
            ),
            "policy": (
                "horizon_aligned_profit_guard_then_price_barrier_then_confirmed_"
                "native_bar_score_exit_then_failed_follow_through_then_hard_time_cap"
            ),
        }
        item.evidence_json = evidence
        item.completed_at = now
        item.updated_at = now
        completed += 1
        if exit_reason == "take_profit":
            take_profit += 1
        elif exit_reason == "stop_loss":
            stop_loss += 1
        elif exit_reason in {"score_breakdown", "score_reversal"}:
            score_exit += 1
        else:
            max_holding += 1
        if exit_subreason in {
            "profit_lock",
            "trailing_profit",
        }:
            profit_protection += 1
        elif exit_subreason == "failed_follow_through":
            failed_follow_through += 1
        if previous_status == "unavailable":
            recovered += 1
    db.flush()
    return {
        "completed": completed,
        "recovered": recovered,
        "deferred": deferred,
        "unavailable": unavailable,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "score_exit": score_exit,
        "max_holding": max_holding,
        "profit_protection": profit_protection,
        "failed_follow_through": failed_follow_through,
    }


def reopen_legacy_prediction_settlements(
    db: Session,
    *,
    user_id: int | None = None,
    limit: int = PREDICTION_SETTLEMENT_BATCH_SIZE,
) -> int:
    """Reopen only unauditable horizon-close outcomes for reconstruction.

    A completed row from another frozen policy is valid historical evidence,
    not a migration candidate.  Reopening every version mismatch used to
    recalculate old signals with new code and then mix incompatible policies in
    one cohort.
    """

    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.exit_reason == "legacy_horizon_close",
        )
        .order_by(AiMonitorPrediction.predicted_at, AiMonitorPrediction.id)
        .limit(max(1, min(int(limit), 250)))
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    # Reopening and settlement share the same row-level ownership protocol so
    # they cannot reset/complete one legacy row concurrently.  Refreshing an
    # identity-map hit is important because evidence_json is replaced as one
    # immutable JSON value.
    statement = statement.execution_options(populate_existing=True).with_for_update(
        skip_locked=True
    )
    items = list(db.scalars(statement).all())
    repair_started_at = utcnow()
    retry_ready_at = repair_started_at - timedelta(
        minutes=PREDICTION_SETTLEMENT_RETRY_MINUTES + 1
    )
    for item in items:
        evidence = dict(item.evidence_json or {})
        stored_risk_plan = evidence.get("risk_plan")
        if isinstance(stored_risk_plan, Mapping):
            frozen_risk_plan = dict(stored_risk_plan)
            frozen_risk_plan["settlement_version"] = (
                PREVIOUS_PREDICTION_SETTLEMENT_VERSION
            )
            evidence["risk_plan"] = frozen_risk_plan
        evidence["settlement_repair"] = {
            "requested_at": repair_started_at.replace(tzinfo=UTC).isoformat(),
            "source_version": item.settlement_version,
            "source_exit_reason": item.exit_reason,
            "target_version": PREVIOUS_PREDICTION_SETTLEMENT_VERSION,
            "status": "pending_recalculation",
        }
        item.status = "pending"
        item.result = None
        item.exit_price = None
        item.exit_at = None
        item.exit_reason = None
        item.exit_subreason = None
        item.raw_return_bps = None
        item.directional_return_bps = None
        item.net_directional_return_bps = None
        item.net_result = None
        item.max_favorable_bps = None
        item.max_adverse_bps = None
        item.peak_favorable_bps_at_exit = None
        item.protected_bps_at_exit = None
        item.completed_at = None
        item.settlement_version = "repair_pending_v4"
        item.evidence_json = evidence
        item.updated_at = retry_ready_at
    db.flush()
    return len(items)

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
        .limit(PREDICTION_SETTLEMENT_BATCH_SIZE)
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
            if not news_stock_relation_supported(row, normalized, relevance):
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
                    "memory_effect": related.get("memory_effect"),
                    "memory_reason": related.get("memory_reason"),
                    "position_effect": related.get("position_effect"),
                    "position_reason": related.get("position_reason"),
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


_NON_ACTIONABLE_RECAP_PATTERNS = (
    re.compile(r"美股.{0,4}收盘|收盘综述|盘后复盘|收盘\s*[：:]"),
    re.compile(r"(?:收涨|收跌|尾盘)(?:[^。；;]{0,28})(?:%|％|点|美元)"),
    re.compile(
        r"\b(?:market close|closing bell|post[- ]market recap|stocks? close[sd]?|"
        r"closed (?:up|down)|ends? (?:up|down))\b",
        re.IGNORECASE,
    ),
)


def news_actionability_snapshot(news_item: Mapping[str, Any]) -> dict[str, Any]:
    """Classify whether a news item can initiate a new prediction.

    Closing recaps describe a move that has already happened.  They remain in
    the evidence and memory windows, but must not be treated as a fresh
    catalyst for a new entry.
    """

    title = str(news_item.get("title") or "").strip()
    reason = str(news_item.get("reason") or "").strip()
    text = " ".join(part for part in (title, reason) if part)
    matched_pattern = next(
        (pattern.pattern for pattern in _NON_ACTIONABLE_RECAP_PATTERNS if pattern.search(text)),
        None,
    )
    return {
        "version": "news_actionability_v1",
        "actionable": matched_pattern is None,
        "reason_code": "CLOSING_RECAP_NOT_A_CATALYST" if matched_pattern else None,
        "matched_pattern": matched_pattern,
    }


def annotate_event_cluster_selection(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Allow one strongest candidate per shared news event and direction.

    A broad story can mention several correlated stocks.  Counting every symbol
    as an independent signal creates concentrated, misleading samples.  The
    strongest candidate remains eligible; the others are still persisted as
    blocked candidates with an auditable cluster owner.
    """

    claimed: dict[tuple[str, str], str] = {}
    for candidate in sorted(
        candidates,
        key=lambda item: float(item.get("news_score") or 0),
        reverse=True,
    ):
        trigger = dict(candidate.get("news_trigger") or {})
        direction = str(candidate.get("direction") or "long")
        symbol = str(candidate.get("symbol") or "")
        news_ids = sorted(
            str(item)
            for item in trigger.get("actionable_new_news_ids") or []
            if str(item)
        )
        owners = sorted(
            {
                claimed[(direction, news_id)]
                for news_id in news_ids
                if (direction, news_id) in claimed
            }
        )
        selected = not owners
        if selected:
            for news_id in news_ids:
                claimed[(direction, news_id)] = symbol
        cluster_seed = ",".join(news_ids) or f"{direction}:{symbol}:no-new-event"
        trigger["event_cluster"] = {
            "version": "shared_news_event_v1",
            "cluster_id": hashlib.sha256(cluster_seed.encode("utf-8")).hexdigest()[:16],
            "selected": selected,
            "selected_symbol": symbol if selected else owners[0],
            "shared_news_ids": news_ids,
            "reason_code": None if selected else "CORRELATED_EVENT_ALREADY_SELECTED",
        }
        candidate["news_trigger"] = trigger
    return list(candidates)


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


def multi_timeframe_technical_snapshot(
    scans: Mapping[str, Mapping[str, Any]],
    indicator_keys: Sequence[str],
    *,
    direction: str,
) -> dict[str, Any]:
    """Evaluate one direction across 15m timing, 1h trend and 4h regime.

    The configured indicators remain the strategy definition.  This function
    changes only how their directional evidence is combined: a 1h setup can
    qualify by itself, while a 15m timing setup must agree with the 4h regime.
    Scores are averaged over confirmed frames so an untriggered auxiliary frame
    does not dilute a valid 1h signal into a false rejection.
    """

    normalized_direction = "short" if direction == "short" else "long"
    frames: dict[str, dict[str, Any]] = {}
    for timeframe, weight in MULTI_TIMEFRAME_TECHNICAL_WEIGHTS.items():
        scan = dict(scans.get(timeframe) or {})
        matched, evidence = match_configured_indicators(
            scan,
            indicator_keys,
            normalized_direction,
        )
        policy = configured_indicator_policy(evidence)
        available = any(bool(item.get("available")) for item in evidence)
        frames[timeframe] = {
            "timeframe": timeframe,
            "role": {
                "15m": "entry_timing",
                "1h": "primary_trend",
                "4h": "regime_filter",
            }[timeframe],
            "weight": weight,
            "available": available,
            "passed": bool(matched),
            "technical_score": float(policy.get("technical_score") or 0.0),
            "matched_indicator_keys": [
                str(item.get("key")) for item in evidence if item.get("matched")
            ],
            "passed_groups": list(policy.get("passed_groups") or []),
            "evaluated_at": int(scan.get("evaluated_at") or 0),
            "policy": policy,
            "indicators": evidence,
        }

    passed_frames = [item for item in frames.values() if item["passed"]]
    score_source = passed_frames or [
        item for item in frames.values() if item["available"]
    ]
    score_weight = sum(float(item["weight"]) for item in score_source)
    technical_score = (
        sum(
            float(item["technical_score"]) * float(item["weight"])
            for item in score_source
        )
        / score_weight
        if score_weight
        else 0.0
    )
    structure_confirmed = bool(frames["1h"]["passed"] or frames["4h"]["passed"])
    timing_confirmed = bool(frames["15m"]["passed"] or frames["1h"]["passed"])
    eligible = bool(structure_confirmed and timing_confirmed)
    return {
        "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
        "direction": normalized_direction,
        "technical_score": round(technical_score, 4),
        "eligible": eligible,
        "structure_confirmed": structure_confirmed,
        "timing_confirmed": timing_confirmed,
        "confirmed_timeframes": [
            timeframe for timeframe, item in frames.items() if item["passed"]
        ],
        "available_timeframes": [
            timeframe for timeframe, item in frames.items() if item["available"]
        ],
        "frames": frames,
        "rule": "1h 可独立确认；否则必须由 15m 入场节奏与 4h 大级别方向共同确认",
    }


def select_directional_candidates_with_technical_context(
    candidates: Sequence[Mapping[str, Any]],
    scans_by_contract: Mapping[str, Mapping[str, Mapping[str, Any]]],
    indicator_keys: Sequence[str],
) -> list[dict[str, Any]]:
    """Choose one auditable long/short side per symbol using news and MTF evidence."""

    contexts: dict[tuple[str, str], dict[str, Any]] = {}

    def context(contract_symbol: str, direction: str) -> dict[str, Any]:
        key = (contract_symbol, direction)
        if key not in contexts:
            contexts[key] = multi_timeframe_technical_snapshot(
                scans_by_contract.get(contract_symbol, {}),
                indicator_keys,
                direction=direction,
            )
        return contexts[key]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in candidates:
        candidate = dict(raw)
        symbol_key = str(
            candidate.get("contract_symbol") or candidate.get("symbol") or ""
        ).upper()
        if symbol_key:
            grouped.setdefault(symbol_key, []).append(candidate)

    selected: list[dict[str, Any]] = []
    for symbol_key, alternatives in grouped.items():
        ranked: list[tuple[tuple[float, float, int], dict[str, Any]]] = []
        selection_rows: list[dict[str, Any]] = []
        for candidate in alternatives:
            direction = "short" if candidate.get("direction") == "short" else "long"
            technical = context(symbol_key, direction)
            news_score = float(candidate.get("news_score") or 0.0)
            ranking_score = (
                news_score * 0.55
                + float(technical["technical_score"]) * 0.45
                + (5.0 if technical["eligible"] else 0.0)
            )
            latest_news = max(
                (int(item.get("ts") or 0) for item in candidate.get("news") or []),
                default=0,
            )
            row = {
                "direction": direction,
                "news_score": round(news_score, 4),
                "technical_score": technical["technical_score"],
                "technical_eligible": technical["eligible"],
                "ranking_score": round(ranking_score, 4),
            }
            selection_rows.append(row)
            ranked.append(((ranking_score, news_score, latest_news), candidate))
        _, winner = max(ranked, key=lambda item: item[0])
        winner_direction = "short" if winner.get("direction") == "short" else "long"
        opposite_direction = "long" if winner_direction == "short" else "short"
        winner["multi_timeframe_technical"] = context(symbol_key, winner_direction)
        winner["opposite_multi_timeframe_technical"] = context(
            symbol_key, opposite_direction
        )
        winner["direction_selection"] = {
            "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
            "selected_direction": winner_direction,
            "alternatives": sorted(
                selection_rows,
                key=lambda item: float(item["ranking_score"]),
                reverse=True,
            ),
            "weights": {"news": 0.55, "technical": 0.45},
            "technical_eligibility_bonus": 5.0,
        }
        selected.append(winner)
    return sorted(
        selected,
        key=lambda item: max(
            (
                float(row.get("ranking_score") or 0.0)
                for row in dict(item.get("direction_selection") or {}).get(
                    "alternatives", []
                )
            ),
            default=0.0,
        ),
        reverse=True,
    )


def match_configured_indicators(
    scan: Mapping[str, Any], indicator_keys: Sequence[str], direction: str = "long"
) -> tuple[bool, list[dict[str, Any]]]:
    """Apply the grouped policy using conditions aligned with signal direction."""

    expected_direction = "bearish" if direction == "short" else "bullish"

    items = [
        *list(scan.get("items") or []),
        *list((scan.get("prediction_features") or {}).get("items") or []),
    ]
    by_key = {str(item.get("key") or ""): item for item in items if isinstance(item, Mapping)}
    evidence: list[dict[str, Any]] = []
    for key in indicator_keys:
        item = by_key.get(key)
        status = str(item.get("status") or "") if item else "unavailable"
        available = bool(
            item
            and item.get("available", True) is not False
            and status not in {"insufficient", "unavailable"}
        )
        matched = bool(
            available
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
                "available": available,
                "group": indicator_group(key),
                "blocking": indicator_group(key) in CORE_INDICATOR_GROUPS
                and key not in NON_BLOCKING_INDICATOR_KEYS,
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
    policy = configured_indicator_policy(evidence)
    return bool(policy["passed"]), evidence


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
        if not global_ai_model_configured(db, legacy_fallback_user_id=user_id):
            raise AiMonitorError("请联系管理员配置并启用全局 DeepSeek")
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
    if not global_ai_model_configured(db, legacy_fallback_user_id=user_id):
        raise AiMonitorError("请联系管理员配置并启用全局 DeepSeek")

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
    uw_signal_policy = unusual_whales_signal_policy(db)
    uw_thresholds = dict(uw_signal_policy["thresholds"])
    # Resolve the small due set before taking write locks.  The former range
    # UPDATE could lock every active row when MySQL selected the
    # (user_id, status, score) index, so a concurrent refresh made an otherwise
    # empty expiry update time out and left that user's gate snapshots stale.
    expired_opportunity_ids = list(
        db.scalars(
            select(AiMonitorOpportunity.id).where(
                AiMonitorOpportunity.user_id == run.user_id,
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at <= now,
            )
        ).all()
    )
    if expired_opportunity_ids:
        db.execute(
            update(AiMonitorOpportunity)
            .where(AiMonitorOpportunity.id.in_(expired_opportunity_ids))
            .values(status="expired", updated_at=now)
        )
    cleanup = cleanup_unpredicted_opportunities(db, run.user_id)
    risk_plan_backfill = backfill_prediction_risk_plans(db, run.user_id)
    # Pending predictions may have been created with a longer lookback than the
    # user's current configuration.  Load the schema-bounded maximum once, then
    # apply each frozen lookback independently during rescoring.
    cutoff = int((now.replace(tzinfo=UTC) - timedelta(hours=168)).timestamp())
    news_rows = db.scalars(
        select(News)
        .where(
            News.ts >= cutoff,
            News.ai_analyzed_at.is_not(None),
            News.related_us_stocks.is_not(None),
        )
        .order_by(News.ts.desc(), News.id.desc())
    ).all()
    memory_news_cutoff = int(
        (
            now.replace(tzinfo=UTC)
            - timedelta(hours=int(config["news_lookback_hours"]))
        ).timestamp()
    )
    memory_news_rows = [
        row for row in news_rows if int(row.ts) >= memory_news_cutoff
    ]
    trigger_window_hours = int(config.get("news_trigger_window_hours", 4))
    trigger_news_cutoff = int(
        (now.replace(tzinfo=UTC) - timedelta(hours=trigger_window_hours)).timestamp()
    )
    trigger_news_rows = [
        row for row in memory_news_rows if int(row.ts) >= trigger_news_cutoff
    ]
    model_call_audit = _news_model_call_audit_index(
        db,
        run.user_id,
        trigger_news_rows,
    )
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
        trigger_news_rows,
        symbol_map,
        minimum_confidence=float(config["minimum_news_confidence"]),
        minimum_mentions=int(config["minimum_news_mentions"]),
    )
    # Preserve both news directions until the 15m/1h/4h technical context has
    # been evaluated. Choosing by news score alone discarded valid shorts when
    # a slightly stronger bullish headline existed for the same symbol.
    all_candidates = [dict(item) for item in directional_candidates]
    unmapped_candidates = [item for item in all_candidates if not item.get("contract_symbol")]
    monitor_symbols = list(config.get("monitor_symbols") or [])
    candidates = filter_monitored_candidates(all_candidates, monitor_symbols)
    consumed_news_ids: dict[tuple[str, str], set[str]] = {}
    consumed_rows = db.execute(
        select(
            AiMonitorOpportunity.symbol,
            AiMonitorOpportunity.direction,
            AiMonitorOpportunity.news_ids_json,
        )
        .join(
            AiMonitorPrediction,
            AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
        )
        .where(
            AiMonitorOpportunity.user_id == run.user_id,
            AiMonitorPrediction.predicted_at
            >= now - timedelta(hours=int(config["news_lookback_hours"])),
        )
    ).all()
    for symbol, direction, row_news_ids in consumed_rows:
        consumed_news_ids.setdefault((str(symbol), str(direction)), set()).update(
            str(news_id) for news_id in (row_news_ids or []) if str(news_id)
        )
    active_candidate_keys = {
        (str(symbol), str(direction))
        for symbol, direction in db.execute(
            select(AiMonitorOpportunity.symbol, AiMonitorOpportunity.direction).where(
                AiMonitorOpportunity.user_id == run.user_id,
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at > now,
            )
        ).all()
    }
    require_new_news = bool(config.get("require_new_news_trigger", True))
    eligible_candidates: list[dict[str, Any]] = []
    reused_news_skipped = 0
    for candidate in candidates:
        candidate_news_ids = {
            str(item.get("id") or "") for item in candidate.get("news", [])
        } - {""}
        key = (str(candidate["symbol"]), str(candidate["direction"]))
        new_news_ids = sorted(candidate_news_ids - consumed_news_ids.get(key, set()))
        actionability_by_id = {
            str(item.get("id") or ""): news_actionability_snapshot(item)
            for item in candidate.get("news", [])
            if str(item.get("id") or "") in new_news_ids
        }
        actionable_new_news_ids = sorted(
            news_id
            for news_id, actionability in actionability_by_id.items()
            if bool(actionability.get("actionable"))
        )
        non_actionable_news_ids = sorted(
            news_id
            for news_id, actionability in actionability_by_id.items()
            if not bool(actionability.get("actionable"))
        )
        newest_news_ts = max(
            (int(item.get("ts") or 0) for item in candidate.get("news", [])),
            default=0,
        )
        candidate["news_trigger"] = {
            "version": "fresh_actionable_news_v2",
            "required": require_new_news,
            "memory_window_hours": int(config["news_lookback_hours"]),
            "trigger_window_hours": trigger_window_hours,
            "has_new_news": bool(new_news_ids),
            "new_news_ids": new_news_ids,
            "has_actionable_new_news": bool(actionable_new_news_ids),
            "actionable_new_news_ids": actionable_new_news_ids,
            "non_actionable_news_ids": non_actionable_news_ids,
            "non_actionable_reasons": sorted(
                {
                    str(actionability.get("reason_code"))
                    for actionability in actionability_by_id.values()
                    if actionability.get("reason_code")
                }
            ),
            "actionability": actionability_by_id,
            "reused_news_count": len(candidate_news_ids) - len(new_news_ids),
            "newest_news_age_minutes": (
                round((int(now.replace(tzinfo=UTC).timestamp()) - newest_news_ts) / 60, 2)
                if newest_news_ts
                else None
            ),
        }
        if require_new_news and not new_news_ids and key not in active_candidate_keys:
            reused_news_skipped += 1
            continue
        eligible_candidates.append(candidate)
    candidates = annotate_event_cluster_selection(eligible_candidates)
    indicator_keys = list(config["indicator_keys"])
    timeframe = str(config["timeframe"])
    technical_scans: dict[str, dict[str, dict[str, Any]]] = {}
    for contract_symbol in sorted(
        {
            str(item.get("contract_symbol") or "").upper()
            for item in candidates
            if str(item.get("contract_symbol") or "").strip()
        }
    ):
        contract_scans: dict[str, dict[str, Any]] = {}
        for technical_timeframe in MULTI_TIMEFRAME_TECHNICAL_WEIGHTS:
            try:
                contract_scans[technical_timeframe] = repository.strategy_indicators(
                    contract_symbol,
                    technical_timeframe,
                )
            except MonitorUnavailable:
                contract_scans[technical_timeframe] = {
                    "items": [],
                    "prediction_features": {"items": []},
                    "evaluated_at": 0,
                }
        technical_scans[contract_symbol] = contract_scans
    candidates = select_directional_candidates_with_technical_context(
        candidates,
        technical_scans,
        indicator_keys,
    )
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
    prediction_max_holding_bars = max(
        1,
        min(24, int(config.get("prediction_max_holding_bars", 4))),
    )
    minimum_indicator_score = float(config["minimum_indicator_score"])
    minimum_combined_score = float(config["minimum_combined_score"])
    minimum_calibration_samples = int(config["minimum_calibration_samples"])
    conservative_prediction_cost_config = readiness_cost_config(config)
    cost_settings = prediction_cost_settings(conservative_prediction_cost_config)
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
    macro_snapshot = macro_market.default_snapshot(repository, now=now)
    realtime_features = latest_realtime_feature_snapshots(
        db,
        [
            *(str(item.get("symbol") or "") for item in candidates),
            *(str(item.get("contract_symbol") or "") for item in candidates),
        ],
    )
    finnhub_signal_quotes = _latest_finnhub_signal_quotes(
        db,
        [str(item.get("symbol") or "") for item in candidates],
        signal_at=now,
    )
    risk_events_by_symbol = active_market_risk_events(
        db,
        now=now,
        symbols=[str(item.get("symbol") or "") for item in candidates],
        blocking_before_minutes=int(uw_thresholds["event_block_before_minutes"]),
        blocking_after_minutes=int(uw_thresholds["event_block_after_minutes"]),
    )
    rescored_pending_predictions = refresh_pending_prediction_scores(
        db,
        user_id=run.user_id,
        news_rows=news_rows,
        symbol_map=symbol_map,
        repository=repository,
        market_flow_inputs=market_flow_inputs,
        now=now,
        macro_market_snapshot=macro_snapshot,
    )
    for candidate in candidates:
        contract_symbol = str(candidate.get("contract_symbol") or "")
        if not contract_symbol:
            unmapped_symbols.append(candidate["symbol"])
            scan = {"items": [], "prediction_features": {"items": []}, "evaluated_at": 0}
        else:
            scan = dict(
                technical_scans.get(contract_symbol.upper(), {}).get(timeframe)
                or {}
            )
            if not scan:
                try:
                    scan = repository.strategy_indicators(contract_symbol, timeframe)
                except MonitorUnavailable:
                    failed_symbols.append(candidate["symbol"])
                    scan = {"items": [], "prediction_features": {"items": []}, "evaluated_at": 0}
        primary_policy_matched, indicator_evidence = match_configured_indicators(
            scan, indicator_keys, candidate["direction"]
        )
        primary_indicator_policy = configured_indicator_policy(indicator_evidence)
        multi_timeframe_technical = dict(
            candidate.get("multi_timeframe_technical") or {}
        )
        opposite_multi_timeframe_technical = dict(
            candidate.get("opposite_multi_timeframe_technical") or {}
        )
        current_mtf_score = float(
            multi_timeframe_technical.get("technical_score")
            or primary_indicator_policy["technical_score"]
            or 0.0
        )
        opposite_mtf_score = float(
            opposite_multi_timeframe_technical.get("technical_score") or 0.0
        )
        opposite_direction_conflict = bool(
            opposite_multi_timeframe_technical.get("eligible")
            and opposite_mtf_score >= current_mtf_score + 8.0
        )
        if multi_timeframe_technical.get("available_timeframes"):
            policy_matched = bool(
                multi_timeframe_technical.get("eligible")
                and not opposite_direction_conflict
            )
            indicator_score = current_mtf_score
        else:
            policy_matched = bool(primary_policy_matched)
            indicator_score = float(primary_indicator_policy["technical_score"])
        indicator_policy = {
            **primary_indicator_policy,
            "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
            "passed": policy_matched,
            "technical_score": round(indicator_score, 4),
            "primary_timeframe": timeframe,
            "primary_timeframe_passed": bool(primary_policy_matched),
            "multi_timeframe": multi_timeframe_technical,
            "opposite_direction": opposite_multi_timeframe_technical,
            "opposite_direction_conflict": opposite_direction_conflict,
        }
        matched_indicator_keys = sorted(
            {
                str(key)
                for frame in dict(multi_timeframe_technical.get("frames") or {}).values()
                for key in dict(frame or {}).get("matched_indicator_keys") or []
            }
            or {
                str(item["key"]) for item in indicator_evidence if item["matched"]
            }
        )
        news_ids = sorted({item["id"] for item in candidate["news"] if item["id"]})
        evaluated_at = max(
            [int(scan.get("evaluated_at") or 0)]
            + [
                int(dict(frame or {}).get("evaluated_at") or 0)
                for frame in dict(multi_timeframe_technical.get("frames") or {}).values()
            ]
        )
        fingerprint = "|".join(
            [
                str(run.user_id),
                contract_symbol or candidate["symbol"],
                candidate["direction"],
                timeframe,
                str(evaluated_at),
                MULTI_TIMEFRAME_TECHNICAL_VERSION,
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
        existing_status = existing.status if existing is not None else None
        existing_prediction = (
            db.scalar(
                select(AiMonitorPrediction).where(
                    AiMonitorPrediction.opportunity_id == existing.id
                )
            )
            if existing is not None
            else None
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
        existing_news_ids = set(existing.news_ids_json or []) if existing is not None else set()
        has_new_material_news = bool(set(news_ids) - existing_news_ids)
        market = dict(
            market_flow_inputs.get("ticker", {}).get(contract_symbol.upper(), {})
        )
        entry_price = float(market.get("price") or 0)
        if market.get("price") is not None:
            market["price"] = entry_price
        risk_metrics = scan.get("risk_metrics")
        risk_metrics = (
            dict(risk_metrics) if isinstance(risk_metrics, Mapping) else {}
        )
        risk_plan = virtual_risk_plan_snapshot(
            entry_price=entry_price,
            direction=str(candidate["direction"]),
            timeframe=timeframe,
            atr_pct=(
                float(risk_metrics["atr_pct"])
                if risk_metrics.get("atr_pct") is not None
                else None
            ),
        )
        market_flow = market_flow_snapshot(
            market_flow_inputs,
            symbol=str(candidate["symbol"]),
            contract_symbol=contract_symbol,
            direction=str(candidate["direction"]),
            now=now,
        )
        realtime_feature = None
        if bool(uw_signal_policy["enabled"]):
            realtime_feature = realtime_features.get(
                str(candidate["symbol"]).upper()
            ) or (
                realtime_features.get(contract_symbol.upper())
                if contract_symbol
                else None
            )
        finnhub_quote_row = finnhub_signal_quotes.get(
            str(candidate["symbol"]).upper()
        )
        finnhub_quote = (
            _finnhub_signal_quote_payload(finnhub_quote_row, signal_at=now)
            if finnhub_quote_row is not None
            else {}
        )
        market_flow = apply_enhanced_market_domains(
            market_flow,
            realtime_feature,
            direction=str(candidate["direction"]),
            blend_into_legacy=bool(uw_signal_policy["score_enabled"]),
        )
        flow_score = float(market_flow["score"])
        score_weights = opportunity_score_weights(config)
        effective_score_weights = effective_opportunity_score_weights(config, market_flow)
        legacy_base_combined_score = weighted_opportunity_score(
            candidate["news_score"], indicator_score, flow_score, config, market_flow
        )
        company_profile = dict(
            market_flow_inputs.get("profile", {}).get(str(candidate["symbol"]).upper(), {})
        )
        market_environment = macro_market.opportunity_market_context(
            macro_snapshot,
            direction=str(candidate["direction"]),
            symbol=str(candidate["symbol"]),
            sector=company_profile.get("sector"),
            industry=company_profile.get("industry"),
        )
        legacy_combined_score = macro_market.apply_market_adjustment(
            legacy_base_combined_score,
            market_environment,
        )
        enhanced_domain_scoring = enhanced_opportunity_domain_score(
            news_score=float(candidate["news_score"]),
            technical_score=indicator_score,
            market_environment=market_environment,
            market_flow=market_flow,
            policy=uw_signal_policy,
        )
        if bool(uw_signal_policy["score_enabled"]) and enhanced_domain_scoring.get(
            "score"
        ) is not None:
            base_combined_score = float(enhanced_domain_scoring["score"])
            # The market-context domain already encodes the directional macro
            # adjustment, so applying the legacy additive adjustment again
            # would double count the same evidence.
            combined_score = base_combined_score
        else:
            base_combined_score = legacy_base_combined_score
            combined_score = legacy_combined_score
        normalized_realtime_feature = realtime_feature_payload(realtime_feature)
        market_session = str(
            dict(market_environment.get("market_session") or {}).get("key")
            or dict(normalized_realtime_feature.get("quote") or {}).get("market_session")
            or normalized_realtime_feature.get("market_session")
            or "unknown"
        ).lower()
        maximum_quote_age_ms = int(
            uw_thresholds[
                "quote_age_regular_ms"
                if market_session == "regular"
                else "quote_age_extended_ms"
            ]
        )
        market_quality = signal_market_quality(
            scan,
            market,
            timeframe,
            now,
            maximum_market_age_seconds=int(config["maximum_market_age_seconds"]),
            minimum_feature_quality=float(config["minimum_feature_quality"]),
            requires_prediction_features=requires_prediction_features,
            enhanced_feature=realtime_feature,
            risk_events=(
                [
                    *risk_events_by_symbol.get("*", []),
                    *risk_events_by_symbol.get(
                        str(candidate["symbol"]).upper(), []
                    ),
                ]
                if bool(uw_signal_policy["enabled"])
                else []
            ),
            maximum_quote_age_ms=maximum_quote_age_ms,
            maximum_spread_bps=float(uw_thresholds["spread_hard_max_bps"]),
            minimum_data_coverage=float(uw_thresholds["min_data_coverage"]),
            maximum_source_divergence_bps=float(
                uw_thresholds["source_divergence_max_bps"]
            ),
            halt_cooldown_seconds=int(uw_thresholds["halt_cooldown_minutes"]) * 60,
        )
        if not market_quality.get("quote") and finnhub_quote:
            # Finnhub provides an auditable signal-time spot observation while
            # UW is disabled or missing.  It must remain last-trade-only: the
            # executable NBBO checks above deliberately stay false.
            market_quality["quote"] = dict(finnhub_quote)
            market_quality["quote_source"] = "finnhub_quote_snapshots"
        # The exchange clock is authoritative.  A stale cash quote may describe
        # its last observed session as regular even after the close.
        market_quality["market_session"] = market_session
        gate_summary = stable_gate_summary(
            market_quality,
            market_flow,
            evaluated_at=now,
            policy_mode=str(uw_signal_policy["effective_mode"]),
        )
        score_components = opportunity_score_components(
            news_score=float(candidate["news_score"]),
            technical_score=indicator_score,
            market_flow=market_flow,
            base_combined_score=base_combined_score,
            market_environment=market_environment,
            combined_score=combined_score,
            configured_weights=score_weights,
            effective_weights=effective_score_weights,
            enhanced_domain_scoring=enhanced_domain_scoring,
            signal_policy=uw_signal_policy,
        )
        news_trigger = dict(candidate.get("news_trigger") or {})
        require_market_quality = bool(
            config.get("require_market_quality_for_prediction", True)
        )
        order_book_gate = (
            dict(market_flow.get("order_book_gate") or {})
            if isinstance(market_flow.get("order_book_gate"), Mapping)
            else None
        )
        gate_summary = prediction_actionability_gate_summary(
            gate_summary,
            market_quality=market_quality,
            market_environment=market_environment,
            news_trigger=news_trigger,
            order_book_gate=order_book_gate,
            require_new_news=require_new_news,
        )
        # This is the Binance execution-quality decision produced by the same
        # canonical gate used below and exposed to the UI.  Cash-market NBBO is
        # retained in warnings/score evidence, but cannot independently block
        # the simulated entry record.
        decision_market_quality_passed = bool(
            gate_summary.get("market_quality_passed")
        )
        decision_hard_conflict = not bool(
            dict(gate_summary.get("decision_checks") or {}).get(
                "directional_conflict_clear", True
            )
        )
        decision_market_quality = {
            **market_quality,
            "passed": decision_market_quality_passed,
            "raw_enhanced_passed": bool(market_quality.get("passed")),
            "policy_mode": str(uw_signal_policy["mode"]),
        }
        technical_confirmed = bool(
            policy_matched and indicator_score >= minimum_indicator_score
        )
        macro_entry_policy = dict(market_environment.get("entry_policy") or {})
        macro_threshold_delta = max(
            0.0, float(macro_entry_policy.get("threshold_delta") or 0)
        )
        effective_minimum_combined_score = min(
            100.0, minimum_combined_score + macro_threshold_delta
        )
        macro_entry_allowed = bool(
            macro_entry_policy.get("entry_allowed", True)
        )
        signal_confirmed = bool(
            technical_confirmed
            and combined_score >= effective_minimum_combined_score
            and macro_entry_allowed
            and bool(gate_summary["passed"])
            and (
                not require_new_news
                or bool(news_trigger.get("has_actionable_new_news"))
                or existing_prediction is not None
            )
            and (not require_market_quality or decision_market_quality_passed)
        )
        virtual_entry_gate = virtual_entry_gate_snapshot(
            direction=str(candidate["direction"]),
            news_score=float(candidate["news_score"]),
            news_mention_count=len(news_ids),
            minimum_news_score=float(config["minimum_news_confidence"]) * 100,
            minimum_news_mentions=int(config["minimum_news_mentions"]),
            indicator_policy_passed=bool(policy_matched),
            indicator_score=indicator_score,
            minimum_indicator_score=minimum_indicator_score,
            combined_score=combined_score,
            minimum_combined_score=effective_minimum_combined_score,
            market_flow_hard_conflict=decision_hard_conflict,
            entry_price=entry_price,
            checked_at=now,
            has_new_trigger_news=bool(
                news_trigger.get("has_actionable_new_news")
                or existing_prediction is not None
            ),
            require_new_trigger_news=require_new_news,
            market_quality_passed=decision_market_quality_passed,
            require_market_quality=require_market_quality,
            macro_entry_allowed=macro_entry_allowed,
            macro_policy=macro_entry_policy,
            order_book_gate=order_book_gate,
        )
        # One source of truth: opportunity lifecycle, simulated entry creation
        # and the frontend all consume this exact immutable gate snapshot.
        # Live-copy account state is deliberately absent; live trading is only
        # a downstream consumer of a committed prediction record.
        signal_confirmed = bool(virtual_entry_gate.get("entry_ready"))
        max_holding_seconds = (
            _TIMEFRAME_SECONDS[timeframe] * prediction_max_holding_bars
        )
        due_at = now + timedelta(seconds=max_holding_seconds)
        # Opportunity visibility and prediction risk now share one explicit
        # lifetime.  The technical timeframe no longer doubles as a one-bar
        # forced exit horizon.
        expires_at = due_at
        cost_breakdown = prediction_cost_breakdown(
            now,
            due_at,
            conservative_prediction_cost_config,
        )
        estimated_cost = float(cost_breakdown["total_cost_bps"])
        readiness = signal_readiness_snapshot(
            matched=technical_confirmed and not decision_hard_conflict,
            indicator_score=indicator_score,
            combined_score=combined_score,
            estimated_cost_bps=estimated_cost,
            market_quality=decision_market_quality,
            calibration=calibrations[candidate["direction"]],
            minimum_indicator_score=minimum_indicator_score,
            minimum_combined_score=effective_minimum_combined_score,
            safety_margin_bps=float(config["live_safety_margin_bps"]),
            market_flow=market_flow,
            market_flow_weight=float(score_weights["market_flow"]),
            minimum_market_flow_quality=float(
                config["minimum_market_flow_quality"]
            ),
            macro_entry_allowed=macro_entry_allowed,
            macro_policy=macro_entry_policy,
        )
        readiness["base_minimum_combined_score"] = minimum_combined_score
        readiness["macro_threshold_delta"] = macro_threshold_delta
        if readiness["status"] == "shadow_ready":
            shadow_ready += 1
        reference_price_time_ms = int(market.get("ts") or 0)
        if 0 < reference_price_time_ms < 1_000_000_000_000:
            reference_price_time_ms *= 1_000
        if not (0 < reference_price_time_ms <= _datetime_ms(now)):
            reference_price_time_ms = 0
        score_snapshot = {
            "news": float(candidate["news_score"]),
            "technical": indicator_score,
            "market_flow": flow_score,
            "market_flow_snapshot": market_flow_history_snapshot(market_flow),
            "base_combined": base_combined_score,
            "macro_adjustment": float(market_environment.get("adjustment") or 0),
            "macro_market": market_environment,
            "combined": combined_score,
            "enhanced_domains": dict(enhanced_domain_scoring.get("domains") or {}),
            "enhanced_effective_weights": dict(
                enhanced_domain_scoring.get("effective_weights") or {}
            ),
            "signal_policy_mode": str(uw_signal_policy["effective_mode"]),
            "weights_version": str(uw_signal_policy["weights_version"]),
            "direction": str(candidate["direction"]),
            "calculated_at": now.isoformat(),
            "reference_price": entry_price if entry_price > 0 and reference_price_time_ms else None,
            "reference_price_time_ms": reference_price_time_ms or None,
        }
        prediction_score_seed: list[dict[str, Any]] = []
        if existing_prediction is not None:
            prediction_evidence = dict(existing_prediction.evidence_json or {})
            if not isinstance(prediction_evidence.get("risk_plan"), Mapping):
                prediction_evidence["risk_plan"] = virtual_risk_plan_snapshot(
                    entry_price=float(existing_prediction.entry_price or 0),
                    direction=existing_prediction.direction,
                    timeframe=existing_prediction.timeframe,
                )
                existing_prediction.evidence_json = prediction_evidence
            prediction_signal_scores = prediction_evidence.get("signal_scores")
            prediction_signal_scores = (
                dict(prediction_signal_scores)
                if isinstance(prediction_signal_scores, Mapping)
                else {}
            )
            prediction_snapshot = prediction_evidence.get("score_snapshot")
            prediction_snapshot = (
                dict(prediction_snapshot)
                if isinstance(prediction_snapshot, Mapping)
                else {}
            )
            prediction_market_flow = prediction_evidence.get("market_flow")
            prediction_market_flow = (
                dict(prediction_market_flow)
                if isinstance(prediction_market_flow, Mapping)
                else {}
            )
            prediction_flow = prediction_signal_scores.get(
                "market_flow",
                prediction_snapshot.get(
                    "market_flow", prediction_market_flow.get("score", flow_score)
                ),
            )
            prediction_score_seed.append(
                {
                    "news": float(existing_prediction.signal_news_score or 0),
                    "technical": float(
                        existing_prediction.signal_indicator_score or 0
                    ),
                    "market_flow": float(prediction_flow),
                    "combined": float(existing_prediction.confidence_score),
                    "calculated_at": existing_prediction.predicted_at.isoformat(),
                }
            )
        score_history = append_score_history(
            dict(existing.evidence_json or {}) if existing is not None else None,
            score_snapshot,
            seed_snapshots=prediction_score_seed,
        )
        evidence = {
            "match_policy": INDICATOR_MATCH_POLICY,
            "indicator_scoring": "continuous_directional_mtf_v3",
            "direction": candidate["direction"],
            "direction_selection": dict(candidate.get("direction_selection") or {}),
            "multi_timeframe_technical": multi_timeframe_technical,
            "confirmed": signal_confirmed,
            "technical_confirmed": technical_confirmed,
            "market_available": bool(contract_symbol),
            "news": candidate["news"][:8],
            "indicators": indicator_evidence,
            "indicator_policy": indicator_policy,
            "matched_indicator_count": len(matched_indicator_keys),
            "configured_indicator_keys": indicator_keys,
            "available_indicator_count": sum(
                bool(item.get("available")) for item in indicator_evidence
            ),
            "required_indicator_count": len(indicator_keys),
            "evaluated_bar_time": evaluated_at,
            "market": market,
            "market_flow": market_flow,
            "quote": dict(market_quality.get("quote") or {}),
            "market_session": str(
                market_quality.get("market_session") or market_session or "unknown"
            ),
            "option_flow": dict(market_flow.get("option_flow") or {}),
            "gex": dict(market_flow.get("gex") or {}),
            "institutional_flow": dict(
                market_flow.get("institutional_flow") or {}
            ),
            "macro_market_snapshot": macro_snapshot,
            "market_environment": market_environment,
            "macro_entry_policy": macro_entry_policy,
            "base_minimum_combined_score": minimum_combined_score,
            "effective_minimum_combined_score": effective_minimum_combined_score,
            "risk_metrics": risk_metrics,
            "risk_plan": risk_plan,
            "max_holding": {
                "version": "timeframe_bars_v1",
                "bars": prediction_max_holding_bars,
                "timeframe": timeframe,
                "seconds": max_holding_seconds,
                "due_at": due_at.replace(tzinfo=UTC).isoformat(),
            },
            "score_weights": score_weights,
            "effective_score_weights": effective_score_weights,
            "enhanced_domain_scoring": enhanced_domain_scoring,
            "unusual_whales_policy": {
                "enabled": bool(uw_signal_policy["enabled"]),
                "mode": str(uw_signal_policy["mode"]),
                "effective_mode": str(uw_signal_policy["effective_mode"]),
                "thresholds": dict(uw_signal_policy["thresholds"]),
                "channels": dict(uw_signal_policy.get("channels") or {}),
                "weights": dict(uw_signal_policy["weights"]),
                "published_version": int(uw_signal_policy["published_version"]),
                "policy_version": str(uw_signal_policy["policy_version"]),
                "weights_version": str(uw_signal_policy["weights_version"]),
                "decision_version": str(uw_signal_policy["decision_version"]),
                "score_applied": bool(uw_signal_policy["score_enabled"]),
                "hard_gate_applied": bool(uw_signal_policy["hard_gate_enabled"]),
            },
            "news_trigger": news_trigger,
            "score_snapshot": score_snapshot,
            "score_history": score_history,
            "virtual_entry_gate": virtual_entry_gate,
            "minimum_news_score": float(config["minimum_news_confidence"]) * 100,
            "minimum_news_mentions": int(config["minimum_news_mentions"]),
            "news_lookback_hours": int(config["news_lookback_hours"]),
            "news_trigger_window_hours": trigger_window_hours,
            "require_market_quality_for_prediction": require_market_quality,
            "market_quality": market_quality,
            "gate_summary": gate_summary,
            "score_components": score_components,
            "data_quality": {
                "status": market_quality.get("data_status"),
                "coverage": market_quality.get("data_coverage"),
                "stale_fields": list(market_quality.get("stale_fields") or []),
                "quote_available": bool(market_quality.get("quote_available")),
                "quote_source": market_quality.get("quote_source"),
                "last_trade_only": bool(
                    dict(market_quality.get("quote") or {}).get("last_trade_only")
                ),
                "flow_quality": market_flow.get("data_quality"),
            },
            "version": {
                "api": OPPORTUNITY_API_VERSION,
                "feature": market_quality.get("feature_version")
                or MARKET_FEATURE_VERSION,
                "weights": str(uw_signal_policy["weights_version"]),
                "decision": str(
                    gate_summary.get("decision_version")
                    or uw_signal_policy["decision_version"]
                ),
                "policy": str(uw_signal_policy["policy_version"]),
                "published_config": int(uw_signal_policy["published_version"]),
            },
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
            # Live scores are recalculated on every opportunity scan, but the
            # signal lifetime must not slide forward forever just because the
            # same news remains inside the lookback window.  A still-unconfirmed
            # candidate may be extended only by genuinely new evidence.  Once
            # a prediction exists its original lifetime remains immutable.
            newly_confirmed = bool(signal_confirmed and existing_status != "discovered")
            if existing_prediction is None and newly_confirmed:
                opportunity.discovered_at = now
            opportunity.expires_at = merged_opportunity_expiration(
                opportunity.expires_at,
                expires_at,
                has_prediction=existing_prediction is not None,
                has_new_material_news=has_new_material_news,
                newly_confirmed=newly_confirmed,
            )
            opportunity.updated_at = now
            merged += 1
        db.flush()
        record_opportunity_gate_decision(
            db,
            opportunity,
            analysis_run_id=run.id,
            decision_at=now,
            evidence=evidence,
            feature=realtime_feature,
        )
        frozen_evidence = (
            dict(existing_prediction.evidence_json or {})
            if existing_prediction is not None
            else evidence
        )
        if opportunity.status == "discovered" or existing_prediction is not None:
            freeze_opportunity_market_snapshot(
                db,
                opportunity,
                evidence=frozen_evidence,
                feature=realtime_feature,
            )
        if opportunity.status == "discovered":
            if existing_prediction is None:
                prediction_evidence = {
                    **evidence,
                    "signal_scores": {
                        "news": candidate["news_score"],
                        "indicator": indicator_score,
                        "market_flow": flow_score,
                        "enhanced_domains": dict(
                            enhanced_domain_scoring.get("domains") or {}
                        ),
                        "enhanced_effective_weights": dict(
                            enhanced_domain_scoring.get("effective_weights") or {}
                        ),
                        "base_combined": base_combined_score,
                        "macro_adjustment": float(
                            market_environment.get("adjustment") or 0
                        ),
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
        "opportunity_cleanup": cleanup,
        "risk_plan_backfill": risk_plan_backfill,
        "rescored_pending_predictions": rescored_pending_predictions,
        "macro_market": {
            "available": bool(macro_snapshot.get("available")),
            "captured_at": macro_snapshot.get("captured_at"),
            "sentiment": macro_snapshot.get("sentiment"),
            "event_risk": (macro_snapshot.get("events") or {}).get("risk_level"),
        },
        "reused_news_skipped_count": reused_news_skipped,
        "monitor_symbols": monitor_symbols,
        "monitor_scope": "selected" if monitor_symbols else "all",
        "indicator_keys": indicator_keys,
        "match_policy": INDICATOR_MATCH_POLICY,
        "indicator_scoring": "continuous_directional_mtf_v3",
        "multi_timeframe_policy": {
            "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
            "weights": dict(MULTI_TIMEFRAME_TECHNICAL_WEIGHTS),
            "rule": "1h 可独立确认；否则必须由 15m 与 4h 共同确认",
        },
        "minimum_indicator_score": minimum_indicator_score,
        "minimum_combined_score": minimum_combined_score,
        "timeframe": timeframe,
        "prediction_max_holding_bars": prediction_max_holding_bars,
        "news_lookback_hours": int(config["news_lookback_hours"]),
        "news_trigger_window_hours": trigger_window_hours,
        "require_new_news_trigger": require_new_news,
        "require_market_quality_for_prediction": bool(
            config.get("require_market_quality_for_prediction", True)
        ),
        "unusual_whales_signal_policy": {
            "enabled": bool(uw_signal_policy["enabled"]),
            "mode": str(uw_signal_policy["mode"]),
            "effective_mode": str(uw_signal_policy["effective_mode"]),
            "published_version": int(uw_signal_policy["published_version"]),
            "weights_version": str(uw_signal_policy["weights_version"]),
            "decision_version": str(uw_signal_policy["decision_version"]),
            "score_applied": bool(uw_signal_policy["score_enabled"]),
            "hard_gate_applied": bool(uw_signal_policy["hard_gate_enabled"]),
        },
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
            summary["read_models"] = refresh_ai_monitor_read_models(
                db,
                user_id=run.user_id,
                prediction_limit=1000,
                score_limit=5000,
            )
            run.summary_json = summary
            run.status = "partial" if summary["failed_symbols"] else "completed"
            run.completed_at = utcnow()
            db.commit()
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"[ai-monitor] opportunity run internal error: {error_type}: {exc}")
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
        args=(engine, master_key, symbols_config),
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
    stale_claim_result = db.execute(
        update(News)
        .where(
            News.ai_claim_batch_id.is_not(None),
            or_(
                News.ai_claimed_at.is_(None),
                News.ai_claimed_at < stale_cutoff,
                News.ai_claim_batch_id.not_in(
                    select(NewsAiBatch.id).where(
                        NewsAiBatch.status.in_(ACTIVE_RUN_STATUSES)
                    )
                ),
            ),
        )
        .values(ai_claim_batch_id=None, ai_claimed_at=None)
    )
    return {
        "batches": int(batch_result.rowcount or 0),
        "runs": int(run_result.rowcount or 0),
        "claims": int(stale_claim_result.rowcount or 0),
    }


def _worker_loop(engine: Engine, master_key: str, symbols_config: Path) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    repository = MonitorRepository(engine, symbols_config)
    while True:
        try:
            with factory() as db:
                recover_stale_runs(db)
                reopen_legacy_prediction_settlements(db)
                settlement = settle_due_predictions(db, repository)
                if settlement["completed"] or settlement["unavailable"]:
                    refresh_ai_monitor_read_models(
                        db,
                        prediction_limit=1000,
                        score_limit=5000,
                    )
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
        time.sleep(PREDICTION_SETTLEMENT_POLL_SECONDS)


def _ingest_worker_loop(engine: Engine, master_key: str, symbols_config: Path) -> None:
    """Run collector-triggered model calls independently from backlog scans."""

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    while True:
        # Poll as well as listening for collector events.  This keeps the fast
        # lane independent from the heavier prediction settlement scheduler and
        # lets a new instance rescue failures emitted by an older deployment.
        _worker_wakeup.wait(timeout=PREDICTION_SETTLEMENT_POLL_SECONDS)
        _worker_wakeup.clear()
        try:
            with factory() as db:
                rescued = _enqueue_failed_legacy_news(db)
            if rescued:
                print(f"[ai-monitor] queued {rescued} news from legacy failed batches")
            while _run_ingested_news(factory, engine, master_key, symbols_config):
                pass
        except Exception as exc:
            print(f"[ai-monitor] immediate news worker error: {type(exc).__name__}")


def _run_ingested_news(
    factory: sessionmaker[Session],
    engine: Engine,
    master_key: str,
    symbols_config: Path,
) -> bool:
    """Immediately analyze one deduplicated batch emitted by the collector."""

    news_ids = _take_ingested_news()
    if not news_ids:
        return False
    minimum_news_ts = int(time.time()) - LIVE_NEWS_MAX_AGE_SECONDS
    with factory() as db:
        pending_ids = list(
            db.scalars(
                select(News.id)
                .where(
                    News.id.in_(news_ids),
                    News.ai_analyzed_at.is_(None),
                    News.ts >= minimum_news_ts,
                )
                .order_by(News.ts.desc(), News.id.desc())
            ).all()
        )
        if not pending_ids:
            return False
        if not global_ai_model_configured(db):
            return False
        user_id = db.scalar(
            select(AiMonitorConfig.user_id)
            .where(AiMonitorConfig.enabled.is_(True))
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
        symbols_config=symbols_config,
        trigger_opportunity=True,
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
