from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPSConnection
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

MAX_RESPONSE_BYTES = 64 * 1024
MAX_WEBHOOK_BYTES = 64 * 1024
MARKET_STATUS_PATH = "/api/v1/stock/market-status"
QUOTE_PATH = "/api/v1/quote"
MarketSession = Literal["pre-market", "regular", "post-market"]
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class FinnhubClientError(RuntimeError):
    """A redacted upstream error that is safe to expose as a category."""

    def __init__(self, category: str):
        super().__init__("Finnhub request failed")
        self.category = category


@dataclass(frozen=True, slots=True)
class FinnhubMarketStatus:
    exchange: Literal["US"]
    holiday: str | None
    is_open: bool
    session: MarketSession | None
    timezone: str
    source_timestamp: int
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FinnhubMarketStatusResult:
    configured: bool
    available: bool
    exchange: Literal["US"] = "US"
    holiday: str | None = None
    is_open: bool | None = None
    session: MarketSession | None = None
    timezone: str | None = None
    source_timestamp: int | None = None
    fetched_at: datetime | None = None
    cached: bool = False
    stale: bool = False
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class FinnhubQuote:
    symbol: str
    price: float
    change: float | None
    change_percent: float | None
    day_high: float | None
    day_low: float | None
    day_open: float | None
    previous_close: float | None
    source_timestamp: int
    fetched_at: datetime
    volume: float | None = None
    live: bool = False


def _https_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "finnhub.io"
        or parsed.port not in (None, 443)
    ):
        raise FinnhubClientError("rejected")
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    try:
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise FinnhubClientError("invalid_response")
        return response.status, body
    except TimeoutError as exc:
        raise FinnhubClientError("timeout") from exc
    except FinnhubClientError:
        raise
    except OSError as exc:
        raise FinnhubClientError("network") from exc
    finally:
        connection.close()


class FinnhubClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 5.0,
        transport: Transport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "finnhub.io"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Finnhub base URL must be the official HTTPS origin")
        self.base_url = "https://finnhub.io"
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _https_transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def market_status(self, exchange: str = "US") -> FinnhubMarketStatus:
        if not self.configured:
            raise FinnhubClientError("not_configured")
        if exchange != "US":
            raise FinnhubClientError("rejected")
        payload = self._get_json(
            MARKET_STATUS_PATH,
            {"exchange": exchange},
            user_agent="QuantDesk/2 FinnhubMarketStatus",
        )
        return _parse_market_status(payload, exchange)

    def quote(self, symbol: str) -> FinnhubQuote:
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) > 16 or not all(
            character.isalnum() or character in {".", "-"} for character in normalized
        ):
            raise FinnhubClientError("rejected")
        payload = self._get_json(
            QUOTE_PATH,
            {"symbol": normalized},
            user_agent="QuantDesk/2 FinnhubUsQuotes",
        )
        return _parse_quote(payload, normalized)

    def _get_json(
        self,
        path: str,
        params: dict[str, str],
        *,
        user_agent: str,
    ) -> Any:
        if not self.configured:
            raise FinnhubClientError("not_configured")
        url = f"{self.base_url}{path}?{urlencode(params)}"
        try:
            status, body = self.transport(
                url,
                {
                    "Accept": "application/json",
                    "User-Agent": user_agent,
                    "X-Finnhub-Token": self.api_key,
                },
                self.timeout_seconds,
            )
        except FinnhubClientError:
            raise
        except TimeoutError as exc:
            raise FinnhubClientError("timeout") from exc
        except OSError as exc:
            raise FinnhubClientError("network") from exc
        if status in {401, 403}:
            raise FinnhubClientError("authentication")
        if status == 429:
            raise FinnhubClientError("rate_limit")
        if status < 200 or status >= 300:
            raise FinnhubClientError("upstream")
        if len(body) > MAX_RESPONSE_BYTES:
            raise FinnhubClientError("invalid_response")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinnhubClientError("invalid_response") from exc
        return payload


