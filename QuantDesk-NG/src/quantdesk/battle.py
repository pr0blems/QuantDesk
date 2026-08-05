"""Explainable, horizon-specific long/short battle predictions.

Version 1 deliberately remains a heuristic ensemble. It records immutable feature
snapshots and forward outcomes so a calibrated model can replace it without
changing the API contract or misrepresenting an untrained score as a win rate.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from . import binance_client, news_intelligence, store

MODEL_KEY = "battle-ensemble"
MODEL_VERSION = 1
FEATURE_SCHEMA_VERSION = 4
COLLECTION_SECONDS = 300
HORIZONS = (300, 900, 3_600)
HORIZON_TIMEFRAME = {300: "15m", 900: "15m", 3_600: "1h"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    result = _number(value, math.nan)
    return result if math.isfinite(result) else None


def _clip(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, value))


def _log_ratio(value: Any) -> float:
    ratio = _number(value)
    return math.tanh(math.log(ratio) * 1.5) if ratio > 0 else 0.0


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[-1] if rows else {}


def build_feature_vector(
    *,
    positioning: dict[str, Any],
    previous_positioning: dict[str, Any] | None,
    microstructure: dict[str, Any] | None,
    scores: dict[str, float],
    up_count: int,
    down_count: int,
    now_ms: int,
) -> tuple[dict[str, Any], float]:
    """Return bounded features plus a strict data-quality score."""

    previous = previous_positioning or {}
    micro = microstructure or {}
    open_interest = _optional_number(positioning.get("open_interest"))
    previous_oi = _optional_number(previous.get("open_interest"))
    mark_price = _number(positioning.get("mark_price"))
    previous_mark = _number(previous.get("mark_price"))
    oi_change = (
        (open_interest / previous_oi - 1.0)
        if open_interest is not None and previous_oi and previous_oi > 0
        else 0.0
    )
    price_change = mark_price / previous_mark - 1.0 if previous_mark > 0 else 0.0
    price_oi_impulse = math.tanh(price_change * 300.0)
    if oi_change <= 0:
        price_oi_impulse *= 0.45
    else:
        price_oi_impulse *= 0.55 + 0.45 * math.tanh(abs(oi_change) * 80.0)

    total_flashes = max(0, int(up_count)) + max(0, int(down_count))
    flash_imbalance = (
        (int(up_count) - int(down_count)) / total_flashes if total_flashes else 0.0
    )
    flash_activity = min(1.0, total_flashes / 120.0)
    received_at = int(micro.get("received_at") or 0)
    micro_age_ms = max(0, now_ms - received_at) if received_at else 10**12
    positioning_at = int(positioning.get("snapshot_at_ms") or 0)
    positioning_age_ms = max(0, now_ms - positioning_at) if positioning_at else 10**12
    depth_levels = max(0, int(micro.get("depth_levels") or 0))

    endpoint_quality = positioning.get("quality_json")
    if isinstance(endpoint_quality, str):
        try:
            endpoint_quality = json.loads(endpoint_quality)
        except (TypeError, ValueError):
            endpoint_quality = {}
    endpoint_quality = endpoint_quality if isinstance(endpoint_quality, dict) else {}
    available_endpoints = sum(
        bool(endpoint_quality.get(name)) for name in ("open_interest", "account_ratio", "taker")
    )
    quality_parts = [
        1.0 if micro_age_ms <= 15_000 else 0.0,
        available_endpoints / 3.0,
        1.0 if positioning_age_ms <= 10 * 60 * 1_000 else 0.0,
        1.0 if mark_price > 0 else 0.0,
        1.0 if depth_levels >= 5 else 0.0,
    ]
    quality = sum(quality_parts) / len(quality_parts)

    features = {
        "book_imbalance": _clip(_number(micro.get("book_imbalance"))),
        "book_imbalance_5": _clip(_number(micro.get("book_imbalance_5"))),
        "depth_levels": depth_levels,
        "aggressive_flow": _clip((_number(micro.get("aggressive_buy_ratio"), 0.5) - 0.5) * 2),
        "velocity": math.tanh(_number(micro.get("price_velocity_bps_60s")) / 8.0),
        "realized_volatility_bps": max(
            0.0, _number(micro.get("realized_volatility_60s"))
        ),
        "spread_bps": max(0.0, _number(micro.get("spread_bps"))),
        "flash_imbalance": _clip(flash_imbalance * (0.35 + 0.65 * flash_activity)),
        "flash_activity": flash_activity,
        "taker_flow": _log_ratio(positioning.get("taker_buy_sell_ratio")),
        "account_crowding": _log_ratio(positioning.get("global_long_short_ratio")),
        "oi_change": _clip(oi_change * 50.0),
        "price_change": _clip(price_change * 300.0),
        "price_oi_impulse": _clip(price_oi_impulse),
        "funding_crowding": _clip(_number(positioning.get("last_funding_rate")) * 1_000.0),
        "trend_15m": _clip(_number(scores.get("15m")) / 100.0),
        "trend_1h": _clip(_number(scores.get("1h")) / 100.0),
        "trend_4h": _clip(_number(scores.get("4h")) / 100.0),
        "micro_age_ms": micro_age_ms,
        "positioning_age_ms": positioning_age_ms,
        "source_timestamp_ms": int(positioning.get("source_timestamp_ms") or 0),
        "data_quality": round(quality, 6),
    }
    return features, quality


def predict(features: dict[str, Any], horizon_seconds: int) -> dict[str, Any]:
    """Create a three-way, explicitly uncalibrated prediction."""

    if horizon_seconds not in HORIZONS:
        raise ValueError("unsupported battle prediction horizon")
    quality = _clip(_number(features.get("data_quality")), 0.0, 1.0)
    trend = (
        _number(features.get("trend_15m"))
        if horizon_seconds in {300, 900}
        else 0.65 * _number(features.get("trend_1h"))
        + 0.35 * _number(features.get("trend_4h"))
    )
    if horizon_seconds == 300:
        weights = {
            "aggressive_flow": 0.25,
            "book_imbalance": 0.15,
            "book_imbalance_5": 0.10,
            "velocity": 0.17,
            "flash_imbalance": 0.10,
            "taker_flow": 0.14,
            "price_oi_impulse": 0.05,
            "trend": 0.04,
        }
    elif horizon_seconds == 900:
        weights = {
            "aggressive_flow": 0.19,
            "book_imbalance": 0.08,
            "book_imbalance_5": 0.05,
            "velocity": 0.10,
            "flash_imbalance": 0.09,
            "taker_flow": 0.15,
            "price_oi_impulse": 0.14,
            "trend": 0.20,
        }
    else:
        weights = {
            "aggressive_flow": 0.10,
            "book_imbalance": 0.04,
            "book_imbalance_5": 0.03,
            "velocity": 0.05,
            "flash_imbalance": 0.04,
            "taker_flow": 0.15,
            "price_oi_impulse": 0.19,
            "trend": 0.40,
        }
    values = {name: _number(features.get(name)) for name in weights if name != "trend"}
    values["trend"] = trend
    contributions = {name: values[name] * weight for name, weight in weights.items()}
    crowding_penalty = -0.08 * _number(features.get("account_crowding"))
    funding_penalty = -0.04 * _number(features.get("funding_crowding"))
    raw_score = sum(contributions.values()) + crowding_penalty + funding_penalty
    score = _clip(raw_score)

    insufficient = (
        quality < 0.70
        or _number(features.get("micro_age_ms"), 10**12) > 15_000
        or _number(features.get("positioning_age_ms"), 10**12) > 10 * 60 * 1_000
    )
    if insufficient:
        result = "neutral"
        long_probability, short_probability, neutral_probability = 0.10, 0.10, 0.80
        confidence = quality * 0.35
        state = "data_insufficient"
    else:
        neutral_probability = _clip(0.18 + (1.0 - abs(score)) * 0.50, 0.18, 0.68)
        directional_probability = 1.0 - neutral_probability
        long_share = 1.0 / (1.0 + math.exp(-3.2 * score))
        long_probability = directional_probability * long_share
        short_probability = directional_probability - long_probability
        result = "long" if score >= 0.18 else "short" if score <= -0.18 else "neutral"
        confidence = min(0.69, quality * (0.32 + 0.55 * abs(score)))
        state = "heuristic"

    # Persist enum-like values as ASCII; the UI localizes them. This avoids a
    # server/client character-set mismatch corrupting a control field.
    confidence_label = "medium" if confidence >= 0.48 else "low"
    volatility = max(2.0, _number(features.get("realized_volatility_bps")))
    projected_range = min(500.0, volatility * math.sqrt(horizon_seconds / 60.0))
    target_bps = max(12.0, projected_range * 0.90)
    stop_bps = max(10.0, target_bps * 0.72)
    gross_edge = (
        abs(score) * target_bps * 0.72 - (1.0 - abs(score)) * stop_bps * 0.18
        if not insufficient
        else None
    )

    ranked = sorted(contributions.items(), key=lambda item: abs(item[1]), reverse=True)
    reason_codes = [name.upper() for name, value in ranked[:3] if abs(value) >= 0.015]
    if abs(_number(features.get("account_crowding"))) >= 0.35:
        reason_codes.append("CROWDING_RISK")
    if insufficient:
        reason_codes.insert(0, "DATA_INSUFFICIENT")
    components = {
        **{name: round(value, 6) for name, value in contributions.items()},
        "account_crowding_penalty": round(crowding_penalty, 6),
        "funding_penalty": round(funding_penalty, 6),
        # Collected for shadow evaluation only. A future calibrated model may
        # assign a non-zero weight after leakage-safe forward validation.
        "verified_event_pressure_shadow": round(
            _number(features.get("verified_event_pressure")), 6
        ),
        "news_weight": 0.0,
    }
    return {
        "prediction_state": state,
        "result": result,
        "battle_score": round(score * 100.0, 4),
        "long_probability": round(long_probability, 8),
        "short_probability": round(short_probability, 8),
        "neutral_probability": round(neutral_probability, 8),
        "confidence_score": round(confidence, 8),
        "confidence_label": confidence_label,
        "gross_edge_bps": round(gross_edge, 6) if gross_edge is not None else None,
        "target_bps": round(target_bps, 6),
        "stop_bps": round(stop_bps, 6),
        "reason_codes": reason_codes,
        "components": components,
    }


def _positioning_snapshot(symbol: str, now_ms: int, mark_price: Any, funding: Any) -> dict[str, Any]:
    quality = {"open_interest": False, "account_ratio": False, "taker": False}
    open_interest: dict[str, Any] = {}
    accounts: dict[str, Any] = {}
    taker: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        open_interest = binance_client.fetch_open_interest(symbol)
        quality["open_interest"] = True
    except Exception as exc:
        errors["open_interest"] = type(exc).__name__
    try:
        accounts = _latest(binance_client.fetch_global_long_short_ratio(symbol))
        quality["account_ratio"] = bool(accounts)
    except Exception as exc:
        errors["account_ratio"] = type(exc).__name__
    try:
        taker = _latest(binance_client.fetch_taker_buy_sell_ratio(symbol))
        quality["taker"] = bool(taker)
    except Exception as exc:
        errors["taker"] = type(exc).__name__
    source_timestamp = max(
        int(open_interest.get("time") or 0),
        int(accounts.get("timestamp") or 0),
        int(taker.get("timestamp") or 0),
    )
    quality["errors"] = errors
    return {
        "symbol": symbol,
        "snapshot_at_ms": now_ms - now_ms % (COLLECTION_SECONDS * 1_000),
        "open_interest": _optional_number(open_interest.get("openInterest")),
        "mark_price": _optional_number(mark_price),
        "global_long_short_ratio": _optional_number(accounts.get("longShortRatio")),
        "long_account_ratio": _optional_number(accounts.get("longAccount")),
        "short_account_ratio": _optional_number(accounts.get("shortAccount")),
        "taker_buy_sell_ratio": _optional_number(taker.get("buySellRatio")),
        "taker_buy_volume": _optional_number(taker.get("buyVol")),
        "taker_sell_volume": _optional_number(taker.get("sellVol")),
        "source_timestamp_ms": source_timestamp or now_ms,
        "last_funding_rate": _optional_number(funding),
        "quality_json": quality,
    }


def _save_positioning(snapshot: dict[str, Any]) -> None:
    store.execute(
        """INSERT INTO market_positioning_snapshots(
               symbol,snapshot_at_ms,open_interest,mark_price,global_long_short_ratio,
               long_account_ratio,short_account_ratio,taker_buy_sell_ratio,taker_buy_volume,
               taker_sell_volume,source_timestamp_ms,quality_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
           ON DUPLICATE KEY UPDATE open_interest=VALUES(open_interest),mark_price=VALUES(mark_price),
               global_long_short_ratio=VALUES(global_long_short_ratio),
               long_account_ratio=VALUES(long_account_ratio),short_account_ratio=VALUES(short_account_ratio),
               taker_buy_sell_ratio=VALUES(taker_buy_sell_ratio),
               taker_buy_volume=VALUES(taker_buy_volume),taker_sell_volume=VALUES(taker_sell_volume),
               source_timestamp_ms=VALUES(source_timestamp_ms),quality_json=VALUES(quality_json)""",
        (
            snapshot["symbol"], snapshot["snapshot_at_ms"], snapshot["open_interest"],
            snapshot["mark_price"], snapshot["global_long_short_ratio"],
            snapshot["long_account_ratio"], snapshot["short_account_ratio"],
            snapshot["taker_buy_sell_ratio"], snapshot["taker_buy_volume"],
            snapshot["taker_sell_volume"], snapshot["source_timestamp_ms"],
            json.dumps(snapshot["quality_json"], ensure_ascii=False),
        ),
    )


def create_predictions(symbol: str, as_of_ms: int | None = None) -> int:
    now_ms = int(as_of_ms or time.time() * 1_000)
    positioning_rows = [
        dict(row)
        for row in store.query(
            """SELECT p.*,r.last_funding_rate FROM market_positioning_snapshots p
               LEFT JOIN binance_contract_rules r ON r.symbol=p.symbol
               WHERE p.symbol=? AND p.snapshot_at_ms<=?
               ORDER BY p.snapshot_at_ms DESC LIMIT 2""",
            (symbol, now_ms),
        )
    ]
    if not positioning_rows:
        return 0
    positioning = positioning_rows[0]
    previous = positioning_rows[1] if len(positioning_rows) > 1 else None
    micro_rows = store.query("SELECT * FROM market_microstructure WHERE symbol=?", (symbol,))
    micro = dict(micro_rows[0]) if micro_rows else None
    score_rows = store.query(
        """SELECT s.tf,s.score FROM scores s JOIN(
               SELECT tf,MAX(open_time) latest FROM scores WHERE symbol=? GROUP BY tf
           ) x ON x.tf=s.tf AND x.latest=s.open_time WHERE s.symbol=?""",
        (symbol, symbol),
    )
    scores = {str(row["tf"]): _number(row["score"]) for row in score_rows}
    movement_rows = store.query(
        """SELECT SUM(up_count) up_count,SUM(down_count) down_count
           FROM contract_price_move_buckets WHERE symbol=? AND bucket_ts>=?""",
        (symbol, int(now_ms / 1_000) - 30 * 60),
    )
    movement = dict(movement_rows[0]) if movement_rows else {}
    features, quality = build_feature_vector(
        positioning=positioning,
        previous_positioning=previous,
        microstructure=micro,
        scores=scores,
        up_count=int(movement.get("up_count") or 0),
        down_count=int(movement.get("down_count") or 0),
        now_ms=now_ms,
    )
    try:
        features.update(news_intelligence.features_for_symbol(symbol, now_ms // 1_000))
    except Exception:
        # News is an optional, shadow-only feature. Its absence must not stop
        # the market prediction or weaken protective execution.
        features.update(
            {
                "verified_event_pressure": 0.0,
                "rumor_pressure": 0.0,
                "event_risk_gate": False,
                "news_data_quality": 0.0,
                "eligible_event_count": 0,
                "feature_state": "shadow_unavailable",
                "feature_version": 1,
            }
        )
    entry_price = _number(positioning.get("mark_price")) or _number(
        (micro or {}).get("mid_price")
    )
    if entry_price <= 0:
        return 0
    feature_time = int(positioning["snapshot_at_ms"])
    inserted = 0
    with store.transaction() as transaction:
        transaction.execute(
            """INSERT IGNORE INTO prediction_feature_snapshots(
                   symbol,as_of_ms,feature_schema_version,features_json,quality_score,created_at)
               VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                symbol,
                feature_time,
                FEATURE_SCHEMA_VERSION,
                json.dumps(features, ensure_ascii=False),
                quality,
            ),
        )
        feature_rows = transaction.query(
            """SELECT id FROM prediction_feature_snapshots
               WHERE symbol=? AND as_of_ms=? AND feature_schema_version=?""",
            (symbol, feature_time, FEATURE_SCHEMA_VERSION),
        )
        feature_id = int(feature_rows[0]["id"])
        for horizon in HORIZONS:
            existing = transaction.query(
                "SELECT id FROM battle_predictions WHERE feature_snapshot_id=? AND horizon_seconds=?",
                (feature_id, horizon),
            )
            if existing:
                continue
            result = predict(features, horizon)
            transaction.execute(
                "UPDATE battle_predictions SET current_marker=NULL WHERE symbol=? AND horizon_seconds=? AND current_marker=1",
                (symbol, horizon),
            )
            transaction.execute(
                """INSERT INTO battle_predictions(
                       public_id,feature_snapshot_id,symbol,horizon_seconds,current_marker,
                       prediction_state,result,battle_score,long_probability,short_probability,
                       neutral_probability,confidence_score,confidence_label,gross_edge_bps,
                       entry_price,spread_bps,target_bps,stop_bps,reason_codes_json,components_json,
                       model_key,model_version,predicted_at_ms,valid_until_ms,created_at)
                   VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    str(uuid.uuid4()), feature_id, symbol, horizon,
                    result["prediction_state"], result["result"], result["battle_score"],
                    result["long_probability"], result["short_probability"],
                    result["neutral_probability"], result["confidence_score"],
                    result["confidence_label"], result["gross_edge_bps"], entry_price,
                    _number(features.get("spread_bps")), result["target_bps"],
                    result["stop_bps"], json.dumps(result["reason_codes"], ensure_ascii=False),
                    json.dumps(result["components"], ensure_ascii=False), MODEL_KEY,
                    MODEL_VERSION, now_ms, now_ms + COLLECTION_SECONDS * 2 * 1_000,
                ),
            )
            prediction_id = int(transaction.query("SELECT LAST_INSERT_ID() id")[0]["id"])
            transaction.execute(
                """INSERT INTO prediction_outcomes(
                       prediction_id,status,cost_bps,due_at_ms,label_version,created_at,updated_at)
                   VALUES(?,'pending',?,?,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (
                    prediction_id,
                    max(0.0, _number(features.get("spread_bps"))),
                    now_ms + horizon * 1_000,
                ),
            )
            inserted += 1
    return inserted


def collect_positioning_cycle(stop_event=None) -> dict[str, int]:
    rules = store.query(
        """SELECT symbol,mark_price,last_funding_rate FROM binance_contract_rules
           WHERE contract_type='TRADIFI_PERPETUAL' AND status='TRADING' ORDER BY symbol"""
    )
    collected = predictions = failures = 0
    for rule in rules:
        if stop_event is not None and stop_event.is_set():
            break
        symbol = str(rule["symbol"])
        try:
            # Timestamp each symbol when it is actually requested. A complete
            # 150-symbol cycle can span minutes and must not backdate labels.
            now_ms = int(time.time() * 1_000)
            snapshot = _positioning_snapshot(
                symbol, now_ms, rule.get("mark_price"), rule.get("last_funding_rate")
            )
            _save_positioning(snapshot)
            collected += 1
            predictions += create_predictions(symbol, now_ms)
        except Exception as exc:
            failures += 1
            store.collector_report(
                "battle_positioning_symbol",
                success=False,
                error=f"{symbol}: {type(exc).__name__}: {exc}",
            )
        if stop_event is not None and stop_event.wait(0.04):
            break
        time.sleep(0.04) if stop_event is None else None
    cleanup_at_ms = int(time.time() * 1_000)
    store.execute(
        "DELETE FROM market_positioning_snapshots WHERE snapshot_at_ms<?",
        (cleanup_at_ms - 35 * 24 * 60 * 60 * 1_000,),
    )
    return {"collected": collected, "predictions": predictions, "failures": failures}


def positioning_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        started = time.monotonic()
        try:
            result = collect_positioning_cycle(stop_event)
            store.collector_report(
                "battle_positioning",
                success=result["collected"] > 0,
                items=result["collected"],
                details=result,
                error="all positioning requests failed" if result["collected"] == 0 else None,
            )
        except Exception as exc:
            store.collector_report("battle_positioning", success=False, error=str(exc))
        wait = max(1.0, COLLECTION_SECONDS - (time.monotonic() - started))
        if stop_event is None:
            time.sleep(wait)
        elif stop_event.wait(wait):
            break


def update_prediction_outcomes(limit: int = 2_000) -> dict[str, int]:
    """Update triple-barrier labels from executable five-second market snapshots."""

    now_ms = int(time.time() * 1_000)
    rows = store.query(
        """SELECT o.id,o.due_at_ms,o.max_favorable_bps,o.max_adverse_bps,
                  p.result,p.entry_price,p.target_bps,p.stop_bps,p.predicted_at_ms,
                  p.spread_bps,m.mid_price,m.window_low_price,m.window_low_event_time,
                  m.window_high_price,m.window_high_event_time,m.received_at
           FROM prediction_outcomes o JOIN battle_predictions p ON p.id=o.prediction_id
           LEFT JOIN market_microstructure m ON m.symbol=p.symbol
           WHERE o.status='pending' ORDER BY o.due_at_ms LIMIT ?""",
        (limit,),
    )
    completed = updated = unavailable = 0
    for row in rows:
        entry = _number(row.get("entry_price"))
        price = _number(row.get("mid_price"))
        if entry <= 0 or price <= 0 or now_ms - int(row.get("received_at") or 0) > 30_000:
            if now_ms >= int(row["due_at_ms"]) + 60_000:
                store.execute(
                    "UPDATE prediction_outcomes SET status='unavailable',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
                unavailable += 1
            continue
        direction = str(row["result"])
        direction_mult = 1 if direction == "long" else -1 if direction == "short" else 0
        raw_bps = (price / entry - 1.0) * 10_000.0
        directional = raw_bps * direction_mult
        high = price
        low = price
        if int(row.get("window_high_event_time") or 0) >= int(row["predicted_at_ms"]):
            high = max(high, _number(row.get("window_high_price"), price))
        if int(row.get("window_low_event_time") or 0) >= int(row["predicted_at_ms"]):
            low = min(low, _number(row.get("window_low_price"), price))
        high_bps = (high / entry - 1.0) * 10_000.0
        low_bps = (low / entry - 1.0) * 10_000.0
        sample_favorable = high_bps if direction_mult >= 0 else -low_bps
        sample_adverse = low_bps if direction_mult >= 0 else -high_bps
        favorable = max(_number(row.get("max_favorable_bps")), sample_favorable)
        adverse = min(_number(row.get("max_adverse_bps")), sample_adverse)
        target = _number(row["target_bps"])
        stop = _number(row["stop_bps"])
        target_hit = favorable >= target
        stop_hit = adverse <= -stop
        if target_hit and stop_hit:
            high_time = int(row.get("window_high_event_time") or now_ms)
            low_time = int(row.get("window_low_event_time") or now_ms)
            if direction == "short":
                target_time, stop_time = low_time, high_time
            else:
                target_time, stop_time = high_time, low_time
            hit = "target" if target_time <= stop_time else "stop"
        else:
            hit = "target" if target_hit else "stop" if stop_hit else None
        finished = hit is not None or now_ms >= int(row["due_at_ms"])
        if finished:
            if hit == "target":
                actual = direction if direction in {"long", "short"} else "long"
            elif hit == "stop":
                actual = (
                    "short" if direction == "long" else "long" if direction == "short" else "short"
                )
            else:
                actual = "neutral"
            cost = max(0.0, _number(row.get("spread_bps")))
            store.execute(
                """UPDATE prediction_outcomes SET status='completed',actual_result=?,exit_price=?,
                       raw_return_bps=?,directional_return_bps=?,max_favorable_bps=?,
                       max_adverse_bps=?,hit_result=?,cost_bps=?,completed_at_ms=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    actual, price, raw_bps, directional - cost, favorable, adverse,
                    hit or "neither", cost, now_ms, row["id"],
                ),
            )
            completed += 1
        else:
            store.execute(
                """UPDATE prediction_outcomes SET max_favorable_bps=?,max_adverse_bps=?,
                       hit_result=COALESCE(hit_result,?),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (favorable, adverse, hit, row["id"]),
            )
            updated += 1
    return {"completed": completed, "updated": updated, "unavailable": unavailable}
