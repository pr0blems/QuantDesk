"""币安数据层：公开行情轮询与统一账户持仓同步。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from threading import Lock
from typing import Any

FAPI = "https://fapi.binance.com"
PAPI = "https://papi.binance.com"

_ALLOWED_BINANCE_HOSTS = frozenset({"fapi.binance.com", "papi.binance.com"})
UA = {"User-Agent": "Mozilla/5.0 (quantdesk-local)"}


class _BinanceRestGovernor:
    """Process-wide IP governor shared by every Binance REST caller.

    Binance applies request weight and temporary bans at the IP level.  Keeping
    this state in the client (rather than in each worker) prevents the market,
    battle and recovery collectors from independently exhausting the quota.
    The governor is deliberately conservative and honours Retry-After on 429
    and 418 responses.
    """

    def __init__(self, rate_per_second: float = 24.0, burst: float = 48.0) -> None:
        self.rate_per_second = max(1.0, float(rate_per_second))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self.blocked_until = 0.0
        self.consecutive_throttles = 0
        self.last_used_weight: int | None = None
        self.lock = Lock()

    def acquire(self, weight: int = 1) -> None:
        cost = max(1.0, float(weight))
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.updated_at)
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
                self.updated_at = now
                wait_for = max(0.0, self.blocked_until - now)
                if wait_for <= 0 and self.tokens >= cost:
                    self.tokens -= cost
                    return
                if wait_for <= 0:
                    wait_for = (cost - self.tokens) / self.rate_per_second
            time.sleep(min(max(wait_for, 0.01), 60.0))

    def response(self, headers: Mapping[str, Any] | None = None) -> None:
        if not headers:
            return
        for key, value in headers.items():
            if str(key).lower() == "x-mbx-used-weight-1m":
                try:
                    self.last_used_weight = int(value)
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def retry_after(headers: Mapping[str, Any] | None) -> float | None:
        if not headers:
            return None
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    return None
        return None

    def throttled(self, retry_after: float | None = None) -> None:
        with self.lock:
            self.consecutive_throttles += 1
            delay = retry_after if retry_after is not None else min(
                60.0, 2.0 ** min(self.consecutive_throttles, 6)
            )
            self.blocked_until = max(self.blocked_until, time.monotonic() + delay)

    def recovered(self) -> None:
        with self.lock:
            self.consecutive_throttles = 0


_REST_GOVERNOR = _BinanceRestGovernor()


def _request_weight(url: str) -> int:
    """Approximate Binance request weight for the endpoints used by QuantDesk."""

    path = urllib.parse.urlsplit(url).path
    if path.endswith("/depth"):
        return 5
    if "/futures/data/" in path or path.endswith("/allForceOrders"):
        return 5
    if path.endswith("/klines") or path.endswith("/ticker/24hr"):
        return 2
    return 1


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
    for attempt in range(retries):
        _REST_GOVERNOR.acquire(_request_weight(target))
        try:
            # Safe because target was restricted to exact Binance HTTPS origins above.
            request = urllib.request.Request(  # noqa: S310
                target,
                headers=request_headers,
                method="GET",
            )
            with _BINANCE_OPENER.open(request, timeout=timeout) as response:  # noqa: S310
                _REST_GOVERNOR.response(getattr(response, "headers", None))
                _REST_GOVERNOR.recovered()
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            _REST_GOVERNOR.response(exc.headers)
            body = exc.read().decode("utf-8", errors="replace")[:200]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code not in {418, 429}:
                break
            retry_after = _REST_GOVERNOR.retry_after(exc.headers)
            # Some Binance gateways put the ban deadline only in the JSON
            # body, not in Retry-After. Parse it so concurrent workers do not
            # retry during the ban and extend the IP block.
            if exc.code == 418:
                match = re.search(r"banned\s+until\s+(\d{10,})", body, re.IGNORECASE)
                if match:
                    try:
                        deadline_ms = int(match.group(1))
                        body_delay = max(0.0, deadline_ms / 1_000.0 - time.time())
                        retry_after = max(retry_after or 0.0, body_delay)
                    except ValueError:
                        pass
            _REST_GOVERNOR.throttled(retry_after)
            if attempt + 1 < retries:
                time.sleep(max(backoff * (attempt + 2), retry_after or 0.0))
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
    raise RuntimeError(f"GET {_safe_endpoint(target)} failed: {last_error}") from None


def fetch_exchange_info() -> Any:
    return _get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=30)


def fetch_mark_prices() -> Any:
    return _get(f"{FAPI}/fapi/v1/premiumIndex", timeout=20)


def fetch_funding_info() -> Any:
    return _get(f"{FAPI}/fapi/v1/fundingInfo", timeout=20)


def fetch_funding_rates(start_time_ms: int, limit: int = 1000) -> Any:
    query = urllib.parse.urlencode({"startTime": int(start_time_ms), "limit": int(limit)})
    return _get(f"{FAPI}/fapi/v1/fundingRate?{query}", timeout=30)


def fetch_trading_schedule() -> Any:
    return _get(f"{FAPI}/fapi/v1/tradingSchedule", timeout=20)


def fetch_tickers() -> Any:
    """Return all futures 24-hour ticker rows in one request."""

    return _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=20)


def fetch_basis(
    symbol: str,
    *,
    period: str = "5m",
    contract_type: str = "PERPETUAL",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Fetch the public USD-M basis curve for one contract.

    The endpoint is intentionally kept public and unsigned so it can run in a
    market worker without exposing account credentials.  Rows include Binance
    ``basis`` and ``annualizedBasisRate`` fields when available.
    """

    if period not in {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}:
        raise ValueError("unsupported Binance basis period")
    query = urllib.parse.urlencode(
        {
            # The public Basis endpoint calls this field ``pair`` (unlike
            # most per-contract futures endpoints which use ``symbol``).
            "pair": symbol.upper(),
            "contractType": contract_type.upper(),
            "period": period,
            "limit": min(500, max(1, int(limit))),
        }
    )
    data = _get(f"{FAPI}/futures/data/basis?{query}", timeout=15, retries=2)
    if not isinstance(data, list):
        raise RuntimeError("Binance basis response is invalid")
    return [row for row in data if isinstance(row, dict)]


