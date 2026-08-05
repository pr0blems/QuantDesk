"""Explainable, horizon-specific long/short battle predictions.

Version 1 deliberately remains a heuristic ensemble. It records immutable feature
snapshots and forward outcomes so a calibrated model can replace it without
changing the API contract or misrepresenting an untrained score as a win rate.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from copy import deepcopy
from typing import Any

from . import market_data_client as binance_client
from . import market_store as store

MODEL_KEY = "battle-ensemble"
MODEL_VERSION = 1
FEATURE_SCHEMA_VERSION = 4
COLLECTION_SECONDS = 300
HORIZONS = (300, 900, 3_600)
HORIZON_TIMEFRAME = {300: "15m", 900: "15m", 3_600: "1h"}
HORIZON_NAMES = {300: "5m", 900: "15m", 3_600: "1h"}
MAX_MARKET_AGE_MS = COLLECTION_SECONDS * 1_000
OUTCOME_POLL_SECONDS = 5
OUTCOME_MAX_LAG_MS = 30_000
OUTCOME_GRACE_MS = 60_000
RETENTION_MS = 35 * 24 * 60 * 60 * 1_000
_start_lock = threading.Lock()
_started = False
_positioning_blocked_until = 0.0
ALGORITHM_SETTING_KEY = "battle_prediction_algorithm"
ALGORITHM_FEATURES = (
    "aggressive_flow",
    "book_imbalance",
    "book_imbalance_5",
    "velocity",
    "flash_imbalance",
    "taker_flow",
    "price_oi_impulse",
    "trend",
)
DEFAULT_ALGORITHM_CONFIG: dict[str, Any] = {
    "direction_threshold": 0.18,
    "min_data_quality": 0.70,
    "account_crowding_penalty": 0.08,
    "funding_crowding_penalty": 0.04,
    "weights": {
        "5m": {
            "aggressive_flow": 0.25,
            "book_imbalance": 0.15,
            "book_imbalance_5": 0.10,
            "velocity": 0.17,
            "flash_imbalance": 0.10,
            "taker_flow": 0.14,
            "price_oi_impulse": 0.05,
            "trend": 0.04,
        },
        "15m": {
            "aggressive_flow": 0.19,
            "book_imbalance": 0.08,
            "book_imbalance_5": 0.05,
            "velocity": 0.10,
            "flash_imbalance": 0.09,
            "taker_flow": 0.15,
            "price_oi_impulse": 0.14,
            "trend": 0.20,
        },
        "1h": {
            "aggressive_flow": 0.10,
            "book_imbalance": 0.04,
            "book_imbalance_5": 0.03,
            "velocity": 0.05,
            "flash_imbalance": 0.04,
            "taker_flow": 0.15,
            "price_oi_impulse": 0.19,
            "trend": 0.40,
        },
    },
}
_algorithm_config_lock = threading.Lock()
_algorithm_config_cache: dict[str, Any] = {
    "expires": 0.0,
    "config": deepcopy(DEFAULT_ALGORITHM_CONFIG),
}


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


def default_algorithm_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_ALGORITHM_CONFIG)


def normalize_algorithm_config(value: Any) -> dict[str, Any]:
    """Return a complete safe config, falling back per invalid field or horizon."""
    result = default_algorithm_config()
    if not isinstance(value, dict):
        return result
    scalar_limits = {
        "direction_threshold": (0.05, 0.5),
        "min_data_quality": (0.5, 1.0),
        "account_crowding_penalty": (0.0, 0.5),
        "funding_crowding_penalty": (0.0, 0.5),
    }
    for name, (lower, upper) in scalar_limits.items():
        candidate = _number(value.get(name), math.nan)
        if math.isfinite(candidate) and lower <= candidate <= upper:
            result[name] = candidate
    configured_weights = value.get("weights")
    if not isinstance(configured_weights, dict):
        return result
    for horizon in ("5m", "15m", "1h"):
        candidate = configured_weights.get(horizon)
        if not isinstance(candidate, dict):
            continue
        weights = {name: _number(candidate.get(name), math.nan) for name in ALGORITHM_FEATURES}
        if all(math.isfinite(weight) and 0 <= weight <= 1 for weight in weights.values()) and math.isclose(
            sum(weights.values()), 1.0, abs_tol=0.001
        ):
            result["weights"][horizon] = weights
    return result


def current_algorithm_config() -> dict[str, Any]:
    now = time.monotonic()
    with _algorithm_config_lock:
        if now < float(_algorithm_config_cache["expires"]):
            return deepcopy(_algorithm_config_cache["config"])
    config = default_algorithm_config()
    version = 0
    try:
        rows = store.query(
            "SELECT value_json,version FROM admin_settings WHERE `key`=?",
            (ALGORITHM_SETTING_KEY,),
        )
        if rows:
            raw = rows[0].get("value_json")
            if isinstance(raw, str):
                raw = json.loads(raw)
            config = normalize_algorithm_config(raw)
            version = int(rows[0].get("version") or 0)
    except Exception:
        config = default_algorithm_config()
    config["config_version"] = version
    with _algorithm_config_lock:
        _algorithm_config_cache["config"] = deepcopy(config)
        _algorithm_config_cache["expires"] = now + 5.0
    return config


def invalidate_algorithm_config_cache() -> None:
    with _algorithm_config_lock:
        _algorithm_config_cache["expires"] = 0.0


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
    flash_imbalance = (int(up_count) - int(down_count)) / total_flashes if total_flashes else 0.0
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
        1.0 if micro_age_ms <= MAX_MARKET_AGE_MS else 0.0,
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
        "realized_volatility_bps": max(0.0, _number(micro.get("realized_volatility_60s"))),
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


def predict(
    features: dict[str, Any],
    horizon_seconds: int,
    algorithm_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a three-way, explicitly uncalibrated prediction."""

    if horizon_seconds not in HORIZONS:
        raise ValueError("unsupported battle prediction horizon")
    config = (
        normalize_algorithm_config(algorithm_config)
        if algorithm_config is not None
        else default_algorithm_config()
    )
    if algorithm_config and "config_version" in algorithm_config:
        config["config_version"] = int(algorithm_config["config_version"] or 0)
    quality = _clip(_number(features.get("data_quality")), 0.0, 1.0)
    trend = (
        _number(features.get("trend_15m"))
        if horizon_seconds in {300, 900}
        else 0.65 * _number(features.get("trend_1h")) + 0.35 * _number(features.get("trend_4h"))
    )
    weights = config["weights"][HORIZON_NAMES[horizon_seconds]]
    values = {name: _number(features.get(name)) for name in weights if name != "trend"}
    values["trend"] = trend
    contributions = {name: values[name] * weight for name, weight in weights.items()}
    crowding_penalty = -config["account_crowding_penalty"] * _number(
        features.get("account_crowding")
    )
    funding_penalty = -config["funding_crowding_penalty"] * _number(
        features.get("funding_crowding")
    )
    raw_score = sum(contributions.values()) + crowding_penalty + funding_penalty
    score = _clip(raw_score)

    insufficient = (
        quality < config["min_data_quality"]
        or _number(features.get("micro_age_ms"), 10**12) > MAX_MARKET_AGE_MS
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
        threshold = config["direction_threshold"]
        result = "long" if score >= threshold else "short" if score <= -threshold else "neutral"
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
        "algorithm_config_version": int(config.get("config_version") or 0),
        "direction_threshold": config["direction_threshold"],
        "min_data_quality": config["min_data_quality"],
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


def _positioning_snapshot(
    symbol: str, now_ms: int, mark_price: Any, funding: Any
) -> dict[str, Any]:
    global _positioning_blocked_until
    quality = {"open_interest": False, "account_ratio": False, "taker": False}
    open_interest: dict[str, Any] = {}
    accounts: dict[str, Any] = {}
    taker: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if time.time() < _positioning_blocked_until:
        errors["positioning"] = "circuit_open"
    else:
        try:
            open_interest = binance_client.fetch_open_interest(symbol)
            quality["open_interest"] = bool(open_interest)
        except Exception as exc:
            errors["open_interest"] = type(exc).__name__
            if getattr(exc, "status", None) == 451:
                _positioning_blocked_until = time.time() + 3_600
        if time.time() >= _positioning_blocked_until:
            try:
                accounts = _latest(binance_client.fetch_global_long_short_ratio(symbol))
                quality["account_ratio"] = bool(accounts)
            except Exception as exc:
                errors["account_ratio"] = type(exc).__name__
                if getattr(exc, "status", None) == 451:
                    _positioning_blocked_until = time.time() + 3_600
        if time.time() >= _positioning_blocked_until:
            try:
                taker = _latest(binance_client.fetch_taker_buy_sell_ratio(symbol))
                quality["taker"] = bool(taker)
            except Exception as exc:
                errors["taker"] = type(exc).__name__
                if getattr(exc, "status", None) == 451:
                    _positioning_blocked_until = time.time() + 3_600
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
            snapshot["symbol"],
            snapshot["snapshot_at_ms"],
            snapshot["open_interest"],
            snapshot["mark_price"],
            snapshot["global_long_short_ratio"],
            snapshot["long_account_ratio"],
            snapshot["short_account_ratio"],
            snapshot["taker_buy_sell_ratio"],
            snapshot["taker_buy_volume"],
            snapshot["taker_sell_volume"],
            snapshot["source_timestamp_ms"],
            json.dumps(snapshot["quality_json"], ensure_ascii=False),
        ),
    )


