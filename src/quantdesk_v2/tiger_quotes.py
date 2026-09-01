from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.client import HTTPSConnection
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

TIGER_QUOTE_ORIGIN = "https://hq2.skytigris.cn"
TIGER_BRIEF_PATH = "/stock_info/brief/all"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_BATCH_SIZE = 200
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")
SYMBOL_ALIASES = {"BRKB": "BRK.B", "PAYP": "PYPL"}

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


class TigerQuoteClientError(RuntimeError):
    """A redacted Tiger upstream error safe for logs and API health output."""

    def __init__(self, category: str):
        super().__init__("Tiger quote request failed")
        self.category = category


@dataclass(frozen=True, slots=True)
class TigerQuote:
    symbol: str
    price: float
    previous_close: float | None
    source_timestamp: int
    fetched_at: datetime
    session: str
    delayed: bool
    live: bool


def _https_transport(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hq2.skytigris.cn"
        or parsed.port not in (None, 443)
        or parsed.path != TIGER_BRIEF_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise TigerQuoteClientError("rejected")
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    try:
        connection.request("POST", parsed.path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TigerQuoteClientError("invalid_response")
        return response.status, payload
    except TimeoutError as exc:
        raise TigerQuoteClientError("timeout") from exc
    except TigerQuoteClientError:
        raise
    except OSError as exc:
        raise TigerQuoteClientError("network") from exc
    finally:
        connection.close()


def _finite_price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def _timestamp_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1_000
    normalized = int(timestamp)
    return normalized if normalized > 0 else None


def _parse_quote(item: Mapping[str, Any], now: datetime) -> TigerQuote | None:
    symbol = TigerQuoteClient.normalize_symbol(str(item.get("symbol") or ""))
    if not SYMBOL_PATTERN.fullmatch(symbol):
        return None

    candidates: list[tuple[int, float, float | None, str]] = []

    def add_candidate(payload: Mapping[str, Any], *, session: str) -> None:
        timestamp = _timestamp_seconds(payload.get("timestamp"))
        price = _finite_price(payload.get("latestPrice"))
        if timestamp is None or price is None:
            return
        candidates.append(
            (timestamp, price, _finite_price(payload.get("preClose")), session)
        )

    add_candidate(item, session="regular")
    for key, session in (
        ("hourTrading", "extended_hours"),
        ("overnightTrading", "overnight"),
    ):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            add_candidate(nested, session=session)
    if not candidates:
        return None

    timestamp, price, previous_close, session = max(candidates, key=lambda value: value[0])
    delay_value = item.get("delay")
    delayed = bool(delay_value not in (None, 0, 0.0, False))
    age_seconds = max(0.0, now.timestamp() - timestamp)
    return TigerQuote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        source_timestamp=timestamp,
        fetched_at=now,
        session=session,
        delayed=delayed,
        live=bool(not delayed and age_seconds <= 120),
    )


class TigerQuoteClient:
    """Minimal server-side client for Tiger's authenticated batch quote endpoint."""

    def __init__(
        self,
        base_url: str,
        authorization: str,
        *,
        timeout_seconds: float = 5.0,
        transport: Transport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hq2.skytigris.cn"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Tiger quote base URL must be the approved HTTPS origin")
        self.base_url = TIGER_QUOTE_ORIGIN
        self.authorization = authorization.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or _https_transport

    @property
    def configured(self) -> bool:
        return bool(self.authorization)

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if normalized.endswith("USDT") or normalized.endswith("USD1"):
            normalized = normalized[:-4]
        return SYMBOL_ALIASES.get(normalized, normalized)

    def quotes(self, symbols: Iterable[str]) -> dict[str, TigerQuote]:
        if not self.configured:
            raise TigerQuoteClientError("not_configured")
        normalized = tuple(
            dict.fromkeys(
                symbol
                for value in symbols
                if (symbol := self.normalize_symbol(value))
                and SYMBOL_PATTERN.fullmatch(symbol)
            )
        )
        if not normalized:
            return {}
        if len(normalized) > MAX_BATCH_SIZE:
            raise TigerQuoteClientError("batch_too_large")
        body = json.dumps(
            {"items": [{"symbol": symbol} for symbol in normalized]},
            separators=(",", ":"),
        ).encode("utf-8")
        status, raw = self.transport(
            f"{self.base_url}{TIGER_BRIEF_PATH}",
            {
                "Accept": "application/json",
                "Authorization": self.authorization,
                "Content-Type": "application/json",
                "User-Agent": "QuantDesk/2 TigerQuotes",
            },
            body,
            self.timeout_seconds,
        )
        if status in {401, 403}:
            raise TigerQuoteClientError("authentication")
        if status == 429:
            raise TigerQuoteClientError("rate_limit")
        if status != 200:
            raise TigerQuoteClientError("upstream")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TigerQuoteClientError("invalid_response") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("ret") != 0
            or not isinstance(items, list)
            or len(items) > MAX_BATCH_SIZE
        ):
            raise TigerQuoteClientError("invalid_response")
        now = datetime.now(UTC)
        quotes: dict[str, TigerQuote] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            quote = _parse_quote(item, now)
            if quote is not None and quote.symbol in normalized:
                quotes[quote.symbol] = quote
        return quotes


