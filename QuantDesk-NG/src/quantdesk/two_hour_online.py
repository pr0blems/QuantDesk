"""Online shadow deployment for the calibrated two-hour direction model."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from . import binance_client, store
from .config_loader import CONFIG_DIR, PROJECT_ROOT
from .two_hour_research import (
    BAR_COLUMNS,
    FEATURE_COLUMNS,
    MultiHeadSelection,
    _connect,
    _multihead_probabilities,
    build_dataset,
    download_underlying_history,
    load_symbol_metadata,
)

MODEL_KEY = "two-hour-lightgbm"
MODEL_VERSION = 1
FEATURE_SCHEMA_VERSION = 5
HORIZON_SECONDS = 7_200
BAR_MS = 300_000
VALID_FOR_MS = 20 * 60 * 1_000
WARMUP_DAYS = 7
MODEL_FILES = {
    "event": "event_model.joblib",
    "direction": "direction_model.joblib",
    "occurrence": "occurrence_model.joblib",
    "terminal": "terminal_model.joblib",
}

_loaded_signature: tuple[str, int] | None = None
_loaded_bundle: tuple[dict[str, Any], dict[str, Any], MultiHeadSelection] | None = None
_last_underlying_refresh = 0.0


def model_directory() -> Path:
    configured = os.environ.get("QUANTDESK_TWO_HOUR_MODEL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / "reports" / "two_hour_models" / "deployed"


def online_cache_path() -> Path:
    configured = os.environ.get("QUANTDESK_TWO_HOUR_ONLINE_CACHE")
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / "data" / "two_hour_online.sqlite3"


def research_cache_path() -> Path:
    configured = os.environ.get("QUANTDESK_TWO_HOUR_RESEARCH_CACHE")
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / "data" / "two_hour_research.sqlite3"


def load_model_bundle() -> tuple[dict[str, Any], dict[str, Any], MultiHeadSelection]:
    global _loaded_bundle, _loaded_signature
    directory = model_directory()
    manifest_path = directory / "manifest.json"
    signature = (str(directory), manifest_path.stat().st_mtime_ns)
    if _loaded_bundle is not None and signature == _loaded_signature:
        return _loaded_bundle
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tuple(manifest.get("features") or ()) != FEATURE_COLUMNS:
        raise ValueError("deployed two-hour model feature schema does not match runtime")
    selection = MultiHeadSelection(**manifest["event_threshold"])
    models = {name: joblib.load(directory / filename) for name, filename in MODEL_FILES.items()}
    _loaded_signature = signature
    _loaded_bundle = (models, manifest, selection)
    return _loaded_bundle


def _copy_recent_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    time_column: str,
    cutoff_ms: int,
) -> int:
    names = ",".join(columns)
    cursor = source.execute(
        f"SELECT {names} FROM {table} WHERE {time_column}>=?",  # noqa: S608
        (cutoff_ms,),
    )
    placeholders = ",".join("?" for _ in columns)
    copied = 0
    while rows := cursor.fetchmany(20_000):
        target.executemany(
            f"INSERT OR REPLACE INTO {table}({names}) VALUES({placeholders})",  # noqa: S608
            rows,
        )
        copied += len(rows)
    return copied


def initialize_cache(cache_path: Path, now_ms: int) -> None:
    target = _connect(cache_path)
    if int(target.execute("SELECT COUNT(*) FROM bars").fetchone()[0]):
        target.close()
        return
    source_path = research_cache_path()
    if not source_path.exists():
        target.close()
        return
    source = sqlite3.connect(source_path)
    cutoff = now_ms - WARMUP_DAYS * 24 * 60 * 60 * 1_000
    specifications = (
        ("bars", BAR_COLUMNS, "open_time"),
        (
            "futures_metrics",
            (
                "symbol",
                "open_time",
                "sum_open_interest",
                "sum_open_interest_value",
                "count_toptrader_long_short_ratio",
                "sum_toptrader_long_short_ratio",
                "count_long_short_ratio",
                "sum_taker_long_short_vol_ratio",
            ),
            "open_time",
        ),
        (
            "underlying_bars",
            ("symbol", "ticker", "open_time", "open", "high", "low", "close", "volume"),
            "open_time",
        ),
        (
            "external_events",
            (
                "symbol",
                "event_time",
                "source",
                "event_type",
                "source_id",
                "sentiment_score",
                "quality_score",
                "metadata_json",
            ),
            "event_time",
        ),
    )
    for table, columns, time_column in specifications:
        _copy_recent_rows(source, target, table, columns, time_column, cutoff)
    target.commit()
    source.close()
    target.close()


def _latest_bar_times(cache_path: Path) -> dict[str, int]:
    connection = _connect(cache_path)
    rows = connection.execute("SELECT symbol,MAX(open_time) FROM bars GROUP BY symbol").fetchall()
    connection.close()
    return {str(symbol): int(open_time) for symbol, open_time in rows if open_time is not None}


def _fetch_symbol_bars(
    symbol: str, market_type: str, start_ms: int, end_ms: int
) -> list[tuple[Any, ...]]:
    output: list[tuple[Any, ...]] = []
    cursor = start_ms
    for _ in range(3):
        rows = binance_client.fetch_research_klines(
            symbol,
            "5m",
            1500,
            start_time_ms=cursor,
            end_time_ms=end_ms,
        )
        accepted = [row for row in rows if row[0] <= end_ms]
        output.extend((symbol, market_type, *row) for row in accepted)
        if not accepted or accepted[-1][0] >= end_ms or len(rows) < 1500:
            break
        cursor = int(accepted[-1][0]) + BAR_MS
    return output


def update_bars(
    cache_path: Path, symbols: list[dict[str, Any]], now_ms: int
) -> dict[str, Any]:
    last_closed = now_ms // BAR_MS * BAR_MS - BAR_MS
    latest = _latest_bar_times(cache_path)
    fallback = last_closed - 5 * 24 * 60 * 60 * 1_000
    rows: list[tuple[Any, ...]] = []
    errors: list[str] = []
    requests = []
    for item in symbols:
        start_ms = max(
            fallback,
            latest.get(item["symbol"], fallback - BAR_MS) + BAR_MS,
        )
        if start_ms <= last_closed:
            requests.append((item, start_ms))
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                _fetch_symbol_bars,
                item["symbol"],
                item["market_type"],
                start_ms,
                last_closed,
            ): item["symbol"]
            for item, start_ms in requests
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
    connection = _connect(cache_path)
    if rows:
        names = ",".join(BAR_COLUMNS)
        placeholders = ",".join("?" for _ in BAR_COLUMNS)
        connection.executemany(
            f"INSERT OR REPLACE INTO bars({names}) VALUES({placeholders})",  # noqa: S608
            rows,
        )
        connection.commit()
    connection.close()
    return {"rows": len(rows), "errors": errors[:25], "last_closed_ms": last_closed}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sync_positioning_metrics(cache_path: Path, now_ms: int) -> int:
    rows = store.query(
        """SELECT symbol,snapshot_at_ms,open_interest,mark_price,
                  global_long_short_ratio,taker_buy_sell_ratio,quality_json
           FROM market_positioning_snapshots WHERE snapshot_at_ms>=?""",
        (now_ms - WARMUP_DAYS * 24 * 60 * 60 * 1_000,),
    )
    output: list[tuple[Any, ...]] = []
    for row in rows:
        open_interest = float(row.get("open_interest") or 0)
        mark_price = float(row.get("mark_price") or 0)
        if open_interest <= 0 or mark_price <= 0:
            continue
        quality = _json_object(row.get("quality_json"))
        output.append(
            (
                str(row["symbol"]),
                int(row["snapshot_at_ms"]),
                open_interest,
                open_interest * mark_price,
                float(quality.get("top_account_long_short_ratio") or 1.0),
                float(quality.get("top_position_long_short_ratio") or 1.0),
                float(row.get("global_long_short_ratio") or 1.0),
                float(row.get("taker_buy_sell_ratio") or 1.0),
            )
        )
    connection = _connect(cache_path)
    if output:
        connection.executemany(
            """INSERT OR REPLACE INTO futures_metrics(
                   symbol,open_time,sum_open_interest,sum_open_interest_value,
                   count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
                   count_long_short_ratio,sum_taker_long_short_vol_ratio
               ) VALUES(?,?,?,?,?,?,?,?)""",
            output,
        )
        connection.commit()
    connection.close()
    return len(output)


def refresh_underlyings(cache_path: Path) -> dict[str, Any] | None:
    global _last_underlying_refresh
    if time.monotonic() - _last_underlying_refresh < 6 * 60 * 60:
        return None
    connection = _connect(cache_path)
    latest = connection.execute("SELECT MAX(open_time) FROM underlying_bars").fetchone()[0]
    connection.close()
    if latest and int(time.time() * 1_000) - int(latest) < 6 * 60 * 60 * 1_000:
        _last_underlying_refresh = time.monotonic()
        return None
    result = download_underlying_history(
        metadata_path=CONFIG_DIR / "tradfi_symbols.json",
        cache_path=cache_path,
        workers=4,
        lookback_days=WARMUP_DAYS,
    )
    _last_underlying_refresh = time.monotonic()
    return result


def _serializable_features(row: Any) -> dict[str, float | None]:
    output: dict[str, float | None] = {}
    for name in FEATURE_COLUMNS:
        value = float(row[name])
        output[name] = value if math.isfinite(value) else None
    return output


def compose_prediction(
    *,
    probability_up: float,
    event_probability: float,
    event_candidate: bool,
    fresh: bool,
    quality_score: float,
    barrier_bps: float,
    selection: MultiHeadSelection,
) -> dict[str, Any]:
    """Apply the locked tuning gates and return a three-way prediction."""

    probability_up = min(1.0, max(0.0, float(probability_up)))
    event_probability = min(1.0, max(0.0, float(event_probability)))
    direction_confidence = max(probability_up, 1 - probability_up)
    quality_score = min(1.0, max(0.0, float(quality_score)))
    quality_ok = quality_score >= 0.75
    selected = (
        fresh
        and quality_ok
        and event_candidate
        and event_probability >= selection.occurrence_threshold
        and direction_confidence >= selection.confidence_threshold
    )
    long_probability = event_probability * probability_up
    short_probability = event_probability * (1 - probability_up)
    neutral_probability = 1 - event_probability
    result = (
        "long"
        if selected and probability_up >= 0.5
        else "short"
        if selected
        else "neutral"
    )
    state = "calibrated" if fresh and quality_ok else "data_insufficient"
    if not fresh or not quality_ok:
        long_probability, short_probability, neutral_probability = 0.1, 0.1, 0.8
    confidence = (
        min(direction_confidence, event_probability)
        if selected
        else max(neutral_probability, 1 - direction_confidence)
    )
    reasons = ["TWO_HOUR_LIGHTGBM"]
    reasons.append("EVENT_CANDIDATE" if event_candidate else "NO_EVENT_CANDIDATE")
    reasons.append(
        "OCCURRENCE_GATE_PASS"
        if event_probability >= selection.occurrence_threshold
        else "OCCURRENCE_GATE_ABSTAIN"
    )
    reasons.append(
        "DIRECTION_GATE_PASS"
        if direction_confidence >= selection.confidence_threshold
        else "DIRECTION_GATE_ABSTAIN"
    )
    if not fresh:
        reasons.insert(0, "DATA_INSUFFICIENT")
    if not quality_ok:
        reasons.insert(0, "QUALITY_GATE_ABSTAIN")
    barrier = min(500.0, max(20.0, float(barrier_bps)))
    return {
        "prediction_state": state,
        "result": result,
        "battle_score": round((probability_up - 0.5) * 200 * event_probability, 6),
        "long_probability": round(long_probability, 8),
        "short_probability": round(short_probability, 8),
        "neutral_probability": round(neutral_probability, 8),
        "confidence_score": round(confidence, 8),
        "confidence_label": (
            "high"
            if selected and confidence >= 0.75
            else "medium"
            if confidence >= 0.55
            else "low"
        ),
        "target_bps": round(barrier, 6),
        "stop_bps": round(barrier, 6),
        "reason_codes": reasons,
        "selected": selected,
        "quality_score": round(quality_score, 6),
    }


def model_results(
    dataset: Any,
    models: dict[str, Any],
    manifest: dict[str, Any],
    selection: MultiHeadSelection,
    now_ms: int,
) -> list[dict[str, Any]]:
    latest = (
        dataset.sort_values(["symbol", "open_time"])
        .groupby("symbol", as_index=False, sort=False)
        .tail(1)
        .reset_index(drop=True)
    )
    if latest.empty:
        return []
    matrix = latest[list(FEATURE_COLUMNS)].to_numpy(dtype=np.float32)
    event_direction, event_occurrence, direction_up, occurrence, terminal_up = (
        _multihead_probabilities(
            event_model=models["event"],
            direction_model=models["direction"],
            occurrence_model=models["occurrence"],
            terminal_model=models["terminal"],
            x=matrix,
        )
    )
    probability_up = (
        selection.direction_weight * direction_up
        + selection.terminal_weight * terminal_up
        + selection.event_direction_weight * event_direction
    )
    combined_occurrence = (
        selection.occurrence_weight * occurrence
        + (1 - selection.occurrence_weight) * event_occurrence
    )
    results: list[dict[str, Any]] = []
    for index, row in latest.iterrows():
        feature_time = int(row["open_time"])
        fresh = 0 <= now_ms - (feature_time + BAR_MS) <= 25 * 60 * 1_000
        finite_coverage = float(np.isfinite(matrix[index]).mean())
        quality = (
            0.55 * float(fresh)
            + 0.25 * min(1.0, finite_coverage / 0.95)
            + 0.20 * float(row.get("metrics_available") or 0)
        )
        prediction = compose_prediction(
            probability_up=float(probability_up[index]),
            event_probability=float(combined_occurrence[index]),
            event_candidate=bool(row["event_candidate"]),
            fresh=fresh,
            quality_score=quality,
            barrier_bps=float(row.get("barrier_bps") or 20.0),
            selection=selection,
        )
        prediction.update(
            {
                "symbol": str(row["symbol"]),
                "feature_open_time_ms": feature_time,
                "features": _serializable_features(row),
                "components": {
                    "deployment_mode": "shadow",
                    "run_id": manifest.get("run_id"),
                    "event_direction_up": round(float(event_direction[index]), 8),
                    "event_occurrence": round(float(event_occurrence[index]), 8),
                    "direction_up": round(float(direction_up[index]), 8),
                    "occurrence": round(float(occurrence[index]), 8),
                    "terminal_up": round(float(terminal_up[index]), 8),
                    "combined_probability_up": round(float(probability_up[index]), 8),
                    "combined_occurrence": round(float(combined_occurrence[index]), 8),
                    "event_candidate": bool(row["event_candidate"]),
                    "selected": prediction["selected"],
                    "thresholds": asdict(selection),
                    "qualification": manifest.get("qualification") or {},
                },
            }
        )
        results.append(prediction)
    return results


def register_model(manifest: dict[str, Any]) -> None:
    store.execute(
        """INSERT INTO prediction_model_versions(
               model_key,version,state,feature_schema_version,training_window_json,
               metrics_json,created_at,updated_at)
           VALUES(?,?,'shadow',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
           ON DUPLICATE KEY UPDATE version=VALUES(version),state=VALUES(state),
               feature_schema_version=VALUES(feature_schema_version),
               training_window_json=VALUES(training_window_json),
               metrics_json=VALUES(metrics_json),updated_at=CURRENT_TIMESTAMP""",
        (
            MODEL_KEY,
            MODEL_VERSION,
            FEATURE_SCHEMA_VERSION,
            json.dumps(manifest.get("split_boundaries") or {}),
            json.dumps(
                {
                    "architecture": manifest.get("architecture"),
                    "run_id": manifest.get("run_id"),
                    "event_threshold": manifest.get("event_threshold"),
                    "qualification": manifest.get("qualification"),
                }
            ),
        ),
    )


def _market_contexts() -> dict[str, tuple[float, float]]:
    rows = store.query(
        """SELECT r.symbol,COALESCE(m.mid_price,t.price,r.mark_price) entry_price,
                  COALESCE(m.spread_bps,0) spread_bps
           FROM binance_contract_rules r
           LEFT JOIN market_microstructure m ON m.symbol=r.symbol
           LEFT JOIN ticker t ON t.symbol=r.symbol
           WHERE r.contract_type='TRADIFI_PERPETUAL' AND r.status='TRADING'"""
    )
    return {
        str(row["symbol"]): (
            float(row.get("entry_price") or 0),
            max(0.0, float(row.get("spread_bps") or 0)),
        )
        for row in rows
    }


def persist_predictions(
    results: list[dict[str, Any]], manifest: dict[str, Any], now_ms: int
) -> int:
    register_model(manifest)
    contexts = _market_contexts()
    inserted = 0
    with store.transaction() as transaction:
        for result in results:
            symbol = result["symbol"]
            entry_price, spread_bps = contexts.get(symbol, (0.0, 0.0))
            if entry_price <= 0:
                continue
            feature_time = int(result["feature_open_time_ms"]) + BAR_MS - 1
            transaction.execute(
                """INSERT IGNORE INTO prediction_feature_snapshots(
                       symbol,as_of_ms,feature_schema_version,features_json,quality_score,created_at)
                   VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    symbol,
                    feature_time,
                    FEATURE_SCHEMA_VERSION,
                    json.dumps(result["features"], separators=(",", ":")),
                    result["quality_score"],
                ),
            )
            feature_rows = transaction.query(
                """SELECT id FROM prediction_feature_snapshots
                   WHERE symbol=? AND as_of_ms=? AND feature_schema_version=?""",
                (symbol, feature_time, FEATURE_SCHEMA_VERSION),
            )
            if not feature_rows:
                continue
            feature_id = int(feature_rows[0]["id"])
            existing = transaction.query(
                """SELECT id FROM battle_predictions
                   WHERE feature_snapshot_id=? AND horizon_seconds=?""",
                (feature_id, HORIZON_SECONDS),
            )
            if existing:
                continue
            transaction.execute(
                """UPDATE battle_predictions SET current_marker=NULL
                   WHERE symbol=? AND horizon_seconds=? AND current_marker=1""",
                (symbol, HORIZON_SECONDS),
            )
            transaction.execute(
                """INSERT INTO battle_predictions(
                       public_id,feature_snapshot_id,symbol,horizon_seconds,current_marker,
                       prediction_state,result,battle_score,long_probability,short_probability,
                       neutral_probability,confidence_score,confidence_label,gross_edge_bps,
                       entry_price,spread_bps,target_bps,stop_bps,reason_codes_json,components_json,
                       model_key,model_version,predicted_at_ms,valid_until_ms,created_at)
                   VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    str(uuid.uuid4()),
                    feature_id,
                    symbol,
                    HORIZON_SECONDS,
                    result["prediction_state"],
                    result["result"],
                    result["battle_score"],
                    result["long_probability"],
                    result["short_probability"],
                    result["neutral_probability"],
                    result["confidence_score"],
                    result["confidence_label"],
                    entry_price,
                    spread_bps,
                    result["target_bps"],
                    result["stop_bps"],
                    json.dumps(result["reason_codes"]),
                    json.dumps(result["components"]),
                    MODEL_KEY,
                    MODEL_VERSION,
                    now_ms,
                    now_ms + VALID_FOR_MS,
                ),
            )
            prediction_id = int(transaction.query("SELECT LAST_INSERT_ID() id")[0]["id"])
            transaction.execute(
                """INSERT INTO prediction_outcomes(
                       prediction_id,status,cost_bps,due_at_ms,label_version,created_at,updated_at)
                   VALUES(?,'pending',?,?,2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (prediction_id, spread_bps, now_ms + HORIZON_SECONDS * 1_000),
            )
            inserted += 1
    return inserted


def run_prediction_cycle(now_ms: int | None = None) -> dict[str, Any]:
    current_ms = int(now_ms or time.time() * 1_000)
    cache_path = online_cache_path()
    initialize_cache(cache_path, current_ms)
    models, manifest, selection = load_model_bundle()
    symbols = load_symbol_metadata(CONFIG_DIR / "tradfi_symbols.json")
    bar_result = update_bars(cache_path, symbols, current_ms)
    metric_rows = sync_positioning_metrics(cache_path, current_ms)
    underlying_result = refresh_underlyings(cache_path)
    dataset = build_dataset(cache_path, sample_minutes=15, require_labels=False)
    results = model_results(dataset, models, manifest, selection, current_ms)
    inserted = persist_predictions(results, manifest, current_ms)
    return {
        "symbols": len(results),
        "signals": sum(item["result"] in {"long", "short"} for item in results),
        "inserted": inserted,
        "bar_rows": bar_result["rows"],
        "bar_errors": bar_result["errors"],
        "metric_rows": metric_rows,
        "underlying": underlying_result,
        "feature_time_min": min(
            (item["feature_open_time_ms"] for item in results), default=None
        ),
        "feature_time_max": max(
            (item["feature_open_time_ms"] for item in results), default=None
        ),
    }


def prediction_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        started = time.monotonic()
        try:
            result = run_prediction_cycle()
            store.collector_report(
                "two_hour_model",
                success=result["symbols"] > 0 and not result["bar_errors"],
                items=result["inserted"],
                details=result,
                error=(
                    f"{len(result['bar_errors'])} Binance kline updates failed"
                    if result["bar_errors"]
                    else None
                ),
            )
        except Exception as exc:
            store.collector_report(
                "two_hour_model",
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        wait = max(30.0, 15 * 60 - (time.monotonic() - started))
        if stop_event is None:
            time.sleep(wait)
        elif stop_event.wait(wait):
            break
