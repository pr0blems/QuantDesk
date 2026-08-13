"""Isolated, point-in-time replay for the AI news + technical signal workflow."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import ai_monitor
from .models import (
    AiMonitorConfig,
    AiMonitorReplayDatasetManifest,
    AiMonitorReplayOutcome,
    AiMonitorReplayRun,
    AiMonitorReplaySignal,
    News,
    utcnow,
)
from .monitor import MonitorRepository
from .strategy_indicators import evaluate_directional_strategy_indicators

TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "4h": 14_400}
ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
ARCHIVE_USER_AGENT = "QuantDesk/2 HistoricalReplay"
CONSERVATIVE_COST_MODEL: dict[str, Any] = {
    "prediction_fee_enabled": True,
    "prediction_fee_bps_per_side": ai_monitor.PREDICTION_FEE_BPS_PER_SIDE,
    "prediction_slippage_enabled": True,
    "prediction_slippage_bps_per_side": ai_monitor.PREDICTION_SLIPPAGE_BPS_PER_SIDE,
    "prediction_funding_enabled": True,
    "prediction_funding_bps_per_8h": ai_monitor.PREDICTION_FUNDING_BPS_PER_8H,
    "forced_for_readiness": True,
    "version": "historical_replay_cost_v1",
}


class HistoricalReplayError(RuntimeError):
    """A safe replay failure suitable for the task status."""


def create_replay_run(
    db: Session,
    repository: MonitorRepository,
    user_id: int,
    *,
    days: int,
    timeframe: str,
    symbols: Sequence[str] | None,
) -> AiMonitorReplayRun:
    if timeframe not in TIMEFRAME_SECONDS:
        raise HistoricalReplayError("unsupported replay timeframe")
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    start_at = now - timedelta(days=days)
    embargo = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * 2)
    oos_start = start_at + (now - start_at) * 0.60 + embargo
    catalog = ai_monitor.monitor_symbol_catalog(repository)
    onboard_by_contract = {
        str(item.get("symbol") or "").strip().upper(): int(item.get("onboardDate") or 0)
        for item in repository.symbols_meta
    }
    config = _json_safe(ai_monitor.config_data(db.get(AiMonitorConfig, user_id)))
    selected = _select_replay_symbols(
        catalog,
        requested=symbols or (),
        configured=config.get("monitor_symbols") or (),
    )
    if not selected:
        raise HistoricalReplayError("no configured Binance TradFi symbols match the request")
    run = AiMonitorReplayRun(
        user_id=user_id,
        status="pending",
        timeframe=timeframe,
        start_at=start_at,
        end_at=now,
        out_of_sample_start_at=oos_start,
        requested_symbols_json=[item["contract_symbol"] for item in selected],
        config_snapshot_json={
            **config,
            "replay_version": "point_in_time_v1",
            "symbol_map": {item["symbol"]: item["contract_symbol"] for item in selected},
            "contract_onboard_ms": {
                item["contract_symbol"]: onboard_by_contract.get(item["contract_symbol"], 0)
                for item in selected
            },
        },
        cost_model_json=dict(CONSERVATIVE_COST_MODEL),
        provenance_json={
            "market_source": "Binance Vision official USD-M monthly archive",
            "news_source": "existing AI-analyzed news table",
            "news_policy": "published_at <= signal_at; no future articles",
            "indicator_policy": "closed candles only; prediction_* features are unavailable",
            "entry_policy": "next bar open after signal bar closes",
            "exit_candle_policy": (
                "15m closed candles for execution and score cadence; signal indicators "
                "remain on the configured timeframe"
            ),
            "exit_policy": (
                "each closed bar: frozen profit guard; then stop/target touch; "
                "confirmed score or failed-follow-through exit; hard time cap last"
            ),
            "sample_policy": "60% train + two-bar embargo + remaining OOS",
            "realtime_tables_untouched": True,
        },
        total_symbols=len(selected),
    )
    db.add(run)
    db.flush()
    return run


def _select_replay_symbols(
    catalog: Sequence[Mapping[str, Any]],
    *,
    requested: Sequence[str],
    configured: Sequence[str],
) -> list[Mapping[str, Any]]:
    """Resolve explicit symbols first, then the tenant's configured monitor scope."""

    requested_set = {
        str(item).strip().upper() for item in requested if str(item).strip()
    }
    configured_set = {
        str(item).strip().upper() for item in configured if str(item).strip()
    }
    effective = requested_set or configured_set
    if not effective:
        return list(catalog)
    return [
        item
        for item in catalog
        if str(item.get("symbol") or "").upper() in effective
        or str(item.get("contract_symbol") or "").upper() in effective
    ]


def replay_run_out(run: AiMonitorReplayRun) -> dict[str, Any]:
    return {
        "id": run.public_id,
        "status": run.status,
        "timeframe": run.timeframe,
        "start_at": _as_utc(run.start_at),
        "end_at": _as_utc(run.end_at),
        "out_of_sample_start_at": _as_utc(run.out_of_sample_start_at),
        "symbols": list(run.requested_symbols_json or []),
        "total_symbols": int(run.total_symbols),
        "completed_symbols": int(run.completed_symbols),
        "total_events": int(run.total_events),
        "generated_signals": int(run.generated_signals),
        "settled_signals": int(run.settled_signals),
        "dataset_hash": run.dataset_hash,
        "cost_model": dict(run.cost_model_json or {}),
        "provenance": dict(run.provenance_json or {}),
        "summary": dict(run.summary_json or {}),
        "error": run.error_message,
        "started_at": _as_utc(run.started_at),
        "completed_at": _as_utc(run.completed_at),
        "created_at": _as_utc(run.created_at),
        "updated_at": _as_utc(run.updated_at),
    }


