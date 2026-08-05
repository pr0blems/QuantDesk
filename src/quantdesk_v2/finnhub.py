from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPSConnection
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

MAX_RESPONSE_BYTES = 64 * 1024
MARKET_STATUS_PATH = "/api/v1/stock/market-status"
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
        url = f"{self.base_url}{MARKET_STATUS_PATH}?{urlencode({'exchange': exchange})}"
        try:
            status, body = self.transport(
                url,
                {
                    "Accept": "application/json",
                    "User-Agent": "QuantDesk/2 FinnhubMarketStatus",
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
        return _parse_market_status(payload, exchange)


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
