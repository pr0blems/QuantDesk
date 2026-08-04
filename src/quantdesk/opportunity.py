"""Shared market feature snapshots and explainable opportunity scanning."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

from quantdesk_v2.strategy_runtime import (
    StrategyMarketDataError,
    adx_series,
    atr_series,
    ema_series,
    normalize_candles,
)

from . import store

FEATURE_SET_KEY = "core_market_features"
FEATURE_SET_VERSION = 1
SCANNER_KEY = "market_bias_v1"
SCANNER_VERSION = 1
TIMEFRAME_SECONDS = {"15m": 900, "1h": 3_600, "4h": 14_400}
FEATURE_PARAMETERS = {
    "fast_ema": 20,
    "slow_ema": 50,
    "atr": 14,
    "adx": 14,
    "donchian": 20,
    "volume": 20,
}
PARAMS_HASH = hashlib.sha256(
    json.dumps(FEATURE_PARAMETERS, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class OpportunityDataError(ValueError):
    """Raised when a symbol cannot produce a trustworthy feature snapshot."""


def _timestamp_scale(timestamp: int) -> int:
    return 1_000 if timestamp >= 100_000_000_000 else 1


def compute_feature_snapshot(
    rows: list[dict[str, Any]], timeframe: str
) -> dict[str, Any]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise OpportunityDataError("unsupported opportunity timeframe")
    try:
        candles = normalize_candles(rows)
    except StrategyMarketDataError as exc:
        raise OpportunityDataError(str(exc)) from None
    if len(candles) < 55:
        raise OpportunityDataError("at least 55 closed candles are required")

    closes = [item.close for item in candles]
    highs = [item.high for item in candles]
    lows = [item.low for item in candles]
    volumes = [item.volume for item in candles]
    fast = ema_series(closes, FEATURE_PARAMETERS["fast_ema"])
    slow = ema_series(closes, FEATURE_PARAMETERS["slow_ema"])
    atr = atr_series(highs, lows, closes, FEATURE_PARAMETERS["atr"])
    adx, plus_di, minus_di = adx_series(
        highs, lows, closes, FEATURE_PARAMETERS["adx"]
    )
    required = (fast[-1], fast[-2], slow[-1], atr[-1], adx[-1], plus_di[-1], minus_di[-1])
    if any(value is None or not math.isfinite(float(value)) for value in required):
        raise OpportunityDataError("indicator warmup is incomplete")

    prior = candles[-FEATURE_PARAMETERS["donchian"] - 1 : -1]
    volume_window = volumes[-FEATURE_PARAMETERS["volume"] - 1 : -1]
    average_volume = sum(volume_window) / len(volume_window)
    latest = candles[-1]
    fast_now = float(fast[-1])
    fast_previous = float(fast[-2])
    slow_now = float(slow[-1])
    atr_now = float(atr[-1])
    scale = _timestamp_scale(latest.open_time)
    expected_delta = TIMEFRAME_SECONDS[timeframe] * scale
    recent = candles[-100:]
    gaps = sum(
        max(0, round((current.open_time - previous.open_time) / expected_delta) - 1)
        for previous, current in zip(recent, recent[1:], strict=False)
        if current.open_time > previous.open_time
    )
    return {
        "symbol_timeframe": timeframe,
        "bar_open_time": latest.open_time,
        "close": latest.close,
        "fast_ema": fast_now,
        "slow_ema": slow_now,
        "fast_slope_pct": (fast_now - fast_previous) / fast_previous * 100,
        "distance_fast_atr": (latest.close - fast_now) / atr_now,
        "atr": atr_now,
        "atr_pct": atr_now / latest.close * 100,
        "adx": float(adx[-1]),
        "plus_di": float(plus_di[-1]),
        "minus_di": float(minus_di[-1]),
        "prior_high": max(item.high for item in prior),
        "prior_low": min(item.low for item in prior),
        "volume_ratio": latest.volume / average_volume if average_volume > 0 else 0.0,
        "momentum_20_pct": (latest.close / closes[-21] - 1) * 100,
        "quality": {
            "usable": True,
            "closed_bar_only": True,
            "bars": len(candles),
            "recent_missing_bars": gaps,
            "coverage_pct": round(len(recent) / (len(recent) + gaps) * 100, 4),
        },
    }


def _timeframe_bias(feature: dict[str, Any]) -> float:
    trend = 30.0 if feature["fast_ema"] > feature["slow_ema"] else -30.0
    slope = max(-20.0, min(20.0, float(feature["fast_slope_pct"]) * 100))
    directional = 18.0 if feature["plus_di"] > feature["minus_di"] else -18.0
    price = 12.0 if feature["close"] > feature["fast_ema"] else -12.0
    strength = min(20.0, max(0.0, (float(feature["adx"]) - 15.0) * 0.8))
    raw = trend + slope + directional + price
    return max(-100.0, min(100.0, raw + math.copysign(strength, raw)))


def classify_opportunity(features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(features) != {"15m", "1h", "4h"}:
        raise OpportunityDataError("15m, 1h and 4h feature snapshots are required")
    biases = {timeframe: _timeframe_bias(feature) for timeframe, feature in features.items()}
    composite = biases["4h"] * 0.4 + biases["1h"] * 0.35 + biases["15m"] * 0.25
    trigger = features["15m"]
    setup = features["1h"]
    if composite >= 25:
        direction = "long"
    elif composite <= -25:
        direction = "short"
    else:
        direction = "neutral"

    breakout_long = trigger["close"] > trigger["prior_high"]
    breakout_short = trigger["close"] < trigger["prior_low"]
    trigger_aligned = (direction == "long" and breakout_long) or (
        direction == "short" and breakout_short
    )
    setup_near_ema = abs(float(setup["distance_fast_atr"])) <= 0.75
    volume_confirmed = float(trigger["volume_ratio"]) >= 1.1
    if direction == "neutral":
        status = "watching"
    elif trigger_aligned and volume_confirmed:
        status = "confirmed"
    elif setup_near_ema:
        status = "watching"
    else:
        status = "detected"

    quality_score = min(
        100.0,
        abs(composite) * 0.65
        + min(float(features["4h"]["adx"]), 40.0) * 0.55
        + min(float(trigger["volume_ratio"]), 3.0) * 5.0,
    )
    if direction == "neutral":
        quality_score = min(49.0, abs(composite) + 10.0)

    reasons = [f"BIAS_{direction.upper()}"]
    if setup_near_ema:
        reasons.append("SETUP_NEAR_EMA20")
    if trigger_aligned:
        reasons.append("STRUCTURE_BREAKOUT")
    if volume_confirmed:
        reasons.append("VOLUME_CONFIRMED")
    if float(features["4h"]["adx"]) >= 18:
        reasons.append("REGIME_TRENDING")
    labels = {"long": "偏多机会", "short": "偏空机会", "neutral": "中性观察"}
    return {
        "scanner_key": SCANNER_KEY,
        "scanner_version": SCANNER_VERSION,
        "primary_timeframe": "15m",
        "direction": direction,
        "status": status,
        "quality_score": round(quality_score, 4),
        "detected_bar_time": int(trigger["bar_open_time"]),
        "expires_bar_time": int(trigger["bar_open_time"])
        + 2 * TIMEFRAME_SECONDS["15m"] * _timestamp_scale(int(trigger["bar_open_time"])),
        "evidence": {
            "summary": f"{labels[direction]} · 质量 {quality_score:.1f}",
            "reason_codes": reasons,
            "composite_bias": round(composite, 4),
            "timeframe_bias": {key: round(value, 4) for key, value in biases.items()},
            "conditions": {
                "setup_near_ema20": setup_near_ema,
                "trigger_aligned": trigger_aligned,
                "volume_confirmed": volume_confirmed,
            },
            "features": features,
        },
    }


def scan_symbol(symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    features = {
        timeframe: compute_feature_snapshot(store.get_klines(normalized, timeframe, 600), timeframe)
        for timeframe in ("15m", "1h", "4h")
    }
    opportunity = classify_opportunity(features)
    detected = int(opportunity["detected_bar_time"])
    dedup_key = f"{SCANNER_KEY}:{normalized}:15m:{detected}"
    with store.transaction() as transaction:
        for timeframe, feature in features.items():
            quality = feature.pop("quality")
            transaction.execute(
                """INSERT INTO market_feature_snapshots(
                       symbol,timeframe,bar_open_time,feature_set_key,feature_set_version,
                       params_hash,values_json,quality_json,computed_at
                   ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON DUPLICATE KEY UPDATE values_json=VALUES(values_json),
                       quality_json=VALUES(quality_json),computed_at=VALUES(computed_at)""",
                (
                    normalized,
                    timeframe,
                    feature["bar_open_time"],
                    FEATURE_SET_KEY,
                    FEATURE_SET_VERSION,
                    PARAMS_HASH,
                    json.dumps(feature, ensure_ascii=False),
                    json.dumps(quality, ensure_ascii=False),
                ),
            )
            feature["quality"] = quality
        transaction.execute(
            """UPDATE market_opportunities SET status='expired',updated_at=CURRENT_TIMESTAMP
               WHERE scanner_key=? AND symbol=?
                 AND status IN ('detected','watching','confirmed') AND expires_bar_time<?""",
            (SCANNER_KEY, normalized, detected),
        )
        transaction.execute(
            """INSERT INTO market_opportunities(
                   public_id,scanner_key,scanner_version,symbol,primary_timeframe,direction,
                   status,quality_score,detected_bar_time,expires_bar_time,evidence_json,
                   dedup_key,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE direction=VALUES(direction),status=VALUES(status),
                   quality_score=VALUES(quality_score),expires_bar_time=VALUES(expires_bar_time),
                   evidence_json=VALUES(evidence_json),updated_at=VALUES(updated_at)""",
            (
                str(uuid.uuid4()),
                SCANNER_KEY,
                SCANNER_VERSION,
                normalized,
                opportunity["primary_timeframe"],
                opportunity["direction"],
                opportunity["status"],
                opportunity["quality_score"],
                opportunity["detected_bar_time"],
                opportunity["expires_bar_time"],
                json.dumps(opportunity["evidence"], ensure_ascii=False),
                dedup_key,
            ),
        )
    return {"symbol": normalized, **opportunity, "dedup_key": dedup_key}


def scan_all(symbols: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    scanned = 0
    failed = 0
    directions = {"long": 0, "short": 0, "neutral": 0}
    with store.advisory_lock("quantdesk-opportunity-scan", 0) as acquired:
        if not acquired:
            return {"scanned": 0, "failed": 0, "skipped": True, "directions": directions}
        for symbol in symbols:
            try:
                result = scan_symbol(symbol)
            except (OpportunityDataError, StrategyMarketDataError, KeyError, ValueError) as exc:
                failed += 1
                print(f"[opportunity] {symbol} skipped: {type(exc).__name__}")
                continue
            except Exception as exc:
                failed += 1
                print(f"[opportunity] {symbol} failed: {type(exc).__name__}")
                continue
            scanned += 1
            directions[result["direction"]] += 1
    return {
        "scanned": scanned,
        "failed": failed,
        "skipped": False,
        "directions": directions,
        "elapsed_ms": round((time.monotonic() - started) * 1_000, 2),
    }