def _record_kline_manifest(
    db: Session,
    run: AiMonitorReplayRun,
    *,
    symbol: str,
    data_type: str,
    timeframe: str,
    purpose: str,
    candles: Sequence[Mapping[str, Any]],
    archive: Mapping[str, Any],
) -> AiMonitorReplayDatasetManifest:
    """Create or refresh a replay kline manifest without duplicate recovery rows."""

    manifest = db.scalar(
        select(AiMonitorReplayDatasetManifest).where(
            AiMonitorReplayDatasetManifest.run_id == run.id,
            AiMonitorReplayDatasetManifest.source == "binance_vision",
            AiMonitorReplayDatasetManifest.symbol == symbol,
            AiMonitorReplayDatasetManifest.data_type == data_type,
        )
    )
    if manifest is None:
        manifest = AiMonitorReplayDatasetManifest(
            run_id=run.id,
            user_id=run.user_id,
            source="binance_vision",
            symbol=symbol,
            data_type=data_type,
        )
        db.add(manifest)
    manifest.coverage_start_at = (
        _ms_datetime(candles[0]["open_time"]) if candles else None
    )
    manifest.coverage_end_at = (
        _ms_datetime(candles[-1]["open_time"]) if candles else None
    )
    manifest.row_count = len(candles)
    manifest.sha256 = _hash_json(archive.get("sha256s") or [])
    manifest.exact_point_in_time = True
    manifest.details_json = {
        **dict(archive),
        "timeframe": timeframe,
        "purpose": purpose,
    }
    return manifest


def execute_replay_run(engine: Engine, run_public_id: str, symbols_config: Any) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with factory() as db:
            run = db.scalar(
                select(AiMonitorReplayRun).where(AiMonitorReplayRun.public_id == run_public_id)
            )
            if run is None or run.status != "pending":
                return
            run.status = "running"
            run.started_at = run.started_at or utcnow()
            run.updated_at = utcnow()
            db.commit()
        repository = MonitorRepository(engine, symbols_config)
        _execute(factory, repository, run_public_id)
    except Exception as exc:  # noqa: BLE001 - background task must persist its terminal state
        with factory() as db:
            run = db.scalar(
                select(AiMonitorReplayRun).where(AiMonitorReplayRun.public_id == run_public_id)
            )
            if run is not None:
                run.status = "failed"
                run.error_message = _safe_error(exc)
                run.completed_at = utcnow()
                run.updated_at = run.completed_at
                db.commit()


def _execute(
    factory: sessionmaker[Session], repository: MonitorRepository, run_public_id: str
) -> None:
    with factory() as db:
        run = db.scalar(
            select(AiMonitorReplayRun).where(AiMonitorReplayRun.public_id == run_public_id)
        )
        if run is None:
            return
        symbols = list(run.requested_symbols_json or [])
        symbol_map = dict((run.config_snapshot_json or {}).get("symbol_map") or {})
        reverse_map = {contract: symbol for symbol, contract in symbol_map.items()}
        news_by_symbol = _load_historical_news(
            db,
            reverse_map,
            run.start_at,
            run.end_at,
            analysis_cutoff=run.created_at,
        )
        news_count = sum(len(items) for items in news_by_symbol.values())
        news_dataset_hash = _hash_json(
            sorted(
                (
                    symbol,
                    str(item.get("id") or ""),
                    int(item.get("ts") or 0),
                    str(item.get("direction") or ""),
                    float(item.get("score") or 0),
                )
                for symbol, items in news_by_symbol.items()
                for item in items
            )
        )
        news_manifest = db.scalar(
            select(AiMonitorReplayDatasetManifest).where(
                AiMonitorReplayDatasetManifest.run_id == run.id,
                AiMonitorReplayDatasetManifest.source == "quantdesk_news_ai",
                AiMonitorReplayDatasetManifest.symbol == "*",
                AiMonitorReplayDatasetManifest.data_type == "news_analysis",
            )
        )
        news_details = {
            "analysis_is_frozen": True,
            "analysis_cutoff": _as_utc(run.created_at).isoformat(),
            "published_at_filter": True,
            "warning": (
                None
                if news_count
                else "No AI-analyzed historical news exists in the requested range."
            ),
        }
        if news_manifest is None:
            news_manifest = AiMonitorReplayDatasetManifest(
                run_id=run.id,
                user_id=run.user_id,
                source="quantdesk_news_ai",
                symbol="*",
                data_type="news_analysis",
                coverage_start_at=run.start_at,
                coverage_end_at=run.end_at,
                row_count=news_count,
                sha256=news_dataset_hash,
                exact_point_in_time=True,
                details_json=news_details,
            )
            db.add(news_manifest)
        else:
            news_manifest.row_count = news_count
            news_manifest.sha256 = news_dataset_hash
            news_manifest.details_json = news_details
        manifest_types_by_symbol: dict[str, set[str]] = {}
        for manifest_symbol, data_type in db.execute(
            select(
                AiMonitorReplayDatasetManifest.symbol,
                AiMonitorReplayDatasetManifest.data_type,
            ).where(
                AiMonitorReplayDatasetManifest.run_id == run.id,
                AiMonitorReplayDatasetManifest.source == "binance_vision",
            )
        ).all():
            manifest_types_by_symbol.setdefault(str(manifest_symbol), set()).add(
                str(data_type)
            )
        required_manifest_types = (
            {"klines"}
            if run.timeframe == "15m"
            else {"klines", "exit_klines_15m"}
        )
        completed = {
            manifest_symbol
            for manifest_symbol, data_types in manifest_types_by_symbol.items()
            if required_manifest_types.issubset(data_types)
        }
        run.total_events = news_count
        run.completed_symbols = len(completed)
        run.generated_signals = int(
            db.scalar(
                select(func.count()).select_from(AiMonitorReplaySignal).where(
                    AiMonitorReplaySignal.run_id == run.id
                )
            )
            or 0
        )
        run.settled_signals = int(
            db.scalar(
                select(func.count()).select_from(AiMonitorReplayOutcome).where(
                    AiMonitorReplayOutcome.run_id == run.id
                )
            )
            or 0
        )
        db.commit()

    for contract in symbols:
        if contract in completed:
            continue
        with factory() as db:
            run = db.scalar(
                select(AiMonitorReplayRun).where(AiMonitorReplayRun.public_id == run_public_id)
            )
            if run is None or run.status != "running":
                return
            symbol = reverse_map.get(contract, contract.removesuffix("USDT"))
            archive_start = max(
                run.start_at,
                _ms_datetime(
                    int(
                        (run.config_snapshot_json or {})
                        .get("contract_onboard_ms", {})
                        .get(contract, 0)
                    )
                )
                if int(
                    (run.config_snapshot_json or {})
                    .get("contract_onboard_ms", {})
                    .get(contract, 0)
                )
                else run.start_at,
            )
            archive = ensure_archive_klines(
                repository.engine,
                contract,
                run.timeframe,
                archive_start,
                run.end_at,
            )
            exit_archive = archive
            if run.timeframe != "15m":
                exit_archive = ensure_archive_klines(
                    repository.engine,
                    contract,
                    "15m",
                    archive_start,
                    run.end_at,
                )
            # Archive upserts use independent engine transactions. End the
            # session's earlier repeatable-read snapshot before loading them.
            db.commit()
            candles = _load_candles(
                db, contract, run.timeframe, run.start_at, run.end_at
            )
            _record_kline_manifest(
                db,
                run,
                symbol=contract,
                data_type="klines",
                timeframe=run.timeframe,
                purpose="signal_generation",
                candles=candles,
                archive=archive,
            )
            exit_candles = candles
            if run.timeframe != "15m":
                exit_candles = _load_candles(
                    db, contract, "15m", run.start_at, run.end_at
                )
                _record_kline_manifest(
                    db,
                    run,
                    symbol=contract,
                    data_type="exit_klines_15m",
                    timeframe="15m",
                    purpose="exit_execution_and_score_cadence",
                    candles=exit_candles,
                    archive=exit_archive,
                )
            generated = _replay_symbol(
                db,
                run,
                symbol,
                contract,
                news_by_symbol.get(symbol, []),
                candles,
                exit_candles,
            )
            run.completed_symbols += 1
            run.generated_signals += generated
            run.settled_signals += generated
            run.updated_at = utcnow()
            db.commit()

    with factory() as db:
        run = db.scalar(
            select(AiMonitorReplayRun).where(AiMonitorReplayRun.public_id == run_public_id)
        )
        if run is None:
            return
        manifest_hashes = list(
            db.scalars(
                select(AiMonitorReplayDatasetManifest.sha256).where(
                    AiMonitorReplayDatasetManifest.run_id == run.id,
                    AiMonitorReplayDatasetManifest.sha256.is_not(None),
                )
            ).all()
        )
        run.dataset_hash = _hash_json(
            sorted(str(item) for item in manifest_hashes if item)
        )
        run.summary_json = replay_readiness_report(db, run.user_id, run_id=run.id)
        run.status = "completed"
        run.completed_at = utcnow()
        run.updated_at = run.completed_at
        db.commit()


