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


def compute_feature_snapshot(rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
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
    adx, plus_di, minus_di = adx_series(highs, lows, closes, FEATURE_PARAMETERS["adx"])
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
    probability = max(0.35, min(0.75, 0.5 + (quality_score - 50.0) / 200.0))
    expected_value = probability * 100.0 - (1.0 - probability) * 70.0 - 2.0

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
        "entry_price": float(trigger["close"]),
        "expected_value_score": round(expected_value if direction != "neutral" else 0.0, 4),
        "detected_bar_time": int(trigger["bar_open_time"]),
        "expires_bar_time": int(trigger["bar_open_time"])
        + 2 * TIMEFRAME_SECONDS["15m"] * _timestamp_scale(int(trigger["bar_open_time"])),
        "evidence": {
            "summary": f"{labels[direction]} · 质量 {quality_score:.1f}",
            "reason_codes": reasons,
            "composite_bias": round(composite, 4),
            "estimated_probability": round(probability, 4),
            "expected_value_bps": round(expected_value if direction != "neutral" else 0.0, 4),
            "calibration_state": "heuristic_pending_outcomes",
            "timeframe_bias": {key: round(value, 4) for key, value in biases.items()},
            "conditions": {
                "setup_near_ema20": setup_near_ema,
                "trigger_aligned": trigger_aligned,
                "volume_confirmed": volume_confirmed,
            },
            "features": features,
        },
    }


def _microstructure(symbol: str) -> dict[str, Any]:
    try:
        rows = store.query(
            "SELECT * FROM market_microstructure WHERE symbol=? AND received_at>=?",
            (symbol, int(time.time() * 1000) - 15_000),
        )
    except Exception:
        return {}
    if not rows:
        return {}
    snapshot = dict(rows[0])
    for key in (
        "bid_price",
        "ask_price",
        "mid_price",
        "spread_bps",
        "book_imbalance",
        "aggressive_buy_ratio",
        "quote_volume_60s",
        "realized_volatility_60s",
        "price_velocity_bps_60s",
    ):
        if snapshot.get(key) is not None:
            snapshot[key] = float(snapshot[key])
    if isinstance(snapshot.get("quality_json"), str):
        try:
            snapshot["quality_json"] = json.loads(snapshot["quality_json"])
        except (TypeError, ValueError):
            snapshot["quality_json"] = {}
    snapshot.pop("updated_at", None)
    return snapshot


