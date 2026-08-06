"""Data freshness, coverage and append-only snapshot archives.

The live tables intentionally remain small and use upserts.  This module takes
minute/five-minute samples from those tables, keeps source telemetry consistent,
and exposes a conservative quality gate for model consumers.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from . import store

ARCHIVE_INTERVAL_SECONDS = 15
DEFAULT_MAX_AGE_MS = {
    "market_microstructure": 15_000,
    "underlying_market_quotes": 10 * 60_000,
    "social": 30 * 60_000,
    "positioning": 10 * 60_000,
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _age_ms(received: Any, now_ms: int, *, seconds: bool = False) -> int | None:
    value = _int(received)
    if not value:
        return None
    if seconds or value < 10**12:
        value *= 1_000
    return max(0, now_ms - value)


def source_quality(
    *,
    source: str,
    age_ms: int | None,
    coverage_ratio: float,
    latency_ms: int | None = None,
    max_age_ms: int | None = None,
) -> dict[str, Any]:
    """Return the canonical quality fields used in APIs and model features."""

    threshold = int(max_age_ms or DEFAULT_MAX_AGE_MS.get(source, 5 * 60_000))
    try:
        coverage = float(coverage_ratio or 0.0)
    except (TypeError, ValueError):
        coverage = 0.0
    coverage = min(1.0, max(0.0, coverage)) if math.isfinite(coverage) else 0.0
    fresh = age_ms is not None and age_ms <= threshold
    usable = bool(fresh and coverage >= 0.80)
    return {
        "age_ms": age_ms,
        "latency_ms": latency_ms if latency_ms is None else max(0, int(latency_ms)),
        "coverage_ratio": round(coverage, 6),
        "max_age_ms": threshold,
        "fresh": fresh,
        "usable": usable,
        "source": source,
    }


def quality_gate(
    quality_score: float,
    *,
    age_ms: int | None = None,
    max_age_ms: int = 15_000,
    coverage_ratio: float = 1.0,
    minimum_score: float = 0.75,
) -> tuple[bool, str]:
    """Conservative model gate; stale/low-coverage inputs must abstain."""

    try:
        score = float(quality_score or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = min(1.0, max(0.0, score)) if math.isfinite(score) else 0.0
    if age_ms is None or age_ms > max_age_ms:
        return False, "stale"
    try:
        coverage = float(coverage_ratio or 0.0)
    except (TypeError, ValueError):
        coverage = 0.0
    if not math.isfinite(coverage) or coverage < 0.80:
        return False, "coverage_low"
    if score < minimum_score:
        return False, "quality_low"
    return True, "ok"


def _archive_micro(now_ms: int, bucket_size: int) -> int:
    rows = store.query(
        "SELECT symbol,event_time,received_at,mid_price,spread_bps,book_imbalance,"
        "aggressive_buy_ratio,trade_count_60s,quote_volume_60s,realized_volatility_60s,"
        "price_velocity_bps_60s,depth_levels,quality_json FROM market_microstructure"
    )
    values = []
    for row in rows:
        event_time = _int(row.get("event_time"))
        received = _int(row.get("received_at"))
        if not event_time:
            continue
        # Both fields are milliseconds in the websocket snapshot table.
        bucket = (event_time // (bucket_size * 1_000)) * bucket_size
        age = _age_ms(received, now_ms)
        latency = max(0, received - event_time) if received and event_time else None
        raw_quality = row.get("quality_json")
        if isinstance(raw_quality, str):
            try:
                raw_quality = json.loads(raw_quality)
            except (TypeError, ValueError):
                raw_quality = {}
        raw_quality = raw_quality if isinstance(raw_quality, dict) else {}
        available = sum(
            row.get(name) is not None
            for name in ("mid_price", "spread_bps", "book_imbalance", "aggressive_buy_ratio")
        )
        quality = {
            **raw_quality,
            **source_quality(
                source="market_microstructure",
                age_ms=age,
                latency_ms=latency,
                coverage_ratio=available / 4,
            ),
        }
        values.append(
            (
                str(row["symbol"]), bucket, bucket_size, event_time, received,
                row.get("mid_price"), row.get("spread_bps"), row.get("book_imbalance"),
                row.get("aggressive_buy_ratio"), row.get("trade_count_60s"),
                row.get("quote_volume_60s"), row.get("realized_volatility_60s"),
                row.get("price_velocity_bps_60s"), row.get("depth_levels"),
                age, latency, quality["coverage_ratio"], json.dumps(quality, ensure_ascii=False),
            )
        )
    if not values:
        return 0
    return store.executemany(
        """INSERT IGNORE INTO market_microstructure_archive(
          symbol,bucket_ts,bucket_size_seconds,event_time,received_at,mid_price,spread_bps,
          book_imbalance,aggressive_buy_ratio,trade_count_60s,quote_volume_60s,
          realized_volatility_60s,price_velocity_bps_60s,depth_levels,source_age_ms,
          latency_ms,coverage_ratio,quality_json,created_at)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        values,
    )