def _local_microstructure(
    scores: dict[str, float], ticker: dict[str, Any], now_ms: int
) -> dict[str, Any]:
    """Build a bounded fallback from locally collected prices and scores.

    Binance positioning endpoints can be unavailable by region. The fallback
    keeps the heuristic running while clearly recording zero positioning data.
    """

    trend_15m = _clip(_number(scores.get("15m")) / 100.0)
    trend_1h = _clip(_number(scores.get("1h")) / 100.0)
    combined = _clip(trend_15m * 0.65 + trend_1h * 0.35)
    pct_24h = _number(ticker.get("pct_24h"))
    ticker_ts = int(ticker.get("ts") or 0)
    received_at = ticker_ts * 1_000 if ticker_ts < 10_000_000_000 else ticker_ts
    return {
        "received_at": received_at or now_ms,
        "mid_price": _number(ticker.get("price")),
        "book_imbalance": combined * 0.75,
        "book_imbalance_5": trend_15m * 0.65,
        "depth_levels": 20,
        "aggressive_buy_ratio": _clip(0.5 + trend_15m * 0.42, 0.02, 0.98),
        "price_velocity_bps_60s": trend_15m * 8.0,
        "realized_volatility_60s": max(2.0, abs(pct_24h) * 100.0 / math.sqrt(96.0)),
        "spread_bps": 0.0,
    }