def ensure_archive_klines(
    engine: Engine,
    symbol: str,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, Any]:
    """Fetch missing official monthly archives, verify checksums and upsert bars."""

    downloaded = 0
    unavailable: list[str] = []
    digests: list[str] = []
    months = _months(start_at, end_at)
    for year, month in months:
        stem = f"{symbol}-{timeframe}-{year:04d}-{month:02d}.zip"
        url = f"{ARCHIVE_ROOT}/{symbol}/{timeframe}/{stem}"
        try:
            checksum_text = _download(f"{url}.CHECKSUM", max_bytes=2_048).decode(
                "ascii", errors="strict"
            )
            expected = checksum_text.strip().split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise HistoricalReplayError("invalid Binance archive checksum")
            archive = _download(url, max_bytes=256 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                unavailable.append(stem)
                continue
            raise
        actual = hashlib.sha256(archive).hexdigest()
        if actual != expected:
            raise HistoricalReplayError(f"checksum mismatch for {stem}")
        rows = _parse_archive(archive)
        _upsert_klines(engine, symbol, timeframe, rows)
        downloaded += len(rows)
        digests.append(actual)
    return {
        "archives_requested": len(months),
        "archives_available": len(digests),
        "bars_downloaded": downloaded,
        "missing_archives": unavailable,
        "sha256s": digests,
        "checksum_verified": True,
    }


def _download(url: str, *, max_bytes: int) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - caller constructs a fixed HTTPS origin
        url, headers={"User-Agent": ARCHIVE_USER_AGENT}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise HistoricalReplayError("historical archive exceeded size limit")
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == 3:
                raise
        time.sleep(0.5 * (2**attempt))
    raise HistoricalReplayError("official archive download retries exhausted")


def _parse_archive(payload: bytes) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        files = [item for item in bundle.infolist() if not item.is_dir()]
        if len(files) != 1 or files[0].file_size > 512 * 1024 * 1024:
            raise HistoricalReplayError("unexpected Binance archive layout")
        with bundle.open(files[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            for row in reader:
                if not row or not row[0].isdigit() or len(row) < 6:
                    continue
                result.append(
                    (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                )
    return result


def _upsert_klines(
    engine: Engine, symbol: str, timeframe: str, rows: Sequence[tuple[Any, ...]]
) -> None:
    statement = text(
        """
        INSERT INTO klines(symbol,tf,open_time,open,high,low,close,volume)
        VALUES(:symbol,:tf,:open_time,:open,:high,:low,:close,:volume)
        ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
                                close=VALUES(close),volume=VALUES(volume)
        """
    )
    values = [
        {
            "symbol": symbol,
            "tf": timeframe,
            "open_time": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
        }
        for row in rows
    ]
    if not values:
        return
    with engine.begin() as connection:
        for start in range(0, len(values), 2_000):
            connection.execute(statement, values[start : start + 2_000])


def _load_historical_news(
    db: Session,
    contract_to_symbol: Mapping[str, str],
    start_at: datetime,
    end_at: datetime,
    *,
    analysis_cutoff: datetime,
) -> dict[str, list[dict[str, Any]]]:
    symbol_set = set(contract_to_symbol.values())
    rows = db.scalars(
        select(News)
        .where(
            News.ts >= int(start_at.replace(tzinfo=UTC).timestamp()),
            News.ts <= int(end_at.replace(tzinfo=UTC).timestamp()),
            News.ai_analyzed_at.is_not(None),
            News.ai_analyzed_at <= analysis_cutoff,
            News.related_us_stocks.is_not(None),
        )
        .order_by(News.ts, News.id)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        confidence = float(row.ai_confidence or 0)
        for related in list(row.related_us_stocks or []):
            if not isinstance(related, Mapping):
                continue
            symbol = str(related.get("symbol") or "").strip().upper()
            if symbol == "BRKB":
                symbol = "BRK.B"
            if symbol == "PAYP":
                symbol = "PYPL"
            if symbol not in symbol_set:
                continue
            raw_direction = str(related.get("direction") or "").lower()
            direction = (
                "long"
                if raw_direction in {"bull", "bullish", "long", "positive"}
                else "short"
                if raw_direction in {"bear", "bearish", "short", "negative"}
                else ""
            )
            if not direction:
                continue
            relevance = min(1.0, max(0.0, float(related.get("relevance") or 0)))
            score = confidence * relevance * 100
            result.setdefault(symbol, []).append(
                {
                    "id": str(row.id),
                    "ts": int(row.ts),
                    "source": row.source,
                    "title": row.title_zh or row.title,
                    "reason": row.ai_reason,
                    "direction": direction,
                    "confidence": confidence,
                    "relevance": relevance,
                    "score": round(score, 4),
                }
            )
    return result


def _load_candles(
    db: Session,
    symbol: str,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    start_ms = int((start_at - timedelta(days=7)).replace(tzinfo=UTC).timestamp() * 1000)
    end_ms = int((end_at + timedelta(days=1)).replace(tzinfo=UTC).timestamp() * 1000)
    rows = db.execute(
        text(
            """
            SELECT open_time,open,high,low,close,volume FROM klines
            WHERE symbol=:symbol AND tf=:tf AND open_time>=:start_ms AND open_time<=:end_ms
            ORDER BY open_time ASC
            """
        ),
        {"symbol": symbol, "tf": timeframe, "start_ms": start_ms, "end_ms": end_ms},
    ).mappings()
    return [dict(item) for item in rows]


def _historical_exit_decision(
    candles: Sequence[Mapping[str, Any]],
    score_observations: Iterable[Mapping[str, Any]],
    *,
    entry_price: float,
    direction: str,
    risk_plan: Mapping[str, Any],
    start_ms: int,
    due_ms: int,
    timeframe_ms: int,
    exit_threshold: float,
    estimated_cost_bps: float = 0.0,
    adaptive_exit_enabled: bool = False,
) -> dict[str, Any] | None:
    """Replay the live exit policy using only information known at each bar close.

    A profit guard frozen from prior closed bars is executable first. Price
    barriers are then evaluated before the score observed at the current bar's
    close. Score reversals exit immediately, while weakening in the original
    direction needs two consecutive observations below the frozen entry
    threshold. The hard cap is considered last.
    """

    score_iterator = iter(score_observations)
    pending_score: dict[str, Any] | None = None
    score_history_exhausted = False
    normalized_direction = "short" if direction == "short" else "long"
    consecutive_low_scores = 0
    latest_score: dict[str, Any] | None = None
    observed_bar_count = 0
    adaptive_exit = (
        ai_monitor.prediction_adaptive_path_exit(
            candles,
            entry_price,
            normalized_direction,
            start_ms,
            due_ms,
            estimated_cost_bps=estimated_cost_bps,
            timeframe_ms=timeframe_ms,
        )
        if adaptive_exit_enabled
        else None
    )
    for candle in candles:
        try:
            open_ms = int(candle.get("open_time") or 0)
            if 0 < open_ms < 1_000_000_000_000:
                open_ms *= 1_000
            close_price = float(candle.get("close") or 0)
        except (TypeError, ValueError):
            continue
        close_ms = open_ms + timeframe_ms
        # A candle opening at the cap has not closed yet.  Its high/low must not
        # influence a decision whose information cutoff is ``due_ms``.
        if open_ms < start_ms or open_ms >= due_ms or close_ms > due_ms:
            continue
        if close_price <= 0:
            continue
        observed_bar_count += 1
        adaptive_time_ms = (
            int(adaptive_exit["price_time_ms"])
            if adaptive_exit is not None
            else None
        )
        adaptive_profit_ready = bool(
            adaptive_time_ms is not None
            and adaptive_time_ms <= close_ms
            and adaptive_exit.get("exit_subreason")
            in {"profit_lock", "trailing_profit"}
        )
        barrier = ai_monitor.prediction_price_barrier_exit(
            [candle],
            entry_price,
            normalized_direction,
            risk_plan,
            open_ms,
            close_ms,
            timeframe_ms=timeframe_ms,
        )
        if adaptive_profit_ready and ai_monitor.adaptive_exit_precedes(
            barrier, adaptive_exit
        ):
            return {
                **adaptive_exit,
                "observed_bar_count": observed_bar_count,
                "score_at_exit": latest_score,
            }
        if barrier is not None:
            return {
                **barrier,
                "observed_bar_count": observed_bar_count,
                "score_at_exit": latest_score,
            }

        while not score_history_exhausted and (
            pending_score is None
            or int(pending_score.get("price_time_ms") or 0) < close_ms
        ):
            try:
                pending_score = dict(next(score_iterator))
            except StopIteration:
                score_history_exhausted = True
                pending_score = None
                break
        score = (
            pending_score
            if pending_score is not None
            and int(pending_score.get("price_time_ms") or 0) == close_ms
            else None
        )
        if score is not None:
            pending_score = None
            latest_score = score
            observed_direction = str(score.get("direction") or normalized_direction)
            if (
                observed_direction in {"long", "short"}
                and observed_direction != normalized_direction
            ):
                return {
                    "reason": "score_reversal",
                    "price": close_price,
                    "price_time_ms": close_ms,
                    "same_bar_conflict": False,
                    "gap_execution": False,
                    "confirmation_points": 1,
                    "exit_threshold": exit_threshold,
                    "observed_bar_count": observed_bar_count,
                    "score_at_exit": latest_score,
                }
            try:
                combined = float(score.get("combined"))
            except (TypeError, ValueError):
                consecutive_low_scores = 0
            else:
                consecutive_low_scores = (
                    consecutive_low_scores + 1
                    if combined < exit_threshold
                    else 0
                )
                if consecutive_low_scores >= 2:
                    return {
                        "reason": "score_breakdown",
                        "price": close_price,
                        "price_time_ms": close_ms,
                        "same_bar_conflict": False,
                        "gap_execution": False,
                        "confirmation_points": 2,
                        "exit_threshold": exit_threshold,
                        "observed_bar_count": observed_bar_count,
                        "score_at_exit": latest_score,
                    }

        if adaptive_time_ms is not None and adaptive_time_ms <= close_ms:
            return {
                **adaptive_exit,
                "observed_bar_count": observed_bar_count,
                "score_at_exit": latest_score,
            }

        if close_ms >= due_ms:
            return {
                "reason": "max_holding_time",
                "price": close_price,
                "price_time_ms": close_ms,
                "same_bar_conflict": False,
                "gap_execution": False,
                "observed_bar_count": observed_bar_count,
                "score_at_exit": latest_score,
            }
    return None


def _historical_score_observation(
    news: Sequence[Mapping[str, Any]],
    evaluated: Mapping[str, Any],
    *,
    held_direction: str,
    observed_at_seconds: int,
    configured_keys: Sequence[str],
    minimum_news_score: float,
    minimum_news_mentions: int,
    news_lookback_seconds: int,
    minimum_indicator_score: float,
    minimum_combined_score: float,
    news_weight: float,
    technical_weight: float,
) -> dict[str, Any]:
    """Build one point-in-time score observation from a closed historical bar."""

    usable_weight = max(news_weight + technical_weight, 1.0)
    directions = (held_direction, "short" if held_direction == "long" else "long")
    candidates: dict[str, dict[str, Any]] = {}
    for candidate_direction in directions:
        news_snapshot = _historical_news_snapshot(
            news,
            direction=candidate_direction,
            signal_at_seconds=observed_at_seconds,
            minimum_score=minimum_news_score,
            minimum_mentions=minimum_news_mentions,
            lookback_seconds=news_lookback_seconds,
        )
        indicator_snapshot = _historical_indicator_snapshot(
            evaluated, configured_keys, candidate_direction
        )
        news_score = (
            sum(float(item["score"]) for item in news_snapshot) / len(news_snapshot)
            if news_snapshot
            else 0.0
        )
        technical_score = float(indicator_snapshot["score"])
        combined_score = (
            news_score * news_weight + technical_score * technical_weight
        ) / usable_weight
        candidates[candidate_direction] = {
            "direction": candidate_direction,
            "news": news_score,
            "technical": technical_score,
            "combined": combined_score,
            "entry_confirmed": bool(
                news_snapshot
                and indicator_snapshot["available_keys"]
                and indicator_snapshot["policy_passed"]
                and technical_score >= minimum_indicator_score
                and combined_score >= minimum_combined_score
            ),
        }
    opposite_direction = directions[1]
    selected = (
        candidates[opposite_direction]
        if candidates[opposite_direction]["entry_confirmed"]
        else candidates[held_direction]
    )
    return {
        "price_time_ms": observed_at_seconds * 1_000,
        "direction": selected["direction"],
        "news": round(float(selected["news"]), 4),
        "technical": round(float(selected["technical"]), 4),
        "combined": round(float(selected["combined"]), 4),
    }


def _replay_symbol(
    db: Session,
    run: AiMonitorReplayRun,
    symbol: str,
    contract: str,
    news: Sequence[Mapping[str, Any]],
    candles: Sequence[Mapping[str, Any]],
    exit_candles: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    if len(candles) < 122 or not news:
        return 0
    interval_ms = TIMEFRAME_SECONDS[run.timeframe] * 1000
    exit_interval_ms = TIMEFRAME_SECONDS["15m"] * 1000
    open_times = [int(item["open_time"]) for item in candles]
    close_times = [open_time + interval_ms for open_time in open_times]
    execution_candles = exit_candles if exit_candles is not None else candles
    execution_open_times = [int(item["open_time"]) for item in execution_candles]
    config = dict(run.config_snapshot_json or {})
    min_news = float(config.get("minimum_news_confidence", 0.6)) * 100
    min_news_mentions = max(1, int(config.get("minimum_news_mentions", 1)))
    news_lookback_seconds = max(1, int(config.get("news_lookback_hours", 24))) * 3600
    min_indicator = float(config.get("minimum_indicator_score", 65))
    min_combined = float(config.get("minimum_combined_score", 70))
    configured_keys = list(config.get("indicator_keys") or ai_monitor.DEFAULT_INDICATOR_KEYS)
    generated = 0
    for event in news:
        if float(event.get("score") or 0) < min_news:
            continue
        published_ms = int(event["ts"]) * 1000
        signal_index = bisect_right(open_times, published_ms) - 1
        if signal_index < 119 or signal_index + 1 >= len(candles):
            continue
        signal_at_ms = open_times[signal_index] + interval_ms
        if signal_at_ms < published_ms:
            signal_index += 1
            signal_at_ms = open_times[signal_index] + interval_ms
        entry_index = signal_index + 1
        # The decision is made after ``signal_index`` closes and enters at the
        # next bar's open.  A one-timeframe holding period therefore settles at
        # that same entry bar's close, not at the close of an additional bar.
        due_index = entry_index
        if due_index >= len(candles):
            continue
        direction = str(event["direction"])
        signal_at_seconds = signal_at_ms // 1000
        news_snapshot = _historical_news_snapshot(
            news,
            direction=direction,
            signal_at_seconds=signal_at_seconds,
            minimum_score=min_news,
            minimum_mentions=min_news_mentions,
            lookback_seconds=news_lookback_seconds,
        )
        unique_news_ids = {
            str(item.get("id") or "") for item in news_snapshot if item.get("id")
        }
        if not news_snapshot:
            continue
        evaluated = evaluate_directional_strategy_indicators(
            candles[max(0, signal_index - 119) : signal_index + 1], run.timeframe
        )
        indicator_snapshot = _historical_indicator_snapshot(
            evaluated, configured_keys, direction
        )
        indicator_score = float(indicator_snapshot["score"])
        available_keys = indicator_snapshot["available_keys"]
        if (
            not available_keys
            or not bool(indicator_snapshot["policy_passed"])
            or indicator_score < min_indicator
        ):
            continue
        news_score = sum(float(item["score"]) for item in news_snapshot) / len(
            news_snapshot
        )
        # Historical replay has no trustworthy order-book snapshot. Re-normalize the
        # configured news/technical weights and record this degraded, stricter mode.
        news_weight = float(config.get("news_score_weight", 45))
        technical_weight = float(config.get("technical_score_weight", 35))
        usable_weight = max(news_weight + technical_weight, 1.0)
        combined_score = (
            news_score * news_weight + indicator_score * technical_weight
        ) / usable_weight
        if combined_score < min_combined:
            continue
        entry_price = float(candles[entry_index]["open"])
        if entry_price <= 0:
            continue
        signal_at = _ms_datetime(signal_at_ms)
        entry_at_ms = open_times[entry_index]
        due_at_ms = open_times[due_index] + interval_ms
        entry_at = _ms_datetime(entry_at_ms)
        due_at = _ms_datetime(due_at_ms)
        risk_plan = ai_monitor.virtual_risk_plan_snapshot(
            entry_price=entry_price,
            direction=direction,
            timeframe=run.timeframe,
        )
        execution_start = bisect_left(execution_open_times, entry_at_ms)
        execution_end = bisect_left(execution_open_times, due_at_ms)
        path = execution_candles[execution_start:execution_end]
        expected_execution_times = list(
            range(entry_at_ms, due_at_ms, exit_interval_ms)
        )
        if [int(item["open_time"]) for item in path] != expected_execution_times:
            # Never bridge a missing execution candle with a later close; doing
            # so would hide an unobserved barrier or score transition.
            continue

        def score_observation_stream(
            execution_path: Sequence[Mapping[str, Any]] = path,
            held_direction: str = direction,
            frozen_news_weight: float = news_weight,
            frozen_technical_weight: float = technical_weight,
        ) -> Iterable[dict[str, Any]]:
            for execution_bar in execution_path:
                observed_at_ms = int(execution_bar["open_time"]) + exit_interval_ms
                closed_signal_index = bisect_right(close_times, observed_at_ms) - 1
                if closed_signal_index < 119:
                    continue
                bar_evaluation = evaluate_directional_strategy_indicators(
                    candles[
                        max(0, closed_signal_index - 119) : closed_signal_index + 1
                    ],
                    run.timeframe,
                )
                yield _historical_score_observation(
                    news,
                    bar_evaluation,
                    held_direction=held_direction,
                    observed_at_seconds=observed_at_ms // 1_000,
                    configured_keys=configured_keys,
                    minimum_news_score=min_news,
                    minimum_news_mentions=min_news_mentions,
                    news_lookback_seconds=news_lookback_seconds,
                    minimum_indicator_score=min_indicator,
                    minimum_combined_score=min_combined,
                    news_weight=frozen_news_weight,
                    technical_weight=frozen_technical_weight,
                )

        exit_threshold = max(0.0, min_combined - 5.0)
        guard_cost_estimate = ai_monitor.prediction_estimated_cost_bps(
            entry_at,
            due_at,
            CONSERVATIVE_COST_MODEL,
        )
        exit_decision = _historical_exit_decision(
            path,
            score_observation_stream(),
            entry_price=entry_price,
            direction=direction,
            risk_plan=risk_plan,
            start_ms=entry_at_ms,
            due_ms=due_at_ms,
            timeframe_ms=exit_interval_ms,
            exit_threshold=exit_threshold,
            estimated_cost_bps=guard_cost_estimate,
            adaptive_exit_enabled=True,
        )
        if exit_decision is None:
            continue
        exit_reason = str(exit_decision["reason"])
        exit_price = float(exit_decision["price"])
        exit_at = _ms_datetime(int(exit_decision["price_time_ms"]))
        exit_score = exit_decision.get("score_at_exit")
        exit_score = dict(exit_score) if isinstance(exit_score, Mapping) else {}
        if exit_price <= 0:
            continue
        dedup_key = hashlib.sha256(
            f"{run.id}|{contract}|{direction}|{signal_at_ms}".encode()
        ).hexdigest()
        exists = db.scalar(
            select(AiMonitorReplaySignal.id).where(AiMonitorReplaySignal.dedup_key == dedup_key)
        )
        if exists is not None:
            continue
        split = _sample_split(signal_at, run.out_of_sample_start_at, run.timeframe)
        signal = AiMonitorReplaySignal(
            run_id=run.id,
            user_id=run.user_id,
            symbol=symbol,
            contract_symbol=contract,
            direction=direction,
            timeframe=run.timeframe,
            sample_split=split,
            news_score=Decimal(str(round(news_score, 4))),
            indicator_score=Decimal(str(round(indicator_score, 4))),
            combined_score=Decimal(str(round(combined_score, 4))),
            signal_at=signal_at,
            entry_at=entry_at,
            due_at=due_at,
            entry_price=Decimal(str(entry_price)),
            dedup_key=dedup_key,
            news_snapshot_json=news_snapshot[:8],
            indicator_snapshot_json=indicator_snapshot,
            evidence_json={
                "point_in_time": True,
                "latest_news_ts": max(int(item["ts"]) for item in news_snapshot),
                "news_mention_count": len(unique_news_ids),
                "news_lookback_hours": int(config.get("news_lookback_hours", 24)),
                "latest_candle_open_time": open_times[signal_index],
                "market_flow_available": False,
                "weight_normalization": {
                    "news": news_weight / usable_weight,
                    "technical": technical_weight / usable_weight,
                },
                "configured_indicator_keys": configured_keys,
                "unavailable_indicator_keys": indicator_snapshot["unavailable_keys"],
                "risk_plan": risk_plan,
                "score_exit_threshold": exit_threshold,
                "exit_candle_timeframe": "15m",
                "exit_policy": (
                    "frozen_profit_guard_then_price_barrier_then_confirmed_score_exit_"
                    "then_failed_follow_through_then_hard_time_cap"
                ),
            },
        )
        db.add(signal)
        db.flush()
        gross = ((exit_price / entry_price) - 1) * 10_000
        if direction == "short":
            gross = -gross
        costs = ai_monitor.prediction_cost_breakdown(
            signal_at, exit_at, CONSERVATIVE_COST_MODEL
        )
        net = gross - float(costs["total_cost_bps"])
        observed_path = path[: int(exit_decision["observed_bar_count"])]
        metric_path = (
            observed_path[:-1]
            if exit_reason in {"take_profit", "stop_loss"}
            else observed_path
        )
        favorable, adverse = _path_metrics(
            metric_path,
            entry_price,
            direction,
            terminal_price=exit_price,
        )
        result = "win" if net > 0 else "loss" if net < 0 else "flat"
        db.add(
            AiMonitorReplayOutcome(
                signal_id=signal.id,
                run_id=run.id,
                user_id=run.user_id,
                sample_split=split,
                exit_at=exit_at,
                exit_price=Decimal(str(exit_price)),
                gross_directional_return_bps=Decimal(str(round(gross, 8))),
                estimated_cost_bps=Decimal(str(costs["total_cost_bps"])),
                net_directional_return_bps=Decimal(str(round(net, 8))),
                max_favorable_bps=Decimal(str(round(favorable, 8))),
                max_adverse_bps=Decimal(str(round(adverse, 8))),
                result=result,
                settlement_json={
                    "version": "historical_replay_adaptive_guard_v4",
                    "entry_policy": "next_bar_open",
                    "exit_policy": (
                        "frozen_profit_guard_then_price_barrier_then_confirmed_score_exit_"
                        "then_failed_follow_through_then_hard_time_cap"
                    ),
                    "exit_reason": exit_reason,
                    "exit_subreason": exit_decision.get("exit_subreason"),
                    "peak_favorable_bps_at_decision": exit_decision.get(
                        "peak_favorable_bps"
                    ),
                    "protected_bps": exit_decision.get("protected_bps"),
                    "risk_plan": risk_plan,
                    "score_at_exit": {
                        "direction": exit_score.get("direction"),
                        "technical": exit_score.get("technical"),
                        "combined": exit_score.get("combined"),
                        "breakdown_threshold": round(exit_threshold, 4),
                        "confirmation_points": exit_decision.get(
                            "confirmation_points"
                        ),
                    },
                    "same_bar_conflict": bool(
                        exit_decision.get("same_bar_conflict")
                    ),
                    "cost_breakdown": costs,
                },
            )
        )
        generated += 1
    return generated


def _historical_news_snapshot(
    news: Sequence[Mapping[str, Any]],
    *,
    direction: str,
    signal_at_seconds: int,
    minimum_score: float,
    minimum_mentions: int,
    lookback_seconds: int,
) -> list[dict[str, Any]]:
    """Freeze the same minimum-mention news aggregate available at decision time."""

    snapshot = [
        dict(item)
        for item in news
        if str(item.get("direction") or "") == direction
        and float(item.get("score") or 0) >= minimum_score
        and signal_at_seconds - lookback_seconds
        <= int(item.get("ts") or 0)
        <= signal_at_seconds
    ]
    unique_ids = {str(item.get("id") or "") for item in snapshot if item.get("id")}
    if len(unique_ids) < minimum_mentions:
        return []
    snapshot.sort(
        key=lambda item: (float(item.get("score") or 0), int(item.get("ts") or 0)),
        reverse=True,
    )
    return snapshot


def _historical_indicator_snapshot(
    scan: Mapping[str, Any], configured_keys: Sequence[str], direction: str
) -> dict[str, Any]:
    by_key = {str(item.get("key")): item for item in list(scan.get("items") or [])}
    available: list[str] = []
    unavailable: list[str] = []
    items: list[dict[str, Any]] = []
    for key in configured_keys:
        item = by_key.get(key)
        if item is None:
            unavailable.append(key)
            continue
        status = str(item.get("status") or "")
        strength = item.get("bearish_strength" if direction == "short" else "bullish_strength")
        triggered = item.get(
            "bearish_triggered" if direction == "short" else "bullish_triggered"
        )
        is_available = bool(
            item.get("available", True) is not False
            and status not in {"insufficient", "unavailable"}
            and strength is not None
        )
        if not is_available:
            unavailable.append(key)
            continue
        numeric = min(100.0, max(0.0, float(strength)))
        available.append(key)
        items.append(
            {
                "key": key,
                "triggered": triggered,
                "matched": triggered is True,
                "available": True,
                "group": ai_monitor.indicator_group(key),
                "strength": round(numeric, 4),
                "status": status,
            }
        )
    policy = ai_monitor.configured_indicator_policy(items)
    return {
        "score": float(policy["technical_score"]),
        "policy_passed": bool(policy["passed"]),
        "policy": policy,
        "available_keys": available,
        "unavailable_keys": unavailable,
        "items": items,
        "source": "closed_binance_klines",
        "prediction_feature_proxy_used": False,
    }


def replay_readiness_report(
    db: Session, user_id: int, *, run_id: int | None = None
) -> dict[str, Any]:
    run = (
        db.get(AiMonitorReplayRun, run_id)
        if run_id is not None
        else db.scalar(
            select(AiMonitorReplayRun)
            .where(AiMonitorReplayRun.user_id == user_id, AiMonitorReplayRun.status == "completed")
            .order_by(AiMonitorReplayRun.completed_at.desc(), AiMonitorReplayRun.id.desc())
        )
    )
    if run is None or run.user_id != user_id:
        return {
            "available": False,
            "quantitative_ready": False,
            "passed_count": 0,
            "total_count": 8,
            "criteria": [],
            "note": "尚未完成独立历史回放；实时预测样本不能替代样本外回放。",
        }
    rows = db.execute(
        select(AiMonitorReplayOutcome, AiMonitorReplaySignal)
        .join(AiMonitorReplaySignal, AiMonitorReplaySignal.id == AiMonitorReplayOutcome.signal_id)
        .where(
            AiMonitorReplayOutcome.run_id == run.id,
            AiMonitorReplayOutcome.user_id == user_id,
            AiMonitorReplaySignal.run_id == run.id,
            AiMonitorReplaySignal.user_id == user_id,
            AiMonitorReplayOutcome.sample_split == "oos",
        )
        .order_by(AiMonitorReplaySignal.signal_at)
    ).all()
    returns = [float(outcome.net_directional_return_bps) for outcome, _ in rows]
    count = len(rows)
    calibration = ai_monitor.edge_calibration_summary(returns, 1000)
    lower = calibration["lower_bound_bps"]
    span = (
        (rows[-1][1].signal_at - rows[0][1].signal_at).total_seconds() / 86_400
        if count >= 2
        else 0.0
    )
    profit = sum(max(value, 0.0) for value in returns)
    loss = abs(sum(min(value, 0.0) for value in returns))
    factor = profit / loss if loss > 0 else None
    doubled = [
        float(outcome.gross_directional_return_bps) - float(outcome.estimated_cost_bps) * 2
        for outcome, _ in rows
    ]
    double_average = sum(doubled) / count if count else None
    directions = {
        "long": sum(signal.direction == "long" for _, signal in rows),
        "short": sum(signal.direction == "short" for _, signal in rows),
    }
    months: dict[str, list[float]] = {}
    symbols: dict[str, float] = {}
    for (_outcome, signal), value in zip(rows, returns, strict=True):
        months.setdefault(signal.signal_at.strftime("%Y-%m"), []).append(value)
        symbols[signal.symbol] = symbols.get(signal.symbol, 0.0) + max(value, 0.0)
    positive_months = sum(sum(values) > 0 for values in months.values())
    concentration = (
        max(symbols.values(), default=0.0) / sum(symbols.values()) * 100
        if sum(symbols.values()) > 0
        else None
    )
    criteria = [
        _criterion("oos_sample_count", "样本外去重信号", count >= 1000, count, "≥ 1,000 条"),
        _criterion("history_span", "样本外时间跨度", span >= 180, round(span, 2), "≥ 180 天"),
        _criterion("confidence_lower", "样本外净收益 95% 下限", lower is not None and lower > 0, lower, "> 0 bps"),
        _criterion("profit_factor", "样本外成本后利润因子", factor is not None and factor >= 1.2, round(factor, 4) if factor is not None else None, "≥ 1.20"),
        _criterion("double_cost", "双倍强制成本压力收益", double_average is not None and double_average > 0, round(double_average, 4) if double_average is not None else None, "> 0 bps"),
        _criterion("positive_months", "样本外正收益月份", positive_months >= 5, positive_months, "≥ 5 个月"),
        _criterion("both_directions", "样本外多空覆盖", directions["long"] >= 100 and directions["short"] >= 100, f"多 {directions['long']} / 空 {directions['short']}", "各 ≥ 100 条"),
        _criterion("concentration", "样本外单品种盈利集中度", concentration is not None and concentration <= 20, round(concentration, 2) if concentration is not None else None, "≤ 20%"),
    ]
    return {
        "available": True,
        "run_id": run.public_id,
        "quantitative_ready": all(item["passed"] for item in criteria),
        "passed_count": sum(item["passed"] for item in criteria),
        "total_count": len(criteria),
        "criteria": criteria,
        "oos_summary": {
            "sample_count": count,
            "average_net_return_bps": round(sum(returns) / count, 4) if count else None,
            "long_count": directions["long"],
            "short_count": directions["short"],
            "positive_months": positive_months,
        },
        "cost_model": dict(run.cost_model_json or {}),
        "note": "仅使用样本外结果，并强制计入保守手续费、滑点与资金成本。",
    }


def _criterion(key: str, label: str, passed: bool, current: Any, required: str) -> dict[str, Any]:
    return {"key": key, "label": label, "passed": passed, "current": current, "required": required}


def _path_metrics(
    candles: Sequence[Mapping[str, Any]],
    entry: float,
    direction: str,
    *,
    terminal_price: float | None = None,
) -> tuple[float, float]:
    highs = [entry, *(float(item["high"]) for item in candles)]
    lows = [entry, *(float(item["low"]) for item in candles)]
    if terminal_price is not None and terminal_price > 0:
        highs.append(terminal_price)
        lows.append(terminal_price)
    if direction == "long":
        favorable = (max(highs) / entry - 1) * 10_000
        adverse = (min(lows) / entry - 1) * 10_000
    else:
        favorable = (1 - min(lows) / entry) * 10_000
        adverse = (1 - max(highs) / entry) * 10_000
    return favorable, adverse


def _sample_split(signal_at: datetime, oos_start: datetime, timeframe: str) -> str:
    embargo = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * 2)
    if signal_at >= oos_start:
        return "oos"
    if signal_at >= oos_start - embargo:
        return "embargo"
    return "train"


def _months(start_at: datetime, end_at: datetime) -> list[tuple[int, int]]:
    cursor = datetime(start_at.year, start_at.month, 1)
    end_month = datetime(end_at.year, end_at.month, 1)
    result: list[tuple[int, int]] = []
    while cursor <= end_month:
        result.append((cursor.year, cursor.month))
        cursor = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return result


def _ms_datetime(value: Any) -> datetime:
    raw = int(value)
    if raw < 10_000_000_000:
        raw *= 1000
    return datetime.fromtimestamp(raw / 1000, UTC).replace(tzinfo=None)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _json_safe(value: Any) -> Any:
    """Freeze ORM-derived snapshots using JSON-native scalar values only."""

    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            default=lambda item: (
                item.isoformat()
                if isinstance(item, (datetime,))
                else float(item)
                if isinstance(item, Decimal)
                else str(item)
            ),
        )
    )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, HistoricalReplayError):
        return str(exc)[:500]
    if isinstance(exc, urllib.error.HTTPError):
        return f"official archive request failed: HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "official archive request failed: network unavailable"
    return f"historical replay failed ({type(exc).__name__})"
