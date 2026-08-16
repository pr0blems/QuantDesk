"""Resilient Unusual Whales websocket ingestion primitives.

The service is deliberately independent from FastAPI and persistence.  Callers
inject an event callback (and, in tests, a transport factory), which keeps the
connection lifecycle deterministic and prevents network access in unit tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from threading import Event, Lock, Thread, current_thread
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from .unusual_whales import _number, _timestamp_ms

UNUSUAL_WHALES_STREAM_ORIGIN = "wss://api.unusualwhales.com/socket"
MAX_STREAM_MESSAGE_BYTES = 2 * 1024 * 1024

_CHANNEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}(?::[A-Z][A-Z0-9.-]{0,14})?$")
_TOKEN_IN_URL = re.compile(r"(?i)(token=)[^&\s]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")

_CHANNEL_BASES = frozenset(
    {
        "contract_screener",
        "custom_alerts",
        "flow-alerts",
        "gex",
        "gex_strike",
        "gex_strike_expiry",
        "interval_flow",
        "lit_trades",
        "market_tide",
        "net_flow",
        "news",
        "off_lit_trades",
        "option_trades",
        "periscope",
        "price",
        "trading_halts",
    }
)
_TICKER_SCOPED_BASES = frozenset(
    {
        "gex",
        "gex_strike",
        "gex_strike_expiry",
        "net_flow",
        "option_trades",
        "periscope",
        "price",
    }
)
_TICKER_REQUIRED_BASES = frozenset({"net_flow"})
_SYMBOL_REQUIRED = frozenset(
    {
        "flow-alerts",
        "gex",
        "gex_strike",
        "gex_strike_expiry",
        "interval_flow",
        "lit_trades",
        "net_flow",
        "off_lit_trades",
        "option_trades",
        "periscope",
        "price",
        "trading_halts",
    }
)

_EVENT_TYPES = {
    "contract_screener": "contract_snapshot",
    "custom_alerts": "custom_alert",
    "flow-alerts": "flow_alert",
    "gex": "gex",
    "gex_strike": "gex_strike",
    "gex_strike_expiry": "gex_strike_expiry",
    "interval_flow": "interval_flow",
    "lit_trades": "lit_trade",
    "market_tide": "market_tide",
    "net_flow": "net_flow",
    "news": "news",
    "off_lit_trades": "off_lit_trade",
    "option_trades": "option_trade",
    "periscope": "periscope",
    "price": "price",
    "trading_halts": "trading_halt",
}

_TIME_FIELDS = (
    "time",
    "timestamp",
    "tape_time",
    "executed_at",
    "end_time",
    "start_time",
    "created_at",
    "trf_executed_at",
)
_SYMBOL_FIELDS = ("ticker", "symbol", "underlying_symbol", "underlying_ticker")


@runtime_checkable
class StreamTransport(Protocol):
    """Small transport surface implemented by ``websockets.sync`` and test fakes."""

    def send(self, message: str) -> Any: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> Any: ...


TransportFactory = Callable[[str], StreamTransport | AbstractContextManager[StreamTransport]]
EventCallback = Callable[["UnusualWhalesStreamEvent"], None]


@dataclass(frozen=True, slots=True)
class UnusualWhalesStreamEvent:
    """Canonical envelope shared by every upstream websocket channel."""

    channel: str
    event_type: str
    symbol: str | None
    event_time_ms: int | None
    received_at_ms: int
    event_id: str
    values: dict[str, Any]
    raw: dict[str, Any]
    quality: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_channel(channel: str) -> str:
    raw = str(channel or "").strip()
    if not _CHANNEL.fullmatch(raw):
        raise ValueError("invalid Unusual Whales websocket channel")
    base, separator, ticker = raw.partition(":")
    if base not in _CHANNEL_BASES:
        raise ValueError("unsupported Unusual Whales websocket channel")
    if separator and base not in _TICKER_SCOPED_BASES:
        raise ValueError("channel does not support ticker scoping")
    if separator and not ticker:
        raise ValueError("ticker-scoped channel requires a ticker")
    if not separator and base in _TICKER_REQUIRED_BASES:
        raise ValueError("channel requires ticker scoping")
    return raw


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")[:240]
    message = _TOKEN_IN_URL.sub(r"\1[redacted]", message)
    message = _BEARER.sub(r"\1[redacted]", message)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _finite_value(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    parsed = _number(value)
    return parsed


def _common_values(base: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose stable cross-channel names while retaining the full raw payload."""

    values: dict[str, Any] = {}
    aliases: dict[str, tuple[str, ...]] = {
        "price": ("close", "price", "underlying_price"),
        "volume": ("vol", "volume", "size"),
        "net_call_premium": ("net_call_premium", "net_call_prem"),
        "net_put_premium": ("net_put_premium", "net_put_prem"),
        "net_volume": ("net_volume",),
        "gamma": ("gamma_per_one_percent_move_oi", "gamma", "gamma_flow"),
        "delta": ("delta_per_one_percent_move_oi", "delta", "delta_flow"),
        "strike": ("strike",),
        "open_interest": ("open_interest",),
        "premium": ("premium", "total_premium"),
        "bid": ("nbbo_bid", "bid"),
        "ask": ("nbbo_ask", "ask"),
    }
    for target, candidates in aliases.items():
        for candidate in candidates:
            if candidate in payload:
                normalized = _finite_value(payload.get(candidate))
                if normalized is not None:
                    values[target] = normalized
                    break
    if base == "trading_halts":
        values["state"] = str(payload.get("state") or "").strip().lower()
        values["reason"] = str(payload.get("reason") or "").strip()
    if base in {"lit_trades", "off_lit_trades"}:
        price = _number(payload.get("price"))
        size = _number(payload.get("size"))
        if price is not None and size is not None and price >= 0 and size >= 0:
            values["notional"] = price * size
        values["venue"] = "off_lit" if base == "off_lit_trades" else "lit"
    return values