def fetch_adl_risk(symbol: str) -> list[dict[str, Any]]:
    """Fetch the current public ADL risk rating for a contract.

    ``symbolAdlRisk`` is the current Futures endpoint.  The older
    ``adlQuantile`` route is retained as a compatibility fallback for regions
    where the newer route has not been rolled out yet.
    """

    query = urllib.parse.urlencode({"symbol": symbol.upper()})
    try:
        data = _get(f"{FAPI}/fapi/v1/symbolAdlRisk?{query}", timeout=15, retries=2)
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        data = _get(f"{FAPI}/futures/data/adlQuantile?{query}", timeout=15, retries=2)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError("Binance ADL risk response is invalid")
    return [row for row in data if isinstance(row, dict)]


def fetch_force_orders(
    symbol: str | None = None,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch recent public liquidation orders (REST fallback for the stream)."""

    parameters: dict[str, str | int] = {"limit": min(1000, max(1, int(limit)))}
    if symbol:
        parameters["symbol"] = symbol.upper()
    if start_time_ms is not None:
        parameters["startTime"] = int(start_time_ms)
    if end_time_ms is not None:
        parameters["endTime"] = int(end_time_ms)
    query = urllib.parse.urlencode(parameters)
    data = _get(f"{FAPI}/fapi/v1/allForceOrders?{query}", timeout=15, retries=2)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError("Binance force-order response is invalid")
    return [row for row in data if isinstance(row, dict)]


# Name used by downstream collectors and older integrations.
fetch_liquidation_orders = fetch_force_orders


def fetch_open_interest(symbol: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"symbol": symbol.upper()})
    data = _get(f"{FAPI}/fapi/v1/openInterest?{query}", timeout=8, retries=2)
    if not isinstance(data, dict):
        raise RuntimeError("Binance open interest response is invalid")
    return data


def fetch_order_book(symbol: str, limit: int = 20) -> dict[str, Any]:
    """Fetch a bounded USD-M futures depth snapshot for a single contract.

    This is deliberately used only as a recovery path when a websocket partial
    depth stream is missing or stale.  Keeping the limit at a supported small
    value gives the monitor a coherent liquidity pool without polling the whole
    market on every flush.
    """

    if limit not in {5, 10, 20, 50, 100, 500, 1000}:
        raise ValueError("Binance order-book limit is unsupported")
    query = urllib.parse.urlencode({"symbol": symbol.upper(), "limit": int(limit)})
    data = _get(f"{FAPI}/fapi/v1/depth?{query}", timeout=5, retries=1)
    if not isinstance(data, dict) or not isinstance(data.get("bids"), list) or not isinstance(
        data.get("asks"), list
    ):
        raise RuntimeError("Binance order-book response is invalid")
    return data


def fetch_global_long_short_ratio(
    symbol: str, period: str = "5m", limit: int = 2
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"symbol": symbol.upper(), "period": period, "limit": int(limit)}
    )
    data = _get(
        f"{FAPI}/futures/data/globalLongShortAccountRatio?{query}",
        timeout=8,
        retries=2,
    )
    if not isinstance(data, list):
        raise RuntimeError("Binance global long/short response is invalid")
    return [item for item in data if isinstance(item, dict)]


def _fetch_long_short_ratio(
    path: str, symbol: str, period: str = "5m", limit: int = 2
) -> list[dict[str, Any]]:
    if path not in {"topLongShortAccountRatio", "topLongShortPositionRatio"}:
        raise ValueError("unsupported Binance long/short ratio endpoint")
    query = urllib.parse.urlencode(
        {"symbol": symbol.upper(), "period": period, "limit": int(limit)}
    )
    data = _get(f"{FAPI}/futures/data/{path}?{query}", timeout=8, retries=2)
    if not isinstance(data, list):
        raise RuntimeError("Binance top-trader long/short response is invalid")
    return [item for item in data if isinstance(item, dict)]


def fetch_top_trader_account_ratio(
    symbol: str, period: str = "5m", limit: int = 2
) -> list[dict[str, Any]]:
    return _fetch_long_short_ratio("topLongShortAccountRatio", symbol, period, limit)


def fetch_top_trader_position_ratio(
    symbol: str, period: str = "5m", limit: int = 2
) -> list[dict[str, Any]]:
    return _fetch_long_short_ratio("topLongShortPositionRatio", symbol, period, limit)


def fetch_taker_buy_sell_ratio(
    symbol: str, period: str = "5m", limit: int = 2
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"symbol": symbol.upper(), "period": period, "limit": int(limit)}
    )
    data = _get(
        f"{FAPI}/futures/data/takerlongshortRatio?{query}",
        timeout=8,
        retries=2,
    )
    if not isinstance(data, list):
        raise RuntimeError("Binance taker buy/sell response is invalid")
    return [item for item in data if isinstance(item, dict)]


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[tuple]:
    query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    data = _get(f"{FAPI}/fapi/v1/klines?{query}", timeout=20)
    # Binance rows: [open_time, open, high, low, close, volume, close_time, ...]
    return [
        (
            int(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]),
        )
        for row in data
    ]


def fetch_research_klines(
    symbol: str,
    interval: str = "5m",
    limit: int = 1500,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
) -> list[tuple]:
    """Return the complete public kline fields required by the ML feature schema."""

    parameters: dict[str, str | int] = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": min(1500, max(1, int(limit))),
    }
    if start_time_ms is not None:
        parameters["startTime"] = int(start_time_ms)
    if end_time_ms is not None:
        parameters["endTime"] = int(end_time_ms)
    data = _get(
        f"{FAPI}/fapi/v1/klines?{urllib.parse.urlencode(parameters)}",
        timeout=20,
        retries=2,
    )
    if not isinstance(data, list):
        raise RuntimeError("Binance kline response is invalid")
    return [
        (
            int(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]),
            float(row[7]),
            int(row[8]),
            float(row[9]),
            float(row[10]),
        )
        for row in data
        if isinstance(row, list) and len(row) >= 11
    ]


def _signed_get(
    base: str,
    path: str,
    api_key: str,
    secret: str,
    params: Mapping[str, str | int] | None = None,
    timeout: float = 25,
) -> Any:
    if base not in {FAPI, PAPI}:
        raise ValueError("signed Binance requests require an approved base URL")
    parsed_path = urllib.parse.urlsplit(path)
    if (
        not path.startswith("/")
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
    ):
        raise ValueError("signed Binance request path is invalid")
    signed_params = dict(params or {})
    signed_params.update({"timestamp": int(time.time() * 1_000), "recvWindow": 10_000})
    query = urllib.parse.urlencode(signed_params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{query}&signature={signature}"
    return _get(
        url,
        headers={"X-MBX-APIKEY": api_key},
        timeout=timeout,
        retries=3,
    )


def fetch_commission_rate(symbol: str, api_key: str, secret: str) -> dict[str, Any]:
    data = _signed_get(
        FAPI,
        "/fapi/v1/commissionRate",
        api_key,
        secret,
        {"symbol": symbol.upper()},
    )
    if not isinstance(data, dict):
        raise RuntimeError("Binance commission response is invalid")
    return data


def fetch_leverage_brackets(api_key: str, secret: str) -> list[dict[str, Any]]:
    data = _signed_get(FAPI, "/fapi/v1/leverageBracket", api_key, secret)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError("Binance leverage bracket response is invalid")
    return [row for row in data if isinstance(row, dict)]


def fetch_positions(api_key: str, secret: str) -> list[dict[str, Any]]:
    """Return non-zero unified-account UM positions."""

    data = _signed_get(PAPI, "/papi/v1/um/positionRisk", api_key, secret)
    positions = []
    for item in data:
        amount = float(item.get("positionAmt", 0))
        if amount == 0:
            continue
        positions.append(
            {
                "symbol": item["symbol"],
                "amt": amount,
                "side": item.get("positionSide") or ("LONG" if amount > 0 else "SHORT"),
                "entry": float(item.get("entryPrice", 0)),
                "mark": float(item.get("markPrice", 0)),
                "upnl": float(item.get("unRealizedProfit", 0)),
                "leverage": int(float(item.get("leverage", 0))),
            }
        )
    return positions
