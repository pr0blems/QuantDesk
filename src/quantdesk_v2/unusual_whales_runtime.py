"""Application-owned Unusual Whales stream and batched persistence runtime."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .ai_monitor import ingest_market_stream_events
from .market_risk_sync import sync_economic_calendar
from .models import (
    MarketStreamEvent,
    OpportunityGateDecision,
    OpportunityMarketSnapshot,
    RealtimeMarketFeatureSnapshot,
)
from .unusual_whales import UnusualWhalesMarketClient
from .unusual_whales_stream import UnusualWhalesStreamClient, UnusualWhalesStreamEvent

DEFAULT_CHANNEL_FLAGS: dict[str, bool] = {
    "price": True,
    "trading_halts": True,
    "interval_flow": True,
    "net_flow": True,
    "market_tide": True,
    "gex": True,
    "lit_trades": True,
    "off_lit_trades": True,
    "flow_alerts": True,
    "option_trades": False,
}

_CHANNEL_NAMES = {
    "price": "price",
    "trading_halts": "trading_halts",
    "interval_flow": "interval_flow",
    "market_tide": "market_tide",
    "gex": "gex",
    "lit_trades": "lit_trades",
    "off_lit_trades": "off_lit_trades",
    "flow_alerts": "flow-alerts",
    "option_trades": "option_trades",
}

_GLOBAL_REST_CHANNELS = frozenset({"market_tide", "vix_term_structure"})
_RUNTIME_LEADER_LOCK_NAME = "quantdesk-unusual-whales-runtime"
_PROCESS_LEADERS_LOCK = Lock()
_PROCESS_LEADERS: set[str] = set()

DEFAULT_RAW_EVENT_RETENTION_DAYS = 14
DEFAULT_FEATURE_RETENTION_DAYS = 90
DEFAULT_RETENTION_CLEANUP_SECONDS = 60 * 60
DEFAULT_RETENTION_BATCH_SIZE = 2_000
DEFAULT_RETENTION_MAX_BATCHES = 10


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        return None
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return round(ordered[position], 2)


def _delete_id_batch(
    db: Session,
    model: type[MarketStreamEvent] | type[RealtimeMarketFeatureSnapshot],
    ids: list[int],
) -> int:
    if not ids:
        return 0
    result = db.execute(delete(model).where(model.id.in_(ids)))
    return max(0, int(result.rowcount or 0))


def cleanup_market_data_retention(
    engine: Engine,
    *,
    now: datetime | None = None,
    raw_event_days: int = DEFAULT_RAW_EVENT_RETENTION_DAYS,
    feature_snapshot_days: int = DEFAULT_FEATURE_RETENTION_DAYS,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    max_batches: int = DEFAULT_RETENTION_MAX_BATCHES,
) -> dict[str, int]:
    """Prune replayable UW data without touching frozen decisions or evidence.

    Raw provider events are the highest-volume tier and therefore have the
    shortest default lifetime. Minute feature snapshots live longer. Features
    referenced by an immutable opportunity snapshot or gate-decision audit are
    excluded rather than relying on ``ON DELETE SET NULL``; this preserves the
    exact source feature as well as the already-frozen JSON evidence.
    """

    cleanup_now = _utc_naive(now or datetime.now(UTC))
    raw_cutoff = cleanup_now - timedelta(days=max(1, int(raw_event_days)))
    feature_cutoff = cleanup_now - timedelta(days=max(1, int(feature_snapshot_days)))
    bounded_batch = max(100, min(20_000, int(batch_size)))
    bounded_batches = max(1, min(100, int(max_batches)))
    deleted_events = 0
    deleted_features = 0

    for _ in range(bounded_batches):
        with Session(engine, expire_on_commit=False) as db:
            ids = list(
                db.scalars(
                    select(MarketStreamEvent.id)
                    .where(MarketStreamEvent.event_time < raw_cutoff)
                    .order_by(MarketStreamEvent.id)
                    .limit(bounded_batch)
                )
            )
            deleted = _delete_id_batch(db, MarketStreamEvent, ids)
            db.commit()
        deleted_events += deleted
        if len(ids) < bounded_batch:
            break

    protected_by_snapshot = select(OpportunityMarketSnapshot.id).where(
        OpportunityMarketSnapshot.market_feature_snapshot_id
        == RealtimeMarketFeatureSnapshot.id
    )
    protected_by_decision = select(OpportunityGateDecision.id).where(
        OpportunityGateDecision.market_feature_snapshot_id
        == RealtimeMarketFeatureSnapshot.id
    )
    feature_eligible = (
        RealtimeMarketFeatureSnapshot.captured_at < feature_cutoff,
        ~protected_by_snapshot.exists(),
        ~protected_by_decision.exists(),
    )
    for _ in range(bounded_batches):
        with Session(engine, expire_on_commit=False) as db:
            ids = list(
                db.scalars(
                    select(RealtimeMarketFeatureSnapshot.id)
                    .where(*feature_eligible)
                    .order_by(RealtimeMarketFeatureSnapshot.id)
                    .limit(bounded_batch)
                )
            )
            deleted = _delete_id_batch(db, RealtimeMarketFeatureSnapshot, ids)
            db.commit()
        deleted_features += deleted
        if len(ids) < bounded_batch:
            break

    with Session(engine) as db:
        event_backlog = int(
            db.scalar(
                select(func.count())
                .select_from(MarketStreamEvent)
                .where(MarketStreamEvent.event_time < raw_cutoff)
            )
            or 0
        )
        feature_backlog = int(
            db.scalar(
                select(func.count())
                .select_from(RealtimeMarketFeatureSnapshot)
                .where(*feature_eligible)
            )
            or 0
        )
        protected_features = int(
            db.scalar(
                select(func.count())
                .select_from(RealtimeMarketFeatureSnapshot)
                .where(
                    RealtimeMarketFeatureSnapshot.captured_at < feature_cutoff,
                    (protected_by_snapshot.exists() | protected_by_decision.exists()),
                )
            )
            or 0
        )
    return {
        "deleted_events": deleted_events,
        "deleted_features": deleted_features,
        "event_backlog": event_backlog,
        "feature_backlog": feature_backlog,
        "protected_features": protected_features,
    }


def _channel_contract_key(channel: str) -> str:
    """Compare vendor contract names without conflating ticker membership."""

    return str(channel or "").strip().lower().partition(":")[0].replace("-", "_")


def validate_stream_subscriptions(
    requested: Iterable[str],
    official_channels: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return contract-supported subscriptions and explicit omissions.

    ``GET /api/socket`` reports base channel capabilities while the websocket
    membership protocol can add a ticker suffix (for example ``price:AAPL``).
    Hyphen/underscore aliases are normalized only for comparison; the outbound
    name remains the allowlisted name produced by ``stream_subscriptions``.
    """

    desired = tuple(sorted(set(str(item).strip() for item in requested if str(item).strip())))
    official_keys = {
        key
        for item in official_channels
        if (key := _channel_contract_key(str(item)))
    }
    accepted = tuple(item for item in desired if _channel_contract_key(item) in official_keys)
    missing = tuple(item for item in desired if _channel_contract_key(item) not in official_keys)
    return accepted, missing