def _parse_market_status(payload: Any, exchange: str) -> FinnhubMarketStatus:
    if not isinstance(payload, dict) or payload.get("exchange") != exchange:
        raise FinnhubClientError("invalid_response")
    is_open = payload.get("isOpen")
    session = payload.get("session")
    timezone = payload.get("timezone")
    timestamp = payload.get("t")
    holiday = payload.get("holiday")
    if (
        not isinstance(is_open, bool)
        or session not in {"pre-market", "regular", "post-market", None}
        or not isinstance(timezone, str)
        or not 1 <= len(timezone) <= 64
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
        or (holiday is not None and not isinstance(holiday, str))
        or (isinstance(holiday, str) and len(holiday) > 200)
    ):
        raise FinnhubClientError("invalid_response")
    try:
        datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise FinnhubClientError("invalid_response") from exc
    return FinnhubMarketStatus(
        exchange="US",
        holiday=holiday,
        is_open=is_open,
        session=session,
        timezone=timezone,
        source_timestamp=timestamp,
        fetched_at=datetime.now(UTC),
    )


def _optional_finite_number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FinnhubClientError("invalid_response")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise FinnhubClientError("invalid_response")
    return numeric


def _parse_quote(payload: Any, symbol: str) -> FinnhubQuote:
    if not isinstance(payload, dict):
        raise FinnhubClientError("invalid_response")
    price = _optional_finite_number(payload, "c")
    timestamp = payload.get("t")
    if (
        price is None
        or price <= 0
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
    ):
        raise FinnhubClientError("no_data")
    return FinnhubQuote(
        symbol=symbol,
        price=price,
        change=_optional_finite_number(payload, "d"),
        change_percent=_optional_finite_number(payload, "dp"),
        day_high=_optional_finite_number(payload, "h"),
        day_low=_optional_finite_number(payload, "l"),
        day_open=_optional_finite_number(payload, "o"),
        previous_close=_optional_finite_number(payload, "pc"),
        source_timestamp=timestamp,
        fetched_at=datetime.now(UTC),
    )


class FinnhubMarketStatusService:
    """Process-local cache with stale-on-error fallback for dashboard polling."""

    def __init__(
        self,
        client: FinnhubClient,
        *,
        cache_seconds: int = 30,
        stale_seconds: int = 900,
    ) -> None:
        self.client = client
        self.cache_seconds = cache_seconds
        self.stale_seconds = stale_seconds
        self._lock = Lock()
        self._cached: FinnhubMarketStatus | None = None
        self._cached_at = 0.0

    def status(self) -> FinnhubMarketStatusResult:
        if not self.client.configured:
            return FinnhubMarketStatusResult(
                configured=False,
                available=False,
                error_category="not_configured",
            )
        with self._lock:
            now = time.monotonic()
            age = now - self._cached_at
            if self._cached is not None and age <= self.cache_seconds:
                return self._result(self._cached, cached=True)
            try:
                status = self.client.market_status("US")
            except FinnhubClientError as exc:
                if self._cached is not None and age <= self.stale_seconds:
                    return self._result(
                        self._cached,
                        cached=True,
                        stale=True,
                        error_category=exc.category,
                    )
                return FinnhubMarketStatusResult(
                    configured=True,
                    available=False,
                    error_category=exc.category,
                )
            self._cached = status
            self._cached_at = now
            return self._result(status, cached=False)

    @staticmethod
    def _result(
        status: FinnhubMarketStatus,
        *,
        cached: bool,
        stale: bool = False,
        error_category: str | None = None,
    ) -> FinnhubMarketStatusResult:
        return FinnhubMarketStatusResult(
            configured=True,
            available=True,
            exchange=status.exchange,
            holiday=status.holiday,
            is_open=status.is_open,
            session=status.session,
            timezone=status.timezone,
            source_timestamp=status.source_timestamp,
            fetched_at=status.fetched_at,
            cached=cached,
            stale=stale,
            error_category=error_category,
        )


@dataclass(frozen=True, slots=True)
class FinnhubWebhookSnapshot:
    configured: bool
    received_events: int
    last_received_at: datetime | None


class FinnhubWebhookReceiver:
    """Authenticate Finnhub callbacks and retain only operational metadata."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._lock = Lock()
        self._received_events = 0
        self._last_received_at: datetime | None = None
        self._last_payload_hash: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    def authenticated(self, supplied: str | None) -> bool:
        if not self.configured or supplied is None:
            return False
        return hmac.compare_digest(
            self._secret.encode("utf-8"),
            supplied.encode("utf-8"),
        )

    def record(self, body: bytes) -> None:
        """Record no payload content; only count, time, and a one-way digest."""

        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._received_events += 1
            self._last_received_at = datetime.now(UTC)
            self._last_payload_hash = digest

    def snapshot(self) -> FinnhubWebhookSnapshot:
        with self._lock:
            return FinnhubWebhookSnapshot(
                configured=self.configured,
                received_events=self._received_events,
                last_received_at=self._last_received_at,
            )
