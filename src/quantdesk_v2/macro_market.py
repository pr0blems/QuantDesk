"""US macro-market context for AI opportunity discovery.

The opportunity engine trades mapped equity contracts, but the market regime is
measured from independent US-market instruments.  Index cards deliberately
identify ETF proxies instead of presenting proxy prices as cash-index prices.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import indicators
from .finnhub import FinnhubClient, FinnhubClientError, FinnhubQuote
from .monitor import MonitorRepository, MonitorUnavailable

TARGET_QUOTES: dict[str, tuple[str, str, str]] = {
    "NDX": ("QQQ", "QQQUSDT", "纳指 100"),
    "SPX": ("SPY", "SPYUSDT", "标普 500"),
    "DJI": ("DIA", "", "道琼斯"),
    "RUT": ("IWM", "IWMUSDT", "罗素 2000"),
    "TECH": ("XLK", "QQQUSDT", "科技"),
    "SEMIS": ("SMH", "SMHUSDT", "半导体"),
    "CRYPTO": ("BITQ", "", "加密关联"),
    "US10Y": ("TLT", "", "10Y 美债代理"),
    "US2Y": ("SHY", "", "2Y 美债代理"),
    "DXY": ("UUP", "", "美元指数代理"),
}
VIX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=5m"
INDEX_SYMBOLS = {
    "NDX": "^NDX",
    "SPX": "^GSPC",
    "DJI": "^DJI",
    "RUT": "^RUT",
}
EASTERN = ZoneInfo("America/New_York")

# Verified against the official Federal Reserve, BLS and BEA 2026 schedules.
# Times are US Eastern and converted with zoneinfo so daylight saving is honored.
MACRO_EVENTS_2026: tuple[tuple[str, str, str, str, str], ...] = (
    ("2026-08-26T08:30:00", "PCE", "个人收入与支出 / PCE", "high", "https://www.bea.gov/news/schedule"),
    ("2026-09-04T08:30:00", "NFP", "非农就业报告", "high", "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("2026-09-11T08:30:00", "CPI", "消费者价格指数 CPI", "high", "https://www.bls.gov/schedule/2026/home.htm"),
    ("2026-09-16T14:00:00", "FOMC", "FOMC 利率决议", "critical", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("2026-09-30T08:30:00", "PCE", "个人收入与支出 / PCE", "high", "https://www.bea.gov/news/schedule"),
    ("2026-10-02T08:30:00", "NFP", "非农就业报告", "high", "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("2026-10-28T14:00:00", "FOMC", "FOMC 利率决议", "critical", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("2026-10-29T08:30:00", "PCE", "个人收入与支出 / PCE", "high", "https://www.bea.gov/news/schedule"),
    ("2026-11-06T08:30:00", "NFP", "非农就业报告", "high", "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("2026-11-10T08:30:00", "CPI", "消费者价格指数 CPI", "high", "https://www.bls.gov/schedule/2026/home.htm"),
    ("2026-12-04T08:30:00", "NFP", "非农就业报告", "high", "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("2026-12-09T14:00:00", "FOMC", "FOMC 利率决议", "critical", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _quote_payload(quote: FinnhubQuote, *, key: str, label: str) -> dict[str, Any]:
    previous_close = quote.previous_close
    intraday = (
        (quote.price - quote.day_open) / quote.day_open * 100
        if quote.day_open not in (None, 0)
        else None
    )
    amplitude = (
        (quote.day_high - quote.day_low) / previous_close * 100
        if quote.day_high is not None
        and quote.day_low is not None
        and previous_close not in (None, 0)
        else None
    )
    return {
        "key": key,
        "label": label,
        "provider_symbol": quote.symbol,
        "price": quote.price,
        "change": quote.change,
        "change_percent": quote.change_percent,
        "intraday_change_percent": round(intraday, 4) if intraday is not None else None,
        "amplitude_percent": round(amplitude, 4) if amplitude is not None else None,
        "day_high": quote.day_high,
        "day_low": quote.day_low,
        "day_open": quote.day_open,
        "previous_close": previous_close,
        "source_timestamp": quote.source_timestamp,
        "available": True,
        "proxy": True,
        "source": "finnhub_etf_proxy",
    }


def _ticker_payload(
    item: Mapping[str, Any], *, key: str, provider_symbol: str, label: str
) -> dict[str, Any]:
    price = _number(item.get("price"))
    return {
        "key": key,
        "label": label,
        "provider_symbol": provider_symbol,
        "price": price,
        "change": None,
        "change_percent": _number(item.get("pct_24h")),
        "intraday_change_percent": None,
        "amplitude_percent": None,
        "day_high": None,
        "day_low": None,
        "day_open": None,
        "previous_close": None,
        "source_timestamp": item.get("ts"),
        "available": price is not None and price > 0,
        "proxy": True,
        "source": "binance_tradfi_proxy",
    }


def _fetch_vix(timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        VIX_URL,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 QuantDesk/2"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS origin
            raw = response.read(512 * 1024)
        payload = json.loads(raw)
        meta = payload["chart"]["result"][0]["meta"]
        price = _number(meta.get("regularMarketPrice"))
        previous = _number(meta.get("chartPreviousClose"))
        if price is None or price <= 0:
            raise ValueError("missing VIX price")
        change_percent = (
            (price - previous) / previous * 100 if previous not in (None, 0) else None
        )
        return {
            "available": True,
            "value": price,
            "change_percent": round(change_percent, 4) if change_percent is not None else None,
            "day_high": _number(meta.get("regularMarketDayHigh")),
            "day_low": _number(meta.get("regularMarketDayLow")),
            "source_timestamp": meta.get("regularMarketTime"),
            "source": "yahoo_finance_vix",
            "proxy": False,
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"available": False, "value": None, "source": "unavailable", "proxy": False}


def _fetch_cash_index(
    key: str,
    provider_symbol: str,
    label: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    quoted = quote(provider_symbol, safe="-^=.")
    query = urlencode({"range": "5d", "interval": "5m", "includePrePost": "true"})
    request = Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quoted}?{query}",
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 QuantDesk/2"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS origin
            raw = response.read(512 * 1024)
        chart = json.loads(raw)["chart"]["result"][0]
        meta = chart["meta"]
        price = _number(meta.get("regularMarketPrice"))
        previous_close = _number(meta.get("chartPreviousClose"))
        day_open = _number(meta.get("regularMarketOpen"))
        day_high = _number(meta.get("regularMarketDayHigh"))
        day_low = _number(meta.get("regularMarketDayLow"))
        timestamps = chart.get("timestamp") or []
        quote_rows = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        interval_opens = quote_rows.get("open") or []
        if day_open is None and timestamps and interval_opens:
            try:
                market_zone = ZoneInfo(str(meta.get("exchangeTimezoneName") or "America/New_York"))
            except (KeyError, ValueError):
                market_zone = EASTERN
            latest_date = datetime.fromtimestamp(int(timestamps[-1]), UTC).astimezone(market_zone).date()
            day_open = next(
                (
                    value
                    for timestamp, raw_open in zip(timestamps, interval_opens, strict=False)
                    if datetime.fromtimestamp(int(timestamp), UTC).astimezone(market_zone).date()
                    == latest_date
                    and (value := _number(raw_open)) is not None
                ),
                None,
            )
        if price is None or price <= 0:
            raise ValueError("missing cash-index price")
        change = price - previous_close if previous_close not in (None, 0) else None
        change_percent = (
            change / previous_close * 100
            if change is not None and previous_close not in (None, 0)
            else None
        )
        intraday = (price - day_open) / day_open * 100 if day_open not in (None, 0) else None
        amplitude = (
            (day_high - day_low) / previous_close * 100
            if day_high is not None and day_low is not None and previous_close not in (None, 0)
            else None
        )
        return {
            "key": key,
            "label": label,
            "provider_symbol": provider_symbol,
            "price": price,
            "change": change,
            "change_percent": round(change_percent, 4) if change_percent is not None else None,
            "intraday_change_percent": round(intraday, 4) if intraday is not None else None,
            "amplitude_percent": round(amplitude, 4) if amplitude is not None else None,
            "day_high": day_high,
            "day_low": day_low,
            "day_open": day_open,
            "previous_close": previous_close,
            "source_timestamp": meta.get("regularMarketTime"),
            "available": True,
            "proxy": False,
            "source": "yahoo_cash_index",
        }
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        return {"key": key, "available": False, "proxy": False, "source": "unavailable"}


def _market_rsi(repository: MonitorRepository, contract_symbol: str) -> float | None:
    if not contract_symbol:
        return None
    try:
        rows = repository.klines(contract_symbol, "1h", 60)
    except MonitorUnavailable:
        return None
    closes = [_number(row.get("close")) for row in rows]
    return indicators.rsi([value for value in closes if value is not None], 14)


def macro_event_calendar(now: datetime) -> dict[str, Any]:
    aware_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    items: list[dict[str, Any]] = []
    for local_text, event_type, title, impact, source_url in MACRO_EVENTS_2026:
        scheduled = datetime.fromisoformat(local_text).replace(tzinfo=EASTERN).astimezone(UTC)
        hours_until = (scheduled - aware_now).total_seconds() / 3600
        if -6 <= hours_until <= 60 * 24:
            items.append(
                {
                    "event_type": event_type,
                    "title": title,
                    "impact": impact,
                    "scheduled_at": scheduled.isoformat(),
                    "hours_until": round(hours_until, 2),
                    "source_url": source_url,
                }
            )
    items.sort(key=lambda item: item["scheduled_at"])
    upcoming = [item for item in items if item["hours_until"] >= 0]
    imminent = [item for item in upcoming if item["hours_until"] <= 6]
    near = [item for item in upcoming if item["hours_until"] <= 24]
    risk_level = "critical" if any(item["impact"] == "critical" for item in imminent) else "high" if imminent else "medium" if near else "normal"
    return {
        "items": items[:12],
        "next_event": upcoming[0] if upcoming else None,
        "risk_level": risk_level,
        "risk_active": risk_level in {"critical", "high", "medium"},
        "source": "official_schedule_snapshot_2026",
    }


class MacroMarketService:
    """Rate-bounded market-regime cache with database fallbacks."""

    def __init__(
        self,
        client: FinnhubClient,
        quote_service: Any | None = None,
        *,
        cache_seconds: int = 60,
        stale_seconds: int = 900,
    ) -> None:
        self.client = client
        self.quote_service = quote_service
        self.cache_seconds = cache_seconds
        self.stale_seconds = stale_seconds
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self, repository: MonitorRepository, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC).replace(tzinfo=None)
        with self._lock:
            age = time.monotonic() - self._cached_at
            if self._cached is not None and age <= self.cache_seconds:
                return dict(self._cached)
            try:
                snapshot = self._build(repository, current)
            except Exception:
                if self._cached is not None and age <= self.stale_seconds:
                    stale = dict(self._cached)
                    stale["stale"] = True
                    stale["cache_age_seconds"] = round(age, 1)
                    return stale
                return unavailable_snapshot(current)
            self._cached = snapshot
            self._cached_at = time.monotonic()
            return dict(snapshot)

    def _build(self, repository: MonitorRepository, now: datetime) -> dict[str, Any]:
        cached_quotes: dict[str, FinnhubQuote] = {}
        if self.quote_service is not None:
            try:
                for item in self.quote_service.snapshot().get("quotes", []):
                    if item.get("available"):
                        fields = {key: item.get(key) for key in FinnhubQuote.__dataclass_fields__}
                        cached_quotes[str(item.get("symbol") or "").upper()] = FinnhubQuote(**fields)
            except (TypeError, ValueError):
                cached_quotes = {}
        try:
            all_tickers = repository.latest_tickers()
        except MonitorUnavailable:
            all_tickers = {}
        contracts = [contract for _, contract, _ in TARGET_QUOTES.values() if contract]
        ticker_map = {
            contract: all_tickers[contract]
            for contract in contracts
            if contract in all_tickers
        }
        missing_provider_symbols = {
            provider_symbol
            for provider_symbol, _, _ in TARGET_QUOTES.values()
            if provider_symbol not in cached_quotes
        }
        if missing_provider_symbols:
            with ThreadPoolExecutor(max_workers=min(6, len(missing_provider_symbols))) as executor:
                pending = {
                    executor.submit(self.client.quote, provider_symbol): provider_symbol
                    for provider_symbol in missing_provider_symbols
                }
                for future in as_completed(pending):
                    provider_symbol = pending[future]
                    try:
                        cached_quotes[provider_symbol] = future.result()
                    except FinnhubClientError:
                        continue
        entries: dict[str, dict[str, Any]] = {}
        for key, (provider_symbol, contract_symbol, label) in TARGET_QUOTES.items():
            quote = cached_quotes.get(provider_symbol)
            if quote is not None:
                entry = _quote_payload(quote, key=key, label=label)
            elif contract_symbol and contract_symbol in ticker_map:
                entry = _ticker_payload(
                    ticker_map[contract_symbol],
                    key=key,
                    provider_symbol=provider_symbol,
                    label=label,
                )
            else:
                entry = {
                    "key": key,
                    "label": label,
                    "provider_symbol": provider_symbol,
                    "available": False,
                    "proxy": True,
                    "source": "unavailable",
                }
            if key in {"NDX", "SPX", "RUT"}:
                rsi_value = _market_rsi(repository, contract_symbol)
                entry["rsi_14_1h"] = round(rsi_value, 2) if rsi_value is not None else None
            entries[key] = entry

        with ThreadPoolExecutor(max_workers=len(INDEX_SYMBOLS)) as executor:
            pending_indices = {
                executor.submit(
                    _fetch_cash_index,
                    key,
                    provider_symbol,
                    TARGET_QUOTES[key][2],
                    self.client.timeout_seconds,
                ): key
                for key, provider_symbol in INDEX_SYMBOLS.items()
            }
            for future in as_completed(pending_indices):
                key = pending_indices[future]
                exact = future.result()
                if exact.get("available"):
                    exact["rsi_14_1h"] = entries[key].get("rsi_14_1h")
                    entries[key] = exact

        vix = _fetch_vix(self.client.timeout_seconds)
        breadth_changes = [
            value
            for item in all_tickers.values()
            if (value := _number(item.get("pct_24h"))) is not None
        ]
        if len(breadth_changes) < 10:
            breadth_changes = [
                value
                for quote in cached_quotes.values()
                if (value := _number(quote.change_percent)) is not None
            ]
        advancers = sum(value > 0 for value in breadth_changes)
        decliners = sum(value < 0 for value in breadth_changes)
        unchanged = max(0, len(breadth_changes) - advancers - decliners)
        ratio = advancers / decliners if decliners else float(advancers) if advancers else None
        breadth = {
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "total": len(breadth_changes),
            "advance_decline_ratio": round(ratio, 4) if ratio is not None else None,
            "coverage": "configured_us_universe",
            "available": len(breadth_changes) >= 10,
        }
        major_changes = [
            _number(entries[key].get("change_percent"))
            for key in ("NDX", "SPX", "RUT")
        ]
        major_changes = [value for value in major_changes if value is not None]
        average_change = sum(major_changes) / len(major_changes) if major_changes else 0.0
        breadth_bias = (
            (advancers - decliners) / (advancers + decliners)
            if advancers + decliners
            else 0.0
        )
        vix_value = _number(vix.get("value"))
        sentiment_score = 50 + average_change * 10 + breadth_bias * 20
        if vix_value is not None:
            sentiment_score -= (vix_value - 20) * 1.5
        sentiment_score = _clamp(sentiment_score, 0, 100)
        sentiment = "euphoria" if sentiment_score >= 68 else "fear" if sentiment_score <= 35 else "neutral"
        direction = "bull" if average_change >= 0.35 else "bear" if average_change <= -0.35 else "neutral"
        events = macro_event_calendar(now)
        available = sum(bool(entries[key].get("available")) for key in ("NDX", "SPX", "DJI", "RUT"))
        return {
            "version": "macro_market_context_v1",
            "available": available >= 2,
            "stale": False,
            "captured_at": now.replace(tzinfo=UTC).isoformat(),
            "indices": [entries[key] for key in ("NDX", "SPX", "DJI", "RUT")],
            "market_rsi": entries["NDX"].get("rsi_14_1h"),
            "vix": vix,
            "breadth": breadth,
            "sectors": [entries[key] for key in ("TECH", "SEMIS", "CRYPTO")],
            "macro_assets": [entries[key] for key in ("US10Y", "US2Y", "DXY")],
            "sentiment": {
                "key": sentiment,
                "label": {"euphoria": "狂热", "fear": "恐慌", "neutral": "中性"}[sentiment],
                "score": round(sentiment_score, 2),
                "direction": direction,
                "average_index_change_percent": round(average_change, 4),
            },
            "events": events,
            "source_note": "大盘优先采用实时指数行情，失败时回退 ETF；VIX 为指数行情；美债与美元为 ETF 代理。",
        }


def unavailable_snapshot(now: datetime) -> dict[str, Any]:
    return {
        "version": "macro_market_context_v1",
        "available": False,
        "stale": False,
        "captured_at": now.replace(tzinfo=UTC).isoformat(),
        "indices": [],
        "market_rsi": None,
        "vix": {"available": False, "value": None, "source": "unavailable", "proxy": False},
        "breadth": {"available": False, "advancers": 0, "decliners": 0, "unchanged": 0, "total": 0, "advance_decline_ratio": None},
        "sectors": [],
        "macro_assets": [],
        "sentiment": {"key": "neutral", "label": "数据不足", "score": 50.0, "direction": "neutral", "average_index_change_percent": 0.0},
        "events": macro_event_calendar(now),
        "source_note": "大盘行情暂不可用，机会评分未应用宏观调整。",
    }


def sector_key(symbol: str, sector: str | None = None, industry: str | None = None) -> str:
    normalized = symbol.upper()
    combined = f"{sector or ''} {industry or ''}".lower()
    if normalized in {"MSTR", "COIN", "MARA", "RIOT", "CLSK", "IBIT"} or any(
        token in combined for token in ("crypto", "bitcoin", "blockchain", "加密")
    ):
        return "CRYPTO"
    if any(token in combined for token in ("semiconductor", "半导体", "chip")):
        return "SEMIS"
    if any(token in combined for token in ("technology", "software", "internet", "科技", "软件")):
        return "TECH"
    return "BROAD"


def opportunity_market_context(
    snapshot: Mapping[str, Any] | None,
    *,
    direction: str,
    symbol: str,
    sector: str | None = None,
    industry: str | None = None,
) -> dict[str, Any]:
    """Return a transparent directional adjustment for one opportunity."""

    if not snapshot or not snapshot.get("available"):
        return {
            "version": "macro_directional_adjustment_v1",
            "available": False,
            "adjustment": 0.0,
            "resonance": "unknown",
            "resonance_label": "大盘数据不足",
            "sector_key": sector_key(symbol, sector, industry),
            "factors": [],
        }
    factors: list[dict[str, Any]] = []

    def add(key: str, label: str, points: float, value: Any) -> None:
        if points:
            factors.append({"key": key, "label": label, "points": points, "value": value})

    long_side = direction == "long"
    indices = {str(item.get("key")): item for item in snapshot.get("indices", []) if isinstance(item, Mapping)}
    ndx_change = _number(indices.get("NDX", {}).get("change_percent"))
    if ndx_change is not None:
        if ndx_change >= 1:
            add("ndx_trend", "纳指强势", 6 if long_side else -7, ndx_change)
        elif ndx_change <= -1:
            add("ndx_trend", "纳指走弱", -10 if long_side else 7, ndx_change)
    vix_value = _number((snapshot.get("vix") or {}).get("value"))
    if vix_value is not None:
        if vix_value >= 30:
            add("vix", "VIX 高恐慌", -15 if long_side else 6, vix_value)
        elif vix_value >= 25:
            add("vix", "VIX 风险升温", -8 if long_side else 3, vix_value)
        elif vix_value < 18:
            add("vix", "VIX 低波动", 3 if long_side else -2, vix_value)
    breadth = snapshot.get("breadth") or {}
    breadth_ratio = _number(breadth.get("advance_decline_ratio"))
    if breadth.get("available") and breadth_ratio is not None:
        if breadth_ratio >= 1.5:
            add("breadth", "上涨家数占优", 4 if long_side else -4, breadth_ratio)
        elif breadth_ratio <= 0.67:
            add("breadth", "下跌家数占优", -5 if long_side else 4, breadth_ratio)
    selected_sector = sector_key(symbol, sector, industry)
    sectors = {str(item.get("key")): item for item in snapshot.get("sectors", []) if isinstance(item, Mapping)}
    sector_change = _number(sectors.get(selected_sector, {}).get("change_percent"))
    if sector_change is not None:
        if sector_change >= 1:
            add("sector", "所属板块强势", 5 if long_side else -5, sector_change)
        elif sector_change <= -1:
            add("sector", "所属板块走弱", -6 if long_side else 5, sector_change)
    event_risk = str((snapshot.get("events") or {}).get("risk_level") or "normal")
    if event_risk in {"critical", "high"}:
        add("macro_event", "重大宏观事件临近", -8, event_risk)
    elif event_risk == "medium":
        add("macro_event", "24 小时内有宏观事件", -4, event_risk)

    adjustment = _clamp(sum(float(item["points"]) for item in factors), -20, 10)
    market_direction = str((snapshot.get("sentiment") or {}).get("direction") or "neutral")
    aligned = (long_side and market_direction == "bull") or (not long_side and market_direction == "bear")
    opposed = (long_side and market_direction == "bear") or (not long_side and market_direction == "bull")
    resonance = "resonant" if aligned else "divergent" if opposed else "neutral"
    return {
        "version": "macro_directional_adjustment_v1",
        "available": True,
        "adjustment": round(adjustment, 4),
        "resonance": resonance,
        "resonance_label": {"resonant": "大盘共振", "divergent": "逆势高风险", "neutral": "大盘中性"}[resonance],
        "sector_key": selected_sector,
        "sector_label": sectors.get(selected_sector, {}).get("label", "大盘"),
        "sector_change_percent": sector_change,
        "market_direction": market_direction,
        "market_rsi": snapshot.get("market_rsi"),
        "vix": vix_value,
        "event_risk": event_risk,
        "factors": factors,
    }


def apply_market_adjustment(base_score: float, context: Mapping[str, Any]) -> float:
    return round(_clamp(float(base_score) + float(context.get("adjustment") or 0), 0, 100), 4)


_default_service: MacroMarketService | None = None


def configure_default_service(service: MacroMarketService) -> None:
    global _default_service
    _default_service = service


def default_snapshot(repository: MonitorRepository, *, now: datetime | None = None) -> dict[str, Any]:
    if _default_service is None:
        return unavailable_snapshot(now or datetime.now(UTC).replace(tzinfo=None))
    return _default_service.snapshot(repository, now=now)
