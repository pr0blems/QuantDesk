from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .finnhub import FinnhubClient, FinnhubClientError, FinnhubQuote
from .models import FinnhubQuoteSnapshot

FINNHUB_STREAM_ORIGIN = "wss://ws.finnhub.io"
FINNHUB_USAGE_SETTING_KEY = "market_data:finnhub:v1"
PRIORITY_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "PLTR",
    "COIN",
    "SPY",
    "QQQ",
    "JPM",
)
SYMBOL_ALIASES = {"BRKB": "BRK.B", "PAYP": "PYPL"}
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")


def _load_us_symbols(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    candidates: list[str] = []
    for item in payload.get("symbols", []):
        if not isinstance(item, dict) or item.get("underlyingType") != "EQUITY":
            continue
        raw = str(item.get("symbol") or "").strip().upper()
        if raw.endswith("USDT"):
            raw = raw[:-4]
        elif raw.endswith("USD1"):
            raw = raw[:-4]
        symbol = SYMBOL_ALIASES.get(raw, raw)
        if SYMBOL_PATTERN.fullmatch(symbol):
            candidates.append(symbol)
    unique = tuple(dict.fromkeys(candidates))
    priority = [symbol for symbol in PRIORITY_SYMBOLS if symbol in unique]
    return tuple(priority + [symbol for symbol in unique if symbol not in priority])


class FinnhubUsQuoteService:
    """One server-side Finnhub stream with rate-bounded REST quote fallback."""

    def __init__(
        self,
        client: FinnhubClient,
        symbols_config: Path,
        *,
        poll_seconds: float = 2.0,
        stale_seconds: int = 600,
        websocket_enabled: bool = True,
        engine: Engine | None = None,
        enabled: bool = True,
        market_open_checker: Callable[[], bool] | None = None,
        persist_interval_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.symbols = _load_us_symbols(symbols_config)
        self.symbol_set = set(self.symbols)
        self.poll_seconds = poll_seconds
        self.stale_seconds = stale_seconds
        self.websocket_enabled = websocket_enabled
        self.engine = engine
        self.enabled = bool(enabled)
        self.market_open_checker = market_open_checker or (lambda: True)
        self.persist_interval_seconds = max(1.0, float(persist_interval_seconds))
        self._lock = Lock()
        self._stop = Event()
        self._wakeup = Event()
        self._quotes: dict[str, FinnhubQuote] = {}
        self._errors: dict[str, str] = {}
        self._dirty_symbols: set[str] = set()
        self._persisted_symbols: set[str] = set()
        self._persisted = 0
        self._write_errors = 0
        self._last_persisted_at: datetime | None = None
        self._last_storage_refresh_monotonic = 0.0
        self._started = False
        self._stream_connected = False
        self._stream_error: str | None = None
        self._threads: list[Thread] = []

    def start(self) -> None:
        if self._started or not self.client.configured or not self.symbols:
            return
        self._hydrate_latest()
        self._started = True
        self._stop.clear()
        self._threads = [
            Thread(target=self._quote_loop, daemon=True, name="finnhub-quotes"),
        ]
        if self.websocket_enabled:
            self._threads.append(
                Thread(target=self._stream_loop, daemon=True, name="finnhub-trades")
            )
        if self.engine is not None:
            self._threads.append(
                Thread(target=self._persistence_loop, daemon=True, name="finnhub-writer")
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []
        self._started = False

    def set_enabled(self, enabled: bool) -> None:
        """Apply the platform switch without discarding the latest DB snapshot."""

        with self._lock:
            self.enabled = bool(enabled)
        self._wakeup.set()

    def _market_open(self) -> bool:
        try:
            return bool(self.market_open_checker())
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _collection_allowed(self) -> bool:
        with self._lock:
            enabled = self.enabled
        return bool(enabled and self.client.configured and self.symbols and self._market_open())

    def _wait(self, seconds: float) -> None:
        self._wakeup.clear()
        self._wakeup.wait(seconds)

    def _quote_loop(self) -> None:
        index = 0
        while not self._stop.is_set():
            if not self._collection_allowed():
                self._wait(5.0)
                continue
            symbol = self.symbols[index % len(self.symbols)]
            index += 1
            try:
                quote = self.client.quote(symbol)
                self._store_rest_quote(quote)
            except FinnhubClientError as exc:
                with self._lock:
                    self._errors[symbol] = exc.category
                if exc.category == "rate_limit":
                    self._wait(30)
                    continue
            self._wait(self.poll_seconds)

    def _store_rest_quote(self, quote: FinnhubQuote) -> None:
        """Store a REST snapshot without replacing an equal/newer live trade."""

        with self._lock:
            previous = self._quotes.get(quote.symbol)
            if previous is None or quote.source_timestamp > previous.source_timestamp:
                self._quotes[quote.symbol] = quote
            elif quote.source_timestamp == previous.source_timestamp and not previous.live:
                self._quotes[quote.symbol] = quote
            self._dirty_symbols.add(quote.symbol)
            self._errors.pop(quote.symbol, None)

    def _stream_loop(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            self._set_stream_state(False, "dependency")
            return
        backoff = 2.0
        while not self._stop.is_set():
            if not self._collection_allowed():
                self._set_stream_state(False, "market_closed" if self.enabled else "disabled")
                self._wait(5.0)
                continue
            url = f"{FINNHUB_STREAM_ORIGIN}?{urlencode({'token': self.client.api_key})}"
            try:
                with connect(
                    url,
                    open_timeout=self.client.timeout_seconds,
                    close_timeout=2,
                    max_size=512 * 1024,
                ) as websocket:
                    for symbol in self.symbols:
                        websocket.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                        if self._stop.wait(0.03):
                            return
                    self._set_stream_state(True, None)
                    backoff = 2.0
                    while not self._stop.is_set() and self._collection_allowed():
                        try:
                            message = websocket.recv(timeout=5)
                        except TimeoutError:
                            continue
                        self._ingest_stream_message(message)
            except Exception:
                self._set_stream_state(False, "disconnected")
                self._wait(backoff)
                backoff = min(backoff * 2, 60)
        self._set_stream_state(False, None)

    def _set_stream_state(self, connected: bool, error: str | None) -> None:
        with self._lock:
            self._stream_connected = connected
            self._stream_error = error

    def _ingest_stream_message(self, message: str | bytes) -> None:
        if not self._collection_allowed():
            return
        try:
            payload = json.loads(message)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("type") != "trade":
            return
        items = payload.get("data")
        if not isinstance(items, list) or len(items) > 10_000:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = item.get("s")
            price = item.get("p")
            timestamp_ms = item.get("t")
            volume = item.get("v")
            if (
                symbol not in self.symbol_set
                or isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or float(price) <= 0
                or isinstance(timestamp_ms, bool)
                or not isinstance(timestamp_ms, int)
                or timestamp_ms <= 0
            ):
                continue
            normalized_volume = (
                float(volume)
                if isinstance(volume, (int, float))
                and not isinstance(volume, bool)
                and math.isfinite(float(volume))
                else None
            )
            now = datetime.now(UTC)
            with self._lock:
                previous = self._quotes.get(symbol)
                source_timestamp = timestamp_ms // 1_000
                if previous is not None and source_timestamp < previous.source_timestamp:
                    continue
                if previous is None:
                    quote = FinnhubQuote(
                        symbol=symbol,
                        price=float(price),
                        change=None,
                        change_percent=None,
                        day_high=None,
                        day_low=None,
                        day_open=None,
                        previous_close=None,
                        source_timestamp=source_timestamp,
                        fetched_at=now,
                        volume=normalized_volume,
                        live=True,
                    )
                else:
                    change = (
                        float(price) - previous.previous_close
                        if previous.previous_close not in (None, 0)
                        else previous.change
                    )
                    change_percent = (
                        change / previous.previous_close * 100
                        if change is not None and previous.previous_close not in (None, 0)
                        else previous.change_percent
                    )
                    quote = replace(
                        previous,
                        price=float(price),
                        change=change,
                        change_percent=change_percent,
                        source_timestamp=source_timestamp,
                        fetched_at=now,
                        volume=normalized_volume,
                        live=True,
                    )
                self._quotes[symbol] = quote
                self._dirty_symbols.add(symbol)
                self._errors.pop(symbol, None)

    @staticmethod
    def _snapshot_quote(row: FinnhubQuoteSnapshot) -> FinnhubQuote:
        fetched_at = row.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return FinnhubQuote(
            symbol=row.symbol,
            price=float(row.price),
            change=float(row.change) if row.change is not None else None,
            change_percent=(float(row.change_percent) if row.change_percent is not None else None),
            day_high=float(row.day_high) if row.day_high is not None else None,
            day_low=float(row.day_low) if row.day_low is not None else None,
            day_open=float(row.day_open) if row.day_open is not None else None,
            previous_close=(float(row.previous_close) if row.previous_close is not None else None),
            source_timestamp=int(row.source_timestamp),
            fetched_at=fetched_at,
            volume=float(row.volume) if row.volume is not None else None,
            live=bool(row.live),
        )

    def _hydrate_latest(self) -> None:
        if self.engine is None:
            return
        try:
            latest_ids = (
                select(func.max(FinnhubQuoteSnapshot.id).label("id"))
                .where(FinnhubQuoteSnapshot.symbol.in_(self.symbols))
                .group_by(FinnhubQuoteSnapshot.symbol)
            )
            with Session(self.engine) as db:
                rows = db.scalars(
                    select(FinnhubQuoteSnapshot).where(FinnhubQuoteSnapshot.id.in_(latest_ids))
                ).all()
        except SQLAlchemyError:
            with self._lock:
                self._write_errors += 1
            return
        with self._lock:
            for row in rows:
                self._quotes[row.symbol] = self._snapshot_quote(row)
                self._persisted_symbols.add(row.symbol)

    def _refresh_reader_cache(self) -> None:
        """Refresh API-only instances from the database-backed worker cache.

        Quote collection belongs to the dedicated market worker.  API processes
        still construct this service so routes can share its read model, but they
        never call ``start`` and therefore do not own the worker's in-memory
        quotes.  Refresh those reader instances from persisted snapshots instead
        of incorrectly returning an empty provider.
        """

        if self.engine is None:
            return
        now = time.monotonic()
        with self._lock:
            if self._started or now - self._last_storage_refresh_monotonic < 1.0:
                return
            # Claim the short refresh window before I/O so concurrent API
            # requests do not stampede the database.
            self._last_storage_refresh_monotonic = now
        self._hydrate_latest()

    def _persist_quotes(self, quotes: Iterable[FinnhubQuote]) -> None:
        if self.engine is None:
            return
        snapshots = list(quotes)
        if not snapshots:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            with Session(self.engine, expire_on_commit=False) as db:
                for quote in snapshots:
                    fetched_value = quote.fetched_at
                    fetched_at = (
                        fetched_value.replace(tzinfo=UTC)
                        if fetched_value.tzinfo is None
                        else fetched_value.astimezone(UTC)
                    ).replace(tzinfo=None)
                    bucket_at = fetched_at.replace(second=0, microsecond=0)
                    row = db.scalar(
                        select(FinnhubQuoteSnapshot).where(
                            FinnhubQuoteSnapshot.symbol == quote.symbol,
                            FinnhubQuoteSnapshot.bucket_at == bucket_at,
                        )
                    )
                    if row is None:
                        row = FinnhubQuoteSnapshot(
                            symbol=quote.symbol,
                            bucket_at=bucket_at,
                            price=quote.price,
                            source_timestamp=quote.source_timestamp,
                            fetched_at=fetched_at,
                            live=quote.live,
                            captured_at=now,
                            updated_at=now,
                        )
                        db.add(row)
                    row.price = quote.price
                    row.change = quote.change
                    row.change_percent = quote.change_percent
                    row.day_high = quote.day_high
                    row.day_low = quote.day_low
                    row.day_open = quote.day_open
                    row.previous_close = quote.previous_close
                    row.volume = quote.volume
                    row.source_timestamp = quote.source_timestamp
                    row.fetched_at = fetched_at
                    row.live = quote.live
                    row.updated_at = now
                db.commit()
        except SQLAlchemyError:
            with self._lock:
                self._dirty_symbols.update(quote.symbol for quote in snapshots)
                self._write_errors += 1
            return
        with self._lock:
            self._persisted += len(snapshots)
            self._persisted_symbols.update(quote.symbol for quote in snapshots)
            self._last_persisted_at = now.replace(tzinfo=UTC)

    def _persistence_loop(self) -> None:
        while not self._stop.is_set():
            self._wait(self.persist_interval_seconds)
            with self._lock:
                symbols = tuple(self._dirty_symbols)
                self._dirty_symbols.clear()
                quotes = [self._quotes[symbol] for symbol in symbols if symbol in self._quotes]
            self._persist_quotes(quotes)
        with self._lock:
            symbols = tuple(self._dirty_symbols)
            self._dirty_symbols.clear()
            quotes = [self._quotes[symbol] for symbol in symbols if symbol in self._quotes]
        self._persist_quotes(quotes)

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if normalized.endswith("USDT") or normalized.endswith("USD1"):
            normalized = normalized[:-4]
        return SYMBOL_ALIASES.get(normalized, normalized)

    def latest_many(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        self._refresh_reader_cache()
        requested = {self.normalize_symbol(symbol) for symbol in symbols}
        now = datetime.now(UTC)
        with self._lock:
            quotes = {key: value for key, value in self._quotes.items() if key in requested}
            persisted = set(self._persisted_symbols)
        result: dict[str, dict[str, Any]] = {}
        for symbol, quote in quotes.items():
            item = asdict(quote)
            item.update(
                {
                    "available": True,
                    "stale": (now - quote.fetched_at).total_seconds() > self.stale_seconds,
                    "storage": "database" if symbol in persisted else "memory_pending",
                }
            )
            result[symbol] = item
        return result

    def snapshot(self) -> dict[str, Any]:
        self._refresh_reader_cache()
        now = datetime.now(UTC)
        with self._lock:
            quotes = dict(self._quotes)
            errors = dict(self._errors)
            stream_connected = self._stream_connected
            stream_error = self._stream_error
            enabled = self.enabled
            persisted_symbols = set(self._persisted_symbols)
            persisted = self._persisted
            write_errors = self._write_errors
            last_persisted_at = self._last_persisted_at
        market_open = self._market_open()
        items: list[dict[str, Any]] = []
        updated_at: datetime | None = None
        for symbol in self.symbols:
            quote = quotes.get(symbol)
            if quote is None:
                items.append(
                    {
                        "symbol": symbol,
                        "available": False,
                        "error_category": errors.get(symbol),
                    }
                )
                continue
            age = max(0.0, (now - quote.fetched_at).total_seconds())
            item = asdict(quote)
            item.update(
                {
                    "available": True,
                    "stale": age > self.stale_seconds,
                    "error_category": errors.get(symbol),
                    "storage": "database" if symbol in persisted_symbols else "memory_pending",
                }
            )
            items.append(item)
            if updated_at is None or quote.fetched_at > updated_at:
                updated_at = quote.fetched_at
        return {
            "configured": self.client.configured,
            "enabled": enabled,
            "market_open_only": True,
            "market_open": market_open,
            "collection_active": bool(enabled and market_open and self._started),
            "source": "finnhub",
            "exchange": "US",
            "total": len(self.symbols),
            "available": sum(1 for item in items if item["available"]),
            "stream_connected": stream_connected,
            "stream_error": stream_error,
            "updated_at": updated_at,
            "persisted": persisted,
            "write_errors": write_errors,
            "last_persisted_at": last_persisted_at,
            "quotes": items,
        }