def normalize_stream_message(
    message: str | bytes,
    *,
    received_at_ms: int | None = None,
    stale_after_ms: int = 2 * 60 * 1_000,
) -> UnusualWhalesStreamEvent:
    """Parse and validate one official ``[channel, payload]`` frame."""

    if isinstance(message, bytes):
        if len(message) > MAX_STREAM_MESSAGE_BYTES:
            raise ValueError("websocket message exceeded size limit")
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("websocket message is not UTF-8") from exc
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_STREAM_MESSAGE_BYTES:
        raise ValueError("websocket message exceeded size limit")
    try:
        decoded = json.loads(message)
    except json.JSONDecodeError as exc:
        raise ValueError("websocket message is invalid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) != 2:
        raise ValueError("websocket message has an invalid envelope")
    channel = normalize_channel(str(decoded[0]))
    payload = decoded[1]
    if not isinstance(payload, Mapping):
        raise ValueError("websocket payload must be an object")
    raw = dict(payload)
    base = channel.partition(":")[0]
    symbol: str | None = None
    for field in _SYMBOL_FIELDS:
        candidate = str(payload.get(field) or "").strip().upper()
        if candidate:
            symbol = candidate
            break
    if symbol is None and ":" in channel:
        symbol = channel.partition(":")[2]
    event_time_ms = next(
        (
            parsed
            for field in _TIME_FIELDS
            if (parsed := _timestamp_ms(payload.get(field))) is not None
        ),
        None,
    )
    now_ms = int(time.time() * 1_000) if received_at_ms is None else int(received_at_ms)
    issues: list[str] = []
    if base in _SYMBOL_REQUIRED and not symbol:
        issues.append("missing_symbol")
    if event_time_ms is None:
        issues.append("missing_event_time")
    else:
        if event_time_ms > now_ms + 5 * 60 * 1_000:
            issues.append("future_event_time")
        elif now_ms - event_time_ms > stale_after_ms:
            issues.append("stale")
    canonical = json.dumps([channel, raw], sort_keys=True, separators=(",", ":"), default=str)
    upstream_id = str(payload.get("id") or payload.get("trade_id") or "").strip()
    event_id = upstream_id or hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return UnusualWhalesStreamEvent(
        channel=channel,
        event_type=_EVENT_TYPES[base],
        symbol=symbol,
        event_time_ms=event_time_ms,
        received_at_ms=now_ms,
        event_id=event_id,
        values=_common_values(base, payload),
        raw=raw,
        quality={
            "source": "unusual_whales_websocket",
            "available": True,
            "valid": not issues,
            "stale": "stale" in issues,
            "age_ms": max(0, now_ms - event_time_ms) if event_time_ms else None,
            "issues": issues,
        },
    )


def _default_transport_factory(*, open_timeout: float, close_timeout: float) -> TransportFactory:
    def factory(url: str) -> StreamTransport | AbstractContextManager[StreamTransport]:
        from websockets.sync.client import connect

        return connect(
            url,
            open_timeout=open_timeout,
            close_timeout=close_timeout,
            max_size=MAX_STREAM_MESSAGE_BYTES,
        )

    return factory


class UnusualWhalesStreamClient:
    """Thread-safe websocket connection manager with replay-safe ingestion."""

    def __init__(
        self,
        api_key_loader: Callable[[], str],
        on_event: EventCallback,
        *,
        channels: tuple[str, ...] = (),
        transport_factory: TransportFactory | None = None,
        receive_timeout: float = 30.0,
        open_timeout: float = 8.0,
        close_timeout: float = 2.0,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
        dedupe_ttl_seconds: float = 15 * 60.0,
        dedupe_max_items: int = 100_000,
        stale_after_ms: int = 2 * 60 * 1_000,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._api_key_loader = api_key_loader
        self._on_event = on_event
        self.receive_timeout = max(0.1, float(receive_timeout))
        self.reconnect_min_seconds = max(0.0, float(reconnect_min_seconds))
        self.reconnect_max_seconds = max(self.reconnect_min_seconds, float(reconnect_max_seconds))
        self.dedupe_ttl_ms = max(1, int(dedupe_ttl_seconds * 1_000))
        self.dedupe_max_items = max(1, int(dedupe_max_items))
        self.stale_after_ms = max(1, int(stale_after_ms))
        self._transport_factory = transport_factory or _default_transport_factory(
            open_timeout=open_timeout,
            close_timeout=close_timeout,
        )
        self._lock = Lock()
        self._send_lock = Lock()
        self._stop = Event()
        self._wait = wait or self._stop.wait
        self._subscriptions = {normalize_channel(item) for item in channels}
        self._transport: StreamTransport | None = None
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._thread: Thread | None = None
        self._status = "stopped"
        self._connected = False
        self._connected_at_ms: int | None = None
        self._last_event_at_ms: int | None = None
        self._last_error: str | None = None
        self._reconnects = 0
        self._received = 0
        self._accepted = 0
        self._duplicates = 0
        self._malformed = 0
        self._callback_errors = 0

    def configured(self) -> bool:
        try:
            return bool(self._api_key_loader().strip())
        except Exception:
            return False

    def start(self) -> None:
        if not self.configured():
            with self._lock:
                self._status = "disabled"
                self._last_error = "credential_not_configured"
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._status = "connecting"
            self._thread = Thread(
                target=self.run_forever,
                daemon=True,
                name="unusual-whales-stream",
            )
            thread = self._thread
        thread.start()

    def stop(self, *, join_timeout: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            transport = self._transport
            thread = self._thread
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                with self._lock:
                    self._last_error = _safe_error(exc)
        if thread is not None and thread is not current_thread():
            thread.join(timeout=max(0.0, join_timeout))
        with self._lock:
            self._thread = None
            self._transport = None
            self._connected = False
            self._status = "stopped"

    def subscribe(self, channel: str) -> None:
        normalized = normalize_channel(channel)
        with self._lock:
            is_new = normalized not in self._subscriptions
            self._subscriptions.add(normalized)
            transport = self._transport if self._connected else None
        if is_new and transport is not None:
            self._send_membership(transport, normalized, "join")

    def unsubscribe(self, channel: str) -> None:
        normalized = normalize_channel(channel)
        with self._lock:
            existed = normalized in self._subscriptions
            self._subscriptions.discard(normalized)
            transport = self._transport if self._connected else None
        if existed and transport is not None:
            self._send_membership(transport, normalized, "leave")

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            last_event_age_ms = (
                max(0, int(time.time() * 1_000) - self._last_event_at_ms)
                if self._last_event_at_ms is not None
                else None
            )
            return {
                "status": self._status,
                "connected": self._connected,
                "healthy": self._connected and self._last_error is None,
                "connected_at_ms": self._connected_at_ms,
                "last_event_at_ms": self._last_event_at_ms,
                "last_event_age_ms": last_event_age_ms,
                "data_stale": (
                    last_event_age_ms is not None and last_event_age_ms > self.stale_after_ms
                ),
                "last_error": self._last_error,
                "reconnects": self._reconnects,
                "received": self._received,
                "accepted": self._accepted,
                "duplicates": self._duplicates,
                "malformed": self._malformed,
                "callback_errors": self._callback_errors,
                "subscriptions": sorted(self._subscriptions),
            }

    def health(self) -> dict[str, Any]:
        """Backward-friendly shorthand for callers that do not need a named snapshot."""

        return self.health_snapshot()

    def run_session(self) -> None:
        """Run one connection lifetime; mainly useful for deterministic tests."""

        api_key = self._api_key_loader().strip()
        if not api_key:
            raise RuntimeError("Unusual Whales API credential is not configured")
        url = f"{UNUSUAL_WHALES_STREAM_ORIGIN}?{urlencode({'token': api_key})}"
        resource = self._transport_factory(url)
        context = resource if hasattr(resource, "__enter__") else nullcontext(resource)
        with context as transport:
            if not isinstance(transport, StreamTransport):
                raise TypeError("transport does not implement the required websocket methods")
            with self._lock:
                self._transport = transport
                self._connected = True
                self._connected_at_ms = int(time.time() * 1_000)
                self._last_error = None
                self._status = "connected"
                subscriptions = sorted(self._subscriptions)
            for channel in subscriptions:
                self._send_membership(transport, channel, "join")
            try:
                while not self._stop.is_set():
                    try:
                        message = transport.recv(timeout=self.receive_timeout)
                    except TimeoutError:
                        ping = getattr(transport, "ping", None)
                        if callable(ping):
                            ping()
                        continue
                    if message is None:
                        raise ConnectionError("websocket transport closed")
                    self._ingest(message)
            finally:
                with self._lock:
                    if self._transport is transport:
                        self._transport = None
                    self._connected = False
                    if not self._stop.is_set():
                        self._status = "reconnecting"

    def run_forever(self, *, max_reconnects: int | None = None) -> None:
        attempts = 0
        delay = self.reconnect_min_seconds
        while not self._stop.is_set():
            with self._lock:
                self._status = "connecting" if attempts == 0 else "reconnecting"
            try:
                self.run_session()
                if self._stop.is_set():
                    break
                raise ConnectionError("websocket session ended")
            except Exception as exc:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._connected = False
                    self._last_error = _safe_error(exc)
                    self._status = "reconnecting"
                if max_reconnects is not None and attempts >= max_reconnects:
                    with self._lock:
                        self._status = "degraded"
                    return
                attempts += 1
                with self._lock:
                    self._reconnects += 1
                if self._wait(delay):
                    break
                delay = min(max(delay * 2, self.reconnect_min_seconds), self.reconnect_max_seconds)
        with self._lock:
            self._connected = False
            self._status = "stopped" if self._stop.is_set() else self._status

    def _send_membership(self, transport: StreamTransport, channel: str, msg_type: str) -> None:
        message = json.dumps(
            {"channel": channel, "msg_type": msg_type},
            separators=(",", ":"),
        )
        with self._send_lock:
            transport.send(message)

    def _ingest(self, message: str | bytes) -> None:
        with self._lock:
            self._received += 1
        try:
            event = normalize_stream_message(message, stale_after_ms=self.stale_after_ms)
        except (TypeError, ValueError):
            with self._lock:
                self._malformed += 1
            return
        if self._is_duplicate(event.event_id, event.received_at_ms):
            with self._lock:
                self._duplicates += 1
            return
        with self._lock:
            self._accepted += 1
            self._last_event_at_ms = event.received_at_ms
        try:
            self._on_event(event)
        except Exception:
            with self._lock:
                self._callback_errors += 1

    def _is_duplicate(self, event_id: str, now_ms: int) -> bool:
        cutoff = now_ms - self.dedupe_ttl_ms
        with self._lock:
            while self._seen:
                _, oldest_time = next(iter(self._seen.items()))
                if oldest_time >= cutoff:
                    break
                self._seen.popitem(last=False)
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return True
            self._seen[event_id] = now_ms
            while len(self._seen) > self.dedupe_max_items:
                self._seen.popitem(last=False)
        return False
