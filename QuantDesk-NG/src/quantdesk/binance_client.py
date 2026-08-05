"""币安数据层：公开行情轮询与统一账户持仓同步。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

FAPI = "https://fapi.binance.com"
PAPI = "https://papi.binance.com"

_ALLOWED_BINANCE_HOSTS = frozenset({"fapi.binance.com", "papi.binance.com"})
UA = {"User-Agent": "Mozilla/5.0 (quantdesk-local)"}


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
        try:
            # Safe because target was restricted to exact Binance HTTPS origins above.
            request = urllib.request.Request(  # noqa: S310
                target,
                headers=request_headers,
                method="GET",
            )
            with _BINANCE_OPENER.open(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code not in {418, 429}:
                break
            if attempt + 1 < retries:
                time.sleep(backoff * (attempt + 2))
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
