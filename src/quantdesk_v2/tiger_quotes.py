from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.client import HTTPSConnection
from threading import Lock
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

TIGER_QUOTE_ORIGIN = "https://hq2.skytigris.cn"
TIGER_BRIEF_PATH = "/stock_info/brief/all"
TIGER_DEPTH_ORIGIN = "https://hq-depth.skytigris.cn"
TIGER_DEPTH_PATH_PREFIX = "/stock_info/ask_bid/arca/"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_BATCH_SIZE = 200
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")
SYMBOL_ALIASES = {"BRKB": "BRK.B", "PAYP": "PYPL"}

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]
DepthTransport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


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
    change: float | None
    change_rate: float | None
    source_timestamp: int
    fetched_at: datetime
    session: str
    delayed: bool
    live: bool


@dataclass(frozen=True, slots=True)
class TigerDepthLevel:
    price: float
    quantity: float
    order_count: int


@dataclass(frozen=True, slots=True)
class TigerDepthSnapshot:
    symbol: str
    bids: tuple[TigerDepthLevel, ...]
    asks: tuple[TigerDepthLevel, ...]
    source_timestamp: int
    fetched_at: datetime

    @property
    def best_bid(self) -> float:
        return self.bids[0].price

    @property
    def best_ask(self) -> float:
        return self.asks[0].price

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    def as_market_snapshot(self, *, stale_seconds: int = 15) -> dict[str, Any]:
        mid_price = self.mid_price

        def rows(levels: tuple[TigerDepthLevel, ...]) -> list[dict[str, Any]]:
            cumulative_notional = 0.0
            output: list[dict[str, Any]] = []
            for rank, level in enumerate(levels, start=1):
                notional = level.price * level.quantity
                cumulative_notional += notional
                output.append(
                    {
                        "rank": rank,
                        "price": level.price,
                        "quantity": level.quantity,
                        "order_count": level.order_count,
                        "notional": notional,
                        "cumulative_notional": cumulative_notional,
                        "distance_bps": (level.price / mid_price - 1) * 10_000,
                    }
                )
            return output

        bids = rows(self.bids)
        asks = rows(self.asks)
        bid_depth = sum(float(row["notional"]) for row in bids)
        ask_depth = sum(float(row["notional"]) for row in asks)
        total_depth = bid_depth + ask_depth
        spread = self.best_ask - self.best_bid
        age_seconds = max(0.0, time.time() - self.source_timestamp)
        largest_bid = max(bids, key=lambda row: float(row["notional"]))
        largest_ask = max(asks, key=lambda row: float(row["notional"]))
        return {
            "available": True,
            "source": "tiger_level2",
            "label": "TG",
            "venue": "us_cash_arca_level2",
            "symbol": self.symbol,
            "contract_symbol": self.symbol,
            "price": mid_price,
            "mid_price": mid_price,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": spread,
            "spread_bps": spread / mid_price * 10_000,
            "bids": bids,
            "asks": asks,
            "limit": max(len(bids), len(asks)),
            "levels_available": max(len(bids), len(asks)),
            "bid_depth_notional": bid_depth,
            "ask_depth_notional": ask_depth,
            "bid_ask_ratio": bid_depth / ask_depth if ask_depth > 0 else None,
            "book_imbalance": (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0,
            "largest_bid_wall": largest_bid,
            "largest_ask_wall": largest_ask,
            "source_timestamp": self.source_timestamp,
            "captured_at": self.source_timestamp,
            "fetched_at": self.fetched_at,
            "age_seconds": age_seconds,
            "live": age_seconds <= stale_seconds,
            "stale": age_seconds > stale_seconds,
            "delayed": False,
            "transport": "tiger_level2",
            "last_update_id": self.source_timestamp,
            "error_category": None,
        }


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


def _depth_https_transport(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hq-depth.skytigris.cn"
        or parsed.port not in (None, 443)
        or not parsed.path.startswith(TIGER_DEPTH_PATH_PREFIX)
        or parsed.query != "props=askBidDepth"
        or parsed.fragment
    ):
        raise TigerQuoteClientError("rejected")
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    try:
        connection.request("GET", f"{parsed.path}?{parsed.query}", headers=headers)
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


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


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

    candidates: list[
        tuple[int, float, float | None, float | None, float | None, str]
    ] = []

    def add_candidate(payload: Mapping[str, Any], *, session: str) -> None:
        timestamp = _timestamp_seconds(payload.get("timestamp"))
        price = _finite_price(payload.get("latestPrice"))
        if timestamp is None or price is None:
            return
        direct_change = _finite_number(payload.get("change"))
        direct_change_rate = _finite_number(payload.get("changeRate"))
        candidates.append(
            (
                timestamp,
                price,
                _finite_price(payload.get("preClose")),
                direct_change,
                direct_change_rate,
                session,
            )
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

    timestamp, price, previous_close, change, change_rate, session = max(
        candidates, key=lambda value: value[0]
    )
    if change is None and previous_close is not None:
        change = price - previous_close
    if change_rate is None and change is not None and previous_close is not None:
        change_rate = change / previous_close
    delay_value = item.get("delay")
    delayed = bool(delay_value not in (None, 0, 0.0, False))
    age_seconds = max(0.0, now.timestamp() - timestamp)
    return TigerQuote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        change=change,
        change_rate=change_rate,
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


class TigerDepthClient:
    """Direct client for the NYSE Arca Level-2 endpoint used by the demo."""

    def __init__(
        self,
        base_url: str,
        authorization: str,
        *,
        timeout_seconds: float = 5.0,
        transport: DepthTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hq-depth.skytigris.cn"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Tiger depth base URL must be the approved HTTPS origin")
        self.base_url = TIGER_DEPTH_ORIGIN
        self.authorization = authorization.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or _depth_https_transport

    @property
    def configured(self) -> bool:
        return bool(self.authorization)

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return TigerQuoteClient.normalize_symbol(symbol)

    @staticmethod
    def _levels(payload: object, *, descending: bool) -> tuple[TigerDepthLevel, ...]:
        if not isinstance(payload, list):
            return ()
        levels: list[TigerDepthLevel] = []
        for item in payload[:100]:
            if not isinstance(item, Mapping):
                continue
            price = _finite_price(item.get("price"))
            quantity = _finite_price(item.get("volume"))
            if price is None or quantity is None:
                continue
            sub_volume = item.get("subVolume")
            levels.append(
                TigerDepthLevel(
                    price=price,
                    quantity=quantity,
                    order_count=len(sub_volume) if isinstance(sub_volume, list) else 0,
                )
            )
        levels.sort(key=lambda item: item.price, reverse=descending)
        return tuple(levels[:40])

    def depth(self, symbol: str) -> TigerDepthSnapshot:
        if not self.configured:
            raise TigerQuoteClientError("not_configured")
        normalized = self.normalize_symbol(symbol)
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise TigerQuoteClientError("invalid_symbol")
        url = f"{self.base_url}{TIGER_DEPTH_PATH_PREFIX}{url_quote(normalized, safe='.-')}?props=askBidDepth"
        status, raw = self.transport(
            url,
            {
                "Accept": "application/json",
                "Authorization": self.authorization,
                "User-Agent": "QuantDesk/2 TigerDepth",
            },
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
        if not isinstance(payload, Mapping):
            raise TigerQuoteClientError("invalid_response")
        depth = payload.get("askBidDepth")
        if not isinstance(depth, Mapping):
            raise TigerQuoteClientError("invalid_response")
        bids = self._levels(depth.get("bid"), descending=True)
        asks = self._levels(depth.get("ask"), descending=False)
        if not bids or not asks or bids[0].price >= asks[0].price:
            raise TigerQuoteClientError("invalid_response")
        now = datetime.now(UTC)
        source_timestamp = (
            _timestamp_seconds(payload.get("timestamp"))
            or _timestamp_seconds(payload.get("serverTime"))
            or int(now.timestamp())
        )
        return TigerDepthSnapshot(
            symbol=normalized,
            bids=bids,
            asks=asks,
            source_timestamp=source_timestamp,
            fetched_at=now,
        )


class TigerUsDepthService:
    """Small shared cache for Tiger Level-2 snapshots and derived TG mid prices."""

    def __init__(
        self,
        client: TigerDepthClient,
        *,
        cache_seconds: float = 2.0,
        stale_seconds: int = 15,
        max_workers: int = 8,
    ) -> None:
        self.client = client
        self.cache_seconds = max(0.5, float(cache_seconds))
        self.stale_seconds = max(5, int(stale_seconds))
        self.max_workers = max(1, min(12, int(max_workers)))
        self._lock = Lock()
        self._fetch_lock = Lock()
        self._snapshots: dict[str, TigerDepthSnapshot] = {}
        self._fetched_monotonic: dict[str, float] = {}
        self._errors: dict[str, str] = {}

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
            refresh = [
                symbol
                for symbol in requested
                if symbol not in self._snapshots
                or now_monotonic - self._fetched_monotonic.get(symbol, 0.0) >= self.cache_seconds
            ]
        if refresh:
            with self._fetch_lock:
                now_monotonic = time.monotonic()
                with self._lock:
                    refresh = [
                        symbol
                        for symbol in requested
                        if symbol not in self._snapshots
                        or now_monotonic - self._fetched_monotonic.get(symbol, 0.0)
                        >= self.cache_seconds
                    ]
                if refresh:
                    with ThreadPoolExecutor(
                        max_workers=min(self.max_workers, len(refresh))
                    ) as executor:
                        futures = {
                            executor.submit(self.client.depth, symbol): symbol
                            for symbol in refresh
                        }
                        for future in as_completed(futures):
                            symbol = futures[future]
                            try:
                                snapshot = future.result()
                            except TigerQuoteClientError as exc:
                                with self._lock:
                                    self._errors[symbol] = exc.category
                                    self._fetched_monotonic[symbol] = now_monotonic
                            else:
                                with self._lock:
                                    self._snapshots[symbol] = snapshot
                                    self._errors.pop(symbol, None)
                                    self._fetched_monotonic[symbol] = now_monotonic
        with self._lock:
            snapshots = {symbol: self._snapshots.get(symbol) for symbol in requested}
            errors = {symbol: self._errors.get(symbol) for symbol in requested}
        result: dict[str, dict[str, Any]] = {}
        for symbol, snapshot in snapshots.items():
            if snapshot is None:
                continue
            item = snapshot.as_market_snapshot(stale_seconds=self.stale_seconds)
            item["error_category"] = errors.get(symbol)
            result[symbol] = item
        return result

    def latest_one(self, symbol: str) -> dict[str, Any] | None:
        normalized = self.client.normalize_symbol(symbol)
        return self.latest_many([normalized]).get(normalized)
