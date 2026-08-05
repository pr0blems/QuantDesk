"""Bounded Binance USD-M diff-depth collection and local order books.

The public entry point :func:`ws_depth_loop` splits a large universe into
independent groups (50 symbols by default).  Each group opens its WebSocket
before requesting REST snapshots, buffers updates during snapshot loading,
and invalidates only that group's books when its connection is interrupted.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from websockets.sync.client import connect

from .binance_rate_limit import BinanceRestRateLimit
from .market_data_client import BinancePublicRequestError, fetch_depth_snapshot

WS_DEPTH_ORIGIN = "wss://fstream.binance.com"
DEFAULT_SNAPSHOT_LIMIT = 500
DEFAULT_GROUP_SIZE = 50
TOP_LEVELS = 100
HEARTBEAT_SECONDS = 2.0

_MAX_GROUP_SIZE = 50
_MAX_TOTAL_SYMBOLS = 1_000
_MAX_STREAM_URI_BYTES = 8_192
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_LEVEL_UPDATES = 2_000
_MAX_STORED_LEVELS = 1_000
_MAX_PENDING_EVENTS = 2_048
_MAX_NUMBER = Decimal("1e30")
_MAX_UPDATE_ID = 2**63 - 1


class _DepthConnection(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DepthMetrics:
    symbol: str
    bid_depth_notional: float
    ask_depth_notional: float
    book_imbalance: float
    book_imbalance_5: float
    depth_levels: int
    ts: int

    def as_dict(self) -> dict[str, Any]:
        """Return a storage-ready mapping with the monitor column names."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class _DepthEvent:
    symbol: str
    first_update_id: int
    final_update_id: int
    previous_final_update_id: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    timestamp: int


def _normalize_symbol(symbol: Any) -> str:
    if not isinstance(symbol, str):
        raise ValueError("invalid Binance depth symbol")
    normalized = symbol.strip().upper()
    if not 2 <= len(normalized) <= 32 or not normalized.isalnum():
        raise ValueError("invalid Binance depth symbol")
    return normalized


def _bounded_integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        if len(value) > 20:
            raise ValueError(f"{label} exceeds the safety limit")
        if not value.isascii() or not value.isdigit():
            raise ValueError(f"{label} must be an integer")
        normalized = int(value)
    else:
        raise ValueError(f"{label} must be an integer")
    minimum = 1 if positive else 0
    if not minimum <= normalized <= _MAX_UPDATE_ID:
        raise ValueError(f"{label} is outside the supported range")
    return normalized


