"""Reproducible two-hour TradFi research and walk-forward validation.

This module deliberately lives outside the online prediction path.  It builds
immutable, timestamped market-only baselines from Binance public archives.  News,
social and order-book features can be joined later by ``symbol`` and ``open_time``
without changing the label or split definitions.
"""

from __future__ import annotations

import csv
import http.client
import io
import json
import math
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, early_stopping, log_evaluation
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import accuracy_score, brier_score_loss, confusion_matrix

ARCHIVE_ORIGIN = "https://data.binance.vision"
DATASET_CACHE_SCHEMA = 2
BAR_COLUMNS = (
    "symbol",
    "market_type",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)
FEATURE_COLUMNS = (
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "ret_24",
    "trend_6_24",
    "trend_24_72",
    "realized_vol_12",
    "realized_vol_24",
    "realized_vol_96",
    "atr_bps_14",
    "rsi_14",
    "volume_z_24",
    "volume_z_96",
    "trade_z_24",
    "taker_ratio",
    "taker_ratio_6",
    "taker_impulse",
    "body_bps",
    "range_bps",
    "upper_wick_bps",
    "lower_wick_bps",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "is_us_regular",
    "is_us_premarket",
    "is_us_afterhours",
    "is_weekend",
    "barrier_bps",
    "event_trigger_strength",
    "external_event",
    "external_earnings",
    "external_periodic",
    "external_sentiment",
    "external_quality",
    "external_text_score",
    "external_guidance_score",
    "external_growth_score",
    "global_ret_1",
    "global_ret_24",
    "group_ret_1",
    "group_ret_24",
    "relative_ret_24",
    "underlying_available",
    "underlying_ret_1",
    "underlying_ret_3",
    "underlying_ret_6",
    "underlying_ret_12",
    "underlying_ret_24",
    "underlying_trend_6_24",
    "underlying_volume_z_24",
    "contract_underlying_gap_1",
    "contract_underlying_gap_24",
    "metrics_available",
    "oi_change_1",
    "oi_change_3",
    "oi_change_12",
    "oi_value_z_24",
    "top_account_bias",
    "top_position_bias",
    "global_account_bias",
    "metrics_taker_bias",
    "top_account_impulse_3",
    "top_position_impulse_3",
    "global_account_impulse_3",
    "metrics_taker_impulse_3",
    "top_global_disagreement",
    "market_EQUITY",
    "market_HK_EQUITY",
    "market_KR_EQUITY",
    "market_COMMODITY",
    "market_PREMARKET",
)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,32}$")
_INTERVAL_RE = re.compile(r"^(1m|3m|5m|15m|30m|1h)$")


@dataclass(frozen=True)
class ArchiveTask:
    symbol: str
    market_type: str
    interval: str
    period_kind: str
    period: str

    @property
    def url(self) -> str:
        filename = f"{self.symbol}-{self.interval}-{self.period}.zip"
        return (
            f"{ARCHIVE_ORIGIN}/data/futures/um/{self.period_kind}/klines/"
            f"{self.symbol}/{self.interval}/{filename}"
        )


@dataclass(frozen=True)
class MetricsArchiveTask:
    symbol: str
    period: str

    @property
    def url(self) -> str:
        filename = f"{self.symbol}-metrics-{self.period}.zip"
        return (
            f"{ARCHIVE_ORIGIN}/data/futures/um/daily/metrics/"
            f"{self.symbol}/{filename}"
        )


@dataclass(frozen=True)
class SplitBoundaries:
    train_end_ms: int
    calibration_end_ms: int
    tuning_end_ms: int
    test_end_ms: int


@dataclass(frozen=True)
class ThresholdChoice:
    threshold: float
    samples: int
    precision: float
    coverage: float
    target_met: bool