class TigerUsQuoteService:
    """Request-scoped batch cache for Tiger cash-equity reference prices.

    The service talks directly to Tiger's upstream endpoint. It never depends on
    the local demo, and it never promotes a last-good snapshot to a live quote.
    """

    def __init__(
        self,
        client: TigerQuoteClient,
        *,
        cache_seconds: float = 2.0,
        stale_seconds: int = 900,
    ) -> None:
        self.client = client
        self.cache_seconds = max(0.5, float(cache_seconds))
        self.stale_seconds = max(60, int(stale_seconds))
        self._lock = Lock()
        self._fetch_lock = Lock()
        self._quotes: dict[str, TigerQuote] = {}
        self._last_fetch_monotonic = 0.0
        self._error_category: str | None = None

    def latest_many(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        requested = tuple(
            dict.fromkeys(
                symbol
                for value in symbols
                if (symbol := self.client.normalize_symbol(value))
                and SYMBOL_PATTERN.fullmatch(symbol)
            )
        )
        if not requested or not self.client.configured:
            return {}
        now_monotonic = time.monotonic()
        with self._lock:
            needs_refresh = (
                any(symbol not in self._quotes for symbol in requested)
                or now_monotonic - self._last_fetch_monotonic >= self.cache_seconds
            )
        if needs_refresh:
            with self._fetch_lock:
                now_monotonic = time.monotonic()
                with self._lock:
                    needs_refresh = (
                        any(symbol not in self._quotes for symbol in requested)
                        or now_monotonic - self._last_fetch_monotonic >= self.cache_seconds
                    )
                if needs_refresh:
                    try:
                        refreshed: dict[str, TigerQuote] = {}
                        for start in range(0, len(requested), MAX_BATCH_SIZE):
                            refreshed.update(
                                self.client.quotes(
                                    requested[start : start + MAX_BATCH_SIZE]
                                )
                            )
                    except TigerQuoteClientError as exc:
                        with self._lock:
                            self._error_category = exc.category
                            self._last_fetch_monotonic = now_monotonic
                    else:
                        with self._lock:
                            self._quotes.update(refreshed)
                            self._error_category = None
                            self._last_fetch_monotonic = now_monotonic
        now = datetime.now(UTC)
        with self._lock:
            quotes = {symbol: self._quotes.get(symbol) for symbol in requested}
            error_category = self._error_category
        result: dict[str, dict[str, Any]] = {}
        for symbol, quote in quotes.items():
            if quote is None:
                continue
            age_seconds = max(0.0, (now.timestamp() - quote.source_timestamp))
            item = asdict(quote)
            item.update(
                {
                    "available": True,
                    "stale": age_seconds > self.stale_seconds,
                    "error_category": error_category,
                }
            )
            result[symbol] = item
        return result