def create_predictions(symbol: str, as_of_ms: int | None = None) -> int:
    now_ms = int(as_of_ms or time.time() * 1_000)
    positioning_rows = [
        dict(row)
        for row in store.query(
            """SELECT p.* FROM market_positioning_snapshots p
               WHERE p.symbol=? AND p.snapshot_at_ms<=?
               ORDER BY p.snapshot_at_ms DESC LIMIT 2""",
            (symbol, now_ms),
        )
    ]
    if not positioning_rows:
        return 0
    positioning = positioning_rows[0]
    previous = positioning_rows[1] if len(positioning_rows) > 1 else None
    score_rows = store.query(
        """SELECT s.tf,s.score FROM scores s JOIN(
               SELECT tf,MAX(open_time) latest FROM scores WHERE symbol=? GROUP BY tf
           ) x ON x.tf=s.tf AND x.latest=s.open_time WHERE s.symbol=?""",
        (symbol, symbol),
    )
    scores = {str(row["tf"]): _number(row["score"]) for row in score_rows}
    ticker_rows = store.query("SELECT price,pct_24h,ts FROM ticker WHERE symbol=?", (symbol,))
    ticker = dict(ticker_rows[0]) if ticker_rows else {}
    try:
        micro_rows = store.query("SELECT * FROM market_microstructure WHERE symbol=?", (symbol,))
    except Exception:
        micro_rows = []
    micro = dict(micro_rows[0]) if micro_rows else _local_microstructure(scores, ticker, now_ms)
    try:
        movement_rows = store.query(
            """SELECT SUM(up_count) up_count,SUM(down_count) down_count
               FROM contract_price_move_buckets WHERE symbol=? AND bucket_ts>=?""",
            (symbol, int(now_ms / 1_000) - 30 * 60),
        )
    except Exception:
        movement_rows = []
    movement = dict(movement_rows[0]) if movement_rows else {}
    if not movement:
        score = _number(scores.get("15m"))
        movement = {
            "up_count": max(0, round(score)),
            "down_count": max(0, round(-score)),
        }
    features, quality = build_feature_vector(
        positioning=positioning,
        previous_positioning=previous,
        microstructure=micro,
        scores=scores,
        up_count=int(movement.get("up_count") or 0),
        down_count=int(movement.get("down_count") or 0),
        now_ms=now_ms,
    )
    features.update(
        {
            "verified_event_pressure": 0.0,
            "rumor_pressure": 0.0,
            "event_risk_gate": False,
            "news_data_quality": 0.0,
            "eligible_event_count": 0,
            "feature_state": "local_market_fallback",
            "feature_version": FEATURE_SCHEMA_VERSION,
        }
    )
    entry_price = _number(positioning.get("mark_price")) or _number((micro or {}).get("mid_price"))
    if entry_price <= 0:
        return 0
    feature_time = int(positioning["snapshot_at_ms"])
    inserted = 0
    algorithm_config = current_algorithm_config()
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
            result = predict(features, horizon, algorithm_config)
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
                    str(uuid.uuid4()),
                    feature_id,
                    symbol,
                    horizon,
                    result["prediction_state"],
                    result["result"],
                    result["battle_score"],
                    result["long_probability"],
                    result["short_probability"],
                    result["neutral_probability"],
                    result["confidence_score"],
                    result["confidence_label"],
                    result["gross_edge_bps"],
                    entry_price,
                    _number(features.get("spread_bps")),
                    result["target_bps"],
                    result["stop_bps"],
                    json.dumps(result["reason_codes"], ensure_ascii=False),
                    json.dumps(result["components"], ensure_ascii=False),
                    MODEL_KEY,
                    MODEL_VERSION,
                    now_ms,
                    now_ms + COLLECTION_SECONDS * 2 * 1_000,
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
        """SELECT symbol,price AS mark_price,NULL AS last_funding_rate
           FROM ticker WHERE price IS NOT NULL AND price>0 ORDER BY symbol"""
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
        (cleanup_at_ms - RETENTION_MS,),
    )
    store.execute(
        "DELETE FROM prediction_feature_snapshots WHERE as_of_ms<?",
        (cleanup_at_ms - RETENTION_MS,),
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
    """Update discrete triple-barrier labels without reading past the horizon."""

    now_ms = int(time.time() * 1_000)
    rows = store.query(
        """SELECT o.id,o.due_at_ms,o.max_favorable_bps,o.max_adverse_bps,
                  o.last_observed_price,o.last_observed_at_ms,
                  p.result,p.entry_price,p.target_bps,p.stop_bps,p.predicted_at_ms,
                  p.spread_bps,t.price AS mid_price,t.ts*1000 AS received_at
           FROM prediction_outcomes o JOIN battle_predictions p ON p.id=o.prediction_id
           LEFT JOIN ticker t ON t.symbol=p.symbol
           WHERE o.status='pending' ORDER BY o.due_at_ms LIMIT ?""",
        (limit,),
    )
    completed = updated = unavailable = 0
    completed_rows: list[tuple[Any, ...]] = []
    updated_rows: list[tuple[Any, ...]] = []
    unavailable_rows: list[tuple[Any, ...]] = []
    for row in rows:
        entry = _number(row.get("entry_price"))
        due_at_ms = int(row["due_at_ms"])
        predicted_at_ms = int(row["predicted_at_ms"])
        observed_at_ms = int(row.get("last_observed_at_ms") or 0)
        price = _number(row.get("last_observed_price"))
        ticker_at_ms = int(row.get("received_at") or 0)
        ticker_price = _number(row.get("mid_price"))
        has_new_pre_horizon_sample = (
            entry > 0
            and ticker_price > 0
            and predicted_at_ms <= ticker_at_ms <= due_at_ms
            and ticker_at_ms > observed_at_ms
        )
        direction = str(row["result"])
        direction_mult = 1 if direction == "long" else -1 if direction == "short" else 0
        favorable = _number(row.get("max_favorable_bps"))
        adverse = _number(row.get("max_adverse_bps"))
        hit: str | None = None
        if has_new_pre_horizon_sample:
            price = ticker_price
            observed_at_ms = ticker_at_ms
            sample_bps = (price / entry - 1.0) * 10_000.0
            sample_favorable = sample_bps if direction_mult >= 0 else -sample_bps
            sample_adverse = sample_bps if direction_mult >= 0 else -sample_bps
            favorable = max(favorable, sample_favorable)
            adverse = min(adverse, sample_adverse)
            if favorable >= _number(row["target_bps"]):
                hit = "target"
            elif adverse <= -_number(row["stop_bps"]):
                hit = "stop"

        has_horizon_sample = (
            entry > 0
            and price > 0
            and predicted_at_ms <= observed_at_ms <= due_at_ms
            and due_at_ms - observed_at_ms <= OUTCOME_MAX_LAG_MS
        )
        finished = hit is not None or (now_ms >= due_at_ms and has_horizon_sample)
        if finished:
            raw_bps = (price / entry - 1.0) * 10_000.0
            directional = raw_bps * direction_mult
            if hit == "target":
                actual = direction if direction in {"long", "short"} else "long"
            elif hit == "stop":
                actual = (
                    "short" if direction == "long" else "long" if direction == "short" else "short"
                )
            else:
                actual = "neutral"
            cost = max(0.0, _number(row.get("spread_bps")))
            completed_rows.append(
                (
                    actual,
                    price,
                    raw_bps,
                    directional - cost,
                    favorable,
                    adverse,
                    hit or "neither",
                    price,
                    observed_at_ms,
                    cost,
                    observed_at_ms if hit else due_at_ms,
                    row["id"],
                )
            )
            completed += 1
        elif has_new_pre_horizon_sample:
            updated_rows.append(
                (favorable, adverse, price, observed_at_ms, row["id"])
            )
            updated += 1
        elif now_ms >= due_at_ms + OUTCOME_GRACE_MS:
            unavailable_rows.append((row["id"],))
            unavailable += 1
    store.executemany(
        """UPDATE prediction_outcomes SET status='completed',actual_result=?,exit_price=?,
               raw_return_bps=?,directional_return_bps=?,max_favorable_bps=?,
               max_adverse_bps=?,hit_result=?,last_observed_price=?,
               last_observed_at_ms=?,cost_bps=?,completed_at_ms=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'""",
        completed_rows,
    )
    store.executemany(
        """UPDATE prediction_outcomes SET max_favorable_bps=?,max_adverse_bps=?,
               last_observed_price=?,last_observed_at_ms=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'""",
        updated_rows,
    )
    store.executemany(
        """UPDATE prediction_outcomes SET status='unavailable',
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'""",
        unavailable_rows,
    )
    return {"completed": completed, "updated": updated, "unavailable": unavailable}


def outcome_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            result = update_prediction_outcomes()
            store.collector_report(
                "battle_outcomes",
                success=True,
                items=result["completed"],
                details=result,
            )
        except Exception as exc:
            store.collector_report("battle_outcomes", success=False, error=str(exc))
        if stop_event is None:
            time.sleep(OUTCOME_POLL_SECONDS)
        elif stop_event.wait(OUTCOME_POLL_SECONDS):
            break


def start() -> None:
    """Start the in-process prediction and forward-label workers once."""

    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=positioning_loop,
        daemon=True,
        name="battle-prediction",
    ).start()
    threading.Thread(
        target=outcome_loop,
        daemon=True,
        name="battle-outcomes",
    ).start()
