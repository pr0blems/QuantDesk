"""Process-wide Binance REST request pacing and rate-limit circuit breaker.

Binance accounts REST request weight by public IP, not by API key.  Every
client in this process therefore shares this limiter: public market data,
signed account reads, and live order requests.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

OFFICIAL_WEIGHT_PER_MINUTE = 2_400
# Preserve headroom for requests made by another process on the same public IP
# and for endpoint-weight changes between exchangeInfo refreshes.
SAFE_WEIGHT_PER_MINUTE = 1_680
_WEIGHT_HEADER_PREFIX = "x-mbx-used-weight-"


class BinanceRestRateLimit(RuntimeError):
    """The shared Binance REST circuit is open until ``retry_at``."""

    def __init__(self, retry_at: float, *, status: int | None = None):
        self.retry_at = float(retry_at)
        # Backwards-compatible name used by the public market collector.
        self.blocked_until = self.retry_at
        self.status = status
        remaining = max(0, int(self.retry_at - time.time()))
        super().__init__(f"Binance REST rate limited; retry in {remaining}s")


@dataclass(frozen=True, slots=True)
class BinanceRateLimitSnapshot:
    blocked: bool
    retry_at: float | None
    retry_after_seconds: float
    used_weight: int | None
    weight_limit: int


def _headers_dict(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if isinstance(headers, Mapping):
        items = headers.items()
    else:
        try:
            items = headers.items()
        except AttributeError:
            return {}
    return {str(name).lower(): str(value) for name, value in items}


def _retry_after_deadline(headers: Mapping[str, str], now: float) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    value = str(raw).strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            deadline = parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return deadline if deadline > now else None
    return now + max(0.0, seconds)


def _ban_deadline(body: bytes | str, now: float) -> float | None:
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    message = str(payload.get("msg") or text) if isinstance(payload, dict) else text
    match = re.search(r"(?:banned?|ban)\D{0,40}until\D*(\d{10,13})", message, re.I)
    if match is None:
        return None
    deadline = float(match.group(1))
    if deadline >= 1_000_000_000_000:
        deadline /= 1_000
    if deadline <= now or deadline > now + 366 * 24 * 60 * 60:
        return None
    return deadline


def _error_code(body: bytes | str) -> int | None:
    try:
        payload = json.loads(
            body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        )
        return int(payload.get("code")) if isinstance(payload, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


class BinanceRestRateLimiter:
    """Smooth REST weight and fail fast while Binance has blocked the IP."""

    def __init__(self, safe_weight_per_minute: int = SAFE_WEIGHT_PER_MINUTE) -> None:
        if not 1 <= safe_weight_per_minute <= OFFICIAL_WEIGHT_PER_MINUTE:
            raise ValueError("invalid Binance REST weight budget")
        self.safe_weight_per_minute = int(safe_weight_per_minute)
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._next_request_at = 0.0
        self._used_weight: int | None = None

    def before_request(self, weight: int) -> None:
        """Reserve request weight, spacing concurrent callers without bursts."""

        normalized = max(1, int(weight))
        now = time.time()
        with self._lock:
            if self._blocked_until > now:
                raise BinanceRestRateLimit(self._blocked_until)
            scheduled = max(now, self._next_request_at)
            spacing = normalized * 60.0 / self.safe_weight_per_minute
            self._next_request_at = scheduled + spacing
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)
        # A different request may have received 418 while this caller waited.
        now = time.time()
        with self._lock:
            if self._blocked_until > now:
                raise BinanceRestRateLimit(self._blocked_until)

    def observe(self, status: int, headers: Any, body: bytes | str) -> None:
        """Consume Binance weight headers and open the circuit on 429/418."""

        normalized_headers = _headers_dict(headers)
        used_values = []
        for name, raw in normalized_headers.items():
            if not name.startswith(_WEIGHT_HEADER_PREFIX):
                continue
            try:
                used_values.append(int(raw))
            except ValueError:
                continue
        used_weight = max(used_values) if used_values else None
        now = time.time()
        code = _error_code(body)
        retry_at: float | None = None
        if status in {418, 429} or code == -1003:
            explicit = [
                value
                for value in (
                    _retry_after_deadline(normalized_headers, now),
                    _ban_deadline(body, now),
                )
                if value is not None
            ]
            retry_at = max(explicit) if explicit else now + (300 if status == 418 else 60)
        elif used_weight is not None and used_weight >= self.safe_weight_per_minute:
            # Weight windows are minute based. Leave a one-second boundary guard.
            retry_at = (int(now) // 60 + 1) * 60 + 1

        with self._lock:
            if used_weight is not None:
                self._used_weight = used_weight
            if retry_at is not None:
                self._blocked_until = max(self._blocked_until, retry_at)
                self._next_request_at = max(self._next_request_at, self._blocked_until)

    def snapshot(self) -> BinanceRateLimitSnapshot:
        now = time.time()
        with self._lock:
            blocked_until = self._blocked_until
            used_weight = self._used_weight
        remaining = max(0.0, blocked_until - now)
        return BinanceRateLimitSnapshot(
            blocked=remaining > 0,
            retry_at=blocked_until if remaining > 0 else None,
            retry_after_seconds=remaining,
            used_weight=used_weight,
            weight_limit=self.safe_weight_per_minute,
        )

    def restore_blocked_until(self, retry_at: Any) -> bool:
        """Restore a persisted finite circuit deadline.

        Stale or implausibly distant values are ignored so a corrupt database
        value cannot leave Binance REST disabled indefinitely.
        """

        if isinstance(retry_at, bool):
            return False
        try:
            deadline = float(retry_at)
        except (TypeError, ValueError, OverflowError):
            return False
        if deadline >= 1_000_000_000_000:
            deadline /= 1_000
        now = time.time()
        if (
            not math.isfinite(deadline)
            or deadline <= now
            or deadline > now + 366 * 24 * 60 * 60
        ):
            return False
        with self._lock:
            self._blocked_until = max(self._blocked_until, deadline)
            self._next_request_at = max(self._next_request_at, self._blocked_until)
        return True

    def reset(self) -> None:
        """Clear in-memory state; intended for isolated tests only."""

        with self._lock:
            self._blocked_until = 0.0
            self._next_request_at = 0.0
            self._used_weight = None


REST_RATE_LIMITER = BinanceRestRateLimiter()


def rest_request_weight(method: str, url: str) -> int:
    """Return a conservative USD-M REST weight estimate for known routes."""

    parsed = urlsplit(url)
    path = parsed.path
    query = parse_qs(parsed.query)
    has_symbol = bool(query.get("symbol"))
    if path == "/fapi/v1/ticker/24hr":
        return 1 if has_symbol else 40
    if path in {"/fapi/v1/ticker/price", "/fapi/v2/ticker/price"}:
        return 1 if has_symbol else 2
    if path == "/fapi/v1/klines":
        try:
            limit = int(query.get("limit", [500])[0])
        except (TypeError, ValueError):
            limit = 500
        if limit < 100:
            return 1
        if limit < 500:
            return 2
        if limit <= 1_000:
            return 5
        return 10
    if path == "/fapi/v1/depth":
        try:
            limit = int(query.get("limit", [500])[0])
        except (TypeError, ValueError):
            limit = 500
        if limit <= 50:
            return 2
        if limit <= 100:
            return 5
        if limit <= 500:
            return 10
        return 20
    if path.endswith("/income"):
        return 30
    if path.endswith("/openOrders") or path.endswith("/openAlgoOrders"):
        return 1 if has_symbol else 40
    if path.endswith("/conditional/openOrders"):
        return 40
    if path.endswith("/account"):
        return 5
    if path.endswith("/positionRisk"):
        return 5
    if path.endswith("/exchangeInfo") or path.endswith("/time"):
        return 1
    if path.endswith("/order") or path.endswith("/algoOrder"):
        return 1
    # Unknown signed/private routes get a conservative allowance.
    return 5 if method.upper() != "GET" or "signature=" in parsed.query else 1


def unpack_transport_response(
    result: tuple[int, bytes] | tuple[int, bytes, Any],
) -> tuple[int, bytes, Any]:
    """Accept legacy two-field test transports and header-aware production ones."""

    if len(result) == 2:
        status, body = result
        return status, body, {}
    status, body, headers = result
    return status, body, headers
