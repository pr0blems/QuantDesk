"""Incremental closed-bar ingestion for running Martingale TP4 Shadow deployments."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .application.martingale_tp4.runtime import timeframe_milliseconds
from .domain.martingale_tp4 import strategy_parameters_from_catalog_parameters
from .models import AdminSetting, StrategyDeployment, StrategyRevision
from .tiger_market_data import (
    TigerBarClient,
    TigerMarketDataError,
    TigerMarketDataRepository,
    VerifiedMarketLink,
    closed_tiger_bars,
    evaluate_bar_quality,
    resolve_verified_contract_market_link,
)

BAR_SYNC_STATUS_KEY = "market_data:martingale_tp4:tiger_bar_sync:v1"
SIGNAL_WARMUP_BARS = 1_000
DAILY_MINIMUM_BARS = 60
MAX_STREAMS_PER_CYCLE = 8


@dataclass(frozen=True, slots=True)
class TigerBarStreamRequirement:
    security_id: int
    symbol: str
    timeframe: str
    trade_session: str
    adjustment: str
    expected_bars: int

    @property
    def identity(self) -> tuple[int, str, str, str, str]:
        return (
            self.security_id,
            self.symbol,
            self.timeframe,
            self.trade_session,
            self.adjustment,
        )


@dataclass(frozen=True, slots=True)
class TigerBarStreamSyncResult:
    symbol: str
    timeframe: str
    fetched_bars: int
    closed_bars: int
    stored_rows: int
    quality_status: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TigerBarDiscovery:
    requirements: tuple[TigerBarStreamRequirement, ...]
    deployment_count: int
    invalid_deployment_count: int
    unverified_contract_count: int


def _save_sync_summary(engine: Engine, summary: dict[str, Any]) -> None:
    with Session(engine) as db:
        row = db.get(AdminSetting, BAR_SYNC_STATUS_KEY)
        if row is None:
            db.add(
                AdminSetting(
                    key=BAR_SYNC_STATUS_KEY,
                    value_json=summary,
                    version=1,
                )
            )
        else:
            row.value_json = summary
            row.version += 1
        db.commit()


def record_bar_sync_unavailable(engine: Engine, *, category: str) -> None:
    """Persist a redacted startup reason when automatic ingestion is disabled."""

    _save_sync_summary(
        engine,
        {
            "state": "disabled",
            "category": category,
            "deployment_count": 0,
            "stream_count": 0,
            "attempted_count": 0,
            "deferred_count": 0,
            "invalid_deployment_count": 0,
            "unverified_contract_count": 0,
            "blocked_quality_count": 0,
            "results": [],
            "failures": [],
            "synced_at": datetime.now(UTC).isoformat(),
            "network_writes": 0,
        },
    )


def _timeframe_delta(timeframe: str) -> timedelta:
    if timeframe == "1d":
        return timedelta(days=1)
    return timedelta(milliseconds=timeframe_milliseconds(timeframe))


def stream_poll_seconds(timeframe: str) -> float:
    if timeframe == "1d":
        return 3_600.0
    duration = _timeframe_delta(timeframe).total_seconds()
    return max(30.0, min(300.0, duration / 2))


def incremental_begin_at(
    requirement: TigerBarStreamRequirement,
    *,
    latest_open_time: int | None,
    now: datetime,
) -> datetime:
    """Choose an overlapping incremental window or a bounded initial warmup."""

    delta = _timeframe_delta(requirement.timeframe)
    if latest_open_time is not None:
        latest = datetime.fromtimestamp(latest_open_time / 1000, tz=UTC)
        return latest - delta
    calendar_multiplier = 4 if requirement.timeframe == "1d" else 10
    return now - delta * requirement.expected_bars * calendar_multiplier


def requirements_for_link(
    snapshot: Mapping[str, Any], link: VerifiedMarketLink
) -> tuple[TigerBarStreamRequirement, ...]:
    raw_parameters = snapshot.get("parameters")
    if snapshot.get("engine_key") != "martingale_tp4" or not isinstance(
        raw_parameters, Mapping
    ):
        raise ValueError("invalid martingale_tp4 revision")
    parameters = strategy_parameters_from_catalog_parameters(raw_parameters)
    common = {
        "security_id": link.security_id,
        "symbol": link.underlying_symbol,
        "trade_session": "regular",
        "adjustment": "none",
    }
    requirements = [
        TigerBarStreamRequirement(
            **common,
            timeframe=parameters.box.timeframe,
            expected_bars=max(SIGNAL_WARMUP_BARS, parameters.box.length + 2),
        )
    ]
    if parameters.box.auto_range:
        requirements.append(
            TigerBarStreamRequirement(
                **common,
                timeframe="1d",
                expected_bars=max(
                    DAILY_MINIMUM_BARS, parameters.box.daily_atr_period + 2
                ),
            )
        )
    return tuple(requirements)


def discover_running_shadow_streams(db: Session) -> TigerBarDiscovery:
    rows = db.execute(
        select(StrategyDeployment, StrategyRevision)
        .join(
            StrategyRevision,
            StrategyRevision.id == StrategyDeployment.strategy_revision_id,
        )
        .where(
            StrategyDeployment.mode == "shadow",
            StrategyDeployment.status == "running",
        )
    ).all()
    required: dict[
        tuple[int, str, str, str, str], TigerBarStreamRequirement
    ] = {}
    deployments = 0
    invalid = 0
    unverified_contracts: set[str] = set()
    for deployment, revision in rows:
        snapshot = dict(revision.snapshot_json or {})
        if snapshot.get("engine_key") != "martingale_tp4":
            continue
        deployments += 1
        symbols = {
            str(value).strip().upper()
            for value in (deployment.universe_override_json or {}).get("symbols", [])
            if str(value).strip()
        }
        if not symbols:
            invalid += 1
            continue
        for contract_symbol in symbols:
            link = resolve_verified_contract_market_link(
                db, contract_symbol=contract_symbol
            )
            if link is None:
                unverified_contracts.add(contract_symbol)
                continue
            try:
                streams = requirements_for_link(snapshot, link)
            except ValueError:
                invalid += 1
                break
            for stream in streams:
                previous = required.get(stream.identity)
                if previous is None or stream.expected_bars > previous.expected_bars:
                    required[stream.identity] = stream
    return TigerBarDiscovery(
        requirements=tuple(
            sorted(
                required.values(),
                key=lambda item: (item.symbol, item.timeframe, item.trade_session),
            )
        ),
        deployment_count=deployments,
        invalid_deployment_count=invalid,
        unverified_contract_count=len(unverified_contracts),
    )


class MartingaleTigerBarSync:
    """Own source-qualified Tiger ingestion without any trading side effect."""

    def __init__(
        self,
        engine: Engine,
        client: TigerBarClient,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.engine = engine
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self._next_due: dict[tuple[int, str, str, str, str], float] = {}

    def sync_stream(
        self, requirement: TigerBarStreamRequirement, *, now: datetime
    ) -> TigerBarStreamSyncResult:
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        now_ms = int(now.timestamp() * 1000)
        with Session(self.engine) as db:
            repository = TigerMarketDataRepository(db)
            latest = repository.latest_open_time(
                symbol=requirement.symbol,
                timeframe=requirement.timeframe,
                trade_session=requirement.trade_session,
                adjustment=requirement.adjustment,
            )
            begin_at = incremental_begin_at(
                requirement,
                latest_open_time=latest,
                now=now,
            )
            requested = self.client.bars(
                requirement.symbol,
                timeframe=requirement.timeframe,
                begin_at=begin_at,
                end_at=now,
                trade_session=requirement.trade_session,
                adjustment=requirement.adjustment,
                total=min(100_000, requirement.expected_bars + 20),
                page_size=min(1_200, requirement.expected_bars + 20),
            )
            closed = closed_tiger_bars(requested, cutoff=now)
            stored = repository.upsert_bars(
                closed, security_id=requirement.security_id
            )
            recent = repository.load_latest_bars(
                symbol=requirement.symbol,
                timeframe=requirement.timeframe,
                trade_session=requirement.trade_session,
                adjustment=requirement.adjustment,
                end_time=now_ms,
                limit=requirement.expected_bars,
            )
            maximum_age = (
                4 * 86_400
                if requirement.timeframe == "1d"
                else int(_timeframe_delta(requirement.timeframe).total_seconds() * 2)
            )
            quality = evaluate_bar_quality(
                recent,
                symbol=requirement.symbol,
                timeframe=requirement.timeframe,
                trade_session=requirement.trade_session,
                adjustment=requirement.adjustment,
                expected_bars=requirement.expected_bars,
                maximum_age_seconds=maximum_age,
                now=now,
            )
            repository.save_quality(quality)
            db.commit()
        return TigerBarStreamSyncResult(
            symbol=requirement.symbol,
            timeframe=requirement.timeframe,
            fetched_bars=len(requested),
            closed_bars=len(closed),
            stored_rows=stored,
            quality_status=quality.status,
            reason_codes=quality.reason_codes,
        )

    def sync_once(self) -> dict[str, Any]:
        now = self.clock()
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        with Session(self.engine) as db:
            discovery = discover_running_shadow_streams(db)
        active_keys = {item.identity for item in discovery.requirements}
        self._next_due = {
            key: due for key, due in self._next_due.items() if key in active_keys
        }
        tick = self.monotonic()
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        deferred = 0
        attempted = 0
        for requirement in discovery.requirements:
            if tick < self._next_due.get(requirement.identity, 0):
                deferred += 1
                continue
            if attempted >= MAX_STREAMS_PER_CYCLE:
                deferred += 1
                continue
            attempted += 1
            self._next_due[requirement.identity] = tick + stream_poll_seconds(
                requirement.timeframe
            )
            try:
                result = self.sync_stream(requirement, now=now)
                results.append(asdict(result))
            except TigerMarketDataError as exc:
                failures.append(
                    {
                        "symbol": requirement.symbol,
                        "timeframe": requirement.timeframe,
                        "category": exc.category,
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "symbol": requirement.symbol,
                        "timeframe": requirement.timeframe,
                        "category": type(exc).__name__,
                    }
                )
        blocked_quality = sum(
            item.get("quality_status") != "usable" for item in results
        )
        summary: dict[str, Any] = {
            "state": (
                "degraded"
                if failures
                or blocked_quality
                or discovery.invalid_deployment_count
                or discovery.unverified_contract_count
                else "healthy"
                if discovery.requirements
                else "idle"
            ),
            "deployment_count": discovery.deployment_count,
            "stream_count": len(discovery.requirements),
            "attempted_count": len(results) + len(failures),
            "deferred_count": deferred,
            "invalid_deployment_count": discovery.invalid_deployment_count,
            "unverified_contract_count": discovery.unverified_contract_count,
            "blocked_quality_count": blocked_quality,
            "results": results,
            "failures": failures,
            "synced_at": now.isoformat(),
            "network_writes": 0,
        }
        _save_sync_summary(self.engine, summary)
        return summary


def start_martingale_tiger_bar_sync_loop(
    engine: Engine,
    client: TigerBarClient,
    *,
    interval_seconds: float = 30.0,
) -> threading.Event:
    if interval_seconds < 1:
        raise ValueError("bar sync interval must be at least one second")
    stop_event = threading.Event()
    service = MartingaleTigerBarSync(engine, client)

    def run() -> None:
        while not stop_event.is_set():
            try:
                service.sync_once()
            except Exception as exc:
                print(
                    "[martingale-tiger-bars] cycle failed: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                )
            stop_event.wait(interval_seconds)

    threading.Thread(
        target=run,
        daemon=True,
        name="martingale-tiger-bars",
    ).start()
    return stop_event