def _underlying_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    return normalized.removesuffix("USDT")


def _symbol_key(symbol: str | None) -> str:
    """Match mapped contracts and vendor tickers without punctuation differences."""

    underlying = _underlying_symbol(str(symbol or ""))
    return "".join(character for character in underlying if character.isalnum())


def stream_subscriptions(
    channel_flags: Mapping[str, Any] | None,
    symbols: Iterable[str],
) -> tuple[str, ...]:
    """Translate stable admin keys into validated vendor subscription names."""

    flags = {**DEFAULT_CHANNEL_FLAGS, **dict(channel_flags or {})}
    subscriptions = {
        vendor_name
        for key, vendor_name in _CHANNEL_NAMES.items()
        if bool(flags.get(key))
    }
    if bool(flags.get("net_flow")):
        subscriptions.update(
            f"net_flow:{underlying}"
            for symbol in symbols
            if (underlying := _underlying_symbol(symbol))
        )
    return tuple(sorted(subscriptions))


class UnusualWhalesRuntime:
    """Keep websocket callbacks fast and persist canonical events in bounded batches."""

    def __init__(
        self,
        engine: Engine,
        api_key_loader: Callable[[], str],
        symbols: Iterable[str],
        *,
        channel_flags: Mapping[str, Any] | None = None,
        websocket_enabled: bool = True,
        rest_client: UnusualWhalesMarketClient | None = None,
        rest_enabled: bool = True,
        calendar_poll_seconds: float = 15 * 60,
        recovery_poll_seconds: float = 5 * 60,
        channel_contract_poll_seconds: float = 6 * 60 * 60,
        rest_snapshot_symbol_limit: int = 48,
        rest_detail_symbol_limit: int = 8,
        event_block_before_minutes: int = 30,
        event_block_after_minutes: int = 15,
        batch_size: int = 100,
        flush_seconds: float = 0.5,
        queue_size: int = 50_000,
        channel_stale_ms: int = 120_000,
        leadership_poll_seconds: float = 5.0,
        raw_event_retention_days: int = DEFAULT_RAW_EVENT_RETENTION_DAYS,
        feature_retention_days: int = DEFAULT_FEATURE_RETENTION_DAYS,
        retention_cleanup_seconds: float = DEFAULT_RETENTION_CLEANUP_SECONDS,
        retention_batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
        retention_max_batches: int = DEFAULT_RETENTION_MAX_BATCHES,
        market_open_checker: Callable[[], bool] | None = None,
        market_open_only: bool = True,
    ) -> None:
        self.engine = engine
        self._channel_flags = dict(channel_flags or {})
        self.symbols = tuple(str(item).strip().upper() for item in symbols if str(item).strip())
        self._symbol_keys = frozenset(
            key for item in self.symbols if (key := _symbol_key(item))
        )
        self.batch_size = max(1, min(1_000, int(batch_size)))
        self.flush_seconds = max(0.05, min(5.0, float(flush_seconds)))
        self.channel_stale_ms = max(1_000, int(channel_stale_ms))
        self.websocket_enabled = bool(websocket_enabled)
        self.rest_client = rest_client
        self.rest_enabled = bool(rest_enabled)
        self.calendar_poll_seconds = max(60.0, float(calendar_poll_seconds))
        self.recovery_poll_seconds = max(15.0, float(recovery_poll_seconds))
        self.channel_contract_poll_seconds = max(
            5 * 60.0, float(channel_contract_poll_seconds)
        )
        self.rest_snapshot_symbol_limit = max(1, min(250, int(rest_snapshot_symbol_limit)))
        self.rest_detail_symbol_limit = max(0, min(50, int(rest_detail_symbol_limit)))
        self.event_block_before_minutes = max(0, int(event_block_before_minutes))
        self.event_block_after_minutes = max(0, int(event_block_after_minutes))
        self.leadership_poll_seconds = max(0.1, float(leadership_poll_seconds))
        self.raw_event_retention_days = max(1, int(raw_event_retention_days))
        self.feature_retention_days = max(1, int(feature_retention_days))
        self.retention_cleanup_seconds = max(60.0, float(retention_cleanup_seconds))
        self.retention_batch_size = max(100, min(20_000, int(retention_batch_size)))
        self.retention_max_batches = max(1, min(100, int(retention_max_batches)))
        self.market_open_checker = market_open_checker or (lambda: True)
        self.market_open_only = bool(market_open_only)
        self._collection_active = False
        self._collection_last_changed_at_ms: int | None = None
        self._queue: Queue[UnusualWhalesStreamEvent] = Queue(maxsize=max(100, queue_size))
        self._stop = Event()
        self._worker_stop = Event()
        self._poll_wakeup = Event()
        self._contract_refresh = Event()
        self._writer: Thread | None = None
        self._rest_poller: Thread | None = None
        self._retention_worker: Thread | None = None
        self._leadership_worker: Thread | None = None
        self._lock = Lock()
        self._leader_connection: Connection | None = None
        self._leader_owner_id = uuid.uuid4().hex
        self._leader_registry_key = hashlib.sha256(
            str(engine.url.render_as_string(hide_password=True)).encode()
        ).hexdigest()
        self._leader_acquired = False
        self._leader_mode = "uninitialized"
        self._leader_acquired_at_ms: int | None = None
        self._leader_last_checked_at_ms: int | None = None
        self._leader_transitions = 0
        self._leader_error: str | None = None
        self._channel_health: dict[str, dict[str, Any]] = {}
        self._persisted = 0
        self._persist_duplicates = 0
        self._dropped = 0
        self._filtered = 0
        self._write_errors = 0
        self._last_write_at_ms: int | None = None
        self._last_write_error: str | None = None
        self._write_latencies_ms: deque[float] = deque(maxlen=256)
        self._persist_rate_samples: deque[tuple[int, int]] = deque(maxlen=2_048)
        self._rest_polls = 0
        self._rest_errors = 0
        self._last_rest_at_ms: int | None = None
        self._last_rest_error: str | None = None
        self._calendar_rows = 0
        self._recovery_cursor = 0
        self._recovery_runs = 0
        self._recovery_events = 0
        self._recovery_quote_rows = 0
        self._recovery_state_rows = 0
        self._recovery_detail_rows = 0
        self._last_recovery_at_ms: int | None = None
        self._last_recovery_reason: str | None = None
        self._retention_runs = 0
        self._retention_errors = 0
        self._retention_deleted_events = 0
        self._retention_deleted_features = 0
        self._retention_event_backlog = 0
        self._retention_feature_backlog = 0
        self._retention_protected_features = 0
        self._retention_last_run_at_ms: int | None = None
        self._retention_last_duration_ms: int | None = None
        self._retention_last_error: str | None = None
        self._requested_subscriptions = set(
            stream_subscriptions(channel_flags, self.symbols)
        )
        self._channel_contract: dict[str, Any] = {
            "status": "unchecked",
            "verified": False,
            "checked_at_ms": None,
            "official_channels": [],
            "requested": sorted(self._requested_subscriptions),
            "active": sorted(self._requested_subscriptions),
            "missing": [],
            "last_error": None,
        }
        self.stream = UnusualWhalesStreamClient(
            api_key_loader,
            self.on_event,
            channels=tuple(sorted(self._requested_subscriptions)),
        )

    def replace_symbols(self, symbols: Iterable[str]) -> bool:
        """Refresh filtering and symbol-scoped subscriptions without a restart."""

        updated = tuple(
            dict.fromkeys(
                str(item).strip().upper() for item in symbols if str(item).strip()
            )
        )
        if updated == self.symbols:
            return False
        with self._lock:
            self.symbols = updated
            self._symbol_keys = frozenset(
                key for item in updated if (key := _symbol_key(item))
            )
            channel_flags = dict(self._channel_flags)
        self.apply_config(
            channel_flags,
            websocket_enabled=self.websocket_enabled,
            rest_enabled=self.rest_enabled,
        )
        return True

    def start(self) -> None:
        with self._lock:
            if self._leadership_worker is not None and self._leadership_worker.is_alive():
                return
            self._stop.clear()
            self._worker_stop.set()
        # Attempt synchronously so the first worker starts collecting without
        # waiting for the watchdog interval. Other workers remain hot standbys.
        if self._try_acquire_leadership():
            self._activate_leader()
        with self._lock:
            self._leadership_worker = Thread(
                target=self._leadership_loop,
                daemon=True,
                name="unusual-whales-leadership",
            )
            self._leadership_worker.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stop.set()
        self._worker_stop.set()
        self._poll_wakeup.set()
        self.stream.stop(join_timeout=min(3.0, max(0.0, join_timeout)))
        writer = self._writer
        rest_poller = self._rest_poller
        retention_worker = self._retention_worker
        leadership_worker = self._leadership_worker
        if writer is not None:
            writer.join(timeout=max(0.0, join_timeout))
        if rest_poller is not None:
            rest_poller.join(timeout=max(0.0, join_timeout))
        if retention_worker is not None:
            retention_worker.join(timeout=max(0.0, join_timeout))
        if leadership_worker is not None:
            leadership_worker.join(timeout=max(0.0, join_timeout))
        self._release_leadership()
        with self._lock:
            self._writer = None
            self._rest_poller = None
            self._retention_worker = None
            self._leadership_worker = None

    def _try_acquire_leadership(self) -> bool:
        with self._lock:
            if self._leader_acquired:
                return True
        dialect = str(self.engine.dialect.name).lower()
        connection: Connection | None = None
        try:
            if dialect in {"mysql", "mariadb"}:
                connection = self.engine.connect()
                acquired = bool(
                    connection.execute(
                        text("SELECT GET_LOCK(:name, 0)"),
                        {"name": _RUNTIME_LEADER_LOCK_NAME},
                    ).scalar_one()
                    == 1
                )
                mode = "mysql_advisory_lock"
            elif dialect == "postgresql":
                connection = self.engine.connect()
                lock_key = int(hashlib.sha256(_RUNTIME_LEADER_LOCK_NAME.encode()).hexdigest()[:15], 16)
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    ).scalar_one()
                )
                mode = "postgres_advisory_lock"
            elif dialect == "sqlite":
                with _PROCESS_LEADERS_LOCK:
                    acquired = self._leader_registry_key not in _PROCESS_LEADERS
                    if acquired:
                        _PROCESS_LEADERS.add(self._leader_registry_key)
                mode = "process_local_sqlite_fallback"
            else:
                acquired = False
                mode = "unsupported_database"
            checked_at_ms = int(time.time() * 1_000)
            with self._lock:
                self._leader_mode = mode
                self._leader_last_checked_at_ms = checked_at_ms
                self._leader_error = None if acquired else (
                    "lock_held_by_another_worker"
                    if mode != "unsupported_database"
                    else "unsupported_database"
                )
                if acquired:
                    self._leader_connection = connection
                    self._leader_acquired = True
                    self._leader_acquired_at_ms = checked_at_ms
                    self._leader_transitions += 1
            if not acquired and connection is not None:
                connection.close()
            return acquired
        except (OSError, RuntimeError, SQLAlchemyError, ValueError) as exc:
            if connection is not None:
                connection.close()
            with self._lock:
                self._leader_acquired = False
                self._leader_last_checked_at_ms = int(time.time() * 1_000)
                self._leader_error = type(exc).__name__
            return False

    def _leadership_is_valid(self) -> bool:
        with self._lock:
            acquired = self._leader_acquired
            connection = self._leader_connection
            mode = self._leader_mode
        if not acquired:
            return False
        try:
            if mode == "mysql_advisory_lock":
                if connection is None:
                    return False
                valid = bool(
                    connection.execute(
                        text("SELECT IS_USED_LOCK(:name) = CONNECTION_ID()"),
                        {"name": _RUNTIME_LEADER_LOCK_NAME},
                    ).scalar_one()
                )
            elif mode == "postgres_advisory_lock":
                if connection is None:
                    return False
                connection.execute(text("SELECT 1")).scalar_one()
                valid = True
            elif mode == "process_local_sqlite_fallback":
                with _PROCESS_LEADERS_LOCK:
                    valid = self._leader_registry_key in _PROCESS_LEADERS
            else:
                valid = False
            with self._lock:
                self._leader_last_checked_at_ms = int(time.time() * 1_000)
                self._leader_error = None if valid else "leadership_lost"
            return valid
        except (OSError, RuntimeError, SQLAlchemyError, ValueError):
            with self._lock:
                self._leader_last_checked_at_ms = int(time.time() * 1_000)
                self._leader_error = "leadership_check_failed"
            return False

    def _release_leadership(self) -> None:
        with self._lock:
            connection = self._leader_connection
            mode = self._leader_mode
            was_acquired = self._leader_acquired
            self._leader_connection = None
            self._leader_acquired = False
            self._leader_acquired_at_ms = None
        if mode == "process_local_sqlite_fallback" and was_acquired:
            with _PROCESS_LEADERS_LOCK:
                _PROCESS_LEADERS.discard(self._leader_registry_key)
        if connection is None:
            return
        try:
            if was_acquired and mode == "mysql_advisory_lock":
                connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"),
                    {"name": _RUNTIME_LEADER_LOCK_NAME},
                )
            elif was_acquired and mode == "postgres_advisory_lock":
                lock_key = int(hashlib.sha256(_RUNTIME_LEADER_LOCK_NAME.encode()).hexdigest()[:15], 16)
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
        except SQLAlchemyError:
            pass
        finally:
            connection.close()

    def _activate_leader(self) -> None:
        # Validate the account's actual websocket contract only on the elected
        # collector. A failed/empty check remains fail-open for availability.
        collection_allowed = self._collection_allowed()
        if (
            collection_allowed
            and self.websocket_enabled
            and self.rest_enabled
            and self.rest_client is not None
        ):
            self._refresh_channel_contract()
        with self._lock:
            if self._stop.is_set() or not self._leader_acquired:
                return
            self._worker_stop.clear()
            if self._writer is None or not self._writer.is_alive():
                self._writer = Thread(
                    target=self._writer_loop,
                    daemon=True,
                    name="unusual-whales-writer",
                )
                self._writer.start()
            if self.rest_client is not None and (
                self._rest_poller is None or not self._rest_poller.is_alive()
            ):
                self._rest_poller = Thread(
                    target=self._rest_loop,
                    daemon=True,
                    name="unusual-whales-rest-poller",
                )
                self._rest_poller.start()
            if self._retention_worker is None or not self._retention_worker.is_alive():
                self._retention_worker = Thread(
                    target=self._retention_loop,
                    daemon=True,
                    name="unusual-whales-retention",
                )
                self._retention_worker.start()
        self._set_collection_active(collection_allowed)
        if self.websocket_enabled and collection_allowed:
            self.stream.start()

    def _deactivate_leader(self) -> None:
        self._set_collection_active(False)
        self._worker_stop.set()
        self._poll_wakeup.set()
        self.stream.stop(join_timeout=2.0)
        for worker in (self._writer, self._rest_poller, self._retention_worker):
            if worker is not None and worker is not self._leadership_worker:
                worker.join(timeout=2.0)
        with self._lock:
            self._writer = None
            self._rest_poller = None
            self._retention_worker = None

    def _leadership_loop(self) -> None:
        while not self._stop.wait(self.leadership_poll_seconds):
            with self._lock:
                acquired = self._leader_acquired
            if acquired and not self._leadership_is_valid():
                self._deactivate_leader()
                self._release_leadership()
                acquired = False
            if not acquired and self._try_acquire_leadership():
                self._activate_leader()
                acquired = True
            if acquired:
                self._sync_collection_state()

    def _market_open(self) -> bool:
        if not self.market_open_only:
            return True
        try:
            return bool(self.market_open_checker())
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _collection_allowed(self) -> bool:
        return bool((self.websocket_enabled or self.rest_enabled) and self._market_open())

    def _set_collection_active(self, active: bool) -> None:
        normalized = bool(active)
        with self._lock:
            if normalized == self._collection_active:
                return
            self._collection_active = normalized
            self._collection_last_changed_at_ms = int(time.time() * 1_000)

    def _sync_collection_state(self) -> None:
        allowed = self._collection_allowed()
        with self._lock:
            previous = self._collection_active
            is_leader = self._leader_acquired
        self._set_collection_active(allowed and is_leader)
        if not is_leader or allowed == previous:
            return
        if allowed:
            self._poll_wakeup.set()
            if self.websocket_enabled:
                self.stream.start()
        else:
            self.stream.stop(join_timeout=2.0)
            self._poll_wakeup.set()

    def on_event(self, event: UnusualWhalesStreamEvent) -> None:
        """Record telemetry and enqueue without opening a database transaction."""

        if not self._collection_allowed():
            with self._lock:
                self._filtered += 1
            return

        base_channel = event.channel.partition(":")[0]
        event_symbol_key = _symbol_key(event.symbol)
        in_scope = base_channel in _GLOBAL_REST_CHANNELS or (
            bool(event_symbol_key) and event_symbol_key in self._symbol_keys
        )
        lag_ms = (
            max(0, event.received_at_ms - event.event_time_ms)
            if event.event_time_ms is not None
            else None
        )
        with self._lock:
            current = self._channel_health.setdefault(base_channel, {"received": 0})
            current.update(
                {
                    "status": "live",
                    "fresh": True,
                    "last_event_at_ms": event.received_at_ms,
                    "last_source_event_at_ms": event.event_time_ms,
                    "lag_ms": lag_ms,
                    "last_symbol": event.symbol,
                    "received": int(current.get("received", 0)) + 1,
                }
            )
            if not in_scope:
                self._filtered += 1
                current["filtered"] = int(current.get("filtered", 0)) + 1
                current["status"] = "filtered"
        if not in_scope:
            return
        try:
            self._queue.put_nowait(event)
        except Full:
            with self._lock:
                self._dropped += 1
                current = self._channel_health.setdefault(base_channel, {"received": 0})
                current["dropped"] = int(current.get("dropped", 0)) + 1
                current["status"] = "backpressure"

    def apply_config(
        self,
        channel_flags: Mapping[str, Any],
        *,
        websocket_enabled: bool,
        rest_enabled: bool | None = None,
        thresholds: Mapping[str, Any] | None = None,
        retention: Mapping[str, Any] | None = None,
    ) -> None:
        """Apply subscriptions immediately; reconnect will restore the same set."""

        self._channel_flags = dict(channel_flags)
        requested = set(stream_subscriptions(channel_flags, self.symbols))
        with self._lock:
            self._requested_subscriptions = requested
            contract = dict(self._channel_contract)
        if bool(contract.get("verified")):
            desired = set(
                validate_stream_subscriptions(
                    requested,
                    contract.get("official_channels") or (),
                )[0]
            )
        else:
            desired = requested
        current = set(self.stream.health_snapshot().get("subscriptions") or ())
        for channel in sorted(current - desired):
            self.stream.unsubscribe(channel)
        for channel in sorted(desired - current):
            self.stream.subscribe(channel)
        with self._lock:
            self._channel_contract.update(
                {
                    "requested": sorted(requested),
                    "active": sorted(desired),
                    "missing": sorted(requested - desired),
                }
            )
        was_enabled = self.websocket_enabled
        self.websocket_enabled = bool(websocket_enabled)
        if rest_enabled is not None:
            self.rest_enabled = bool(rest_enabled)
        if thresholds is not None:
            self.event_block_before_minutes = max(
                0,
                int(
                    thresholds.get(
                        "event_block_before_minutes", self.event_block_before_minutes
                    )
                ),
            )
            self.event_block_after_minutes = max(
                0,
                int(
                    thresholds.get(
                        "event_block_after_minutes", self.event_block_after_minutes
                    )
                ),
            )
        if retention is not None:
            self.raw_event_retention_days = max(
                1,
                int(retention.get("raw_event_days", self.raw_event_retention_days)),
            )
            self.feature_retention_days = max(
                1,
                int(
                    retention.get(
                        "feature_snapshot_days", self.feature_retention_days
                    )
                ),
            )
            self.retention_cleanup_seconds = max(
                60.0,
                float(
                    retention.get(
                        "cleanup_interval_minutes",
                        self.retention_cleanup_seconds / 60.0,
                    )
                )
                * 60.0,
            )
            self.retention_batch_size = max(
                100,
                min(
                    20_000,
                    int(
                        retention.get(
                            "cleanup_batch_size", self.retention_batch_size
                        )
                    ),
                ),
            )
            self.retention_max_batches = max(
                1,
                min(
                    100,
                    int(
                        retention.get(
                            "cleanup_max_batches", self.retention_max_batches
                        )
                    ),
                ),
            )
        self._contract_refresh.set()
        self._poll_wakeup.set()
        with self._lock:
            is_leader = self._leader_acquired
        if self.websocket_enabled and not was_enabled and is_leader:
            self._sync_collection_state()
        elif was_enabled and not self.websocket_enabled and is_leader:
            self.stream.stop()
            self._sync_collection_state()

    def channel_health_snapshot(self) -> dict[str, dict[str, Any]]:
        now_ms = int(time.time() * 1_000)
        with self._lock:
            snapshot = {key: dict(value) for key, value in self._channel_health.items()}
        for value in snapshot.values():
            event_time = value.get("last_event_at_ms")
            age_ms = max(0, now_ms - int(event_time)) if event_time is not None else None
            value["age_ms"] = age_ms
            if age_ms is not None and age_ms > self.channel_stale_ms:
                value["fresh"] = False
                value["status"] = "stale"
        return snapshot

    def health_snapshot(self) -> dict[str, Any]:
        stream_health = self.stream.health_snapshot()
        now_ms = int(time.time() * 1_000)
        with self._lock:
            while (
                self._persist_rate_samples
                and self._persist_rate_samples[0][0] < now_ms - 60_000
            ):
                self._persist_rate_samples.popleft()
            events_per_minute = sum(item[1] for item in self._persist_rate_samples)
            latencies = tuple(self._write_latencies_ms)
            writer_health = {
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "queue_utilization": round(
                    self._queue.qsize() / max(1, self._queue.maxsize), 4
                ),
                "persisted": self._persisted,
                "events_per_minute": events_per_minute,
                "duplicates": self._persist_duplicates,
                "dropped": self._dropped,
                "filtered": self._filtered,
                "write_errors": self._write_errors,
                "write_latency_ms": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "p99": _percentile(latencies, 0.99),
                    "samples": len(latencies),
                },
                "last_write_at_ms": self._last_write_at_ms,
                "last_write_error": self._last_write_error,
            }
            leadership_health = {
                "status": "leader" if self._leader_acquired else "standby",
                "is_leader": self._leader_acquired,
                "instance_id": self._leader_owner_id[:12],
                "mode": self._leader_mode,
                "lock_name": _RUNTIME_LEADER_LOCK_NAME,
                "acquired_at_ms": self._leader_acquired_at_ms,
                "last_checked_at_ms": self._leader_last_checked_at_ms,
                "transitions": self._leader_transitions,
                "last_error": self._leader_error,
                "standby_takeover_seconds": self.leadership_poll_seconds,
            }
            retention_health = {
                "status": (
                    "error"
                    if self._retention_last_error
                    else "ready"
                    if self._retention_runs
                    else "pending"
                ),
                "raw_event_days": self.raw_event_retention_days,
                "feature_snapshot_days": self.feature_retention_days,
                "cleanup_interval_seconds": self.retention_cleanup_seconds,
                "batch_size": self.retention_batch_size,
                "max_batches_per_run": self.retention_max_batches,
                "runs": self._retention_runs,
                "errors": self._retention_errors,
                "deleted_events": self._retention_deleted_events,
                "deleted_features": self._retention_deleted_features,
                "event_backlog": self._retention_event_backlog,
                "feature_backlog": self._retention_feature_backlog,
                "protected_features": self._retention_protected_features,
                "last_run_at_ms": self._retention_last_run_at_ms,
                "last_duration_ms": self._retention_last_duration_ms,
                "last_error": self._retention_last_error,
                "protected_tables": [
                    "opportunity_market_snapshots",
                    "opportunity_gate_decisions",
                ],
            }
            rest_health = {
                "status": (
                    "disabled"
                    if not self.rest_enabled
                    else "market_closed"
                    if self.market_open_only and not self._market_open()
                    else "unconfigured"
                    if self.rest_client is None
                    else "degraded"
                    if self._last_rest_error and self._recovery_events > 0
                    else "error"
                    if self._last_rest_error
                    else "ready"
                ),
                "enabled": self.rest_enabled,
                "polls": self._rest_polls,
                "errors": self._rest_errors,
                "last_poll_at_ms": self._last_rest_at_ms,
                "last_error": self._last_rest_error,
                "calendar_rows": self._calendar_rows,
                "recovery_runs": self._recovery_runs,
                "recovery_events": self._recovery_events,
                "recovery_quote_rows": self._recovery_quote_rows,
                "recovery_state_rows": self._recovery_state_rows,
                "recovery_detail_rows": self._recovery_detail_rows,
                "last_recovery_at_ms": self._last_recovery_at_ms,
                "last_recovery_reason": self._last_recovery_reason,
                "snapshot_symbol_limit": self.rest_snapshot_symbol_limit,
                "detail_symbol_limit": self.rest_detail_symbol_limit,
                "channel_contract": dict(self._channel_contract),
            }
            collection_health = {
                "active": self._collection_active,
                "market_open_only": self.market_open_only,
                "market_open": self._market_open(),
                "last_changed_at_ms": self._collection_last_changed_at_ms,
            }
        return {
            **stream_health,
            "leadership": leadership_health,
            "writer": writer_health,
            "retention": retention_health,
            "rest": rest_health,
            "collection": collection_health,
        }

    def _set_stream_subscriptions(self, desired: Iterable[str]) -> None:
        target = set(desired)
        current = set(self.stream.health_snapshot().get("subscriptions") or ())
        for channel in sorted(current - target):
            self.stream.unsubscribe(channel)
        for channel in sorted(target - current):
            self.stream.subscribe(channel)

    def _refresh_channel_contract(self) -> bool:
        """Validate configured memberships against the account's `/api/socket` contract."""

        client = self.rest_client
        if client is None:
            return False
        checked_at_ms = int(time.time() * 1_000)
        try:
            payload = client.websocket_channels()
            official = tuple(
                str(item).strip()
                for item in payload.get("channels", ())
                if str(item).strip()
            )
            official_keys = {_channel_contract_key(item) for item in official}
            known_keys = {
                _channel_contract_key(item) for item in _CHANNEL_NAMES.values()
            } | {"net_flow"}
            # Empty and wholly unknown responses are not authoritative.  Keeping
            # the previous subscriptions is safer than turning a schema drift
            # or temporary entitlement response into a complete feed outage.
            verified = bool(official_keys & known_keys)
            with self._lock:
                requested = set(self._requested_subscriptions)
            if verified:
                accepted, missing = validate_stream_subscriptions(requested, official)
                active = set(accepted)
                self._set_stream_subscriptions(active)
                status = "verified" if not missing else "partial"
                error = None
            else:
                active = requested
                missing = ()
                status = "unverified"
                error = "empty_or_unknown_contract"
                self._set_stream_subscriptions(active)
            with self._lock:
                self._channel_contract = {
                    "status": status,
                    "verified": verified,
                    "checked_at_ms": checked_at_ms,
                    "official_channels": sorted(set(official)),
                    "requested": sorted(requested),
                    "active": sorted(active),
                    "missing": sorted(missing),
                    "last_error": error,
                }
            return verified
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._channel_contract.update(
                    {
                        "status": "error",
                        "verified": False,
                        "checked_at_ms": checked_at_ms,
                        "last_error": type(exc).__name__,
                    }
                )
            return False

    def _snapshot_symbol_chunk(self) -> tuple[str, ...]:
        symbols = tuple(
            dict.fromkeys(
                underlying
                for item in self.symbols
                if (underlying := _underlying_symbol(item))
            )
        )
        if not symbols:
            return ()
        limit = min(len(symbols), self.rest_snapshot_symbol_limit)
        with self._lock:
            start = self._recovery_cursor % len(symbols)
            self._recovery_cursor = (start + limit) % len(symbols)
        return tuple(symbols[(start + offset) % len(symbols)] for offset in range(limit))

    @staticmethod
    def _rest_event(
        *,
        channel: str,
        event_type: str,
        symbol: str | None,
        values: Mapping[str, Any],
        raw: Mapping[str, Any],
        quality: Mapping[str, Any] | None,
        event_time_ms: int | None,
        reason: str,
    ) -> UnusualWhalesStreamEvent:
        received_at_ms = int(time.time() * 1_000)
        normalized_time = int(event_time_ms or received_at_ms)
        event_identity = json.dumps(
            ["rest", channel, symbol, normalized_time, event_type],
            separators=(",", ":"),
        )
        normalized_quality = dict(quality or {})
        issues = list(normalized_quality.get("issues") or [])
        normalized_quality.update(
            {
                "source": str(normalized_quality.get("source") or "unusual_whales_rest"),
                "transport": "rest_compensation",
                "recovery_reason": reason,
                "available": bool(normalized_quality.get("available", True)),
                "valid": bool(normalized_quality.get("valid", not issues)),
                "issues": issues,
            }
        )
        return UnusualWhalesStreamEvent(
            channel=channel,
            event_type=event_type,
            symbol=symbol,
            event_time_ms=normalized_time,
            received_at_ms=received_at_ms,
            event_id="rest-" + hashlib.sha256(event_identity.encode()).hexdigest()[:27],
            values=dict(values),
            raw=dict(raw),
            quality=normalized_quality,
        )

    def _quote_event(
        self,
        symbol: str,
        quote: Mapping[str, Any],
        reason: str,
    ) -> UnusualWhalesStreamEvent:
        quality = dict(quote.get("quality") or {})
        event_time_ms = (
            quote.get("quote_time_ms")
            or quote.get("trade_time_ms")
            or quality.get("source_time_ms")
        )
        values = {
            key: quote.get(key)
            for key in (
                "price",
                "bid",
                "ask",
                "volume",
                "midpoint",
                "spread_bps",
                "quote_age_ms",
                "size_imbalance",
            )
            if quote.get(key) is not None
        }
        raw = {
            **values,
            "bid_size": quote.get("bid_size"),
            "ask_size": quote.get("ask_size"),
            "market_time": quote.get("market_time"),
            "snapshot_kind": "quote",
        }
        return self._rest_event(
            channel=f"price:{symbol}",
            event_type="price_snapshot",
            symbol=symbol,
            values=values,
            raw=raw,
            quality=quality,
            event_time_ms=int(event_time_ms) if event_time_ms is not None else None,
            reason=reason,
        )

    def _state_event(
        self,
        symbol: str,
        state: Mapping[str, Any],
        reason: str,
    ) -> UnusualWhalesStreamEvent:
        quality = dict(state.get("quality") or {})
        values = {
            key: state.get(key)
            for key in (
                "price",
                "open",
                "high",
                "low",
                "previous_close",
                "total_volume",
                "volume",
            )
            if state.get(key) is not None
        }
        return self._rest_event(
            channel=f"price:{symbol}",
            event_type="price_state_snapshot",
            symbol=symbol,
            values=values,
            raw={
                **values,
                "market_time": state.get("market_time"),
                "snapshot_kind": "stock_state",
            },
            quality=quality,
            event_time_ms=quality.get("source_time_ms"),
            reason=reason,
        )

    def _global_rest_events(
        self,
        client: UnusualWhalesMarketClient,
        reason: str,
    ) -> tuple[list[UnusualWhalesStreamEvent], list[str]]:
        events: list[UnusualWhalesStreamEvent] = []
        errors: list[str] = []
        try:
            tide = client.market_tide()
            quality = dict(tide.get("quality") or {})
            events.append(
                self._rest_event(
                    channel="market_tide",
                    event_type="market_tide_snapshot",
                    symbol=None,
                    values={
                        key: tide.get(key)
                        for key in (
                            "net_call_premium",
                            "net_put_premium",
                            "net_premium",
                            "net_volume",
                            "bias",
                        )
                        if tide.get(key) is not None
                    },
                    raw={
                        "timestamp": tide.get("timestamp"),
                        "samples": tide.get("samples"),
                        "snapshot_kind": "market_tide",
                    },
                    quality=quality,
                    event_time_ms=quality.get("source_time_ms"),
                    reason=reason,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"market_tide:{type(exc).__name__}")
        try:
            term = client.vix_term_structure(history_days=30)
            quality = dict(term.get("quality") or {})
            latest = dict(term.get("latest") or {})
            events.append(
                self._rest_event(
                    channel="vix_term_structure",
                    event_type="vix_term_structure_snapshot",
                    symbol=None,
                    values=latest,
                    raw={
                        "latest": latest,
                        "history": list(term.get("history") or [])[-10:],
                        "snapshot_kind": "vix_term_structure",
                    },
                    quality=quality,
                    event_time_ms=quality.get("source_time_ms"),
                    reason=reason,
                )
            )
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            errors.append(f"vix_term_structure:{type(exc).__name__}")
        return events, errors

    def _detail_rest_events(
        self,
        client: UnusualWhalesMarketClient,
        symbols: tuple[str, ...],
        reason: str,
    ) -> tuple[list[UnusualWhalesStreamEvent], list[str]]:
        detail_symbols = symbols[: self.rest_detail_symbol_limit]
        if not detail_symbols:
            return [], []

        def fetch(symbol: str, domain: str) -> tuple[str, str, Mapping[str, Any]]:
            if domain == "gex":
                return symbol, domain, client.gex_levels(symbol)
            return symbol, domain, client.off_lit_price_levels(symbol)

        events: list[UnusualWhalesStreamEvent] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(6, len(detail_symbols) * 2)) as executor:
            futures = {
                executor.submit(fetch, symbol, domain): (symbol, domain)
                for symbol in detail_symbols
                for domain in ("gex", "off_lit")
            }
            for future in as_completed(futures):
                symbol, domain = futures[future]
                try:
                    _, _, payload = future.result()
                except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"{domain}:{type(exc).__name__}")
                    continue
                quality = dict(payload.get("quality") or {})
                if domain == "gex":
                    values = {
                        key: payload.get(key)
                        for key in ("call_wall", "put_wall", "gamma_flip", "gamma_magnet")
                        if payload.get(key) is not None
                    }
                    channel = f"gex:{symbol}"
                    raw = {**values, "snapshot_kind": "gex_levels"}
                else:
                    values = {
                        key: payload.get(key)
                        for key in (
                            "total_lit_volume",
                            "total_off_lit_volume",
                            "off_lit_ratio",
                        )
                        if payload.get(key) is not None
                    }
                    # A price-level aggregate is not a directional trade.  Keep
                    # it replayable without letting the trade aggregator count
                    # it as institutional-flow coverage.
                    channel = "off_lit_price_levels"
                    raw = {
                        **values,
                        "levels": list(payload.get("levels") or [])[:20],
                        "snapshot_kind": "off_lit_price_levels",
                    }
                events.append(
                    self._rest_event(
                        channel=channel,
                        event_type=f"{domain}_snapshot",
                        symbol=symbol,
                        values=values,
                        raw=raw,
                        quality=quality,
                        event_time_ms=quality.get("source_time_ms"),
                        reason=reason,
                    )
                )
        return events, errors

    def _poll_recovery_snapshot(self, reason: str) -> None:
        client = self.rest_client
        if client is None:
            return
        symbols = self._snapshot_symbol_chunk()
        errors: list[str] = []
        quote_rows: dict[str, Mapping[str, Any]] = {}
        state_rows: dict[str, Mapping[str, Any]] = {}
        try:
            quote_rows = client.stock_quotes(symbols)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            errors.append(f"stock_quotes:{type(exc).__name__}")
        missing = tuple(symbol for symbol in symbols if symbol not in quote_rows)
        if missing:
            try:
                state_rows = client.stock_states(missing)
            except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                errors.append(f"stock_states:{type(exc).__name__}")

        events = [
            self._quote_event(symbol, payload, reason)
            for symbol, payload in quote_rows.items()
        ]
        events.extend(
            self._state_event(symbol, payload, reason)
            for symbol, payload in state_rows.items()
        )
        global_events, global_errors = self._global_rest_events(client, reason)
        detail_events, detail_errors = self._detail_rest_events(client, symbols, reason)
        events.extend(global_events)
        events.extend(detail_events)
        errors.extend(global_errors)
        errors.extend(detail_errors)
        for event in events:
            self.on_event(event)
        now_ms = int(time.time() * 1_000)
        with self._lock:
            self._rest_polls += 1
            self._recovery_runs += 1
            self._recovery_events += len(events)
            self._recovery_quote_rows += len(quote_rows)
            self._recovery_state_rows += len(state_rows)
            self._recovery_detail_rows += len(detail_events)
            self._last_rest_at_ms = now_ms
            self._last_recovery_at_ms = now_ms
            self._last_recovery_reason = reason
            self._last_rest_error = ",".join(sorted(set(errors))) if errors else None
            if errors:
                self._rest_errors += 1

    def _rest_loop(self) -> None:
        now = time.monotonic()
        next_calendar = now
        next_contract = now + self.channel_contract_poll_seconds
        next_recovery = now
        startup = True
        was_connected = False
        while not self._stop.is_set() and not self._worker_stop.is_set():
            now = time.monotonic()
            if (
                self.rest_enabled
                and self.rest_client is not None
                and self._collection_allowed()
            ):
                stream_health = self.stream.health_snapshot()
                connected = bool(stream_health.get("connected"))
                stale = bool(stream_health.get("data_stale"))
                connected_at_ms = stream_health.get("connected_at_ms")
                silent = bool(
                    connected
                    and int(stream_health.get("accepted") or 0) == 0
                    and connected_at_ms is not None
                    and int(time.time() * 1_000) - int(connected_at_ms)
                    > self.channel_stale_ms
                )
                needs_recovery = (
                    not self.websocket_enabled or not connected or stale or silent
                )
                # Stock Quote, GEX levels and off-lit price levels are REST-only
                # verification domains.  Keep validating them on a bounded,
                # rotating cadence even while the websocket is healthy; a live
                # last-trade stream must not make an old NBBO look current.
                if startup or now >= next_recovery:
                    reason = (
                        "startup"
                        if startup
                        else "periodic_validation"
                        if not needs_recovery
                        else "rest_only"
                        if not self.websocket_enabled
                        else "stream_stale"
                        if stale
                        else "stream_silent"
                        if silent
                        else "stream_disconnected"
                        if was_connected
                        else "stream_unavailable"
                    )
                    self._poll_recovery_snapshot(reason)
                    next_recovery = time.monotonic() + self.recovery_poll_seconds
                if now >= next_calendar:
                    self._poll_economic_calendar()
                    next_calendar = time.monotonic() + self.calendar_poll_seconds
                if self.websocket_enabled and (
                    now >= next_contract or self._contract_refresh.is_set()
                ):
                    self._contract_refresh.clear()
                    self._refresh_channel_contract()
                    next_contract = time.monotonic() + self.channel_contract_poll_seconds
                was_connected = connected
                startup = False
                deadlines = [next_calendar, next_contract, next_recovery]
                delay = max(0.25, min(30.0, min(deadlines) - time.monotonic()))
            else:
                startup = False
                delay = 5.0 if self.market_open_only else 30.0
            self._poll_wakeup.clear()
            self._poll_wakeup.wait(delay)

    def _poll_economic_calendar(self) -> None:
        try:
            client = self.rest_client
            if client is None:
                return
            payload = client.economic_calendar()
            with Session(self.engine, expire_on_commit=False) as db:
                result = sync_economic_calendar(
                    db,
                    payload,
                    block_before_minutes=self.event_block_before_minutes,
                    block_after_minutes=self.event_block_after_minutes,
                )
                db.commit()
            with self._lock:
                self._rest_polls += 1
                self._last_rest_at_ms = int(time.time() * 1_000)
                self._last_rest_error = None
                self._calendar_rows = int(result["created"]) + int(result["updated"])
        except (AssertionError, OSError, RuntimeError, SQLAlchemyError, ValueError) as exc:
            with self._lock:
                self._rest_errors += 1
                self._last_rest_error = type(exc).__name__

    def _writer_loop(self) -> None:
        while (
            not self._stop.is_set() and not self._worker_stop.is_set()
        ) or not self._queue.empty():
            try:
                first = self._queue.get(timeout=self.flush_seconds)
            except Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.flush_seconds
            while len(batch) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except Empty:
                    break
            try:
                write_started = time.monotonic()
                with Session(self.engine, expire_on_commit=False) as db:
                    result = ingest_market_stream_events(db, batch)
                    db.commit()
                write_duration_ms = (time.monotonic() - write_started) * 1_000
                accepted = int(result["accepted"])
                write_at_ms = int(time.time() * 1_000)
                with self._lock:
                    self._persisted += accepted
                    self._persist_duplicates += int(result["duplicates"])
                    self._last_write_at_ms = write_at_ms
                    self._last_write_error = None
                    self._write_latencies_ms.append(write_duration_ms)
                    if accepted:
                        self._persist_rate_samples.append((write_at_ms, accepted))
            except (SQLAlchemyError, ValueError) as exc:
                with self._lock:
                    self._write_errors += 1
                    self._last_write_error = type(exc).__name__
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _retention_loop(self) -> None:
        # Run immediately on leadership acquisition, then at the configured
        # interval. A new leader safely resumes from the remaining backlog.
        while not self._stop.is_set() and not self._worker_stop.is_set():
            started = time.monotonic()
            try:
                result = cleanup_market_data_retention(
                    self.engine,
                    raw_event_days=self.raw_event_retention_days,
                    feature_snapshot_days=self.feature_retention_days,
                    batch_size=self.retention_batch_size,
                    max_batches=self.retention_max_batches,
                )
                with self._lock:
                    self._retention_runs += 1
                    self._retention_deleted_events += int(result["deleted_events"])
                    self._retention_deleted_features += int(result["deleted_features"])
                    self._retention_event_backlog = int(result["event_backlog"])
                    self._retention_feature_backlog = int(result["feature_backlog"])
                    self._retention_protected_features = int(
                        result["protected_features"]
                    )
                    self._retention_last_error = None
            except (OSError, RuntimeError, SQLAlchemyError, ValueError) as exc:
                with self._lock:
                    self._retention_errors += 1
                    self._retention_last_error = type(exc).__name__
            finally:
                with self._lock:
                    self._retention_last_run_at_ms = int(time.time() * 1_000)
                    self._retention_last_duration_ms = round(
                        (time.monotonic() - started) * 1_000
                    )
            self._worker_stop.wait(self.retention_cleanup_seconds)
