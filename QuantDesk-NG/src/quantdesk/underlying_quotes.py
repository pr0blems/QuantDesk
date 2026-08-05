"""Collect public underlying-market quotes for the contract monitor."""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

from . import store
from .config_loader import settings, symbols_meta

YAHOO_ORIGIN = "https://query1.finance.yahoo.com"
POLL_SECONDS = 120

_DIRECT_ALIASES = {
    "BRKB": "BRK-B",
    "PAYP": "PYPL",
}
_NATIVE_OVERRIDES: dict[str, tuple[str | None, str, str]] = {
    "ANTHROPICUSDT": (None, "unlisted", "pre_ipo"),
    "OPENAIUSDT": (None, "unlisted", "pre_ipo"),
    "SPCXUSDT": (None, "unlisted", "pre_ipo"),
    "SPCXUSD1": (None, "unlisted", "pre_ipo"),
    "DRAMUSDT": (None, "index", "thematic_index"),
    "BZUSDT": ("BZ=F", "benchmark", "commodity_future"),
    "CLUSDT": ("CL=F", "benchmark", "commodity_future"),
    "COPPERUSDT": ("HG=F", "benchmark", "commodity_future"),
    "NATGASUSDT": ("NG=F", "benchmark", "commodity_future"),
    "XAGUSDT": ("SI=F", "benchmark", "commodity_future"),
    "XAUUSDT": ("GC=F", "benchmark", "commodity_future"),
    "XPDUSDT": ("PA=F", "benchmark", "commodity_future"),
    "XPTUSDT": ("PL=F", "benchmark", "commodity_future"),
    "STXXUSDT": ("^STOXX50E", "benchmark", "index"),
    "HK0700USDT": ("0700.HK", "native", "hk_equity"),
    "TENCENTUSDT": ("0700.HK", "native", "hk_equity"),
    "HK1810USDT": ("1810.HK", "native", "hk_equity"),
    "POPMARTUSDT": ("9992.HK", "native", "hk_equity"),
    "GIGADEVUSDT": ("603986.SS", "native", "cn_equity"),
    "SAMSUNGUSDT": ("005930.KS", "native", "kr_equity"),
    "SKHYNIXUSDT": ("000660.KS", "native", "kr_equity"),
    "HYUNDAIUSDT": ("005380.KS", "native", "kr_equity"),
    "MINIMAXUSDT": ("0100.HK", "native", "hk_equity"),
    "ZHIPUUSDT": ("2513.HK", "native", "hk_equity"),
    "QNTXUSDT": (None, "unlisted", "pre_ipo"),
}


@dataclass(frozen=True, slots=True)
class UnderlyingMapping:
    contract_symbol: str
    quote_symbol: str | None
    relation: str
    instrument_type: str


