"""币安数据层：公开行情轮询与统一账户持仓同步。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .binance_rate_limit import (
    REST_RATE_LIMITER,
    BinanceRestRateLimit,
    rest_request_weight,
)

FAPI = "https://fapi.binance.com"
_ALLOWED_BINANCE_HOSTS = frozenset({"fapi.binance.com"})
UA = {"User-Agent": "Mozilla/5.0 (quantdesk-local)"}
_KLINE_INTERVAL_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
_BINANCE_KLINE_PAGE_SIZE = 1_500
_MAX_ON_DEMAND_KLINES = 50_000
# Preserve the existing public exception name for callers and tests.
BinancePublicRateLimit = BinanceRestRateLimit


class BinancePublicRequestError(RuntimeError):
    """A public REST failure classified for resumable market collection."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
    ) -> None:
        self.retryable = bool(retryable)
        self.status = status
        super().__init__(message)


def public_rest_retry_after() -> float:
    """Return seconds remaining on the process-wide public REST circuit."""
    return REST_RATE_LIMITER.snapshot().retry_after_seconds


def public_rest_blocked_until() -> float:
    """Return the active process-wide REST deadline, or zero when available."""

    return REST_RATE_LIMITER.snapshot().retry_at or 0.0


def restore_public_rest_circuit(blocked_until: Any) -> bool:
    """Restore a previously persisted process-wide REST circuit deadline."""

    return REST_RATE_LIMITER.restore_blocked_until(blocked_until)


def public_rest_available() -> bool:
    return public_rest_retry_after() <= 0


