"""Read-only presentation of the battle engine's persisted prediction features."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

PREDICTION_FEATURE_COUNT = 8
FEATURE_NEUTRAL_BAND = 0.05
MICRO_MAX_AGE_MS = 5 * 60 * 1_000
POSITIONING_MAX_AGE_MS = 10 * 60 * 1_000
SNAPSHOT_MAX_AGE_MS = 10 * 60 * 1_000


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _score(value: float | None) -> str:
    return "--" if value is None else f"{value:+.3f}"


def _quality(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.1f}%"


def _age(value: float | None) -> str:
    if value is None or value >= 10**11:
        return "不可用"
    if value < 60_000:
        return f"{max(0, round(value / 1_000))} 秒"
    return f"{value / 60_000:.1f} 分钟"


def _direction(value: float) -> str:
    if value >= FEATURE_NEUTRAL_BAND:
        return "bullish"
    if value <= -FEATURE_NEUTRAL_BAND:
        return "bearish"
    return "neutral"


def _direction_label(status: str) -> str:
    return {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(
        status, "数据不足"
    )


def _directional_strength(
    value: float | None,
    quality_score: float | None,
    direction_sign: int,
) -> float | None:
    """Map a normalized -1..1 engine feature to a quality-adjusted 0..100 score."""

    if value is None:
        return None
    normalized = max(-1.0, min(1.0, direction_sign * value))
    quality = max(0.0, min(1.0, quality_score if quality_score is not None else 0.5))
    raw_score = 50.0 + normalized * 50.0
    adjusted = 50.0 + (raw_score - 50.0) * (0.5 + quality * 0.5)
    return round(max(0.0, min(100.0, adjusted)), 4)


def _metric(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value}


def _item(
    *,
    key: str,
    name: str,
    category: str,
    description: str,
    value: float | None,
    available: bool,
    source: str,
    source_age_ms: float | None,
    quality_score: float | None,
) -> dict[str, Any]:
    status = _direction(value) if available and value is not None else "insufficient"
    direction = _direction_label(status)
    if status == "insufficient":
        summary = f"{source}当前不可用或已经过期，本项不参与方向判断。"
    else:
        summary = (
            f"归一化得分 {_score(value)}，当前为{direction}；"
            f"±{FEATURE_NEUTRAL_BAND:.2f} 内按中性展示。"
        )
    return {
        "key": f"prediction_{key}",
        "feature_key": key,
        "name": name,
        "category": f"预测因子 · {category}",
        "description": description,
        "triggered": None if status == "insufficient" else status != "neutral",
        "status": status,
        "direction": None if status == "insufficient" else status,
        "summary": summary,
        "source": source,
        "available": available,
        "normalized_value": value,
        "source_age_ms": source_age_ms,
        "quality_score": quality_score,
        "bullish_strength": _directional_strength(value, quality_score, 1)
        if available
        else None,
        "bearish_strength": _directional_strength(value, quality_score, -1)
        if available
        else None,
        "metrics": [
            _metric("归一化得分", _score(value)),
            _metric("当前方向", direction),
            _metric("来源时效", _age(source_age_ms)),
            _metric("快照质量", _quality(quality_score)),
        ],
    }


def _definitions(timeframe: str) -> Sequence[tuple[str, str, str, str, str, str]]:
    trend_field = {"15m": "trend_15m", "1h": "trend_1h", "4h": "trend_4h"}.get(
        timeframe, "trend_15m"
    )
    return (
        (
            "aggressive_flow",
            "主动成交",
            "成交",
            "主动买入与主动卖出的强弱差异，正值偏多、负值偏空。",
            "micro",
            "逐笔成交",
        ),
        (
            "book_imbalance",
            "订单失衡",
            "盘口",
            "完整深度买卖盘的力量差异，反映订单簿整体倾斜。",
            "micro",
            "订单簿深度",
        ),
        (
            "book_imbalance_5",
            "近百档失衡",
            "盘口",
            "盘口近端流动性的买卖力量差异，强调靠近成交价的挂单。",
            "micro",
            "近端订单簿",
        ),
        (
            "velocity",
            "价格速度",
            "动量",
            "最近一分钟价格变化速度经归一化后的短线动量。",
            "micro",
            "实时价格流",
        ),
        (
            "flash_imbalance",
            "闪动失衡",
            "动量",
            "近 30 分钟上涨与下跌闪动次数的方向差异。",
            "snapshot",
            "闪动统计",
        ),
        (
            "taker_flow",
            "Taker流向",
            "成交",
            "Binance 主动买卖比的归一化结果，反映吃单资金方向。",
            "positioning",
            "Taker 买卖比",
        ),
        (
            "price_oi_impulse",
            "价格×持仓量",
            "持仓",
            "价格变化与未平仓量变化结合后的联合冲量。",
            "positioning",
            "价格与持仓量",
        ),
        (
            trend_field,
            "周期趋势",
            "趋势",
            f"{timeframe} 周期趋势评分的归一化结果。",
            "snapshot",
            f"{timeframe} K 线评分",
        ),
    )


def evaluate_prediction_feature_indicators(
    snapshot: Mapping[str, Any] | None,
    timeframe: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Render the eight battle inputs without recomputing or fabricating source data."""

    current_ms = int(time.time() * 1_000) if now_ms is None else int(now_ms)
    row = snapshot or {}
    raw_features = row.get("features")
    features = raw_features if isinstance(raw_features, Mapping) else {}
    as_of_ms = int(_optional_number(row.get("as_of_ms")) or 0)
    snapshot_age_ms = max(0, current_ms - as_of_ms) if as_of_ms else 10**12
    micro_age_ms = _optional_number(features.get("micro_age_ms"))
    positioning_age_ms = _optional_number(features.get("positioning_age_ms"))
    quality_score = _optional_number(row.get("quality_score"))
    if quality_score is None:
        quality_score = _optional_number(features.get("data_quality"))
    snapshot_fresh = bool(as_of_ms and snapshot_age_ms <= SNAPSHOT_MAX_AGE_MS)

    items: list[dict[str, Any]] = []
    for field, name, category, description, source_kind, source in _definitions(timeframe):
        value = _optional_number(features.get(field))
        source_age_ms = snapshot_age_ms
        available = snapshot_fresh
        if source_kind == "micro":
            source_age_ms = micro_age_ms
            available = snapshot_fresh and micro_age_ms is not None and micro_age_ms <= MICRO_MAX_AGE_MS
            if field in {"aggressive_flow", "velocity"}:
                # These values used to be persisted as neutral fallbacks even
                # though the depth stream did not contain signed trades or a
                # one-minute price series.  Treat both legacy and explicitly
                # unavailable snapshots as missing rather than bearish/bullish
                # failures.
                available = available and bool(features.get(f"{field}_available", False))
        elif source_kind == "positioning":
            source_age_ms = positioning_age_ms
            available = (
                snapshot_fresh
                and positioning_age_ms is not None
                and positioning_age_ms <= POSITIONING_MAX_AGE_MS
            )
        items.append(
            _item(
                key="trend" if field.startswith("trend_") else field,
                name=name,
                category=category,
                description=description,
                value=value,
                available=available,
                source=source,
                source_age_ms=source_age_ms,
                quality_score=quality_score,
            )
        )

    return {
        "timeframe": timeframe,
        "as_of_ms": as_of_ms or None,
        "snapshot_age_ms": snapshot_age_ms if as_of_ms else None,
        "quality_score": quality_score,
        "count": len(items),
        "bullish_count": sum(item["status"] == "bullish" for item in items),
        "bearish_count": sum(item["status"] == "bearish" for item in items),
        "neutral_count": sum(item["status"] == "neutral" for item in items),
        "insufficient_count": sum(item["status"] == "insufficient" for item in items),
        "items": items,
    }