def contract_mappings() -> list[UnderlyingMapping]:
    mappings: list[UnderlyingMapping] = []
    for item in symbols_meta.get("symbols", []):
        contract = str(item.get("symbol") or "").upper()
        if not contract:
            continue
        override = _NATIVE_OVERRIDES.get(contract)
        if override is not None:
            quote_symbol, relation, instrument_type = override
        else:
            base = contract.removesuffix("USDT").removesuffix("USD1")
            quote_symbol = _DIRECT_ALIASES.get(base, base)
            relation = "direct"
            instrument_type = "us_equity"
        mappings.append(
            UnderlyingMapping(
                contract_symbol=contract,
                quote_symbol=quote_symbol,
                relation=relation,
                instrument_type=instrument_type,
            )
        )
    return mappings


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _array_value(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    return _number(values[index])


def _rolling_change(
    timestamps: list[Any], closes: list[Any], latest_index: int | None, window_seconds: int
) -> float | None:
    """Return a rolling return using the latest valid bar and a prior bar.

    The monitor uses 2m, 5m, 10m and 24h windows for Binance contracts.  Use
    the same named windows for the paired public-market quote rather than
    showing a session-only percentage beside a rolling contract percentage.
    """

    if latest_index is None:
        return None
    latest_price = _array_value(closes, latest_index)
    latest_time = _number(timestamps[latest_index])
    if latest_price is None or latest_time is None:
        return None
    target_time = latest_time - window_seconds
    reference_price: float | None = None
    for index in range(min(latest_index - 1, len(timestamps) - 1, len(closes) - 1), -1, -1):
        timestamp = _number(timestamps[index])
        candidate = _array_value(closes, index)
        if timestamp is not None and timestamp <= target_time and candidate is not None:
            reference_price = candidate
            break
    if reference_price is None or reference_price <= 0:
        return None
    return (latest_price / reference_price - 1) * 100


def _market_state(meta: dict[str, Any], now_seconds: int) -> str:
    periods = meta.get("currentTradingPeriod")
    if not isinstance(periods, dict):
        return "unknown"
    for key, label in (
        ("pre", "pre_market"),
        ("regular", "regular"),
        ("post", "after_hours"),
    ):
        period = periods.get(key)
        if not isinstance(period, dict):
            continue
        start = int(period.get("start") or 0)
        end = int(period.get("end") or 0)
        if start <= now_seconds < end:
            return label
    return "closed"


def parse_chart_payload(
    payload: dict[str, Any], *, now_ms: int
) -> dict[str, Any] | None:
    results = payload.get("chart", {}).get("result") or []
    if not results or not isinstance(results[0], dict):
        return None
    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    timestamps = result.get("timestamp") or []
    quote_sets = result.get("indicators", {}).get("quote") or []
    quote = quote_sets[0] if quote_sets and isinstance(quote_sets[0], dict) else {}
    closes = quote.get("close") or []
    latest_index = next(
        (
            index
            for index in range(min(len(timestamps), len(closes)) - 1, -1, -1)
            if _array_value(closes, index) is not None
        ),
        None,
    )
    regular_price = _number(meta.get("regularMarketPrice"))
    latest_price = _array_value(closes, latest_index) if latest_index is not None else None
    price = latest_price if latest_price is not None else regular_price
    previous_close = _number(meta.get("chartPreviousClose")) or _number(
        meta.get("previousClose")
    )
    change_pct = (
        (price / previous_close - 1) * 100
        if price is not None and previous_close is not None and previous_close > 0
        else None
    )
    rolling_changes = {
        window: _rolling_change(timestamps, closes, latest_index, window)
        for window in (120, 300, 600, 86_400)
    }
    market_time_ms = (
        int(timestamps[latest_index]) * 1_000
        if latest_index is not None
        else int(meta.get("regularMarketTime") or 0) * 1_000 or None
    )
    market_state = _market_state(meta, now_ms // 1_000)
    age_ms = max(0, now_ms - market_time_ms) if market_time_ms else None
    active = market_state in {"pre_market", "regular", "after_hours"}
    status = "stale" if active and (age_ms is None or age_ms > 10 * 60 * 1_000) else "ok"
    opens = [_number(value) for value in quote.get("open") or []]
    highs = [_number(value) for value in quote.get("high") or []]
    lows = [_number(value) for value in quote.get("low") or []]
    volumes = [_number(value) for value in quote.get("volume") or []]
    return {
        "display_name": str(meta.get("shortName") or meta.get("longName") or "")[:191]
        or None,
        "source": "yahoo_chart",
        "status": status,
        "market_state": market_state,
        "currency": str(meta.get("currency") or "")[:12] or None,
        "exchange_name": str(meta.get("exchangeName") or "")[:64] or None,
        "price": price,
        "previous_close": previous_close,
        "change_pct": change_pct,
        "pct_2m": rolling_changes[120],
        "pct_5m": rolling_changes[300],
        "pct_10m": rolling_changes[600],
        "pct_24h": rolling_changes[86_400],
        "regular_market_price": regular_price,
        "day_open": _number(meta.get("regularMarketOpen"))
        or next((value for value in opens if value is not None), None),
        "day_high": _number(meta.get("regularMarketDayHigh"))
        or max((value for value in highs if value is not None), default=None),
        "day_low": _number(meta.get("regularMarketDayLow"))
        or min((value for value in lows if value is not None), default=None),
        "volume": _number(meta.get("regularMarketVolume"))
        or sum(value for value in volumes if value is not None),
        "market_time_ms": market_time_ms,
        "received_at_ms": now_ms,
        "quality_json": {
            "age_ms": age_ms,
            "latency_ms": max(0, now_ms - market_time_ms) if market_time_ms else None,
            "coverage_ratio": sum(
                value is not None for value in (price, previous_close, change_pct)
            ) / 3,
            "timezone": meta.get("exchangeTimezoneName"),
            "data_granularity": meta.get("dataGranularity"),
            "price_source": "latest_chart_bar" if latest_price is not None else "regular_market",
        },
    }


def fetch_quote(quote_symbol: str, *, retries: int = 3) -> dict[str, Any] | None:
    quoted = urllib.parse.quote(quote_symbol, safe="-^=.")
    query = urllib.parse.urlencode(
        {
            "interval": "1m",
            "range": "2d",
            "includePrePost": "true",
            "events": "div,splits",
        }
    )
    url = f"{YAHOO_ORIGIN}/v8/finance/chart/{quoted}?{query}"
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed Yahoo HTTPS origin
                url,
                headers={"User-Agent": "Mozilla/5.0 (QuantDesk-NG underlying quotes)"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname not in {
                    "query1.finance.yahoo.com",
                    "query2.finance.yahoo.com",
                }:
                    raise ValueError("Yahoo quote redirect changed origin")
                payload = json.loads(response.read().decode("utf-8"))
            return parse_chart_payload(payload, now_ms=int(time.time() * 1_000))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = exc
        except (
            http.client.IncompleteRead,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
        time.sleep(0.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return None


def _persist_rows(rows: list[dict[str, Any]]) -> int:
    columns = (
        "contract_symbol",
        "quote_symbol",
        "relation",
        "instrument_type",
        "display_name",
        "source",
        "status",
        "market_state",
        "currency",
        "exchange_name",
        "price",
        "previous_close",
        "change_pct",
        "pct_2m",
        "pct_5m",
        "pct_10m",
        "pct_24h",
        "regular_market_price",
        "day_open",
        "day_high",
        "day_low",
        "volume",
        "market_time_ms",
        "received_at_ms",
        "quality_json",
    )
    values = [
        tuple(
            json.dumps(row[name], ensure_ascii=False)
            if name == "quality_json"
            else row.get(name)
            for name in columns
        )
        for row in rows
    ]
    sql = f"""INSERT INTO underlying_market_quotes({','.join(columns)},created_at,updated_at)
              VALUES({','.join('?' for _ in columns)},CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
              ON DUPLICATE KEY UPDATE
                quote_symbol=VALUES(quote_symbol),relation=VALUES(relation),
                instrument_type=VALUES(instrument_type),
                display_name=COALESCE(VALUES(display_name),display_name),source=VALUES(source),
                status=CASE WHEN VALUES(status)='ok' THEN 'ok'
                            WHEN price IS NULL THEN VALUES(status) ELSE 'stale' END,
                market_state=VALUES(market_state),currency=COALESCE(VALUES(currency),currency),
                exchange_name=COALESCE(VALUES(exchange_name),exchange_name),
                price=COALESCE(VALUES(price),price),
                previous_close=COALESCE(VALUES(previous_close),previous_close),
                change_pct=COALESCE(VALUES(change_pct),change_pct),
                pct_2m=COALESCE(VALUES(pct_2m),pct_2m),
                pct_5m=COALESCE(VALUES(pct_5m),pct_5m),
                pct_10m=COALESCE(VALUES(pct_10m),pct_10m),
                pct_24h=COALESCE(VALUES(pct_24h),pct_24h),
                regular_market_price=COALESCE(VALUES(regular_market_price),regular_market_price),
                day_open=COALESCE(VALUES(day_open),day_open),
                day_high=COALESCE(VALUES(day_high),day_high),
                day_low=COALESCE(VALUES(day_low),day_low),
                volume=COALESCE(VALUES(volume),volume),
                market_time_ms=COALESCE(VALUES(market_time_ms),market_time_ms),
                received_at_ms=VALUES(received_at_ms),quality_json=VALUES(quality_json),
                updated_at=CURRENT_TIMESTAMP"""
    return store.executemany(sql, values)


def collect_quote_cycle(stop_event=None) -> dict[str, Any]:
    mappings = contract_mappings()
    requested = sorted({item.quote_symbol for item in mappings if item.quote_symbol})
    quotes: dict[str, dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_quote, symbol): symbol for symbol in requested}
        for future in as_completed(futures):
            symbol = futures[future]
            if stop_event is not None and stop_event.is_set():
                break
            try:
                quotes[symbol] = future.result()
            except Exception as exc:
                quotes[symbol] = None
                errors[symbol] = f"{type(exc).__name__}: {exc}"
    now_ms = int(time.time() * 1_000)
    rows: list[dict[str, Any]] = []
    for mapping in mappings:
        base = {
            **asdict(mapping),
            "display_name": None,
            "source": "mapping_only" if mapping.quote_symbol is None else "yahoo_chart",
            "status": "unsupported" if mapping.quote_symbol is None else "unavailable",
            "market_state": "unavailable",
            "currency": None,
            "exchange_name": None,
            "price": None,
            "previous_close": None,
            "change_pct": None,
            "pct_2m": None,
            "pct_5m": None,
            "pct_10m": None,
            "pct_24h": None,
            "regular_market_price": None,
            "day_open": None,
            "day_high": None,
            "day_low": None,
            "volume": None,
            "market_time_ms": None,
            "received_at_ms": now_ms,
            "quality_json": {
                "error": errors.get(mapping.quote_symbol or ""),
                "mapping_only": mapping.quote_symbol is None,
                "age_ms": None,
                "latency_ms": None,
                "coverage_ratio": 0.0,
            },
        }
        quote = quotes.get(mapping.quote_symbol or "")
        if quote is not None:
            base.update(quote)
        rows.append(base)
    _persist_rows(rows)
    return {
        "contracts": len(mappings),
        "requested_symbols": len(requested),
        "ok": sum(row["status"] == "ok" for row in rows),
        "stale": sum(row["status"] == "stale" for row in rows),
        "unavailable": sum(row["status"] == "unavailable" for row in rows),
        "unsupported": sum(row["status"] == "unsupported" for row in rows),
        "errors": dict(list(errors.items())[:25]),
    }


def quote_loop(stop_event=None) -> None:
    interval = max(60, int(settings.get("underlying_quote_seconds", POLL_SECONDS)))
    while stop_event is None or not stop_event.is_set():
        started = time.monotonic()
        try:
            result = collect_quote_cycle(stop_event)
            store.collector_report(
                "underlying_quotes",
                success=result["ok"] + result["stale"] > 0,
                items=result["ok"],
                details=result,
                error="all mapped quote requests failed"
                if result["ok"] + result["stale"] == 0
                else None,
            )
        except Exception as exc:
            store.collector_report(
                "underlying_quotes", success=False, error=f"{type(exc).__name__}: {exc}"
            )
        wait = max(1.0, interval - (time.monotonic() - started))
        if stop_event is None:
            time.sleep(wait)
        elif stop_event.wait(wait):
            break
