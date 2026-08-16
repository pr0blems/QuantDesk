"""Server-side Unusual Whales market-data adapter.

The adapter deliberately keeps the upstream origin fixed and loads the bearer
credential through a callback.  Secrets therefore never enter browser payloads
or source rows.  It is used as a high-frequency market context source; mapped
Binance contract prices remain the execution/settlement price of record.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

UNUSUAL_WHALES_ORIGIN = "https://api.unusualwhales.com"
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_MARKET_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UnusualWhalesMarketError(RuntimeError):
    """Raised when the allowlisted market-data request cannot be completed."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "invalid_data",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retryable = retryable


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed


def _timestamp_ms(value: Any) -> int | None:
    """Normalize an epoch or ISO-8601 upstream timestamp to milliseconds."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        if parsed < 10_000_000_000:  # seconds
            parsed *= 1_000
        else:
            while parsed > 10_000_000_000_000:  # micro/nanoseconds
                parsed /= 1_000
        return int(parsed)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed_number = float(raw)
    except ValueError:
        parsed_number = None
    if parsed_number is not None and math.isfinite(parsed_number):
        return _timestamp_ms(parsed_number)
    try:
        parsed_date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_date.tzinfo is None:
        parsed_date = parsed_date.replace(tzinfo=UTC)
    return int(parsed_date.timestamp() * 1_000)


def _quality(
    source: str,
    *,
    available: bool,
    source_time: Any = None,
    stale_after_ms: int | None = None,
    issues: Sequence[str] = (),
) -> dict[str, Any]:
    received_at_ms = int(time.time() * 1_000)
    source_time_ms = _timestamp_ms(source_time)
    age_ms = max(0, received_at_ms - source_time_ms) if source_time_ms else None
    issue_list = list(dict.fromkeys(str(item) for item in issues if item))
    stale = bool(stale_after_ms is not None and age_ms is not None and age_ms > stale_after_ms)
    if stale:
        issue_list.append("stale")
    return {
        "source": source,
        "available": bool(available),
        "valid": bool(available) and not issue_list,
        "stale": stale,
        "source_time_ms": source_time_ms,
        "received_at_ms": received_at_ms,
        "age_ms": age_ms,
        "issues": issue_list,
    }


def _market_date_value(value: str | date | None) -> str | None:
    if value is None:
        return None
    raw = value.isoformat() if isinstance(value, date) else str(value).strip()
    if not _MARKET_DATE.fullmatch(raw):
        raise UnusualWhalesMarketError("invalid market date")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise UnusualWhalesMarketError("invalid market date") from exc
    return raw


class UnusualWhalesMarketClient:
    """Small synchronous client for stock-state, quote and market-tide data."""

    def __init__(
        self,
        api_key_loader: Callable[[], str],
        *,
        timeout_seconds: float = 5.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.25,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key_loader = api_key_loader
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_retries = max(0, min(int(max_retries), 3))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._wait = wait
        self._credential_lock = Lock()
        self._credential = ""
        self._credential_loaded_at = 0.0

    def _api_key(self) -> str:
        with self._credential_lock:
            if self._credential and time.monotonic() - self._credential_loaded_at < 60:
                return self._credential
            self._credential = self._api_key_loader().strip()
            self._credential_loaded_at = time.monotonic()
            return self._credential

    def configured(self) -> bool:
        try:
            return bool(self._api_key())
        except Exception:
            return False

    def _get(self, path: str, *, maximum_bytes: int = 2 * 1024 * 1024) -> Any:
        if not path.startswith("/api/") or "://" in path or "\\" in path:
            raise UnusualWhalesMarketError("invalid Unusual Whales API path")
        api_key = self._api_key()
        if not api_key:
            raise UnusualWhalesMarketError("Unusual Whales API credential is not configured")
        request = Request(  # noqa: S310 - origin is fixed HTTPS and path is allowlisted
            f"{UNUSUAL_WHALES_ORIGIN}{path}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "QuantDesk/2 market-context",
            },
        )
        raw: bytes | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(  # noqa: S310 - request origin is fixed HTTPS
                    request, timeout=self.timeout_seconds
                ) as response:
                    raw = response.read(maximum_bytes + 1)
                break
            except HTTPError as exc:
                status_code = int(exc.code)
                category = (
                    "authentication"
                    if status_code in {401, 403}
                    else "rate_limit"
                    if status_code == 429
                    else "upstream"
                    if status_code >= 500
                    else "request"
                )
                retryable = status_code == 429 or status_code >= 500
                if retryable and attempt < self.max_retries:
                    self._wait(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise UnusualWhalesMarketError(
                    "Unusual Whales market request failed",
                    category=category,
                    status_code=status_code,
                    retryable=retryable,
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    self._wait(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise UnusualWhalesMarketError(
                    "Unusual Whales market request failed",
                    category="network",
                    retryable=True,
                ) from exc
        if raw is None:
            raise UnusualWhalesMarketError(
                "Unusual Whales market request failed",
                category="network",
                retryable=True,
            )
        if len(raw) > maximum_bytes:
            raise UnusualWhalesMarketError("Unusual Whales response exceeded the size limit")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnusualWhalesMarketError("Unusual Whales returned invalid JSON") from exc
        if not isinstance(payload, Mapping) or "data" not in payload:
            raise UnusualWhalesMarketError("Unusual Whales returned an invalid envelope")
        return payload["data"]

    @staticmethod
    def _path(path: str, params: Mapping[str, Any] | None = None) -> str:
        filtered = {
            key: value for key, value in (params or {}).items() if value is not None and value != ""
        }
        return f"{path}?{urlencode(filtered)}" if filtered else path

    @staticmethod
    def _ticker(value: str) -> str:
        ticker = value.strip().upper()
        if not _TICKER.fullmatch(ticker):
            raise UnusualWhalesMarketError("invalid US ticker")
        return ticker

    def stock_state(self, ticker: str) -> dict[str, Any]:
        symbol = self._ticker(ticker)
        data = self._get(f"/api/stock/{quote(symbol, safe='.-')}/stock-state")
        if not isinstance(data, Mapping):
            raise UnusualWhalesMarketError("Unusual Whales stock state is unavailable")
        price = _number(data.get("close"))
        issues = [] if price not in (None, 0) else ["missing_price"]
        result = {
            "symbol": symbol,
            "price": price,
            "open": _number(data.get("open")),
            "high": _number(data.get("high")),
            "low": _number(data.get("low")),
            "previous_close": _number(data.get("prev_close")),
            "market_time": str(data.get("market_time") or "").lower(),
            "tape_time": data.get("tape_time"),
            "total_volume": _number(data.get("total_volume")),
            "volume": _number(data.get("volume")),
        }
        result["quality"] = _quality(
            "unusual_whales_stock_state",
            available=price not in (None, 0),
            source_time=data.get("tape_time"),
            stale_after_ms=15 * 60 * 1_000,
            issues=issues,
        )
        return result

    def stock_quote(self, ticker: str) -> dict[str, Any]:
        """Return the latest trade/NBBO reference for diagnostics and future UI use."""

        symbol = self._ticker(ticker)
        data = self._get(f"/api/stock/{quote(symbol, safe='.-')}/quote")
        if not isinstance(data, Mapping):
            raise UnusualWhalesMarketError("Unusual Whales stock quote is unavailable")
        trade = data.get("last_trade") if isinstance(data.get("last_trade"), Mapping) else {}
        quote_row = data.get("quote") if isinstance(data.get("quote"), Mapping) else {}
        bid = quote_row.get("bid") if isinstance(quote_row.get("bid"), Mapping) else {}
        ask = quote_row.get("ask") if isinstance(quote_row.get("ask"), Mapping) else {}
        values = data.get("quote_values") if isinstance(data.get("quote_values"), Mapping) else {}
        price = _number(trade.get("price"))
        bid_price = _number(bid.get("price"))
        ask_price = _number(ask.get("price"))
        midpoint = _number(values.get("midpoint"))
        if midpoint is None and bid_price is not None and ask_price is not None:
            midpoint = (bid_price + ask_price) / 2
        spread_bps = _number(values.get("spread_bps"))
        if (
            spread_bps is None
            and bid_price is not None
            and ask_price is not None
            and midpoint not in (None, 0)
        ):
            spread_bps = max(0.0, (ask_price - bid_price) / midpoint * 10_000)
        quote_age_ms = _integer(values.get("quote_age_ms"))
        quote_time_ms = _timestamp_ms(quote_row.get("time"))
        issues: list[str] = []
        if price in (None, 0):
            issues.append("missing_trade")
        if bid_price is None or ask_price is None:
            issues.append("missing_nbbo")
        elif ask_price < bid_price:
            issues.append("crossed_nbbo")
        if quote_age_ms is not None and quote_age_ms < 0:
            issues.append("invalid_quote_age")
        elif quote_age_ms is not None and quote_age_ms > 10 * 60 * 1_000:
            issues.append("stale_quote")
        result = {
            "symbol": symbol,
            "price": price,
            "trade_time_ms": _timestamp_ms(trade.get("time")),
            "volume": _number(trade.get("vol")),
            "market_time": str(data.get("market_time") or "").lower(),
            "bid": bid_price,
            "bid_size": _integer(bid.get("size")),
            "ask": ask_price,
            "ask_size": _integer(ask.get("size")),
            "quote_time_ms": quote_time_ms,
            "midpoint": midpoint,
            "size_weighted_midpoint": _number(values.get("size_weighted_midpoint")),
            "spread": _number(values.get("spread")),
            "spread_bps": spread_bps,
            "quote_age_ms": quote_age_ms,
            "size_imbalance": _number(values.get("size_imbalance")),
        }
        result["quality"] = _quality(
            "unusual_whales_stock_quote",
            available=price not in (None, 0),
            source_time=quote_time_ms or result["trade_time_ms"],
            stale_after_ms=10 * 60 * 1_000,
            issues=issues,
        )
        return result

    def stock_states(
        self, tickers: Sequence[str], *, workers: int = 6
    ) -> dict[str, dict[str, Any]]:
        symbols = list(dict.fromkeys(self._ticker(item) for item in tickers))
        if not symbols:
            return {}
        output: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(max(1, workers), len(symbols))) as executor:
            pending = {executor.submit(self.stock_state, symbol): symbol for symbol in symbols}
            for future in as_completed(pending):
                symbol = pending[future]
                try:
                    state = future.result()
                except UnusualWhalesMarketError:
                    continue
                if state.get("price") not in (None, 0):
                    output[symbol] = state
        return output

    def stock_quotes(
        self, tickers: Sequence[str], *, workers: int = 6
    ) -> dict[str, dict[str, Any]]:
        """Fetch quote snapshots concurrently while isolating per-symbol failures."""

        symbols = list(dict.fromkeys(self._ticker(item) for item in tickers))
        if not symbols:
            return {}
        output: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(max(1, workers), len(symbols))) as executor:
            pending = {executor.submit(self.stock_quote, symbol): symbol for symbol in symbols}
            for future in as_completed(pending):
                symbol = pending[future]
                try:
                    quote_row = future.result()
                except UnusualWhalesMarketError:
                    continue
                if quote_row.get("price") not in (None, 0):
                    output[symbol] = quote_row
        return output

    def market_tide(self) -> dict[str, Any]:
        data = self._get("/api/market/market-tide?interval_5m=true")
        rows = [row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []
        if not rows:
            return {
                "available": False,
                "source": "unusual_whales_market_tide",
                "quality": _quality(
                    "unusual_whales_market_tide",
                    available=False,
                    issues=("empty",),
                ),
            }
        latest = max(rows, key=lambda row: str(row.get("timestamp") or ""))
        call_premium = _number(latest.get("net_call_premium"))
        put_premium = _number(latest.get("net_put_premium"))
        net_premium = (
            call_premium - put_premium
            if call_premium is not None and put_premium is not None
            else None
        )
        net_volume = _number(latest.get("net_volume"))
        bias_value = net_premium if net_premium is not None else net_volume
        bias = "bull" if (bias_value or 0) > 0 else "bear" if (bias_value or 0) < 0 else "neutral"
        result = {
            "available": True,
            "source": "unusual_whales_market_tide",
            "timestamp": latest.get("timestamp"),
            "net_call_premium": call_premium,
            "net_put_premium": put_premium,
            "net_premium": net_premium,
            "net_volume": net_volume,
            "bias": bias,
            "samples": len(rows),
        }
        result["quality"] = _quality(
            "unusual_whales_market_tide",
            available=True,
            source_time=latest.get("timestamp"),
            stale_after_ms=30 * 60 * 1_000,
            issues=() if bias_value is not None else ("missing_directional_values",),
        )
        return result

    def websocket_channels(self) -> dict[str, Any]:
        """Return the websocket channels enabled for the configured subscription."""

        data = self._get("/api/socket")
        if isinstance(data, list):
            raw_rows = data
        elif isinstance(data, Mapping):
            nested = next(
                (
                    data.get(key)
                    for key in ("channels", "items", "results")
                    if isinstance(data.get(key), list)
                ),
                None,
            )
            raw_rows = nested if nested is not None else [
                {"channel": key}
                for key, enabled in data.items()
                if isinstance(key, str) and enabled is True
            ]
        else:
            raw_rows = []
        channels: list[str] = []
        for row in raw_rows[:256]:
            if isinstance(row, str):
                channel = row.strip()
            elif isinstance(row, Mapping):
                channel = str(
                    row.get("channel")
                    or row.get("name")
                    or row.get("topic")
                    or row.get("id")
                    or ""
                ).strip()
            else:
                continue
            if channel and len(channel) <= 96 and not any(
                character.isspace() for character in channel
            ):
                channels.append(channel)
        channels = list(dict.fromkeys(channels))
        return {
            "channels": channels,
            "quality": _quality(
                "unusual_whales_websocket_channels",
                available=bool(channels),
                issues=() if channels else ("empty",),
            ),
        }

    def gex_levels(self, ticker: str, *, market_date: str | date | None = None) -> dict[str, Any]:
        """Return the official call/put walls, zero-gamma flip and gamma magnet."""

        symbol = self._ticker(ticker)
        path = self._path(
            f"/api/stock/{quote(symbol, safe='.-')}/gex-levels",
            {"date": _market_date_value(market_date)},
        )
        data = self._get(path)
        if not isinstance(data, Mapping):
            raise UnusualWhalesMarketError("Unusual Whales GEX levels are unavailable")
        levels = {
            "call_wall": _number(data.get("call_wall")),
            "put_wall": _number(data.get("put_wall")),
            "gamma_flip": _number(data.get("gamma_flip")),
            "gamma_magnet": _number(data.get("gamma_magnet")),
        }
        available = any(value is not None for value in levels.values())
        return {
            "symbol": symbol,
            "market_date": _market_date_value(market_date),
            **levels,
            "quality": _quality(
                "unusual_whales_gex_levels",
                available=available,
                issues=() if available else ("empty",),
            ),
        }

    def off_lit_price_levels(
        self,
        ticker: str,
        *,
        market_date: str | date | None = None,
    ) -> dict[str, Any]:
        """Return sanitized Nasdaq lit and FINRA off-lit volume by execution price."""

        symbol = self._ticker(ticker)
        normalized_date = _market_date_value(market_date)
        path = self._path(
            f"/api/stock/{quote(symbol, safe='.-')}/stock-volume-price-levels",
            {"date": normalized_date},
        )
        data = self._get(path, maximum_bytes=4 * 1024 * 1024)
        raw_rows = data if isinstance(data, list) else []
        levels: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            price_value = _number(row.get("price"))
            lit_volume = _number(row.get("lit_vol"))
            off_lit_volume = _number(row.get("off_vol"))
            if price_value is None or price_value <= 0:
                continue
            if lit_volume is None and off_lit_volume is None:
                continue
            lit_volume = max(0.0, lit_volume or 0.0)
            off_lit_volume = max(0.0, off_lit_volume or 0.0)
            levels.append(
                {
                    "price": price_value,
                    "lit_volume": lit_volume,
                    "off_lit_volume": off_lit_volume,
                    "total_volume": lit_volume + off_lit_volume,
                    "off_lit_ratio": (
                        off_lit_volume / (lit_volume + off_lit_volume)
                        if lit_volume + off_lit_volume > 0
                        else None
                    ),
                }
            )
        levels.sort(key=lambda item: item["total_volume"], reverse=True)
        total_lit = sum(item["lit_volume"] for item in levels)
        total_off_lit = sum(item["off_lit_volume"] for item in levels)
        total = total_lit + total_off_lit
        return {
            "symbol": symbol,
            "market_date": normalized_date,
            "levels": levels,
            "total_lit_volume": total_lit,
            "total_off_lit_volume": total_off_lit,
            "off_lit_ratio": total_off_lit / total if total else None,
            "quality": _quality(
                "unusual_whales_off_lit_price_levels",
                available=bool(levels),
                issues=() if levels else ("empty",),
            ),
        }

    def economic_calendar(self) -> dict[str, Any]:
        """Return economic events sorted by their UTC event time."""

        data = self._get("/api/market/economic-calendar")
        raw_rows = data if isinstance(data, list) else []
        events: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            event_name = str(row.get("event") or "").strip()
            event_time_ms = _timestamp_ms(row.get("time"))
            if not event_name or event_time_ms is None:
                continue
            events.append(
                {
                    "event": event_name,
                    "event_time_ms": event_time_ms,
                    "time": row.get("time"),
                    "type": str(row.get("type") or "").strip().lower(),
                    "forecast": row.get("forecast"),
                    "previous": row.get("prev"),
                    "reported_period": row.get("reported_period"),
                }
            )
        events.sort(key=lambda item: item["event_time_ms"])
        return {
            "events": events,
            "quality": _quality(
                "unusual_whales_economic_calendar",
                available=bool(events),
                issues=() if events else ("empty",),
            ),
        }

    def vix_term_structure(self, *, history_days: int = 90) -> dict[str, Any]:
        """Return VIX futures term structure without inventing undocumented fields."""

        try:
            normalized_history_days = int(history_days)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UnusualWhalesMarketError("history_days must be between 1 and 365") from exc
        if isinstance(history_days, bool) or not 1 <= normalized_history_days <= 365:
            raise UnusualWhalesMarketError("history_days must be between 1 and 365")
        data = self._get(
            self._path(
                "/api/volatility/vix-term-structure",
                {"history_days": normalized_history_days},
            ),
            maximum_bytes=4 * 1024 * 1024,
        )
        if not isinstance(data, Mapping):
            raise UnusualWhalesMarketError("Unusual Whales VIX term structure is unavailable")
        latest = dict(data.get("latest")) if isinstance(data.get("latest"), Mapping) else {}
        history = (
            [dict(row) for row in data.get("history", []) if isinstance(row, Mapping)]
            if isinstance(data.get("history"), list)
            else []
        )
        source_time = next(
            (latest.get(key) for key in ("timestamp", "time", "date") if latest.get(key)),
            None,
        )
        available = bool(latest or history)
        return {
            "latest": latest,
            "history": history,
            "history_days": normalized_history_days,
            "quality": _quality(
                "unusual_whales_vix_term_structure",
                available=available,
                source_time=source_time,
                stale_after_ms=36 * 60 * 60 * 1_000,
                issues=() if available else ("empty",),
            ),
        }