def _validate_binance_url(url: str) -> str:
    """Allow only exact Binance HTTPS origins used by the market collector."""

    if not isinstance(url, str) or not url:
        raise ValueError("Binance URL must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError("Binance URL must not contain control characters")
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Binance URL contains an invalid port") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in _ALLOWED_BINANCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("Binance URL must use an approved HTTPS origin")
    return url


def _safe_endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class _BinanceRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        _validate_binance_url(new_url)
        original = urllib.parse.urlsplit(request.full_url)
        redirected = urllib.parse.urlsplit(new_url)
        original_origin = (
            original.scheme.lower(),
            original.hostname,
            original.port or 443,
        )
        redirected_origin = (
            redirected.scheme.lower(),
            redirected.hostname,
            redirected.port or 443,
        )
        if original_origin != redirected_origin:
            raise ValueError("Binance redirects must remain on the original HTTPS origin")
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


_BINANCE_OPENER = urllib.request.build_opener(_BinanceRedirectHandler())


def _get(
    url: str,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20,
    retries: int = 3,
    backoff: float = 2.0,
) -> Any:
    target = _validate_binance_url(url)
    if retries < 1:
        raise ValueError("retries must be at least one")
    request_headers = dict(UA)
    request_headers.update(headers or {})
    last_error = "request did not run"
    last_status: int | None = None
    for attempt in range(retries):
        try:
            REST_RATE_LIMITER.before_request(rest_request_weight("GET", target))
            # Safe because target was restricted to exact Binance HTTPS origins above.
            request = urllib.request.Request(  # noqa: S310
                target,
                headers=request_headers,
                method="GET",
            )
            with _BINANCE_OPENER.open(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
                REST_RATE_LIMITER.observe(
                    int(getattr(response, "status", 200)),
                    getattr(response, "headers", {}),
                    raw,
                )
                payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                try:
                    payload_code = int(payload.get("code"))
                except (TypeError, ValueError):
                    payload_code = None
                if payload_code == -1003:
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    REST_RATE_LIMITER.observe(429, {}, raw)
                    snapshot = REST_RATE_LIMITER.snapshot()
                    raise BinancePublicRateLimit(snapshot.retry_at or time.time() + 60, status=429)
            return payload
        except BinancePublicRateLimit:
            raise
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:65_536]
            last_error = f"HTTP {exc.code}: {body[:200]}"
            last_status = int(exc.code)
            try:
                payload = json.loads(body)
                payload_code = int(payload.get("code")) if isinstance(payload, dict) else None
            except (json.JSONDecodeError, TypeError, ValueError):
                payload_code = None
            REST_RATE_LIMITER.observe(exc.code, exc.headers, body)
            if exc.code in {418, 429} or payload_code == -1003:
                snapshot = REST_RATE_LIMITER.snapshot()
                raise BinancePublicRateLimit(
                    snapshot.retry_at or time.time() + 60, status=exc.code
                ) from None
            if 500 <= exc.code < 600:
                if attempt + 1 < retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise BinancePublicRequestError(
                    f"GET {_safe_endpoint(target)} failed: {last_error}",
                    retryable=True,
                    status=exc.code,
                ) from None
            raise BinancePublicRequestError(
                f"GET {_safe_endpoint(target)} failed: {last_error}",
                retryable=False,
                status=exc.code,
            ) from None
        except (
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            urllib.error.URLError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < retries:
                time.sleep(backoff * (attempt + 1))
    raise BinancePublicRequestError(
        f"GET {_safe_endpoint(target)} failed: {last_error}",
        retryable=True,
        status=last_status,
    ) from None


def fetch_exchange_info() -> Any:
    return _get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=30)


def fetch_tickers() -> Any:
    """Return all futures 24-hour ticker rows in one request."""

    return _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=20)


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[tuple]:
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    return _normalize_kline_rows(_get(f"{FAPI}/fapi/v1/klines?{query}", timeout=20))


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    max_bars: int = _MAX_ON_DEMAND_KLINES,
) -> list[tuple]:
    """Fetch one closed historical range from Binance with bounded pagination."""

    normalized_symbol = str(symbol).strip().upper()
    if not 2 <= len(normalized_symbol) <= 32 or not normalized_symbol.isalnum():
        raise ValueError("invalid Binance kline symbol")
    interval_ms = _KLINE_INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise ValueError("unsupported Binance kline interval")
    if (
        isinstance(start_ms, bool)
        or isinstance(end_ms, bool)
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms < start_ms
    ):
        raise ValueError("invalid Binance kline range")
    if not 1 <= max_bars <= _MAX_ON_DEMAND_KLINES:
        raise ValueError("invalid Binance kline limit")

    now_ms = int(time.time() * 1_000)
    last_closed_open = now_ms - (now_ms % interval_ms) - interval_ms
    final_open = min(end_ms - (end_ms % interval_ms), last_closed_open)
    first_open = start_ms - (start_ms % interval_ms)
    if final_open < first_open:
        return []
    expected_bars = (final_open - first_open) // interval_ms + 1
    if expected_bars > max_bars:
        raise ValueError(f"Binance kline range exceeds the {max_bars} bar limit")

    output: list[tuple] = []
    seen: set[int] = set()
    cursor = first_open
    while cursor <= final_open and len(output) < max_bars:
        page_limit = min(_BINANCE_KLINE_PAGE_SIZE, max_bars - len(output))
        query = urllib.parse.urlencode(
            {
                "symbol": normalized_symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": final_open,
                "limit": page_limit,
            }
        )
        page = _normalize_kline_rows(
            _get(f"{FAPI}/fapi/v1/klines?{query}", timeout=20)
        )
        accepted = [row for row in page if cursor <= row[0] <= final_open]
        for row in accepted:
            if row[0] not in seen:
                seen.add(row[0])
                output.append(row)
        if not accepted:
            break
        next_cursor = max(row[0] for row in accepted) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline pagination made no progress")
        cursor = next_cursor
        if len(page) < page_limit:
            break
    output.sort(key=lambda row: row[0])
    return output


def _normalize_kline_rows(data: Any) -> list[tuple]:
    if not isinstance(data, list):
        raise RuntimeError("Binance kline response must be an array")
    rows: list[tuple] = []
    try:
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise ValueError("invalid row")
            rows.append(
                (
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
            )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Binance returned invalid kline data") from exc
    return rows
