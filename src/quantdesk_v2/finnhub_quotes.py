from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlencode

from .finnhub import FinnhubClient, FinnhubClientError, FinnhubQuote

FINNHUB_STREAM_ORIGIN = "wss://ws.finnhub.io"
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
    ) -> None:
        self.client = client
        self.symbols = _load_us_symbols(symbols_config)
        self.symbol_set = set(self.symbols)
        self.poll_seconds = poll_seconds
        self.stale_seconds = stale_seconds
        self.websocket_enabled = websocket_enabled
        self._lock = Lock()
        self._stop = Event()
        self._quotes: dict[str, FinnhubQuote] = {}
        self._errors: dict[str, str] = {}
        self._started = False
        self._stream_connected = False
        self._stream_error: str | None = None
        self._threads: list[Thread] = []

    def start(self) -> None:
        if self._started or not self.client.configured or not self.symbols:
            return
        self._started = True
        self._stop.clear()
        self._threads = [
            Thread(target=self._quote_loop, daemon=True, name="finnhub-quotes"),
        ]
        if self.websocket_enabled:
            self._threads.append(
                Thread(target=self._stream_loop, daemon=True, name="finnhub-trades")
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []
        self._started = False

    def _quote_loop(self) -> None:
        index = 0
        while not self._stop.is_set():
            symbol = self.symbols[index % len(self.symbols)]
            index += 1
            try:
                quote = self.client.quote(symbol)
                self._store_rest_quote(quote)
            except FinnhubClientError as exc:
                with self._lock:
                    self._errors[symbol] = exc.category
                if exc.category == "rate_limit":
                    self._stop.wait(30)
                    continue
            self._stop.wait(self.poll_seconds)

    def _store_rest_quote(self, quote: FinnhubQuote) -> None:
        """Store a REST snapshot without replacing an equal/newer live trade."""

        with self._lock:
            previous = self._quotes.get(quote.symbol)
            if previous is None or quote.source_timestamp > previous.source_timestamp:
                self._quotes[quote.symbol] = quote
            elif quote.source_timestamp == previous.source_timestamp and not previous.live:
                self._quotes[quote.symbol] = quote
            self._errors.pop(quote.symbol, None)

    def _stream_loop(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            self._set_stream_state(False, "dependency")
            return
        backoff = 2.0
        while not self._stop.is_set():
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
                    while not self._stop.is_set():
                        try:
                            message = websocket.recv(timeout=5)
                        except TimeoutError:
                            continue
                        self._ingest_stream_message(message)
            except Exception:
                self._set_stream_state(False, "disconnected")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        self._set_stream_state(False, None)

    def _set_stream_state(self, connected: bool, error: str | None) -> None:
        with self._lock:
            self._stream_connected = connected
            self._stream_error = error

    def _ingest_stream_message(self, message: str | bytes) -> None:
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
                        source_timestamp=timestamp_ms // 1_000,
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
                        source_timestamp=timestamp_ms // 1_000,
                        fetched_at=now,
                        volume=normalized_volume,
                        live=True,
                    )
                self._quotes[symbol] = quote
                self._errors.pop(symbol, None)

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._lock:
            quotes = dict(self._quotes)
            errors = dict(self._errors)
            stream_connected = self._stream_connected
            stream_error = self._stream_error
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
                }
            )
            items.append(item)
            if updated_at is None or quote.fetched_at > updated_at:
                updated_at = quote.fetched_at
        return {
            "configured": self.client.configured,
            "source": "finnhub",
            "exchange": "US",
            "total": len(self.symbols),
            "available": sum(1 for item in items if item["available"]),
            "stream_connected": stream_connected,
            "stream_error": stream_error,
            "updated_at": updated_at,
            "quotes": items,
        }