@dataclass(frozen=True)
class MultiHeadSelection:
    samples: int
    precision: float
    coverage: float
    direction_weight: float
    terminal_weight: float
    event_direction_weight: float
    occurrence_weight: float
    occurrence_threshold: float
    confidence_threshold: float
    target_precision: float


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bars(
            symbol TEXT NOT NULL,
            market_type TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            quote_volume REAL NOT NULL,
            trades INTEGER NOT NULL,
            taker_buy_volume REAL NOT NULL,
            taker_buy_quote_volume REAL NOT NULL,
            PRIMARY KEY(symbol,open_time)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_bars_time ON bars(open_time)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS futures_metrics(
            symbol TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            sum_open_interest REAL NOT NULL,
            sum_open_interest_value REAL NOT NULL,
            count_toptrader_long_short_ratio REAL NOT NULL,
            sum_toptrader_long_short_ratio REAL NOT NULL,
            count_long_short_ratio REAL NOT NULL,
            sum_taker_long_short_vol_ratio REAL NOT NULL,
            PRIMARY KEY(symbol,open_time)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_futures_metrics_time ON futures_metrics(open_time)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS external_events(
            symbol TEXT NOT NULL,
            event_time INTEGER NOT NULL,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            sentiment_score REAL NOT NULL DEFAULT 0,
            quality_score REAL NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(source,source_id,symbol)
        )
        """
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS ix_external_events_symbol_time
           ON external_events(symbol,event_time)"""
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS external_fetch_log(
            source TEXT NOT NULL,
            period TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            records INTEGER NOT NULL,
            PRIMARY KEY(source,period)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS underlying_bars(
            symbol TEXT NOT NULL,
            ticker TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY(symbol,open_time)
        ) WITHOUT ROWID
        """
    )
    return connection


def load_symbol_metadata(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    output: list[dict[str, Any]] = []
    for item in payload.get("symbols", []):
        symbol = str(item.get("symbol") or "").upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            continue
        output.append(
            {
                "symbol": symbol,
                "market_type": str(item.get("underlyingType") or "UNKNOWN").upper(),
                "onboard_ms": int(item.get("onboardDate") or 0),
            }
        )
    return output


def archive_tasks(
    symbols: Sequence[dict[str, Any]],
    *,
    interval: str,
    as_of: date,
    lookback_days: int,
) -> list[ArchiveTask]:
    if not _INTERVAL_RE.fullmatch(interval):
        raise ValueError("unsupported research interval")
    earliest = as_of - timedelta(days=max(1, lookback_days))
    current_month = as_of.replace(day=1)
    tasks: list[ArchiveTask] = []
    for item in symbols:
        onboard = datetime.fromtimestamp(item["onboard_ms"] / 1000, UTC).date()
        start = max(earliest, onboard)
        month = start.replace(day=1)
        while month < current_month:
            tasks.append(
                ArchiveTask(
                    item["symbol"], item["market_type"], interval, "monthly", month.strftime("%Y-%m")
                )
            )
            next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            month = next_month
        day = max(start, current_month)
        while day < as_of:
            tasks.append(
                ArchiveTask(
                    item["symbol"], item["market_type"], interval, "daily", day.isoformat()
                )
            )
            day += timedelta(days=1)
    return tasks


def _normalise_timestamp(value: str) -> int:
    timestamp = int(value)
    while timestamp > 10_000_000_000_000:
        timestamp //= 1000
    return timestamp


def parse_archive(content: bytes, task: ArchiveTask) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError("Binance archive must contain exactly one CSV")
        with archive.open(names[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            for row in reader:
                if not row or row[0] == "open_time":
                    continue
                if len(row) < 11:
                    continue
                rows.append(
                    (
                        task.symbol,
                        task.market_type,
                        _normalise_timestamp(row[0]),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                        float(row[5]),
                        float(row[7]),
                        int(row[8]),
                        float(row[9]),
                        float(row[10]),
                    )
                )
    return rows


def download_archive(task: ArchiveTask, retries: int = 3) -> list[tuple[Any, ...]]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed HTTPS archive origin
                task.url,
                headers={"User-Agent": "QuantDesk-NG two-hour research"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                if response.geturl().split("/", 3)[:3] != task.url.split("/", 3)[:3]:
                    raise ValueError("archive redirect changed origin")
                return parse_archive(response.read(), task)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            last_error = exc
        except (
            http.client.IncompleteRead,
            OSError,
            TimeoutError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            last_error = exc
        time.sleep(0.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return []


def _task_bounds_ms(task: ArchiveTask) -> tuple[int, int]:
    if task.period_kind == "monthly":
        start = datetime.strptime(task.period, "%Y-%m").replace(tzinfo=UTC)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return int(start.timestamp() * 1000), int(next_month.timestamp() * 1000)
    start = datetime.strptime(task.period, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _task_is_cached(connection: sqlite3.Connection, task: ArchiveTask) -> bool:
    start_ms, end_ms = _task_bounds_ms(task)
    row = connection.execute(
        "SELECT 1 FROM bars WHERE symbol=? AND open_time>=? AND open_time<? LIMIT 1",
        (task.symbol, start_ms, end_ms),
    ).fetchone()
    return row is not None


def download_history(
    *,
    metadata_path: Path,
    cache_path: Path,
    interval: str = "5m",
    lookback_days: int = 120,
    workers: int = 8,
    as_of: date | None = None,
) -> dict[str, Any]:
    symbols = load_symbol_metadata(metadata_path)
    tasks = archive_tasks(
        symbols,
        interval=interval,
        as_of=as_of or datetime.now(UTC).date(),
        lookback_days=lookback_days,
    )
    connection = _connect(cache_path)
    cached_tasks = sum(1 for task in tasks if _task_is_cached(connection, task))
    tasks = [task for task in tasks if not _task_is_cached(connection, task)]
    inserted = archives = missing = failures = 0
    errors: list[str] = []
    insert_sql = (
        "INSERT OR REPLACE INTO bars(" + ",".join(BAR_COLUMNS) + ") VALUES(" + ",".join("?" * len(BAR_COLUMNS)) + ")"
    )
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(download_archive, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                rows = future.result()
                if rows:
                    connection.executemany(insert_sql, rows)
                    connection.commit()
                    inserted += len(rows)
                    archives += 1
                else:
                    missing += 1
            except Exception as exc:  # keep other independent archives usable
                failures += 1
                errors.append(f"{task.symbol}/{task.period}: {type(exc).__name__}: {exc}")
    counts = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT symbol),MIN(open_time),MAX(open_time) FROM bars"
    ).fetchone()
    connection.close()
    return {
        "requested_archives": len(tasks),
        "already_cached_archives": cached_tasks,
        "downloaded_archives": archives,
        "missing_archives": missing,
        "failed_archives": failures,
        "inserted_rows": inserted,
        "cached_rows": int(counts[0] or 0),
        "cached_symbols": int(counts[1] or 0),
        "first_open_time": counts[2],
        "last_open_time": counts[3],
        "errors": errors[:50],
    }


def metrics_archive_tasks(
    symbols: Sequence[dict[str, Any]], *, as_of: date, lookback_days: int
) -> list[MetricsArchiveTask]:
    earliest = as_of - timedelta(days=max(1, lookback_days))
    tasks: list[MetricsArchiveTask] = []
    for item in symbols:
        onboard = datetime.fromtimestamp(item["onboard_ms"] / 1000, UTC).date()
        day = max(earliest, onboard)
        while day < as_of:
            tasks.append(MetricsArchiveTask(item["symbol"], day.isoformat()))
            day += timedelta(days=1)
    return tasks


def _parse_metrics_archive(
    content: bytes, task: MetricsArchiveTask
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError("Binance metrics archive must contain exactly one CSV")
        with archive.open(names[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            required = {
                "create_time",
                "symbol",
                "sum_open_interest",
                "sum_open_interest_value",
                "count_toptrader_long_short_ratio",
                "sum_toptrader_long_short_ratio",
                "count_long_short_ratio",
                "sum_taker_long_short_vol_ratio",
            }
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("Binance metrics archive schema changed")
            for row in reader:
                if str(row.get("symbol") or "").upper() != task.symbol:
                    continue
                open_interest = str(row.get("sum_open_interest") or "").strip()
                open_interest_value = str(row.get("sum_open_interest_value") or "").strip()
                if not open_interest or not open_interest_value:
                    continue
                timestamp = datetime.fromisoformat(str(row["create_time"])).replace(tzinfo=UTC)
                rows.append(
                    (
                        task.symbol,
                        int(timestamp.timestamp() * 1000),
                        float(open_interest),
                        float(open_interest_value),
                        float(row["count_toptrader_long_short_ratio"] or 1.0),
                        float(row["sum_toptrader_long_short_ratio"] or 1.0),
                        float(row["count_long_short_ratio"] or 1.0),
                        float(row["sum_taker_long_short_vol_ratio"] or 1.0),
                    )
                )
    return rows


def _download_metrics_archive(
    task: MetricsArchiveTask, retries: int = 3
) -> list[tuple[Any, ...]]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed HTTPS archive origin
                task.url,
                headers={"User-Agent": "QuantDesk-NG two-hour metrics research"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                if response.geturl().split("/", 3)[:3] != task.url.split("/", 3)[:3]:
                    raise ValueError("metrics archive redirect changed origin")
                return _parse_metrics_archive(response.read(), task)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            last_error = exc
        except (
            http.client.IncompleteRead,
            OSError,
            TimeoutError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            last_error = exc
        time.sleep(0.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return []


def _metrics_task_is_cached(
    connection: sqlite3.Connection, task: MetricsArchiveTask
) -> bool:
    start = datetime.fromisoformat(task.period).replace(tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    row = connection.execute(
        "SELECT 1 FROM futures_metrics WHERE symbol=? AND open_time>=? AND open_time<? LIMIT 1",
        (task.symbol, start_ms, end_ms),
    ).fetchone()
    return row is not None


def download_futures_metrics(
    *,
    metadata_path: Path,
    cache_path: Path,
    lookback_days: int = 700,
    workers: int = 16,
    as_of: date | None = None,
) -> dict[str, Any]:
    symbols = load_symbol_metadata(metadata_path)
    tasks = metrics_archive_tasks(
        symbols,
        as_of=as_of or datetime.now(UTC).date(),
        lookback_days=lookback_days,
    )
    connection = _connect(cache_path)
    cached_tasks = sum(1 for task in tasks if _metrics_task_is_cached(connection, task))
    tasks = [task for task in tasks if not _metrics_task_is_cached(connection, task)]
    inserted = archives = missing = failures = 0
    errors: list[str] = []
    insert_sql = """INSERT OR REPLACE INTO futures_metrics(
        symbol,open_time,sum_open_interest,sum_open_interest_value,
        count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
        count_long_short_ratio,sum_taker_long_short_vol_ratio
    ) VALUES(?,?,?,?,?,?,?,?)"""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(_download_metrics_archive, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                rows = future.result()
                if rows:
                    connection.executemany(insert_sql, rows)
                    connection.commit()
                    inserted += len(rows)
                    archives += 1
                else:
                    missing += 1
            except Exception as exc:
                failures += 1
                errors.append(f"{task.symbol}/{task.period}: {type(exc).__name__}: {exc}")
    counts = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT symbol),MIN(open_time),MAX(open_time) FROM futures_metrics"
    ).fetchone()
    connection.close()
    return {
        "requested_archives": len(tasks),
        "already_cached_archives": cached_tasks,
        "downloaded_archives": archives,
        "missing_archives": missing,
        "failed_archives": failures,
        "inserted_rows": inserted,
        "cached_rows": int(counts[0] or 0),
        "cached_symbols": int(counts[1] or 0),
        "first_open_time": counts[2],
        "last_open_time": counts[3],
        "errors": errors[:50],
    }


def _sec_json(url: str, retries: int = 3) -> Any:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
        raise ValueError("SEC research request used an unapproved origin")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - validated SEC HTTPS origins
                url,
                headers={
                    "User-Agent": "QuantDesk-NG/0.2 research-contact research@localhost",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname not in {
                    "www.sec.gov",
                    "data.sec.gov",
                }:
                    raise ValueError("SEC redirect changed to an unapproved origin")
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = exc
        except (
            http.client.IncompleteRead,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
        time.sleep(0.75 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return None


def _sec_events_for_symbol(
    symbol: str,
    cik: int,
    minimum_time_ms: int,
    maximum_time_ms: int,
) -> list[tuple[Any, ...]]:
    time.sleep(0.15)
    payload = _sec_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if not isinstance(payload, dict):
        return []
    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []
    forms = recent.get("form", [])
    output: list[tuple[Any, ...]] = []
    accepted = recent.get("acceptanceDateTime", [])
    items = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])
    primary_documents = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    allowed_forms = {"8-K", "10-K", "10-Q", "20-F", "6-K"}
    for index, form in enumerate(forms):
        if form not in allowed_forms or index >= len(accepted):
            continue
        try:
            event_time = int(
                datetime.fromisoformat(str(accepted[index]).replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except ValueError:
            continue
        if event_time < minimum_time_ms or event_time > maximum_time_ms:
            continue
        item_text = str(items[index]) if index < len(items) else ""
        if form == "8-K" and "2.02" in item_text:
            event_type = "EARNINGS"
            quality = 1.0
        elif form in {"10-K", "10-Q", "20-F"}:
            event_type = "PERIODIC"
            quality = 0.85
        else:
            event_type = "CORPORATE"
            quality = 0.70
        accession = str(accessions[index]) if index < len(accessions) else f"{event_time}"
        metadata = {
            "form": form,
            "items": item_text,
            "primary_document": (
                str(primary_documents[index]) if index < len(primary_documents) else ""
            ),
            "description": str(descriptions[index]) if index < len(descriptions) else "",
            "cik": cik,
        }
        output.append(
            (
                symbol,
                event_time,
                "SEC_EDGAR",
                event_type,
                accession,
                0.0,
                quality,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return output


def download_sec_events(
    *,
    metadata_path: Path,
    cache_path: Path,
    workers: int = 4,
    lookback_days: int = 730,
) -> dict[str, Any]:
    symbols = load_symbol_metadata(metadata_path)
    connection = _connect(cache_path)
    bounds = connection.execute("SELECT MIN(open_time),MAX(open_time) FROM bars").fetchone()
    if not bounds or bounds[0] is None or bounds[1] is None:
        connection.close()
        raise ValueError("download market history before SEC events")
    ticker_payload = _sec_json("https://www.sec.gov/files/company_tickers.json")
    ticker_map = {
        str(item.get("ticker") or "").upper(): int(item["cik_str"])
        for item in (ticker_payload or {}).values()
        if isinstance(item, dict) and item.get("cik_str") is not None
    }
    ticker_aliases = {"BRKB": "BRK-B"}
    matched: list[tuple[str, int]] = []
    for item in symbols:
        if item["market_type"] != "EQUITY":
            continue
        ticker = item["symbol"].removesuffix("USDT")
        ticker = ticker_aliases.get(ticker, ticker)
        cik = ticker_map.get(ticker)
        if cik is not None:
            matched.append((item["symbol"], cik))
    rows: list[tuple[Any, ...]] = []
    errors: list[str] = []
    minimum_event_time = max(
        0,
        int((datetime.now(UTC) - timedelta(days=max(1, lookback_days))).timestamp() * 1000),
    )
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
        futures = {
            executor.submit(
                _sec_events_for_symbol,
                symbol,
                cik,
                minimum_event_time,
                int(bounds[1]) + 2 * 60 * 60 * 1000,
            ): symbol
            for symbol, cik in matched
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
    if rows:
        connection.executemany(
            """INSERT INTO external_events(
                   symbol,event_time,source,event_type,source_id,sentiment_score,
                   quality_score,metadata_json
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(source,source_id,symbol) DO UPDATE SET
                   event_time=excluded.event_time,
                   event_type=excluded.event_type,
                   quality_score=MAX(external_events.quality_score,excluded.quality_score)""",
            rows,
        )
        connection.commit()
    totals = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT symbol) FROM external_events WHERE source='SEC_EDGAR'"
    ).fetchone()
    connection.close()
    return {
        "eligible_equity_contracts": sum(
            1 for item in symbols if item["market_type"] == "EQUITY"
        ),
        "matched_sec_tickers": len(matched),
        "events_downloaded": len(rows),
        "cached_events": int(totals[0] or 0),
        "event_symbols": int(totals[1] or 0),
        "lookback_days": int(lookback_days),
        "errors": errors[:50],
    }


def _yahoo_chart(
    symbol: str, ticker: str, start_seconds: int, end_seconds: int, retries: int = 3
) -> list[tuple[Any, ...]]:
    quoted_ticker = urllib.parse.quote(ticker, safe="-^=")
    query = urllib.parse.urlencode(
        {
            "period1": int(start_seconds),
            "period2": int(end_seconds),
            "interval": "1h",
            "events": "history",
            "includePrePost": "true",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quoted_ticker}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed Yahoo HTTPS origin
                url, headers={"User-Agent": "Mozilla/5.0 (QuantDesk-NG research)"}
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname not in {
                    "query1.finance.yahoo.com",
                    "query2.finance.yahoo.com",
                }:
                    raise ValueError("Yahoo redirect changed origin")
                payload = json.loads(response.read().decode("utf-8"))
            result = payload.get("chart", {}).get("result") or []
            if not result:
                return []
            timestamps = result[0].get("timestamp") or []
            quotes = result[0].get("indicators", {}).get("quote") or []
            if not quotes:
                return []
            quote = quotes[0]
            output: list[tuple[Any, ...]] = []
            for index, timestamp in enumerate(timestamps):
                values = [
                    quote.get(column, [None] * len(timestamps))[index]
                    for column in ("open", "high", "low", "close", "volume")
                ]
                if any(value is None for value in values):
                    continue
                output.append(
                    (
                        symbol,
                        ticker,
                        int(timestamp) * 1000,
                        float(values[0]),
                        float(values[1]),
                        float(values[2]),
                        float(values[3]),
                        float(values[4]),
                    )
                )
            return output
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            last_error = exc
        except (
            http.client.IncompleteRead,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
        time.sleep(0.75 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return []


def download_underlying_history(
    *,
    metadata_path: Path,
    cache_path: Path,
    workers: int = 4,
    lookback_days: int = 700,
) -> dict[str, Any]:
    symbols = load_symbol_metadata(metadata_path)
    ticker_payload = _sec_json("https://www.sec.gov/files/company_tickers.json")
    sec_tickers = {
        str(item.get("ticker") or "").upper()
        for item in (ticker_payload or {}).values()
        if isinstance(item, dict)
    }
    aliases = {"BRKB": "BRK-B"}
    matched: list[tuple[str, str]] = []
    for item in symbols:
        if item["market_type"] != "EQUITY":
            continue
        base = item["symbol"].removesuffix("USDT")
        ticker = aliases.get(base, base)
        if ticker in sec_tickers:
            matched.append((item["symbol"], ticker))
    end_seconds = int(time.time())
    start_seconds = end_seconds - max(1, lookback_days) * 24 * 60 * 60
    connection = _connect(cache_path)
    inserted = 0
    completed = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
        futures = {
            executor.submit(_yahoo_chart, symbol, ticker, start_seconds, end_seconds): symbol
            for symbol, ticker in matched
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                rows = future.result()
                if rows:
                    connection.executemany(
                        """INSERT OR REPLACE INTO underlying_bars(
                               symbol,ticker,open_time,open,high,low,close,volume
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        rows,
                    )
                    connection.commit()
                    inserted += len(rows)
                    completed += 1
            except Exception as exc:
                errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
    totals = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT symbol),MIN(open_time),MAX(open_time) FROM underlying_bars"
    ).fetchone()
    connection.close()
    return {
        "matched_symbols": len(matched),
        "completed_symbols": completed,
        "inserted_rows": inserted,
        "cached_rows": int(totals[0] or 0),
        "cached_symbols": int(totals[1] or 0),
        "first_open_time": totals[2],
        "last_open_time": totals[3],
        "lookback_days": int(lookback_days),
        "errors": errors[:50],
    }


def _nasdaq_json(day: date, retries: int = 3) -> Any:
    query = urllib.parse.urlencode({"date": day.isoformat()})
    url = f"https://api.nasdaq.com/api/calendar/earnings?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed Nasdaq HTTPS origin
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (QuantDesk-NG research)",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.nasdaq.com",
                    "Referer": "https://www.nasdaq.com/",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname != "api.nasdaq.com":
                    raise ValueError("Nasdaq redirect changed origin")
                return json.loads(response.read().decode("utf-8"))
        except (
            http.client.IncompleteRead,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
        time.sleep(0.75 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return None


def _parse_market_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("$", "").replace(",", "").replace("%", "")
    if not text or text.lower() in {"n/a", "na", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def download_earnings_surprises(*, cache_path: Path, workers: int = 4) -> dict[str, Any]:
    connection = _connect(cache_path)
    event_rows = connection.execute(
        """SELECT symbol,event_time,source_id,metadata_json
           FROM external_events
           WHERE source='SEC_EDGAR' AND event_type='EARNINGS'
           ORDER BY event_time"""
    ).fetchall()
    events_by_date: dict[date, list[sqlite3.Row | tuple[Any, ...]]] = {}
    for row in event_rows:
        event_day = datetime.fromtimestamp(int(row[1]) / 1000, UTC).date()
        events_by_date.setdefault(event_day, []).append(row)
    cached_periods = {
        str(row[0])
        for row in connection.execute(
            "SELECT period FROM external_fetch_log WHERE source='NASDAQ_EARNINGS'"
        ).fetchall()
    }
    requested_days = [day for day in events_by_date if day.isoformat() not in cached_periods]
    payloads: dict[date, Any] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
        futures = {executor.submit(_nasdaq_json, day): day for day in requested_days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                payloads[day] = future.result()
            except Exception as exc:
                errors.append(f"{day}: {type(exc).__name__}: {exc}")
    updates = 0
    for day, payload in payloads.items():
        rows = payload.get("data", {}).get("rows", []) if isinstance(payload, dict) else []
        calendar = {
            str(item.get("symbol") or "").upper(): item
            for item in rows or []
            if isinstance(item, dict)
        }
        for event in events_by_date.get(day, []):
            symbol, _, source_id, metadata_json = event
            ticker = str(symbol).removesuffix("USDT")
            ticker = {"BRKB": "BRK-B"}.get(ticker, ticker)
            item = calendar.get(ticker)
            if item is None:
                continue
            actual = _parse_market_number(item.get("eps"))
            forecast = _parse_market_number(item.get("epsForecast"))
            surprise = _parse_market_number(item.get("surprise"))
            if surprise is None and actual is not None and forecast not in {None, 0.0}:
                surprise = (actual - forecast) / abs(float(forecast)) * 100
            if surprise is None:
                continue
            metadata = json.loads(str(metadata_json) or "{}")
            metadata["nasdaq_earnings"] = {
                "actual_eps": actual,
                "forecast_eps": forecast,
                "surprise_pct": surprise,
                "estimates": _parse_market_number(item.get("noOfEsts")),
                "fiscal_quarter": str(item.get("fiscalQuarterEnding") or ""),
            }
            score = math.tanh(float(surprise) / 10.0)
            connection.execute(
                """UPDATE external_events
                   SET sentiment_score=?,quality_score=1.0,metadata_json=?
                   WHERE source='SEC_EDGAR' AND source_id=? AND symbol=?""",
                (
                    score,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    source_id,
                    symbol,
                ),
            )
            updates += 1
        connection.execute(
            """INSERT OR REPLACE INTO external_fetch_log(source,period,fetched_at,records)
               VALUES('NASDAQ_EARNINGS',?,?,?)""",
            (day.isoformat(), int(time.time() * 1000), len(rows or [])),
        )
    connection.commit()
    enriched = connection.execute(
        """SELECT COUNT(*) FROM external_events
           WHERE event_type='EARNINGS' AND ABS(sentiment_score)>0"""
    ).fetchone()
    connection.close()
    return {
        "earnings_events": len(event_rows),
        "calendar_days": len(events_by_date),
        "already_cached_days": len(cached_periods),
        "requested_days": len(requested_days),
        "updated_events": updates,
        "enriched_events": int(enriched[0] or 0),
        "errors": errors[:50],
    }


def _sec_text(url: str, retries: int = 3) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.sec.gov":
        raise ValueError("SEC document request used an unapproved origin")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - validated SEC HTTPS origin
                url,
                headers={
                    "User-Agent": "QuantDesk-NG research admin@quantdesk.example",
                    "Accept-Encoding": "identity",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname != "www.sec.gov":
                    raise ValueError("SEC document redirect changed origin")
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ""
            last_error = exc
        except (http.client.IncompleteRead, OSError, TimeoutError, ValueError) as exc:
            last_error = exc
        time.sleep(0.75 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return ""


def _plain_document(html: str) -> str:
    content = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    content = re.sub(r"<style\b[^>]*>.*?</style>", " ", content, flags=re.I | re.S)
    content = unescape(re.sub(r"<[^>]+>", " ", content))
    return re.sub(r"\s+", " ", content).strip()


def _document_features(text: str) -> dict[str, Any]:
    lower = text.lower()
    core = lower.split("forward-looking statements", 1)[0][:20_000]
    positive_phrases = {
        "record revenue": 2.0,
        "record results": 2.0,
        "strongest quarter": 2.0,
        "strong growth": 1.5,
        "raises guidance": 2.5,
        "raised guidance": 2.5,
        "increased guidance": 2.0,
        "above expectations": 2.0,
        "exceeded expectations": 2.0,
        "reaffirmed guidance": 0.7,
        "accelerating growth": 1.5,
    }
    negative_phrases = {
        "lowers guidance": 2.5,
        "lowered guidance": 2.5,
        "reduced guidance": 2.0,
        "below expectations": 2.0,
        "missed expectations": 2.0,
        "weak demand": 1.5,
        "net loss": 1.0,
        "impairment": 1.0,
        "going concern": 2.5,
        "material weakness": 1.5,
    }
    positive = sum(core.count(phrase) * weight for phrase, weight in positive_phrases.items())
    negative = sum(core.count(phrase) * weight for phrase, weight in negative_phrases.items())
    guidance_positive = sum(
        core.count(phrase)
        for phrase in ("raises guidance", "raised guidance", "increased guidance")
    )
    guidance_negative = sum(
        core.count(phrase)
        for phrase in ("lowers guidance", "lowered guidance", "reduced guidance")
    )
    growth_positive = sum(
        float(match)
        for match in re.findall(
            r"(?:revenue|sales|earnings)[^.]{0,80}(?:up|grew|increased)\s+(\d+(?:\.\d+)?)\s*percent",
            core,
        )
    )
    growth_negative = sum(
        float(match)
        for match in re.findall(
            r"(?:revenue|sales|earnings)[^.]{0,80}(?:down|fell|decreased)\s+(\d+(?:\.\d+)?)\s*percent",
            core,
        )
    )
    headline = re.sub(r"\s+", " ", text[:500]).strip()
    return {
        "text_score": math.tanh((positive - negative) / 4),
        "guidance_score": float(np.clip(guidance_positive - guidance_negative, -2, 2) / 2),
        "growth_score": math.tanh((growth_positive - growth_negative) / 50),
        "positive_weight": positive,
        "negative_weight": negative,
        "headline": headline[:500],
    }


def _sec_earnings_text_features(event: tuple[Any, ...]) -> tuple[str, str, dict[str, Any]]:
    symbol, source_id, metadata_json = event
    metadata = json.loads(str(metadata_json) or "{}")
    cik = int(metadata.get("cik") or 0)
    primary_document = str(metadata.get("primary_document") or "")
    if cik <= 0 or not primary_document or not re.fullmatch(r"[A-Za-z0-9._-]+", primary_document):
        return str(symbol), str(source_id), {}
    accession = str(source_id).replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
    primary_html = _sec_text(urllib.parse.urljoin(base, primary_document))
    if not primary_html:
        return str(symbol), str(source_id), {}
    candidates: list[str] = []
    for href, anchor in re.findall(
        r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        primary_html,
        flags=re.I | re.S,
    ):
        anchor_text = _plain_document(anchor).lower()
        if "99.1" in anchor_text or "press release" in anchor_text:
            candidates.append(href)
    document_html = primary_html
    if candidates:
        target = urllib.parse.urljoin(base, candidates[0])
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme == "https" and parsed.hostname == "www.sec.gov":
            exhibit_html = _sec_text(target)
            if exhibit_html:
                document_html = exhibit_html
    return str(symbol), str(source_id), _document_features(_plain_document(document_html))


def download_sec_text_features(*, cache_path: Path, workers: int = 8) -> dict[str, Any]:
    connection = _connect(cache_path)
    rows = connection.execute(
        """SELECT symbol,source_id,metadata_json FROM external_events
           WHERE source='SEC_EDGAR' AND event_type='EARNINGS'
           ORDER BY event_time"""
    ).fetchall()
    pending = []
    for row in rows:
        metadata = json.loads(str(row[2]) or "{}")
        if "sec_text_features" not in metadata:
            pending.append(row)
    updates = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {executor.submit(_sec_earnings_text_features, row): row[0] for row in pending}
        for future in as_completed(futures):
            try:
                symbol, source_id, features = future.result()
                if not features:
                    continue
                existing = connection.execute(
                    """SELECT metadata_json FROM external_events
                       WHERE source='SEC_EDGAR' AND source_id=? AND symbol=?""",
                    (source_id, symbol),
                ).fetchone()
                metadata = json.loads(str(existing[0]) or "{}") if existing else {}
                metadata["sec_text_features"] = features
                connection.execute(
                    """UPDATE external_events SET metadata_json=?
                       WHERE source='SEC_EDGAR' AND source_id=? AND symbol=?""",
                    (
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        source_id,
                        symbol,
                    ),
                )
                updates += 1
                if updates % 25 == 0:
                    connection.commit()
            except Exception as exc:
                errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
    connection.commit()
    enriched = connection.execute(
        """SELECT COUNT(*) FROM external_events
           WHERE event_type='EARNINGS' AND metadata_json LIKE '%sec_text_features%'"""
    ).fetchone()
    connection.close()
    return {
        "earnings_events": len(rows),
        "pending_events": len(pending),
        "updated_events": updates,
        "enriched_events": int(enriched[0] or 0),
        "errors": errors[:50],
    }


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=max(6, window // 3)).mean()
    std = series.rolling(window, min_periods=max(6, window // 3)).std()
    return (series - mean) / std.replace(0, np.nan)


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0).rolling(window, min_periods=window).mean()
    loss = -change.clip(upper=0).rolling(window, min_periods=window).mean()
    strength = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + strength)


def _first_touch_labels(frame: pd.DataFrame, barrier_bps: pd.Series, horizon: int) -> np.ndarray:
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    barrier = barrier_bps.to_numpy(dtype=float)
    output = np.full(len(frame), np.nan)
    for index in range(len(frame) - horizon):
        if not math.isfinite(barrier[index]) or barrier[index] <= 0:
            continue
        upper = close[index] * (1 + barrier[index] / 10_000)
        lower = close[index] * (1 - barrier[index] / 10_000)
        label = 0.0
        for offset in range(1, horizon + 1):
            up = high[index + offset] >= upper
            down = low[index + offset] <= lower
            if up and down:
                label = np.nan  # OHLC bars cannot reveal which boundary came first.
                break
            if up:
                label = 1.0
                break
            if down:
                label = -1.0
                break
        output[index] = label
    return output


def feature_frame_for_symbol(
    bars: pd.DataFrame,
    *,
    horizon_bars: int = 24,
    sample_minutes: int = 15,
) -> pd.DataFrame:
    frame = bars.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    close = frame["close"].astype(float)
    open_price = frame["open"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    log_return = np.log(close / close.shift(1))
    for period in (1, 3, 6, 12, 24):
        frame[f"ret_{period}"] = close.pct_change(period) * 10_000
    ema_6 = close.ewm(span=6, adjust=False).mean()
    ema_24 = close.ewm(span=24, adjust=False).mean()
    ema_72 = close.ewm(span=72, adjust=False).mean()
    frame["trend_6_24"] = (ema_6 / ema_24 - 1) * 10_000
    frame["trend_24_72"] = (ema_24 / ema_72 - 1) * 10_000
    for period in (12, 24, 96):
        frame[f"realized_vol_{period}"] = (
            log_return.rolling(period, min_periods=max(6, period // 3)).std()
            * math.sqrt(horizon_bars)
            * 10_000
        )
    previous = close.shift(1)
    true_range = pd.concat(
        ((high - low), (high - previous).abs(), (low - previous).abs()), axis=1
    ).max(axis=1)
    frame["atr_bps_14"] = true_range.rolling(14, min_periods=8).mean() / close * 10_000
    frame["rsi_14"] = _rsi(close)
    frame["volume_z_24"] = _rolling_zscore(np.log1p(volume), 24)
    frame["volume_z_96"] = _rolling_zscore(np.log1p(volume), 96)
    frame["trade_z_24"] = _rolling_zscore(np.log1p(frame["trades"].astype(float)), 24)
    frame["taker_ratio"] = frame["taker_buy_volume"] / volume.replace(0, np.nan)
    frame["taker_ratio_6"] = frame["taker_ratio"].rolling(6, min_periods=3).mean()
    frame["taker_impulse"] = frame["taker_ratio_6"] - frame["taker_ratio"].rolling(
        24, min_periods=8
    ).mean()
    frame["body_bps"] = (close / open_price - 1) * 10_000
    frame["range_bps"] = (high / low.replace(0, np.nan) - 1) * 10_000
    frame["upper_wick_bps"] = (high - np.maximum(open_price, close)) / close * 10_000
    frame["lower_wick_bps"] = (np.minimum(open_price, close) - low) / close * 10_000

    timestamps = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    eastern = timestamps.dt.tz_convert("America/New_York")
    minutes = eastern.dt.hour * 60 + eastern.dt.minute
    hour_fraction = timestamps.dt.hour + timestamps.dt.minute / 60
    frame["hour_sin"] = np.sin(2 * math.pi * hour_fraction / 24)
    frame["hour_cos"] = np.cos(2 * math.pi * hour_fraction / 24)
    frame["dow_sin"] = np.sin(2 * math.pi * timestamps.dt.dayofweek / 7)
    frame["dow_cos"] = np.cos(2 * math.pi * timestamps.dt.dayofweek / 7)
    frame["is_us_regular"] = ((minutes >= 570) & (minutes < 960)).astype(int)
    frame["is_us_premarket"] = ((minutes >= 240) & (minutes < 570)).astype(int)
    frame["is_us_afterhours"] = ((minutes >= 960) & (minutes < 1200)).astype(int)
    frame["is_weekend"] = (timestamps.dt.dayofweek >= 5).astype(int)

    historical_two_hour = close.pct_change(horizon_bars).abs() * 10_000
    frame["barrier_bps"] = (
        historical_two_hour.rolling(576, min_periods=96).median().mul(0.75).clip(20, 500)
    )
    frame["event_label"] = _first_touch_labels(frame, frame["barrier_bps"], horizon_bars)
    future_close = close.shift(-horizon_bars)
    future_time = frame["open_time"].shift(-horizon_bars)
    expected_ms = horizon_bars * 5 * 60 * 1000
    contiguous = future_time - frame["open_time"] == expected_ms
    frame["terminal_label"] = np.where(future_close >= close, 1.0, -1.0)
    frame.loc[~contiguous, ["event_label", "terminal_label"]] = np.nan
    frame["terminal_return_bps"] = (future_close / close - 1) * 10_000
    frame.loc[~contiguous, "terminal_return_bps"] = np.nan
    sample_ms = max(5, sample_minutes) * 60 * 1000
    return frame.loc[frame["open_time"] % sample_ms == 0].copy()


def _join_external_events(
    dataset: pd.DataFrame, events: pd.DataFrame, *, sample_minutes: int
) -> pd.DataFrame:
    output = dataset.copy()
    defaults = {
        "external_event": 0.0,
        "external_earnings": 0.0,
        "external_periodic": 0.0,
        "external_sentiment": 0.0,
        "external_quality": 0.0,
        "external_text_score": 0.0,
        "external_guidance_score": 0.0,
        "external_growth_score": 0.0,
    }
    if events.empty:
        for column, value in defaults.items():
            output[column] = value
        return output
    sample_ms = max(5, sample_minutes) * 60 * 1000
    events = events.copy()
    events["open_time"] = ((events["event_time"] + sample_ms - 1) // sample_ms) * sample_ms
    events["external_event"] = 1.0
    events["external_earnings"] = (events["event_type"] == "EARNINGS").astype(float)
    events["external_periodic"] = (events["event_type"] == "PERIODIC").astype(float)
    text_features = events["metadata_json"].map(
        lambda value: json.loads(str(value) or "{}").get("sec_text_features", {})
    )
    events["external_text_score"] = text_features.map(
        lambda value: float(value.get("text_score") or 0)
    )
    events["external_guidance_score"] = text_features.map(
        lambda value: float(value.get("guidance_score") or 0)
    )
    events["external_growth_score"] = text_features.map(
        lambda value: float(value.get("growth_score") or 0)
    )
    aggregate = events.groupby(["symbol", "open_time"], as_index=False).agg(
        external_event=("external_event", "max"),
        external_earnings=("external_earnings", "max"),
        external_periodic=("external_periodic", "max"),
        external_sentiment=("sentiment_score", "mean"),
        external_quality=("quality_score", "max"),
        external_text_score=("external_text_score", "mean"),
        external_guidance_score=("external_guidance_score", "mean"),
        external_growth_score=("external_growth_score", "mean"),
    )
    output = output.merge(aggregate, on=["symbol", "open_time"], how="left")
    for column, value in defaults.items():
        output[column] = output[column].fillna(value)
    return output


def _underlying_features(bars: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "symbol",
        "open_time",
        "underlying_available",
        "underlying_ret_1",
        "underlying_ret_3",
        "underlying_ret_6",
        "underlying_ret_12",
        "underlying_ret_24",
        "underlying_trend_6_24",
        "underlying_volume_z_24",
    ]
    if bars.empty:
        return pd.DataFrame(columns=columns)
    frame = bars.copy()
    frame["open_time"] = frame["open_time"].astype(np.int64) // 300_000 * 300_000
    frame = (
        frame.sort_values(["symbol", "open_time"])
        .groupby(["symbol", "open_time"], as_index=False)
        .agg(close=("close", "last"), volume=("volume", "sum"))
    )
    outputs: list[pd.DataFrame] = []
    for _, group in frame.groupby("symbol", sort=False):
        group = group.copy()
        close = group["close"].astype(float)
        for period in (1, 3, 6, 12, 24):
            group[f"underlying_ret_{period}"] = close.pct_change(period) * 10_000
        ema_6 = close.ewm(span=6, adjust=False).mean()
        ema_24 = close.ewm(span=24, adjust=False).mean()
        group["underlying_trend_6_24"] = (ema_6 / ema_24 - 1) * 10_000
        group["underlying_volume_z_24"] = _rolling_zscore(
            np.log1p(group["volume"].astype(float)), 24
        )
        group["underlying_available"] = 1.0
        outputs.append(group[columns])
    return pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame(columns=columns)


def _metrics_features(metrics: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "symbol",
        "open_time",
        "metrics_available",
        "oi_change_1",
        "oi_change_3",
        "oi_change_12",
        "oi_value_z_24",
        "top_account_bias",
        "top_position_bias",
        "global_account_bias",
        "metrics_taker_bias",
        "top_account_impulse_3",
        "top_position_impulse_3",
        "global_account_impulse_3",
        "metrics_taker_impulse_3",
        "top_global_disagreement",
    ]
    if metrics.empty:
        return pd.DataFrame(columns=columns)
    outputs: list[pd.DataFrame] = []
    ratio_columns = {
        "count_toptrader_long_short_ratio": ("top_account_bias", "top_account_impulse_3"),
        "sum_toptrader_long_short_ratio": ("top_position_bias", "top_position_impulse_3"),
        "count_long_short_ratio": ("global_account_bias", "global_account_impulse_3"),
        "sum_taker_long_short_vol_ratio": ("metrics_taker_bias", "metrics_taker_impulse_3"),
    }
    for _, group in metrics.sort_values(["symbol", "open_time"]).groupby(
        "symbol", sort=False
    ):
        group = group.copy()
        open_interest = group["sum_open_interest"].astype(float)
        for period in (1, 3, 12):
            group[f"oi_change_{period}"] = open_interest.pct_change(period) * 10_000
        group["oi_value_z_24"] = _rolling_zscore(
            np.log1p(group["sum_open_interest_value"].astype(float)), 24
        )
        for source, (target, impulse) in ratio_columns.items():
            group[target] = np.log(group[source].astype(float).clip(lower=1e-6))
            group[impulse] = group[target].diff(3)
        group["top_global_disagreement"] = (
            group["top_account_bias"] - group["global_account_bias"]
        )
        group["metrics_available"] = 1.0
        outputs.append(group[columns])
    return pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame(columns=columns)


def build_dataset(
    cache_path: Path, *, sample_minutes: int = 15, require_labels: bool = True
) -> pd.DataFrame:
    connection = _connect(cache_path)
    bars = pd.read_sql_query(
        """SELECT symbol,market_type,open_time,open,high,low,close,volume,
                  quote_volume,trades,taker_buy_volume,taker_buy_quote_volume
           FROM bars ORDER BY symbol,open_time""",
        connection,
    )
    events = pd.read_sql_query(
        """SELECT symbol,event_time,event_type,sentiment_score,quality_score,metadata_json
           FROM external_events ORDER BY symbol,event_time""",
        connection,
    )
    underlying = pd.read_sql_query(
        """SELECT symbol,open_time,close,volume
           FROM underlying_bars ORDER BY symbol,open_time""",
        connection,
    )
    metrics = pd.read_sql_query(
        """SELECT symbol,open_time,sum_open_interest,sum_open_interest_value,
                  count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
                  count_long_short_ratio,sum_taker_long_short_vol_ratio
           FROM futures_metrics ORDER BY symbol,open_time""",
        connection,
    )
    connection.close()
    if bars.empty:
        raise ValueError("research cache has no bars")
    frames = [
        feature_frame_for_symbol(group, sample_minutes=sample_minutes)
        for _, group in bars.groupby("symbol", sort=True)
    ]
    dataset = pd.concat(frames, ignore_index=True)
    dataset = _join_external_events(dataset, events, sample_minutes=sample_minutes)
    underlying_frame = _underlying_features(underlying)
    dataset = dataset.merge(underlying_frame, on=["symbol", "open_time"], how="left")
    underlying_columns = [
        column for column in underlying_frame.columns if column not in {"symbol", "open_time"}
    ]
    for column in underlying_columns:
        dataset[column] = dataset[column].fillna(0.0)
    dataset["contract_underlying_gap_1"] = (
        dataset["ret_1"] - dataset["underlying_ret_1"]
    )
    dataset["contract_underlying_gap_24"] = (
        dataset["ret_24"] - dataset["underlying_ret_24"]
    )
    metrics_frame = _metrics_features(metrics)
    dataset = dataset.merge(metrics_frame, on=["symbol", "open_time"], how="left")
    metrics_columns = [
        column for column in metrics_frame.columns if column not in {"symbol", "open_time"}
    ]
    for column in metrics_columns:
        dataset[column] = dataset[column].fillna(0.0)
    market_trigger = pd.concat(
        [
            dataset["ret_3"].abs() / dataset["barrier_bps"].replace(0, np.nan),
            dataset["ret_6"].abs() / dataset["barrier_bps"].replace(0, np.nan),
            dataset["volume_z_24"].clip(lower=0) / 3,
            dataset["taker_impulse"].abs() * 6,
        ],
        axis=1,
    ).max(axis=1)
    dataset["event_trigger_strength"] = market_trigger.fillna(0) + dataset[
        "external_event"
    ] * 2
    dataset["event_candidate"] = (
        (dataset["event_trigger_strength"] >= 1.0) | (dataset["external_event"] > 0)
    )
    for period in (1, 24):
        source = f"ret_{period}"
        dataset[f"global_ret_{period}"] = dataset.groupby("open_time")[source].transform(
            "median"
        )
        dataset[f"group_ret_{period}"] = dataset.groupby(
            ["market_type", "open_time"]
        )[source].transform("median")
    dataset["relative_ret_24"] = dataset["ret_24"] - dataset["group_ret_24"]
    for market_type in ("EQUITY", "HK_EQUITY", "KR_EQUITY", "COMMODITY", "PREMARKET"):
        dataset[f"market_{market_type}"] = (dataset["market_type"] == market_type).astype(int)
    if require_labels:
        dataset = dataset.dropna(subset=["terminal_label"])
    return dataset.reset_index(drop=True)


def load_or_build_dataset(cache_path: Path, *, sample_minutes: int = 15) -> pd.DataFrame:
    dataset_path = cache_path.parent / f"two_hour_dataset_{sample_minutes}m.joblib"
    manifest_path = dataset_path.with_suffix(".manifest.json")
    database_stat = cache_path.stat()
    signature = {
        "schema_version": DATASET_CACHE_SCHEMA,
        "database_size": database_stat.st_size,
        "database_mtime_ns": database_stat.st_mtime_ns,
        "sample_minutes": int(sample_minutes),
        "features": list(FEATURE_COLUMNS),
    }
    if dataset_path.exists():
        manifest: dict[str, Any] | None = None
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = None
        legacy_cache_is_current = (
            manifest is None
            and dataset_path.stat().st_mtime_ns >= database_stat.st_mtime_ns
        )
        if manifest == signature or legacy_cache_is_current:
            dataset = joblib.load(dataset_path)
            required = set(FEATURE_COLUMNS) | {
                "symbol",
                "open_time",
                "event_label",
                "terminal_label",
                "event_candidate",
            }
            if required.issubset(dataset.columns):
                if legacy_cache_is_current:
                    manifest_path.write_text(
                        json.dumps(signature, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                return dataset
    dataset = build_dataset(cache_path, sample_minutes=sample_minutes)
    temporary_path = dataset_path.with_suffix(".tmp.joblib")
    joblib.dump(dataset, temporary_path, compress=0)
    temporary_path.replace(dataset_path)
    manifest_path.write_text(
        json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dataset


def chronological_boundaries(
    timestamps: Iterable[int], *, fractions: tuple[float, float, float] = (0.55, 0.15, 0.15)
) -> SplitBoundaries:
    unique = np.unique(np.asarray(list(timestamps), dtype=np.int64))
    if len(unique) < 16:
        raise ValueError("not enough unique timestamps for chronological validation")
    train_fraction, calibration_fraction, tuning_fraction = fractions
    train_index = max(1, int(len(unique) * train_fraction))
    calibration_index = max(train_index + 1, int(len(unique) * (train_fraction + calibration_fraction)))
    tuning_index = max(
        calibration_index + 1,
        int(len(unique) * (train_fraction + calibration_fraction + tuning_fraction)),
    )
    tuning_index = min(tuning_index, len(unique) - 1)
    return SplitBoundaries(
        int(unique[train_index - 1]),
        int(unique[calibration_index - 1]),
        int(unique[tuning_index - 1]),
        int(unique[-1]),
    )


def split_masks(frame: pd.DataFrame, boundaries: SplitBoundaries) -> dict[str, np.ndarray]:
    timestamp = frame["open_time"].to_numpy(dtype=np.int64)
    embargo_ms = 2 * 60 * 60 * 1000
    return {
        "train": timestamp <= boundaries.train_end_ms - embargo_ms,
        "calibration": (timestamp > boundaries.train_end_ms)
        & (timestamp <= boundaries.calibration_end_ms - embargo_ms),
        "tuning": (timestamp > boundaries.calibration_end_ms)
        & (timestamp <= boundaries.tuning_end_ms - embargo_ms),
        "test": timestamp > boundaries.tuning_end_ms,
    }


def _fit_classifier(x: np.ndarray, y: np.ndarray, seed: int) -> HistGradientBoostingClassifier:
    model = HistGradientBoostingClassifier(
        learning_rate=0.07,
        max_iter=180,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.0,
        class_weight="balanced",
        early_stopping=True,
        random_state=seed,
    )
    model.fit(x, y)
    return model


def _bounded_indices(mask: np.ndarray, maximum_rows: int) -> np.ndarray:
    indices = np.flatnonzero(mask)
    if len(indices) <= maximum_rows:
        return indices
    positions = np.linspace(0, len(indices) - 1, maximum_rows, dtype=np.int64)
    return indices[positions]


def _calibrate(
    model: HistGradientBoostingClassifier, x: np.ndarray, y: np.ndarray
) -> CalibratedClassifierCV:
    method = "isotonic" if len(y) >= 5_000 else "sigmoid"
    calibrated = CalibratedClassifierCV(FrozenEstimator(model), method=method)
    calibrated.fit(x, y)
    return calibrated


def _fit_lightgbm_classifier(
    train_x: np.ndarray,
    train_y: np.ndarray,
    calibration_x: np.ndarray,
    calibration_y: np.ndarray,
    *,
    seed: int,
    class_count: int,
) -> CalibratedClassifierCV:
    model = LGBMClassifier(
        objective="multiclass" if class_count == 3 else "binary",
        n_estimators=700,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.85,
        colsample_bytree=0.90,
        reg_lambda=4.0,
        reg_alpha=0.2,
        class_weight="balanced",
        n_jobs=-1,
        verbosity=-1,
        random_state=seed,
    )
    model.fit(
        train_x,
        train_y,
        eval_X=calibration_x,
        eval_y=calibration_y,
        callbacks=[early_stopping(60, verbose=False), log_evaluation(0)],
    )
    calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="isotonic")
    calibrated.fit(calibration_x, calibration_y)
    return calibrated


def _class_probability(
    model: CalibratedClassifierCV, x: np.ndarray, label: int
) -> np.ndarray:
    classes = np.asarray(model.classes_)
    index = int(np.flatnonzero(classes == label)[0])
    return model.predict_proba(x)[:, index]


def _multihead_probabilities(
    *,
    event_model: CalibratedClassifierCV,
    direction_model: CalibratedClassifierCV,
    occurrence_model: CalibratedClassifierCV,
    terminal_model: CalibratedClassifierCV,
    x: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    event_probability = event_model.predict_proba(x)
    event_classes = np.asarray(event_model.classes_)
    up_index = int(np.flatnonzero(event_classes == 1)[0])
    down_index = int(np.flatnonzero(event_classes == -1)[0])
    no_event_index = int(np.flatnonzero(event_classes == 0)[0])
    event_direction_up = event_probability[:, up_index] / np.maximum(
        event_probability[:, up_index] + event_probability[:, down_index], 1e-9
    )
    event_occurrence = 1 - event_probability[:, no_event_index]
    direction_up = _class_probability(direction_model, x, 1)
    occurrence = _class_probability(occurrence_model, x, 1)
    terminal_up = _class_probability(terminal_model, x, 1)
    return event_direction_up, event_occurrence, direction_up, occurrence, terminal_up


def choose_multihead_selection(
    *,
    event_direction_up: np.ndarray,
    event_occurrence: np.ndarray,
    direction_up: np.ndarray,
    occurrence: np.ndarray,
    terminal_up: np.ndarray,
    actual: np.ndarray,
    minimum_samples: int = 100,
    target_precision: float = 0.755,
) -> MultiHeadSelection:
    choices: list[MultiHeadSelection] = []
    fallbacks: list[MultiHeadSelection] = []
    for direction_weight in (0.50, 0.70, 0.85, 1.0):
        for terminal_weight in (0.0, 0.15, 0.30):
            event_direction_weight = 1 - direction_weight - terminal_weight
            if event_direction_weight < -1e-9:
                continue
            event_direction_weight = max(0.0, event_direction_weight)
            probability_up = (
                direction_weight * direction_up
                + terminal_weight * terminal_up
                + event_direction_weight * event_direction_up
            )
            direction = np.where(probability_up >= 0.5, 1, -1)
            confidence = np.maximum(probability_up, 1 - probability_up)
            for occurrence_weight in (0.50, 0.75, 1.0):
                combined_occurrence = (
                    occurrence_weight * occurrence
                    + (1 - occurrence_weight) * event_occurrence
                )
                for occurrence_threshold in np.arange(0.50, 0.951, 0.025):
                    eligible = np.flatnonzero(combined_occurrence >= occurrence_threshold)
                    if len(eligible) < minimum_samples:
                        continue
                    order = eligible[np.argsort(-confidence[eligible], kind="stable")]
                    correct = (direction[order] == actual[order]).astype(np.int64)
                    counts = np.arange(1, len(order) + 1)
                    precision = np.cumsum(correct) / counts
                    valid = np.flatnonzero(
                        (counts >= minimum_samples) & (precision >= target_precision)
                    )
                    best_index = int(valid[-1]) if len(valid) else int(np.argmax(precision))
                    threshold = float(confidence[order[best_index]])
                    selected = (combined_occurrence >= occurrence_threshold) & (
                        confidence >= threshold
                    )
                    samples = int(selected.sum())
                    if samples < minimum_samples:
                        continue
                    observed = float(np.mean(direction[selected] == actual[selected]))
                    choice = MultiHeadSelection(
                        samples=samples,
                        precision=observed,
                        coverage=float(samples / len(actual)),
                        direction_weight=direction_weight,
                        terminal_weight=terminal_weight,
                        event_direction_weight=event_direction_weight,
                        occurrence_weight=occurrence_weight,
                        occurrence_threshold=float(occurrence_threshold),
                        confidence_threshold=threshold,
                        target_precision=target_precision,
                    )
                    fallbacks.append(choice)
                    if observed >= target_precision:
                        choices.append(choice)
    if choices:
        return max(choices, key=lambda item: (item.coverage, item.precision))
    if fallbacks:
        return max(fallbacks, key=lambda item: (item.precision, item.samples))
    return MultiHeadSelection(
        samples=0,
        precision=0.0,
        coverage=0.0,
        direction_weight=1.0,
        terminal_weight=0.0,
        event_direction_weight=0.0,
        occurrence_weight=1.0,
        occurrence_threshold=0.95,
        confidence_threshold=0.95,
        target_precision=target_precision,
    )


def _multihead_event_metrics(
    *,
    event_direction_up: np.ndarray,
    event_occurrence: np.ndarray,
    direction_up: np.ndarray,
    occurrence: np.ndarray,
    terminal_up: np.ndarray,
    actual: np.ndarray,
    selection: MultiHeadSelection,
) -> dict[str, Any]:
    probability_up = (
        selection.direction_weight * direction_up
        + selection.terminal_weight * terminal_up
        + selection.event_direction_weight * event_direction_up
    )
    direction = np.where(probability_up >= 0.5, 1, -1)
    confidence = np.maximum(probability_up, 1 - probability_up)
    combined_occurrence = (
        selection.occurrence_weight * occurrence
        + (1 - selection.occurrence_weight) * event_occurrence
    )
    selected = (combined_occurrence >= selection.occurrence_threshold) & (
        confidence >= selection.confidence_threshold
    )
    samples = int(selected.sum())
    actual_events = actual != 0
    return {
        "samples": samples,
        "precision": float(np.mean(direction[selected] == actual[selected])) if samples else 0.0,
        "coverage": float(samples / len(actual)) if len(actual) else 0.0,
        "actual_event_rate": float(actual_events.mean()) if len(actual) else 0.0,
        "directional_recall": (
            float(np.sum(selected & (direction == actual)) / np.sum(actual_events))
            if np.sum(actual_events)
            else 0.0
        ),
        "predicted_up": int(np.sum(selected & (direction == 1))),
        "predicted_down": int(np.sum(selected & (direction == -1))),
    }


def choose_event_threshold(
    probabilities: np.ndarray,
    classes: np.ndarray,
    actual: np.ndarray,
    *,
    minimum_samples: int = 100,
    target_precision: float = 0.80,
) -> ThresholdChoice:
    up_index = int(np.flatnonzero(classes == 1)[0])
    down_index = int(np.flatnonzero(classes == -1)[0])
    no_event_index = int(np.flatnonzero(classes == 0)[0])
    direction = np.where(probabilities[:, up_index] >= probabilities[:, down_index], 1, -1)
    direction_probability = np.maximum(
        probabilities[:, up_index], probabilities[:, down_index]
    )
    no_event_probability = probabilities[:, no_event_index]
    choices: list[ThresholdChoice] = []
    for threshold in np.arange(0.50, 0.951, 0.01):
        selected = (direction_probability >= threshold) & (
            direction_probability > no_event_probability
        )
        samples = int(selected.sum())
        if samples < minimum_samples:
            continue
        precision = float(np.mean(direction[selected] == actual[selected]))
        choices.append(
            ThresholdChoice(
                float(round(threshold, 2)),
                samples,
                precision,
                samples / len(actual),
                precision >= target_precision,
            )
        )
    passing = [choice for choice in choices if choice.target_met]
    if passing:
        return max(passing, key=lambda item: (item.coverage, item.precision))
    if choices:
        return max(choices, key=lambda item: (item.precision, item.samples))
    return ThresholdChoice(0.95, 0, 0.0, 0.0, False)


def _event_metrics(
    model: CalibratedClassifierCV,
    x: np.ndarray,
    actual: np.ndarray,
    threshold: float,
) -> dict[str, Any]:
    probabilities = model.predict_proba(x)
    classes = np.asarray(model.classes_)
    up_index = int(np.flatnonzero(classes == 1)[0])
    down_index = int(np.flatnonzero(classes == -1)[0])
    no_event_index = int(np.flatnonzero(classes == 0)[0])
    direction = np.where(probabilities[:, up_index] >= probabilities[:, down_index], 1, -1)
    direction_probability = np.maximum(
        probabilities[:, up_index], probabilities[:, down_index]
    )
    selected = (direction_probability >= threshold) & (
        direction_probability > probabilities[:, no_event_index]
    )
    samples = int(selected.sum())
    actual_events = actual != 0
    return {
        "samples": samples,
        "precision": float(np.mean(direction[selected] == actual[selected])) if samples else 0.0,
        "coverage": float(samples / len(actual)) if len(actual) else 0.0,
        "actual_event_rate": float(actual_events.mean()) if len(actual) else 0.0,
        "directional_recall": (
            float(np.sum(selected & (direction == actual)) / np.sum(actual_events))
            if np.sum(actual_events)
            else 0.0
        ),
        "predicted_up": int(np.sum(selected & (direction == 1))),
        "predicted_down": int(np.sum(selected & (direction == -1))),
    }


def _endpoint_metrics(actual: np.ndarray, probability_up: np.ndarray) -> dict[str, Any]:
    predicted = np.where(probability_up >= 0.5, 1, -1)
    binary_actual = (actual == 1).astype(int)
    return {
        "samples": int(len(actual)),
        "accuracy": float(accuracy_score(actual, predicted)),
        "brier": float(brier_score_loss(binary_actual, probability_up)),
        "predicted_up": int(np.sum(predicted == 1)),
        "predicted_down": int(np.sum(predicted == -1)),
        "confusion_matrix": confusion_matrix(actual, predicted, labels=[-1, 1]).tolist(),
    }


def _evaluation_rows(frame: pd.DataFrame, mask: np.ndarray) -> pd.DataFrame:
    two_hours_ms = 2 * 60 * 60 * 1000
    return frame.loc[mask & (frame["open_time"].to_numpy() % two_hours_ms == 0)].copy()


def _event_evaluation_rows(frame: pd.DataFrame, mask: np.ndarray) -> pd.DataFrame:
    candidates = frame.loc[mask & frame["event_candidate"].to_numpy(dtype=bool)].copy()
    candidates = candidates.sort_values(["symbol", "open_time"])
    keep: list[int] = []
    cooldown_ms = 2 * 60 * 60 * 1000
    for _, group in candidates.groupby("symbol", sort=False):
        last_time = -cooldown_ms
        for index, timestamp in zip(group.index, group["open_time"], strict=True):
            if int(timestamp) - last_time >= cooldown_ms:
                keep.append(index)
                last_time = int(timestamp)
    return candidates.loc[keep].copy()


def train_and_backtest(
    *,
    cache_path: Path,
    report_path: Path,
    model_dir: Path,
    seed: int = 20260805,
    sample_minutes: int = 15,
    maximum_fit_rows: int = 800_000,
) -> dict[str, Any]:
    dataset = load_or_build_dataset(cache_path, sample_minutes=sample_minutes)
    boundaries = chronological_boundaries(dataset["open_time"])
    masks = split_masks(dataset, boundaries)
    feature_names = list(FEATURE_COLUMNS)
    x = dataset[feature_names].to_numpy(dtype=np.float32)
    terminal_y = dataset["terminal_label"].to_numpy(dtype=np.int8)
    event_y = dataset["event_label"].to_numpy(dtype=float)

    terminal_train_indices = _bounded_indices(masks["train"], maximum_fit_rows)
    terminal_calibration_indices = _bounded_indices(masks["calibration"], 300_000)
    terminal_model = _fit_lightgbm_classifier(
        x[terminal_train_indices],
        terminal_y[terminal_train_indices],
        x[terminal_calibration_indices],
        terminal_y[terminal_calibration_indices],
        seed=seed,
        class_count=2,
    )
    event_candidates = dataset["event_candidate"].to_numpy(dtype=bool)
    event_train = masks["train"] & event_candidates & np.isfinite(event_y)
    event_calibration = masks["calibration"] & event_candidates & np.isfinite(event_y)
    event_train_indices = _bounded_indices(event_train, maximum_fit_rows)
    event_calibration_indices = _bounded_indices(event_calibration, 300_000)
    event_labels = np.nan_to_num(event_y, nan=0.0).astype(np.int8)
    event_model = _fit_lightgbm_classifier(
        x[event_train_indices],
        event_labels[event_train_indices],
        x[event_calibration_indices],
        event_labels[event_calibration_indices],
        seed=seed + 1,
        class_count=3,
    )
    direction_train = event_train & (event_y != 0)
    direction_calibration = event_calibration & (event_y != 0)
    direction_train_indices = _bounded_indices(direction_train, maximum_fit_rows)
    direction_calibration_indices = _bounded_indices(direction_calibration, 300_000)
    direction_model = _fit_lightgbm_classifier(
        x[direction_train_indices],
        event_labels[direction_train_indices],
        x[direction_calibration_indices],
        event_labels[direction_calibration_indices],
        seed=seed + 2,
        class_count=2,
    )
    occurrence_labels = (event_y != 0).astype(np.int8)
    occurrence_model = _fit_lightgbm_classifier(
        x[event_train_indices],
        occurrence_labels[event_train_indices],
        x[event_calibration_indices],
        occurrence_labels[event_calibration_indices],
        seed=seed + 3,
        class_count=2,
    )

    tuning = _event_evaluation_rows(dataset, masks["tuning"])
    tuning_x = tuning[feature_names].to_numpy(dtype=np.float32)
    tuning_event_mask = np.isfinite(tuning["event_label"].to_numpy(dtype=float))
    tuning_probabilities = _multihead_probabilities(
        event_model=event_model,
        direction_model=direction_model,
        occurrence_model=occurrence_model,
        terminal_model=terminal_model,
        x=tuning_x[tuning_event_mask],
    )
    selection = choose_multihead_selection(
        event_direction_up=tuning_probabilities[0],
        event_occurrence=tuning_probabilities[1],
        direction_up=tuning_probabilities[2],
        occurrence=tuning_probabilities[3],
        terminal_up=tuning_probabilities[4],
        actual=tuning.loc[tuning_event_mask, "event_label"].to_numpy(dtype=np.int8),
        minimum_samples=max(100, int(tuning_event_mask.sum() * 0.005)),
        target_precision=0.755,
    )

    test = _evaluation_rows(dataset, masks["test"])
    test_x = test[feature_names].to_numpy(dtype=np.float32)
    terminal_probability = _class_probability(terminal_model, test_x, 1)
    terminal_metrics = _endpoint_metrics(
        test["terminal_label"].to_numpy(dtype=np.int8), terminal_probability
    )
    event_test = _event_evaluation_rows(dataset, masks["test"])
    event_test_x = event_test[feature_names].to_numpy(dtype=np.float32)
    event_test_mask = np.isfinite(event_test["event_label"].to_numpy(dtype=float))
    event_test_probabilities = _multihead_probabilities(
        event_model=event_model,
        direction_model=direction_model,
        occurrence_model=occurrence_model,
        terminal_model=terminal_model,
        x=event_test_x[event_test_mask],
    )
    event_metrics = _multihead_event_metrics(
        event_direction_up=event_test_probabilities[0],
        event_occurrence=event_test_probabilities[1],
        direction_up=event_test_probabilities[2],
        occurrence=event_test_probabilities[3],
        terminal_up=event_test_probabilities[4],
        actual=event_test.loc[event_test_mask, "event_label"].to_numpy(dtype=np.int8),
        selection=selection,
    )
    event_metrics["candidate_rows"] = int(len(event_test))
    event_metrics["candidate_symbols"] = int(event_test["symbol"].nunique())
    event_metrics["external_event_rows"] = int((event_test["external_event"] > 0).sum())

    terminal_predictions = np.where(terminal_probability >= 0.5, 1, -1)
    test = test.assign(terminal_prediction=terminal_predictions)
    group_metrics: dict[str, Any] = {}
    symbol_metrics: dict[str, dict[str, Any]] = {}
    for market_type, group in test.groupby("market_type"):
        group_metrics[str(market_type)] = {
            "samples": int(len(group)),
            "symbols": int(group["symbol"].nunique()),
            "terminal_accuracy": float(
                np.mean(group["terminal_prediction"] == group["terminal_label"])
            ),
        }
    for symbol, group in test.groupby("symbol"):
        symbol_metrics[str(symbol)] = {
            "samples": int(len(group)),
            "accuracy": float(
                np.mean(group["terminal_prediction"] == group["terminal_label"])
            ),
        }

    market_types = sorted(str(value) for value in dataset["market_type"].unique())
    generated_at = datetime.now(UTC)
    completed_days = int(
        pd.to_datetime(test["open_time"], unit="ms", utc=True).dt.date.nunique()
    )
    feature_coverage: dict[str, dict[str, int]] = {}
    for split_name, split_mask in masks.items():
        split_frame = dataset.loc[split_mask]
        feature_coverage[split_name] = {
            "rows": int(len(split_frame)),
            "external_events": int(split_frame["external_event"].sum()),
            "earnings_events": int(split_frame["external_earnings"].sum()),
            "earnings_surprise_events": int((split_frame["external_sentiment"] != 0).sum()),
            "underlying_rows": int((split_frame["underlying_available"] > 0).sum()),
            "futures_metrics_rows": int((split_frame["metrics_available"] > 0).sum()),
        }
    qualification = {
        "all_contracts_present": int(test["symbol"].nunique()) == 151,
        "minimum_event_samples_met": int(event_metrics["samples"]) >= 500,
        "minimum_observation_days_met": completed_days >= 20,
        "minimum_semantic_training_events_met": (
            feature_coverage["train"]["earnings_surprise_events"] >= 50
        ),
        "event_precision_target_met": float(event_metrics["precision"]) >= 0.75,
    }
    qualification["qualified"] = all(qualification.values())
    report: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": generated_at.isoformat(),
        "method": "lightgbm_multihead_train_calibrate_tune_locked_test",
        "sample_minutes": int(sample_minutes),
        "label": {
            "horizon_seconds": 7200,
            "terminal": "close at t+2h versus close at prediction time",
            "event": "first touch of a past-volatility-only dynamic barrier",
            "barrier_floor_bps": 20,
            "barrier_cap_bps": 500,
        },
        "features": feature_names,
        "data": {
            "rows": int(len(dataset)),
            "symbols": int(dataset["symbol"].nunique()),
            "external_events": int(dataset["external_event"].sum()),
            "underlying_rows": int((dataset["underlying_available"] > 0).sum()),
            "futures_metrics_rows": int((dataset["metrics_available"] > 0).sum()),
            "market_types": market_types,
            "first_time_ms": int(dataset["open_time"].min()),
            "last_time_ms": int(dataset["open_time"].max()),
        },
        "split_boundaries": asdict(boundaries),
        "split_rows": {name: int(mask.sum()) for name, mask in masks.items()},
        "fit_rows": {
            "terminal_train": int(len(terminal_train_indices)),
            "terminal_calibration": int(len(terminal_calibration_indices)),
            "event_train": int(len(event_train_indices)),
            "event_calibration": int(len(event_calibration_indices)),
            "direction_train": int(len(direction_train_indices)),
            "direction_calibration": int(len(direction_calibration_indices)),
        },
        "feature_coverage_by_split": feature_coverage,
        "official_test_rows": int(len(test)),
        "official_test_symbols": int(test["symbol"].nunique()),
        "official_test_days": completed_days,
        "event_threshold": asdict(selection),
        "terminal_test": terminal_metrics,
        "event_test": event_metrics,
        "macro_symbol_terminal_accuracy": (
            float(np.mean([item["accuracy"] for item in symbol_metrics.values()]))
            if symbol_metrics
            else 0.0
        ),
        "qualification": qualification,
        "group_test": group_metrics,
        "symbol_terminal_metrics": symbol_metrics,
        "limitations": [
            "market features include OHLCV, taker flow, open interest and long-short ratios",
            "matched underlying prices are joined at the same or earlier five-minute timestamp",
            "historical news, social and order-book snapshots are not fabricated",
            "multi-head weights and thresholds are selected on tuning data only",
            "correlated contracts are reported by symbol and market group as well as pooled",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    run_id = generated_at.strftime("%Y%m%dT%H%M%SZ")
    candidate_dir = model_dir / "candidates" / run_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "generated_at": report["generated_at"],
        "features": feature_names,
        "architecture": "lightgbm_multihead",
        "event_threshold": asdict(selection),
        "split_boundaries": asdict(boundaries),
        "qualification": qualification,
        "report": str(report_path),
    }
    joblib.dump(terminal_model, candidate_dir / "terminal_model.joblib")
    joblib.dump(event_model, candidate_dir / "event_model.joblib")
    joblib.dump(direction_model, candidate_dir / "direction_model.joblib")
    joblib.dump(occurrence_model, candidate_dir / "occurrence_model.joblib")
    (candidate_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if qualification["qualified"]:
        champion_dir = model_dir / "champion"
        champion_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(terminal_model, champion_dir / "terminal_model.joblib")
        joblib.dump(event_model, champion_dir / "event_model.joblib")
        joblib.dump(direction_model, champion_dir / "direction_model.joblib")
        joblib.dump(occurrence_model, champion_dir / "occurrence_model.joblib")
        (champion_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report["candidate_model_dir"] = str(candidate_dir)
    report["champion_promoted"] = bool(qualification["qualified"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
