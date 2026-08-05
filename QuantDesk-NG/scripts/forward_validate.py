"""Validate the current two-hour candidate on fresh Binance data."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import text

from quantdesk.two_hour_research import (
    BAR_COLUMNS,
    FEATURE_COLUMNS,
    MultiHeadSelection,
    _connect,
    _endpoint_metrics,
    _evaluation_rows,
    _event_evaluation_rows,
    _multihead_probabilities,
    build_dataset,
    download_earnings_surprises,
    download_sec_events,
    download_sec_text_features,
    download_underlying_history,
    load_symbol_metadata,
)
from quantdesk_v2.database import engine

FAPI_ORIGIN = "https://fapi.binance.com"


def _json_request(path: str, parameters: dict[str, Any], retries: int = 4) -> Any:
    query = urllib.parse.urlencode(parameters)
    url = f"{FAPI_ORIGIN}{path}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed Binance HTTPS origin
                url,
                headers={"User-Agent": "QuantDesk-NG forward validation"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                parsed = urllib.parse.urlsplit(response.geturl())
                if parsed.scheme != "https" or parsed.hostname != "fapi.binance.com":
                    raise ValueError("Binance forward-validation redirect changed origin")
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {418, 429}:
                time.sleep(2 * (attempt + 1))
                continue
            if exc.code in {400, 404}:
                return []
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return []


def _server_time_ms() -> int:
    payload = _json_request("/fapi/v1/time", {})
    return int(payload["serverTime"])


def _fetch_klines(
    symbol: str, market_type: str, start_ms: int, end_ms: int
) -> list[tuple[Any, ...]]:
    payload = _json_request(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1500,
        },
    )
    return [
        (
            symbol,
            market_type,
            int(row[0]),
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
        for row in payload
        if int(row[0]) <= end_ms
    ]


def _ratio_map(path: str, symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    payload = _json_request(
        path,
        {
            "symbol": symbol,
            "period": "5m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500,
        },
    )
    key = "buySellRatio" if path.endswith("takerlongshortRatio") else "longShortRatio"
    return {int(row["timestamp"]): float(row[key]) for row in payload}


def _fetch_metrics(symbol: str, start_ms: int, end_ms: int) -> list[tuple[Any, ...]]:
    interest = _json_request(
        "/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "5m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500,
        },
    )
    top_account = _ratio_map(
        "/futures/data/topLongShortAccountRatio", symbol, start_ms, end_ms
    )
    top_position = _ratio_map(
        "/futures/data/topLongShortPositionRatio", symbol, start_ms, end_ms
    )
    global_account = _ratio_map(
        "/futures/data/globalLongShortAccountRatio", symbol, start_ms, end_ms
    )
    taker = _ratio_map("/futures/data/takerlongshortRatio", symbol, start_ms, end_ms)
    return [
        (
            symbol,
            int(row["timestamp"]),
            float(row["sumOpenInterest"]),
            float(row["sumOpenInterestValue"]),
            top_account.get(int(row["timestamp"]), 1.0),
            top_position.get(int(row["timestamp"]), 1.0),
            global_account.get(int(row["timestamp"]), 1.0),
            taker.get(int(row["timestamp"]), 1.0),
        )
        for row in interest
    ]


def _copy_history(source_path: Path, target_path: Path, warmup_start_ms: int) -> None:
    source = sqlite3.connect(source_path)
    target = _connect(target_path)
    table_specs = {
        "bars": (
            BAR_COLUMNS,
            "open_time>=?",
        ),
        "futures_metrics": (
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
            "open_time>=?",
        ),
        "underlying_bars": (
            ("symbol", "ticker", "open_time", "open", "high", "low", "close", "volume"),
            "open_time>=?",
        ),
        "external_events": (
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
            "event_time>=?",
        ),
    }
    for table, (columns, where) in table_specs.items():
        names = ",".join(columns)
        rows = source.execute(
            f"SELECT {names} FROM {table} WHERE {where}",  # noqa: S608 - fixed table map
            (warmup_start_ms,),
        ).fetchall()
        placeholders = ",".join("?" for _ in columns)
        target.executemany(
            f"INSERT OR REPLACE INTO {table}({names}) VALUES({placeholders})",  # noqa: S608
            rows,
        )
    fetch_rows = source.execute(
        "SELECT source,period,fetched_at,records FROM external_fetch_log"
    ).fetchall()
    target.executemany(
        "INSERT OR REPLACE INTO external_fetch_log(source,period,fetched_at,records) "
        "VALUES(?,?,?,?)",
        fetch_rows,
    )
    target.commit()
    target.close()
    source.close()


def _insert_live_rows(
    target_path: Path,
    bar_rows: list[tuple[Any, ...]],
    metric_rows: list[tuple[Any, ...]],
) -> None:
    connection = _connect(target_path)
    connection.executemany(
        "INSERT OR REPLACE INTO bars(" + ",".join(BAR_COLUMNS) + ") VALUES(" + ",".join(
            "?" for _ in BAR_COLUMNS
        ) + ")",
        bar_rows,
    )
    connection.executemany(
        """INSERT OR REPLACE INTO futures_metrics(
               symbol,open_time,sum_open_interest,sum_open_interest_value,
               count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
               count_long_short_ratio,sum_taker_long_short_vol_ratio
           ) VALUES(?,?,?,?,?,?,?,?)""",
        metric_rows,
    )
    connection.commit()
    connection.close()


def _wilson(correct: int, samples: int) -> list[float]:
    if samples == 0:
        return [0.0, 0.0]
    z = 1.959963984540054
    probability = correct / samples
    denominator = 1 + z * z / samples
    centre = (probability + z * z / (2 * samples)) / denominator
    margin = z * math.sqrt(
        probability * (1 - probability) / samples + z * z / (4 * samples * samples)
    ) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _news_summary(start_ms: int, end_ms: int) -> dict[str, Any]:
    start_seconds = start_ms // 1000
    end_seconds = end_ms // 1000
    try:
        with engine.connect() as connection:
            total = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM news WHERE ts>=:start AND ts<=:end"),
                    {"start": start_seconds, "end": end_seconds},
                ).scalar_one()
            )
            rows = connection.execute(
                text(
                    "SELECT COALESCE(source,'unknown'),COUNT(*) FROM news "
                    "WHERE ts>=:start AND ts<=:end GROUP BY COALESCE(source,'unknown') "
                    "ORDER BY COUNT(*) DESC"
                ),
                {"start": start_seconds, "end": end_seconds},
            ).all()
        return {"total": total, "sources": {str(name): int(count) for name, count in rows}}
    except Exception as exc:
        return {"total": 0, "sources": {}, "error": type(exc).__name__}


def _evaluate_window(
    dataset: pd.DataFrame,
    start_ms: int,
    end_ms: int,
    models: dict[str, Any],
    selection: MultiHeadSelection,
) -> dict[str, Any]:
    mask = (dataset["open_time"].to_numpy(dtype=np.int64) >= start_ms) & (
        dataset["open_time"].to_numpy(dtype=np.int64) <= end_ms
    )
    events = _event_evaluation_rows(dataset, mask)
    event_x = events[list(FEATURE_COLUMNS)].to_numpy(dtype=np.float32)
    probabilities = _multihead_probabilities(
        event_model=models["event"],
        direction_model=models["direction"],
        occurrence_model=models["occurrence"],
        terminal_model=models["terminal"],
        x=event_x,
    )
    probability_up = (
        selection.direction_weight * probabilities[2]
        + selection.terminal_weight * probabilities[4]
        + selection.event_direction_weight * probabilities[0]
    )
    direction = np.where(probability_up >= 0.5, 1, -1)
    confidence = np.maximum(probability_up, 1 - probability_up)
    combined_occurrence = (
        selection.occurrence_weight * probabilities[3]
        + (1 - selection.occurrence_weight) * probabilities[1]
    )
    selected = (combined_occurrence >= selection.occurrence_threshold) & (
        confidence >= selection.confidence_threshold
    )
    actual = events["event_label"].to_numpy(dtype=np.int8)
    samples = int(selected.sum())
    correct = int(np.sum(selected & (direction == actual)))
    terminal_rows = _evaluation_rows(dataset, mask)
    terminal_x = terminal_rows[list(FEATURE_COLUMNS)].to_numpy(dtype=np.float32)
    terminal_probability = models["terminal"].predict_proba(terminal_x)
    terminal_classes = np.asarray(models["terminal"].classes_)
    terminal_up = terminal_probability[:, int(np.flatnonzero(terminal_classes == 1)[0])]
    terminal_metrics = _endpoint_metrics(
        terminal_rows["terminal_label"].to_numpy(dtype=np.int8), terminal_up
    )
    return {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "candidate_rows": int(len(events)),
        "signals": samples,
        "correct": correct,
        "precision": float(correct / samples) if samples else 0.0,
        "precision_wilson_95": _wilson(correct, samples),
        "predicted_up": int(np.sum(selected & (direction == 1))),
        "predicted_down": int(np.sum(selected & (direction == -1))),
        "external_event_candidates": int((events["external_event"] > 0).sum()),
        "selected_external_events": int(np.sum(selected & (events["external_event"] > 0))),
        "terminal": terminal_metrics,
    }


def parser() -> argparse.ArgumentParser:
    project = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--cache", type=Path, default=project / "data" / "two_hour_research.sqlite3")
    value.add_argument("--metadata", type=Path, default=project / "config" / "tradfi_symbols.json")
    value.add_argument("--model-dir", type=Path, required=False)
    value.add_argument("--workers", type=int, default=12)
    return value


def main() -> None:
    arguments = parser().parse_args()
    project = Path(__file__).resolve().parents[1]
    report = json.loads((project / "reports" / "two_hour_backtest.json").read_text(encoding="utf-8"))
    model_dir = arguments.model_dir or Path(report["candidate_model_dir"])
    selection = MultiHeadSelection(**report["event_threshold"])
    models = {
        "event": joblib.load(model_dir / "event_model.joblib"),
        "direction": joblib.load(model_dir / "direction_model.joblib"),
        "occurrence": joblib.load(model_dir / "occurrence_model.joblib"),
        "terminal": joblib.load(model_dir / "terminal_model.joblib"),
    }
    server_ms = _server_time_ms()
    last_closed_ms = server_ms // 300_000 * 300_000 - 300_000
    shanghai_now = datetime.fromtimestamp(server_ms / 1000, UTC) + timedelta(hours=8)
    calendar_start = (shanghai_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1, hours=8))
    calendar_start_ms = int(calendar_start.replace(tzinfo=UTC).timestamp() * 1000)
    strict_start_ms = max(calendar_start_ms, int(report["data"]["last_time_ms"]) + 300_000)
    maturity_end_ms = last_closed_ms - 2 * 60 * 60 * 1000
    warmup_start_ms = calendar_start_ms - 4 * 24 * 60 * 60 * 1000
    run_id = datetime.fromtimestamp(server_ms / 1000, UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = project / "data" / "forward_eval" / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "forward.sqlite3"
    _copy_history(arguments.cache, target_path, warmup_start_ms)
    symbols = load_symbol_metadata(arguments.metadata)
    local_last = sqlite3.connect(arguments.cache).execute("SELECT MAX(open_time) FROM bars").fetchone()[0]
    fetch_start_ms = max(int(local_last or 0) + 300_000, warmup_start_ms)
    bar_rows: list[tuple[Any, ...]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, arguments.workers)) as executor:
        futures = {
            executor.submit(
                _fetch_klines,
                item["symbol"],
                item["market_type"],
                fetch_start_ms,
                last_closed_ms,
            ): item["symbol"]
            for item in symbols
        }
        for future in as_completed(futures):
            try:
                bar_rows.extend(future.result())
            except Exception as exc:
                errors.append(f"klines/{futures[future]}: {type(exc).__name__}: {exc}")
    metrics_start_ms = max(
        int(sqlite3.connect(arguments.cache).execute("SELECT MAX(open_time) FROM futures_metrics").fetchone()[0] or 0)
        + 300_000,
        warmup_start_ms,
    )
    metric_rows: list[tuple[Any, ...]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(arguments.workers, 8))) as executor:
        futures = {
            executor.submit(_fetch_metrics, item["symbol"], metrics_start_ms, last_closed_ms): item[
                "symbol"
            ]
            for item in symbols
        }
        for future in as_completed(futures):
            try:
                metric_rows.extend(future.result())
            except Exception as exc:
                errors.append(f"metrics/{futures[future]}: {type(exc).__name__}: {exc}")
    _insert_live_rows(target_path, bar_rows, metric_rows)
    underlying_result = download_underlying_history(
        metadata_path=arguments.metadata,
        cache_path=target_path,
        workers=4,
        lookback_days=7,
    )
    sec_result = download_sec_events(
        metadata_path=arguments.metadata,
        cache_path=target_path,
        workers=4,
        lookback_days=7,
    )
    earnings_result = download_earnings_surprises(cache_path=target_path, workers=4)
    text_result = download_sec_text_features(cache_path=target_path, workers=8)
    dataset = build_dataset(target_path, sample_minutes=15)
    yesterday_end_ms = min(
        maturity_end_ms,
        calendar_start_ms + 24 * 60 * 60 * 1000 - 300_000,
    )
    today_start_ms = calendar_start_ms + 24 * 60 * 60 * 1000
    output = {
        "schema_version": 1,
        "generated_at": datetime.fromtimestamp(server_ms / 1000, UTC).isoformat(),
        "timezone": "Asia/Shanghai",
        "model_dir": str(model_dir),
        "model_training_data_end_ms": int(report["data"]["last_time_ms"]),
        "calendar_window": _evaluate_window(
            dataset, calendar_start_ms, maturity_end_ms, models, selection
        ),
        "strict_fresh_window": _evaluate_window(
            dataset, strict_start_ms, maturity_end_ms, models, selection
        ),
        "yesterday": _evaluate_window(
            dataset, calendar_start_ms, yesterday_end_ms, models, selection
        ),
        "today_matured": _evaluate_window(
            dataset, today_start_ms, maturity_end_ms, models, selection
        ),
        "ingestion": {
            "live_kline_rows": len(bar_rows),
            "live_metrics_rows": len(metric_rows),
            "symbols": len(symbols),
            "errors": errors,
            "underlying": underlying_result,
            "sec": sec_result,
            "earnings": earnings_result,
            "sec_text": text_result,
            "production_news": _news_summary(calendar_start_ms, server_ms),
        },
        "selection": asdict(selection),
        "limitations": [
            "today excludes predictions whose full two-hour outcome is not available yet",
            "production RSS/news items are counted but are not model features in this candidate",
            "a one-to-two-day precision estimate has a wide confidence interval",
        ],
    }
    report_path = project / "reports" / f"forward_validation_{run_id}.json"
    report_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
