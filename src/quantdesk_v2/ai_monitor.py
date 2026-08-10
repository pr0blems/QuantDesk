"""Tenant-scoped AI news analysis and technical-indicator opportunity scans."""

from __future__ import annotations

import hashlib
import json
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
    News,
    NewsAiBatch,
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
        "last_news_run_at": config.last_news_run_at,
        "last_opportunity_run_at": config.last_opportunity_run_at,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
        "persisted": True,
        "match_policy": "all",
        "indicator_conflicts": indicator_conflicts(indicator_keys),
    }


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
        "best_directional_return_bps": round(max(returns), 4) if returns else None,
        "worst_directional_return_bps": round(min(returns), 4) if returns else None,
        "confirmed_count": sum(bool(item.get("technical_confirmed")) for item in items),
        "candidate_count": sum(not bool(item.get("technical_confirmed")) for item in items),
    }


def settleable_historical_outcomes(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Exclude expired opportunities that cannot be priced at their settlement time."""

    return [dict(item) for item in items if item.get("result") != "unavailable"]


def historical_opportunity_analytics(
    db: Session,
    repository: MonitorRepository,
    user_id: int,
    *,
    limit: int = 300,
) -> dict[str, Any]:
    """Evaluate expired opportunities against the nearest 15-minute market price."""

    now = utcnow()
    opportunities = db.scalars(
        select(AiMonitorOpportunity)
        .where(
            AiMonitorOpportunity.user_id == user_id,
            or_(
                AiMonitorOpportunity.expires_at <= now,
                AiMonitorOpportunity.status.in_(("expired", "dismissed")),
            ),
        )
        .order_by(AiMonitorOpportunity.discovered_at.desc(), AiMonitorOpportunity.id.desc())
        .limit(limit)
    ).all()
    window_ms = 45 * 60 * 1_000
    grouped: dict[str, list[AiMonitorOpportunity]] = {}
    for item in opportunities:
        evidence = dict(item.evidence_json or {})
        market = evidence.get("market") if isinstance(evidence.get("market"), Mapping) else {}
        entry_price = float(market.get("price") or 0)
        if evidence.get("market_available") is False or entry_price <= 0:
            continue
        grouped.setdefault(item.contract_symbol, []).append(item)

    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        targets = [_datetime_ms(item.expires_at) for item in symbol_items]
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(targets) - window_ms,
                max(targets) + window_ms,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []

    outcomes: list[dict[str, Any]] = []
    for item in opportunities:
        evidence = dict(item.evidence_json or {})
        market = evidence.get("market") if isinstance(evidence.get("market"), Mapping) else {}
        entry_price = float(market.get("price") or 0)
        settlement = historical_settlement_price(
            candles_by_symbol.get(item.contract_symbol, []),
            _datetime_ms(item.expires_at),
        )
        result: dict[str, Any] | None = None
        if entry_price > 0 and settlement is not None:
            result = prediction_outcome(entry_price, float(settlement["price"]), item.direction)
        outcomes.append(
            {
                "id": item.public_id,
                "symbol": item.symbol,
                "contract_symbol": item.contract_symbol,
                "direction": item.direction,
                "timeframe": item.timeframe,
                "technical_confirmed": bool(evidence.get("confirmed", item.status == "discovered")),
                "news_score": float(item.news_score),
                "indicator_score": float(item.indicator_score),
                "combined_score": float(item.combined_score),
                "entry_price": entry_price if entry_price > 0 else None,
                "exit_price": float(settlement["price"]) if settlement else None,
                "settled_price_at": datetime.fromtimestamp(
                    int(settlement["price_time_ms"]) / 1_000, UTC
                )
                if settlement
                else None,
                "raw_return_bps": result["raw_return_bps"] if result else None,
                "directional_return_bps": result["directional_return_bps"] if result else None,
                "result": result["result"] if result else "unavailable",
                "signal_time": item.discovered_at,
                "expires_at": item.expires_at,
            }
        )
    settleable = settleable_historical_outcomes(outcomes)
    summary = summarize_historical_opportunities(settleable)
    summary["discarded_unavailable_count"] = len(outcomes) - len(settleable)
    return {
        "summary": summary,
        "items": settleable,
        "note": "按历史机会有效期结束时附近的 15 分钟真实行情计算；仅作虚拟统计，不执行任何交易。",
    }


def settle_due_predictions(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Settle due virtual predictions at the nearest historical due-time price."""

    now = utcnow()
    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.status == "pending",
            AiMonitorPrediction.due_at <= now,
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
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(targets) - window_ms,
                max(targets) + window_ms,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []
    completed = 0
    unavailable = 0
    for item in items:
        settlement = historical_settlement_price(
            candles_by_symbol.get(item.contract_symbol, []),
            _datetime_ms(item.due_at),
        )
        exit_price = float(settlement["price"]) if settlement is not None else 0.0
        entry_price = float(item.entry_price or 0)
        if entry_price <= 0 or exit_price <= 0:
            item.status = "unavailable"
            item.completed_at = now
            unavailable += 1
            continue
        outcome = prediction_outcome(entry_price, exit_price, item.direction)
        item.status = "completed"
        item.result = str(outcome["result"])
        item.exit_price = Decimal(str(exit_price))
        item.raw_return_bps = Decimal(str(outcome["raw_return_bps"]))
        item.directional_return_bps = Decimal(str(outcome["directional_return_bps"]))
        item.completed_at = now
        completed += 1
    db.flush()
    return {"completed": completed, "unavailable": unavailable}


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
        evidence.append(
            {
                "key": key,
                "name": (item.get("bearish_name") if bearish_strategy else item.get("name"))
                if item
                else key,
                "matched": matched,
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
    awaiting_indicators = 0
    predictions_created = 0
    duplicated = 0
    merged = 0
    failed_symbols: list[str] = []
    unmapped_symbols: list[str] = [str(item["symbol"]) for item in unmapped_candidates]
    indicator_keys = list(config["indicator_keys"])
    timeframe = str(config["timeframe"])
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
                len(matched_indicator_keys) / len(indicator_keys) * 100,
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
        if contract_symbol:
            try:
                ticker = repository._query(
                    "SELECT price,ts FROM ticker WHERE symbol=? LIMIT 1",
                    (contract_symbol,),
                )
            except MonitorUnavailable:
                ticker = []
        else:
            ticker = []
        market = dict(ticker[0]) if ticker else {}
        entry_price = float(market.get("price") or 0)
        if market.get("price") is not None:
            market["price"] = entry_price
        combined_score = round(candidate["news_score"] * 0.55 + indicator_score * 0.45, 4)
        expires_at = now + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe] * 2)
        evidence = {
            "match_policy": "all",
            "direction": candidate["direction"],
            "confirmed": matched,
            "market_available": bool(contract_symbol),
            "news": candidate["news"][:8],
            "indicators": indicator_evidence,
            "matched_indicator_count": len(matched_indicator_keys),
            "required_indicator_count": len(indicator_keys),
            "evaluated_bar_time": evaluated_at,
            "market": market,
        }
        if existing is None:
            opportunity = AiMonitorOpportunity(
                public_id=str(uuid.uuid4()),
                user_id=run.user_id,
                analysis_run_id=run.id,
                symbol=candidate["symbol"],
                contract_symbol=contract_symbol or candidate["symbol"],
                direction=candidate["direction"],
                status="discovered" if matched else "candidate",
                timeframe=timeframe,
                news_score=Decimal(str(candidate["news_score"])),
                indicator_score=Decimal(str(indicator_score)),
                combined_score=Decimal(str(combined_score)),
                matched_indicator_keys_json=matched_indicator_keys,
                news_ids_json=news_ids,
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
            opportunity.status = (
                "discovered" if matched or opportunity.status == "discovered" else "candidate"
            )
            opportunity.news_score = Decimal(str(candidate["news_score"]))
            opportunity.indicator_score = Decimal(str(indicator_score))
            opportunity.combined_score = Decimal(str(combined_score))
            opportunity.matched_indicator_keys_json = matched_indicator_keys
            opportunity.news_ids_json = news_ids
            if opportunity.status == "discovered":
                evidence["confirmed"] = True
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
            due_at = now + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
            if prediction_exists is None:
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
                        entry_price=Decimal(str(entry_price)) if entry_price > 0 else None,
                        evidence_json=evidence,
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
        "awaiting_indicator_count": awaiting_indicators,
        "prediction_count": predictions_created,
        "duplicate_count": duplicated,
        "failed_symbols": failed_symbols[:20],
        "unmapped_symbols": unmapped_symbols[:50],
        "settled_predictions": settlement,
        "monitor_symbols": monitor_symbols,
        "monitor_scope": "selected" if monitor_symbols else "all",
        "indicator_keys": indicator_keys,
        "match_policy": "all",
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