def _archive_underlying(now_ms: int, bucket_size: int) -> int:
    rows = store.query(
        "SELECT contract_symbol,quote_symbol,status,market_state,market_time_ms,price,"
        "change_pct,volume,received_at_ms,quality_json FROM underlying_market_quotes"
    )
    values = []
    for row in rows:
        received = _int(row.get("received_at_ms"))
        bucket = (received // (bucket_size * 1_000)) * bucket_size
        market_time = _int(row.get("market_time_ms"))
        age = _age_ms(market_time, now_ms)
        latency = max(0, received - market_time) if received and market_time else None
        available = sum(row.get(name) is not None for name in ("price", "change_pct", "volume"))
        raw_quality = row.get("quality_json")
        if isinstance(raw_quality, str):
            try:
                raw_quality = json.loads(raw_quality)
            except (TypeError, ValueError):
                raw_quality = {}
        raw_quality = raw_quality if isinstance(raw_quality, dict) else {}
        quality = {
            **raw_quality,
            **source_quality(
                source="underlying_market_quotes",
                age_ms=age,
                latency_ms=latency,
                coverage_ratio=available / 3,
            ),
        }
        values.append(
            (
                str(row["contract_symbol"]), bucket, bucket_size,
                row.get("quote_symbol"), row.get("status"), row.get("market_state"),
                market_time or None, row.get("price"), row.get("change_pct"), row.get("volume"),
                age, latency, quality["coverage_ratio"], json.dumps(quality, ensure_ascii=False),
            )
        )
    if not values:
        return 0
    return store.executemany(
        """INSERT IGNORE INTO underlying_market_quotes_archive(
          contract_symbol,bucket_ts,bucket_size_seconds,quote_symbol,status,market_state,
          market_time_ms,price,change_pct,volume,source_age_ms,latency_ms,coverage_ratio,
          quality_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        values,
    )


def _archive_social(now_ms: int, bucket_size: int) -> int:
    rows = store.query(
        "SELECT symbol,st_bull,st_bear,st_msgs,ape_mentions,ape_upvotes,ape_rank,"
        "ape_rank_24h,ts FROM social"
    )
    values = []
    for row in rows:
        source_ts = _int(row.get("ts"))
        if not source_ts:
            continue
        bucket = (source_ts // bucket_size) * bucket_size
        age = _age_ms(source_ts, now_ms, seconds=True)
        available = sum(row.get(name) is not None for name in ("st_msgs", "ape_mentions", "ape_upvotes"))
        quality = source_quality(
            source="social", age_ms=age, latency_ms=None, coverage_ratio=available / 3,
        )
        values.append(
            (
                str(row["symbol"]), bucket, bucket_size, source_ts,
                row.get("st_bull"), row.get("st_bear"), row.get("st_msgs"),
                row.get("ape_mentions"), row.get("ape_upvotes"), row.get("ape_rank"),
                row.get("ape_rank_24h"), age, quality["coverage_ratio"],
                json.dumps(quality, ensure_ascii=False),
            )
        )
    if not values:
        return 0
    return store.executemany(
        """INSERT IGNORE INTO social_archive(
          symbol,bucket_ts,bucket_size_seconds,source_ts,st_bull,st_bear,st_msgs,
          ape_mentions,ape_upvotes,ape_rank,ape_rank_24h,source_age_ms,coverage_ratio,
          quality_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        values,
    )


def archive_current_snapshots(now_ms: int | None = None) -> dict[str, int]:
    """Archive the current upsert tables at both 1m and 5m resolutions."""

    now = int(now_ms or time.time() * 1_000)
    result = {"microstructure": 0, "underlying": 0, "social": 0}
    for bucket_size in (60, 300):
        for key, fn in (
            ("microstructure", _archive_micro),
            ("underlying", _archive_underlying),
            ("social", _archive_social),
        ):
            try:
                result[key] += int(fn(now, bucket_size))
            except Exception as exc:
                store.record_data_quality_event(
                    "snapshot_archive", "archive_error", severity="warning",
                    details={"table": key, "error": f"{type(exc).__name__}: {exc}"},
                )
    return result


def archive_loop(stop_event=None) -> None:
    """Small worker target; register it in the market role when enabled."""

    while stop_event is None or not stop_event.is_set():
        try:
            result = archive_current_snapshots()
            store.collector_report(
                "data-quality-archive", success=True,
                items=sum(result.values()), details=result,
            )
        except Exception as exc:
            store.collector_report("data-quality-archive", success=False, error=str(exc))
        if stop_event is None:
            time.sleep(ARCHIVE_INTERVAL_SECONDS)
        elif stop_event.wait(ARCHIVE_INTERVAL_SECONDS):
            break