def _decimal_value(value: Any, label: str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    raw = str(value).strip()
    if not raw or len(raw) > 64:
        raise ValueError(f"{label} exceeds the safety limit")
    try:
        normalized = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not normalized.is_finite() or abs(normalized) > _MAX_NUMBER:
        raise ValueError(f"{label} is outside the supported range")
    if normalized < 0 or (not allow_zero and normalized == 0):
        raise ValueError(f"{label} must be {'non-negative' if allow_zero else 'positive'}")
    return normalized


def _levels(value: Any, label: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} levels must be an array")
    if len(value) > _MAX_LEVEL_UPDATES:
        raise ValueError(f"{label} level count exceeds the safety limit")
    output = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise ValueError(f"{label} contains an invalid level")
        output.append(
            (
                _decimal_value(row[0], f"{label} price", allow_zero=False),
                _decimal_value(row[1], f"{label} quantity", allow_zero=True),
            )
        )
    return tuple(output)


def _timestamp_seconds(value: Any, *, fallback: int | None = None) -> int:
    try:
        timestamp = _bounded_integer(value, "depth timestamp", positive=True)
    except ValueError:
        if fallback is None:
            raise
        return int(fallback)
    # Binance event timestamps are milliseconds.  Accept seconds as well so
    # persisted/replayed fixtures don't accidentally get divided twice.
    return timestamp // 1_000 if timestamp >= 100_000_000_000 else timestamp


def _parse_event(payload: Any, expected_symbol: str) -> _DepthEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("Binance depth update must be an object")
    if payload.get("e") not in (None, "depthUpdate"):
        raise ValueError("unexpected Binance depth event type")
    symbol = _normalize_symbol(payload.get("s"))
    if symbol != expected_symbol:
        raise ValueError("Binance depth event symbol mismatch")
    first_update_id = _bounded_integer(payload.get("U"), "first update id", positive=True)
    final_update_id = _bounded_integer(payload.get("u"), "final update id", positive=True)
    previous_final_update_id = _bounded_integer(
        payload.get("pu"), "previous final update id"
    )
    if first_update_id > final_update_id:
        raise ValueError("Binance depth update id range is reversed")
    event_timestamp = payload.get("E", payload.get("T"))
    return _DepthEvent(
        symbol=symbol,
        first_update_id=first_update_id,
        final_update_id=final_update_id,
        previous_final_update_id=previous_final_update_id,
        bids=_levels(payload.get("b"), "bid"),
        asks=_levels(payload.get("a"), "ask"),
        timestamp=_timestamp_seconds(event_timestamp),
    )


class DepthOrderBook:
    """Thread-safe local book reconciled from one REST snapshot and diff events."""

    def __init__(
        self,
        symbol: str,
        *,
        max_pending_events: int = _MAX_PENDING_EVENTS,
        max_stored_levels: int = _MAX_STORED_LEVELS,
    ) -> None:
        if isinstance(max_pending_events, bool) or not 1 <= max_pending_events <= 10_000:
            raise ValueError("invalid pending depth event limit")
        if isinstance(max_stored_levels, bool) or not TOP_LEVELS <= max_stored_levels <= 5_000:
            raise ValueError("invalid stored depth level limit")
        self.symbol = _normalize_symbol(symbol)
        self._max_stored_levels = int(max_stored_levels)
        self._pending: deque[_DepthEvent] = deque(maxlen=int(max_pending_events))
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._last_update_id: int | None = None
        self._timestamp = 0
        self._synced = False
        self._awaiting_bridge = False
        self._lock = threading.RLock()

    @property
    def synced(self) -> bool:
        with self._lock:
            return self._synced

    @property
    def needs_snapshot(self) -> bool:
        return not self.synced

    @property
    def last_update_id(self) -> int | None:
        with self._lock:
            return self._last_update_id

    @property
    def pending_events(self) -> int:
        with self._lock:
            return len(self._pending)

    def invalidate(self) -> None:
        """Discard state after a stream disconnect; the next session must resync."""

        with self._lock:
            self._bids.clear()
            self._asks.clear()
            self._pending.clear()
            self._last_update_id = None
            self._timestamp = 0
            self._synced = False
            self._awaiting_bridge = False

    def feed(self, payload: Any, *, publish: bool = True) -> DepthMetrics | None:
        """Buffer or apply one ``depthUpdate`` event.

        ``None`` means the event was stale, buffered, invalidated by a sequence
        gap, or otherwise did not produce a publishable synchronized state.
        Malformed exchange input raises ``ValueError`` so the collector can
        invalidate and explicitly request a fresh snapshot.
        """

        event = _parse_event(payload, self.symbol)
        with self._lock:
            if not self._synced or self._last_update_id is None:
                self._pending.append(event)
                return None
            if self._awaiting_bridge:
                # A REST snapshot can be newer than every event currently in
                # the receive buffer.  In that case the first later event is
                # allowed to overlap the snapshot's update id even though its
                # ``pu`` refers to the last event seen before the snapshot.
                # Treating it as an ordinary subsequent event causes a false
                # gap and an endless snapshot/resync loop.
                if event.final_update_id < self._last_update_id:
                    return None
                if not _bridges_snapshot(event, self._last_update_id):
                    self._mark_unsynced_unlocked((event,))
                    return None
                self._apply_event_unlocked(event)
                self._awaiting_bridge = False
                if self._is_crossed_unlocked():
                    self._mark_unsynced_unlocked(())
                    return None
                return self._metrics_unlocked() if publish else None
            if event.final_update_id <= self._last_update_id:
                return None
            if event.previous_final_update_id != self._last_update_id:
                self._mark_unsynced_unlocked((event,))
                return None
            self._apply_event_unlocked(event)
            if self._is_crossed_unlocked():
                self._mark_unsynced_unlocked(())
                return None
            # Updating sequence/book state is required for every diff event,
            # but sorting up to 1,000 Decimal levels for every 500 ms message
            # is unnecessary when storage already coalesces writes.  The
            # collector publishes one calculated snapshot per heartbeat.
            return self._metrics_unlocked() if publish else None

    def load_snapshot(self, payload: Any) -> DepthMetrics | None:
        """Atomically install a REST snapshot and replay buffered diff events."""

        if not isinstance(payload, Mapping):
            raise ValueError("Binance depth snapshot must be an object")
        snapshot_id = _bounded_integer(
            payload.get("lastUpdateId"), "snapshot update id", positive=True
        )
        bids = {price: quantity for price, quantity in _levels(payload.get("bids"), "bid") if quantity}
        asks = {price: quantity for price, quantity in _levels(payload.get("asks"), "ask") if quantity}
        if not bids or not asks:
            raise ValueError("Binance depth snapshot must contain both sides")
        if max(bids) >= min(asks):
            raise ValueError("Binance depth snapshot is crossed")
        now = int(time.time())
        snapshot_timestamp = _timestamp_seconds(
            payload.get("E", payload.get("T")), fallback=now
        )

        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            self._bids = bids
            self._asks = asks
            self._prune_unlocked()
            self._last_update_id = snapshot_id
            self._timestamp = snapshot_timestamp
            self._synced = True
            self._awaiting_bridge = True

            candidates = [event for event in pending if event.final_update_id >= snapshot_id]
            if candidates:
                first = candidates[0]
                if not _bridges_snapshot(first, snapshot_id):
                    self._mark_unsynced_unlocked(candidates)
                    return None
                self._apply_event_unlocked(first)
                self._awaiting_bridge = False
                for index, event in enumerate(candidates[1:], start=1):
                    previous_update_id = self._last_update_id
                    if previous_update_id is None:
                        self._mark_unsynced_unlocked(candidates[index:])
                        return None
                    if event.final_update_id <= previous_update_id:
                        continue
                    if event.previous_final_update_id != previous_update_id:
                        self._mark_unsynced_unlocked(candidates[index:])
                        return None
                    self._apply_event_unlocked(event)
            if self._is_crossed_unlocked():
                self._mark_unsynced_unlocked(())
                return None
            return self._metrics_unlocked()

    def metrics(self) -> DepthMetrics | None:
        with self._lock:
            return self._metrics_unlocked() if self._synced else None

    def heartbeat(self, timestamp: int | None = None) -> DepthMetrics | None:
        """Refresh freshness while a live stream still owns this synchronized book."""

        if isinstance(timestamp, bool) or (timestamp is not None and not isinstance(timestamp, int)):
            raise ValueError("depth heartbeat timestamp must be an integer")
        now = int(time.time()) if timestamp is None else timestamp
        if now <= 0 or now > _MAX_UPDATE_ID:
            raise ValueError("depth heartbeat timestamp must be positive")
        with self._lock:
            if not self._synced:
                return None
            self._timestamp = max(self._timestamp, now)
            return self._metrics_unlocked()

    def _mark_unsynced_unlocked(self, events: Sequence[_DepthEvent]) -> None:
        self._bids.clear()
        self._asks.clear()
        self._pending.clear()
        self._pending.extend(events[-self._pending.maxlen :])
        self._last_update_id = None
        self._timestamp = 0
        self._synced = False
        self._awaiting_bridge = False

    def _apply_event_unlocked(self, event: _DepthEvent) -> None:
        for price, quantity in event.bids:
            if quantity:
                self._bids[price] = quantity
            else:
                self._bids.pop(price, None)
        for price, quantity in event.asks:
            if quantity:
                self._asks[price] = quantity
            else:
                self._asks.pop(price, None)
        self._last_update_id = event.final_update_id
        self._timestamp = max(self._timestamp, event.timestamp)
        self._prune_unlocked()

    def _prune_unlocked(self) -> None:
        if len(self._bids) > self._max_stored_levels:
            keep = set(sorted(self._bids, reverse=True)[: self._max_stored_levels])
            self._bids = {price: quantity for price, quantity in self._bids.items() if price in keep}
        if len(self._asks) > self._max_stored_levels:
            keep = set(sorted(self._asks)[: self._max_stored_levels])
            self._asks = {price: quantity for price, quantity in self._asks.items() if price in keep}

    def _is_crossed_unlocked(self) -> bool:
        return not self._bids or not self._asks or max(self._bids) >= min(self._asks)

    def _metrics_unlocked(self) -> DepthMetrics:
        bids = sorted(self._bids.items(), reverse=True)[:TOP_LEVELS]
        asks = sorted(self._asks.items())[:TOP_LEVELS]
        bid_notional = sum((price * quantity for price, quantity in bids), Decimal(0))
        ask_notional = sum((price * quantity for price, quantity in asks), Decimal(0))
        bid_near = sum((price * quantity for price, quantity in bids[:5]), Decimal(0))
        ask_near = sum((price * quantity for price, quantity in asks[:5]), Decimal(0))
        return DepthMetrics(
            symbol=self.symbol,
            bid_depth_notional=float(bid_notional),
            ask_depth_notional=float(ask_notional),
            book_imbalance=_imbalance(bid_notional, ask_notional),
            book_imbalance_5=_imbalance(bid_near, ask_near),
            depth_levels=min(len(bids), len(asks), TOP_LEVELS),
            ts=self._timestamp,
        )


def _imbalance(bid_notional: Decimal, ask_notional: Decimal) -> float:
    denominator = bid_notional + ask_notional
    if denominator <= 0:
        return 0.0
    value = float((bid_notional - ask_notional) / denominator)
    return max(-1.0, min(1.0, value)) if math.isfinite(value) else 0.0


def _bridges_snapshot(event: _DepthEvent, snapshot_id: int) -> bool:
    """Return whether the first diff event can anchor a REST snapshot."""

    overlaps_snapshot = event.first_update_id <= snapshot_id <= event.final_update_id
    directly_continues = (
        event.previous_final_update_id == snapshot_id
        and event.first_update_id <= snapshot_id + 1 <= event.final_update_id
    )
    return overlaps_snapshot or directly_continues


def depth_symbol_groups(
    symbols: Sequence[str], group_size: int = DEFAULT_GROUP_SIZE
) -> tuple[tuple[str, ...], ...]:
    """Normalize, deduplicate, and split symbols into isolated WS groups."""

    if isinstance(group_size, bool) or not 1 <= group_size <= _MAX_GROUP_SIZE:
        raise ValueError(f"depth WebSocket group size must be 1-{_MAX_GROUP_SIZE}")
    normalized = tuple(dict.fromkeys(_normalize_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("at least one Binance depth symbol is required")
    if len(normalized) > _MAX_TOTAL_SYMBOLS:
        raise ValueError("Binance depth symbol count exceeds the safety limit")
    return tuple(
        normalized[index : index + group_size]
        for index in range(0, len(normalized), group_size)
    )


def _depth_uri(symbols: Sequence[str]) -> str:
    streams = "/".join(f"{symbol.lower()}@depth@500ms" for symbol in symbols)
    uri = f"{WS_DEPTH_ORIGIN}/public/stream?streams={streams}"
    if len(uri.encode("ascii")) > _MAX_STREAM_URI_BYTES:
        raise ValueError("Binance combined depth stream URI exceeds the safety limit")
    return uri


def _connect_depth(symbols: Sequence[str]) -> _DepthConnection:
    """Open one allowlisted combined stream; called only by the loop."""

    uri = _depth_uri(symbols)
    return connect(
        uri,
        open_timeout=15,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=_MAX_MESSAGE_BYTES,
        max_queue=256,
        compression=None,
        user_agent_header="quantdesk-local",
    )


class DepthStreamCollector:
    """Collect one isolated combined-stream group and resync gaps via REST."""

    def __init__(
        self,
        symbols: Sequence[str],
        on_metrics: Callable[[DepthMetrics], None],
        *,
        snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
        snapshot_fetcher: Callable[[str, int], Mapping[str, Any]] = fetch_depth_snapshot,
        connect_factory: Callable[[Sequence[str]], _DepthConnection] = _connect_depth,
    ) -> None:
        groups = depth_symbol_groups(symbols, _MAX_GROUP_SIZE)
        if len(groups) != 1:
            raise ValueError("one DepthStreamCollector may contain at most 50 symbols")
        if snapshot_limit not in {100, 500, 1_000}:
            raise ValueError("depth collector snapshot limit must be 100, 500, or 1000")
        if not callable(on_metrics) or not callable(snapshot_fetcher) or not callable(connect_factory):
            raise TypeError("depth collector callbacks must be callable")
        self.symbols = groups[0]
        self.books = {symbol: DepthOrderBook(symbol) for symbol in self.symbols}
        self._on_metrics = on_metrics
        self._snapshot_limit = int(snapshot_limit)
        self._snapshot_fetcher = snapshot_fetcher
        self._connect_factory = connect_factory
        self._tasks: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=len(self.symbols) * 4)
        self._state_lock = threading.Lock()
        self._queued: set[tuple[int, str]] = set()
        self._inflight: set[tuple[int, str]] = set()
        self._generation = 0
        self._session_active = False

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        pause = should_pause or (lambda: False)
        worker = threading.Thread(
            target=self._snapshot_worker,
            args=(stop, pause),
            daemon=True,
            name=f"depth-snapshot-{self.symbols[0]}",
        )
        worker.start()
        backoff = 2.0
        while not stop.is_set():
            if self._paused(pause):
                stop.wait(5.0)
                continue
            connection: _DepthConnection | None = None
            generation = 0
            try:
                connection = self._connect_factory(self.symbols)
                generation = self._activate_session()
                for symbol in self.symbols:
                    self._request_snapshot(generation, symbol)
                print(f"[depth] Binance stream connected ({len(self.symbols)} symbols)")
                backoff = 2.0
                next_control_check = time.monotonic() + 1.0
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                while not stop.is_set():
                    try:
                        message = connection.recv(timeout=1.0)
                    except TimeoutError:
                        message = None
                    if message is not None:
                        self._process_message(message, generation)
                    monotonic_now = time.monotonic()
                    if monotonic_now >= next_control_check:
                        next_control_check = monotonic_now + 1.0
                        if self._paused(pause):
                            break
                    if monotonic_now >= next_heartbeat:
                        next_heartbeat = monotonic_now + HEARTBEAT_SECONDS
                        self._emit_heartbeats(generation)
            except Exception as exc:
                if not stop.is_set():
                    print(
                        f"[depth] stream group disconnected ({str(exc)[:100]}); "
                        f"retrying in {backoff:.0f}s"
                    )
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
            finally:
                if generation:
                    self._deactivate_session(generation)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception as exc:
                        print(f"[depth] stream close failed ({str(exc)[:100]})")
        worker.join(timeout=1)

    @staticmethod
    def _paused(callback: Callable[[], bool]) -> bool:
        try:
            return bool(callback())
        except Exception as exc:
            # Fail closed: a broken admin-control read must not silently keep
            # consuming market data that an operator may have paused.
            print(f"[depth] pause-state check failed ({str(exc)[:100]}); pausing")
            return True

    def _activate_session(self) -> int:
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._session_active = True
            self._queued.clear()
            while True:
                try:
                    self._tasks.get_nowait()
                except queue.Empty:
                    break
        for book in self.books.values():
            book.invalidate()
        return generation

    def _deactivate_session(self, generation: int) -> None:
        with self._state_lock:
            if self._generation == generation:
                self._session_active = False
        for book in self.books.values():
            book.invalidate()

    def _generation_is_active(self, generation: int) -> bool:
        with self._state_lock:
            return self._session_active and self._generation == generation

    def _request_snapshot(self, generation: int, symbol: str) -> None:
        task = (generation, symbol)
        with self._state_lock:
            if (
                not self._session_active
                or self._generation != generation
                or task in self._queued
                or task in self._inflight
            ):
                return
            self._queued.add(task)
            try:
                self._tasks.put_nowait(task)
            except queue.Full:
                self._queued.discard(task)

    def _snapshot_worker(
        self, stop: threading.Event, should_pause: Callable[[], bool]
    ) -> None:
        while not stop.is_set():
            try:
                task = self._tasks.get(timeout=0.25)
            except queue.Empty:
                continue
            generation, symbol = task
            with self._state_lock:
                self._queued.discard(task)
                if not self._session_active or self._generation != generation:
                    continue
                self._inflight.add(task)
            retry_delay = 0.0
            try:
                if self._paused(should_pause):
                    continue
                payload = self._snapshot_fetcher(symbol, self._snapshot_limit)
                if not self._generation_is_active(generation):
                    continue
                metrics = self.books[symbol].load_snapshot(payload)
                if metrics is not None:
                    self._emit(metrics)
                else:
                    retry_delay = 0.5
            except BinanceRestRateLimit as exc:
                retry_delay = max(1.0, min(30.0, exc.retry_at - time.time()))
            except BinancePublicRequestError as exc:
                retry_delay = 2.0 if exc.retryable else 10.0
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[depth] invalid snapshot for {symbol} ({str(exc)[:100]})")
                retry_delay = 5.0
            except Exception as exc:
                print(f"[depth] snapshot failed for {symbol} ({str(exc)[:100]})")
                retry_delay = 5.0
            finally:
                with self._state_lock:
                    self._inflight.discard(task)
            if (
                not retry_delay
                and self._generation_is_active(generation)
                and self.books[symbol].needs_snapshot
                and not self._paused(should_pause)
            ):
                # The receiver may have invalidated this book while its prior
                # snapshot callback was still completing.
                retry_delay = 0.5
            if retry_delay and not stop.wait(retry_delay):
                self._request_snapshot(generation, symbol)

    def _process_message(self, message: str | bytes, generation: int) -> None:
        if not self._generation_is_active(generation):
            return
        if isinstance(message, bytes):
            if len(message) > _MAX_MESSAGE_BYTES:
                raise ValueError("Binance depth message exceeds the safety limit")
            text = message.decode("utf-8", errors="strict")
        elif isinstance(message, str):
            if len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
                raise ValueError("Binance depth message exceeds the safety limit")
            text = message
        else:
            raise ValueError("Binance depth message must be text")
        payload = json.loads(text)
        stream = None
        if isinstance(payload, Mapping) and "data" in payload:
            stream = payload.get("stream")
            payload = payload.get("data")
        if not isinstance(payload, Mapping):
            raise ValueError("Binance depth message must contain an event object")
        symbol = _normalize_symbol(payload.get("s"))
        book = self.books.get(symbol)
        if book is None:
            raise ValueError("Binance depth message contains an unrequested symbol")
        if stream is not None and stream != f"{symbol.lower()}@depth@500ms":
            raise ValueError("Binance combined depth stream name mismatch")
        try:
            metrics = book.feed(payload, publish=False)
        except ValueError as exc:
            book.invalidate()
            self._request_snapshot(generation, symbol)
            print(f"[depth] invalid event for {symbol} ({str(exc)[:100]}); resyncing symbol")
            return
        if book.needs_snapshot:
            self._request_snapshot(generation, symbol)
        if metrics is not None:
            self._emit(metrics)

    def _emit_heartbeats(self, generation: int) -> None:
        if not self._generation_is_active(generation):
            return
        timestamp = int(time.time())
        for symbol, book in self.books.items():
            if book.needs_snapshot:
                self._request_snapshot(generation, symbol)
                continue
            metrics = book.heartbeat(timestamp)
            if metrics is not None:
                self._emit(metrics)

    def _emit(self, metrics: DepthMetrics) -> None:
        try:
            self._on_metrics(metrics)
        except Exception as exc:
            print(
                f"[depth] metrics consumer failed for {metrics.symbol} "
                f"({str(exc)[:100]}); keeping stream open"
            )


def ws_depth_loop(
    symbols: Sequence[str],
    on_metrics: Callable[[DepthMetrics], None],
    *,
    stop_event: threading.Event | None = None,
    should_pause: Callable[[], bool] | None = None,
    group_size: int = DEFAULT_GROUP_SIZE,
    snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> None:
    """Run isolated depth collectors, splitting large universes by group size."""

    groups = depth_symbol_groups(symbols, group_size)
    stop = stop_event or threading.Event()
    collectors = [
        DepthStreamCollector(group, on_metrics, snapshot_limit=snapshot_limit)
        for group in groups
    ]
    if len(collectors) == 1:
        collectors[0].run_forever(stop, should_pause)
        return
    threads = [
        threading.Thread(
            target=collector.run_forever,
            args=(stop, should_pause),
            daemon=True,
            name=f"depth-stream-{index + 1}",
        )
        for index, collector in enumerate(collectors)
    ]
    for thread in threads:
        thread.start()
    while not stop.wait(0.5):
        if any(not thread.is_alive() for thread in threads):
            stop.set()
            raise RuntimeError("a Binance depth collector group stopped unexpectedly")
    for thread in threads:
        thread.join(timeout=1)