def _candidate(
    base: dict[str, Any],
    *,
    scanner_key: str,
    quality_delta: float,
    summary: str,
    reason: str,
    micro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = json.loads(json.dumps(base))
    quality = max(0.0, min(100.0, float(item["quality_score"]) + quality_delta))
    probability = max(0.35, min(0.78, 0.5 + (quality - 50.0) / 180.0))
    spread = max(0.0, float((micro or {}).get("spread_bps") or 0.0))
    expected_value = probability * 100.0 - (1.0 - probability) * 70.0 - spread - 2.0
    item.update(
        scanner_key=scanner_key,
        scanner_version=1,
        quality_score=round(quality, 4),
        expected_value_score=round(expected_value, 4),
    )
    item["evidence"]["summary"] = summary
    item["evidence"]["reason_codes"] = list(
        dict.fromkeys([*item["evidence"].get("reason_codes", []), reason])
    )
    item["evidence"]["estimated_probability"] = round(probability, 4)
    item["evidence"]["expected_value_bps"] = round(expected_value, 4)
    if micro:
        item["evidence"]["microstructure"] = micro
    return item


def classify_ensemble(
    features: dict[str, dict[str, Any]], micro: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Create independent candidates so each scanner can be calibrated and retired."""
    base = classify_opportunity(features)
    candidates = [base]
    trigger = features["15m"]
    setup = features["1h"]
    direction = base["direction"]
    if direction != "neutral" and float(trigger["volume_ratio"]) >= 1.35:
        candidates.append(
            _candidate(
                base,
                scanner_key="volatility_expansion_v1",
                quality_delta=6.0,
                summary=f"波动扩张 · {direction} · 成交量确认",
                reason="VOLATILITY_EXPANSION",
                micro=micro,
            )
        )
    if direction != "neutral" and abs(float(setup["distance_fast_atr"])) <= 0.75:
        candidates.append(
            _candidate(
                base,
                scanner_key="pullback_continuation_v1",
                quality_delta=3.0,
                summary=f"趋势回调延续 · {direction}",
                reason="PULLBACK_CONTINUATION",
                micro=micro,
            )
        )
    if direction != "neutral" and micro:
        imbalance = float(micro.get("book_imbalance") or 0.0)
        buy_ratio = float(micro.get("aggressive_buy_ratio") or 0.5)
        aligned = (direction == "long" and imbalance >= 0.12 and buy_ratio >= 0.56) or (
            direction == "short" and imbalance <= -0.12 and buy_ratio <= 0.44
        )
        if aligned:
            candidates.append(
                _candidate(
                    base,
                    scanner_key="order_flow_alignment_v1",
                    quality_delta=10.0,
                    summary=f"订单流共振 · {direction}",
                    reason="ORDER_FLOW_ALIGNED",
                    micro=micro,
                )
            )
    return candidates


def _persist_candidate(transaction, symbol: str, candidate: dict[str, Any]) -> str:
    scanner_key = candidate["scanner_key"]
    detected = int(candidate["detected_bar_time"])
    dedup_key = f"{scanner_key}:{symbol}:15m:{detected}"
    transaction.execute(
        """UPDATE market_opportunities SET status='expired',current_marker=NULL,
               updated_at=CURRENT_TIMESTAMP
           WHERE scanner_key=? AND symbol=? AND current_marker=1 AND dedup_key<>?""",
        (scanner_key, symbol, dedup_key),
    )
    transaction.execute(
        """INSERT INTO market_opportunities(
               public_id,scanner_key,scanner_version,symbol,primary_timeframe,direction,
               status,quality_score,current_marker,entry_price,expected_value_score,
               detected_bar_time,expires_bar_time,evidence_json,dedup_key,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
           ON DUPLICATE KEY UPDATE status=VALUES(status),current_marker=1,
               quality_score=VALUES(quality_score),entry_price=VALUES(entry_price),
               expected_value_score=VALUES(expected_value_score),expires_bar_time=VALUES(expires_bar_time),
               evidence_json=VALUES(evidence_json),updated_at=VALUES(updated_at)""",
        (
            str(uuid.uuid4()),
            scanner_key,
            candidate.get("scanner_version", 1),
            symbol,
            candidate["primary_timeframe"],
            candidate["direction"],
            candidate["status"],
            candidate["quality_score"],
            candidate["entry_price"],
            candidate["expected_value_score"],
            detected,
            candidate["expires_bar_time"],
            json.dumps(candidate["evidence"], ensure_ascii=False),
            dedup_key,
        ),
    )
    row = transaction.query(
        "SELECT id,status FROM market_opportunities WHERE dedup_key=?", (dedup_key,)
    )[0]
    transaction.execute(
        """INSERT IGNORE INTO opportunity_events(
               opportunity_id,event_key,event_type,previous_status,next_status,payload_json,
               event_time,created_at)
           VALUES(?,?,'OBSERVED',NULL,?,?,?,CURRENT_TIMESTAMP)""",
        (
            row["id"],
            f"observed:{dedup_key}:{candidate['status']}",
            candidate["status"],
            json.dumps(
                {"quality_score": candidate["quality_score"], "scanner_key": scanner_key},
                ensure_ascii=False,
            ),
            int(time.time() * 1000),
        ),
    )
    return dedup_key


def scan_symbol(symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    features = {
        timeframe: compute_feature_snapshot(store.get_klines(normalized, timeframe, 600), timeframe)
        for timeframe in ("15m", "1h", "4h")
    }
    micro = _microstructure(normalized)
    candidates = classify_ensemble(features, micro)
    ticker_rows = store.query("SELECT price,ts FROM ticker WHERE symbol=?", (normalized,))
    reference_price = float(micro.get("mid_price") or 0)
    reference_source = "websocket_mid"
    if reference_price <= 0 and ticker_rows:
        reference_price = float(ticker_rows[0].get("price") or 0)
        reference_source = "latest_ticker"
    if reference_price <= 0:
        reference_price = float(features["15m"]["close"])
        reference_source = "closed_bar_fallback"
    for candidate in candidates:
        candidate["entry_price"] = reference_price
        candidate["evidence"]["entry_reference"] = {
            "source": reference_source,
            "price": reference_price,
            "captured_at": int(time.time() * 1000),
        }
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
        keys = [_persist_candidate(transaction, normalized, item) for item in candidates]
    primary = candidates[0]
    return {
        "symbol": normalized,
        **primary,
        "dedup_key": keys[0],
        "candidate_count": len(candidates),
        "scanner_keys": [item["scanner_key"] for item in candidates],
    }


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
