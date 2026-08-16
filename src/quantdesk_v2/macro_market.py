"""US macro-market context for AI opportunity discovery.

The opportunity engine trades mapped equity contracts, but the market regime is
measured from independent US-market instruments.  Index cards deliberately
identify ETF proxies instead of presenting proxy prices as cash-index prices.
"""

from __future__ import annotations

import json
import math
import time
from calendar import monthrange
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import indicators
from .finnhub import FinnhubClient, FinnhubClientError, FinnhubQuote
from .monitor import MonitorRepository, MonitorUnavailable
from .unusual_whales import UnusualWhalesMarketClient

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
_MARKET_TIDE_STREAM_LOCK = Lock()
_MARKET_TIDE_STREAM_POINTS: list[dict[str, Any]] = []

MARKET_SESSION_LABELS = {
    "premarket": "盘前",
    "regular": "盘中",
    "postmarket": "盘后",
    "closed": "休市",
}


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (nth - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter date used to derive the NYSE Good Friday closure."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    month_offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * month_offset) // 451
    month = (h + month_offset - 7 * m + 114) // 31
    day = (h + month_offset - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nyse_holidays(year: int) -> set[date]:
    return {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed_holiday(date(year, 6, 19)),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),
        # New Year's Day can be observed on December 31 of the prior year.
        _observed_fixed_holiday(date(year + 1, 1, 1)),
    }


def _is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _nyse_holidays(day.year)


def _market_moment(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, datetime_time(hour, minute), tzinfo=EASTERN)


def _next_regular_open(local: datetime, *, include_today: bool = True) -> datetime:
    candidate = local.date()
    if not include_today or not _is_nyse_trading_day(candidate) or local >= _market_moment(candidate, 9, 30):
        candidate += timedelta(days=1)
    while not _is_nyse_trading_day(candidate):
        candidate += timedelta(days=1)
    return _market_moment(candidate, 9, 30)


def us_market_session(
    now: datetime,
    *,
    upstream_sessions: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the current US equity session without confusing stale tape data.

    The clock is authoritative on weekends and outside extended hours.  During
    extended/regular hours a matching upstream ``market_time`` confirms that the
    feed is live.  Exchange holidays remain conservatively labelled by the clock
    unless the upstream is actively producing a matching session.
    """

    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    local = aware.astimezone(EASTERN)
    minutes = local.hour * 60 + local.minute
    trading_day = _is_nyse_trading_day(local.date())
    if not trading_day:
        key = "closed"
    elif 4 * 60 <= minutes < 9 * 60 + 30:
        key = "premarket"
    elif 9 * 60 + 30 <= minutes < 16 * 60:
        key = "regular"
    elif 16 * 60 <= minutes < 20 * 60:
        key = "postmarket"
    else:
        key = "closed"
    normalized = [
        value.strip().lower()
        for value in upstream_sessions
        if value and value.strip().lower() in MARKET_SESSION_LABELS
    ]
    confirmed = key != "closed" and key in normalized
    if key == "premarket":
        countdown_target = _market_moment(local.date(), 9, 30)
        countdown_label = "距正式开盘"
    elif key == "regular":
        countdown_target = _market_moment(local.date(), 16)
        countdown_label = "距常规收盘"
    else:
        countdown_target = _next_regular_open(local)
        countdown_label = "距下次开盘"
    next_open = _next_regular_open(local, include_today=key in {"closed", "premarket"})
    return {
        "key": key,
        "label": MARKET_SESSION_LABELS[key],
        "timezone": "America/New_York",
        "local_time": local.isoformat(),
        "source": "clock_and_unusual_whales" if confirmed else "us_market_clock",
        "upstream_confirmed": confirmed,
        "upstream_sessions": sorted(set(normalized)),
        "realtime_expected": key in {"premarket", "regular", "postmarket"},
        "allows_new_entries": key == "regular",
        "is_trading_day": trading_day,
        "countdown_label": countdown_label,
        "countdown_target_at": countdown_target.isoformat(),
        "next_open_at": next_open.isoformat(),
        "regular_open_time": "09:30 ET",
        "regular_close_time": "16:00 ET",
        "reliability": {
            "premarket": 0.72,
            "regular": 1.0,
            "postmarket": 0.58,
            "closed": 0.0,
        }[key],
    }

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


def _market_tide_timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(
                float(value) / (1_000 if float(value) > 10**11 else 1),
                tz=UTC,
            )
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return parsed


def _market_tide_directional_value(row: Mapping[str, Any]) -> tuple[float, str] | None:
    """Use signed flow fields only; Call/Put identity is not a direction."""

    for key in ("directional_value", "net_volume", "net_flow", "signed_flow"):
        value = _number(row.get(key))
        if value is not None:
            return value, key
    return None


def _market_tide_raw_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("points", "rows", "history", "data", "samples_data"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            rows.extend(item for item in value if isinstance(item, Mapping))
    raw = payload.get("raw")
    if isinstance(raw, Mapping):
        for key in ("points", "rows", "history", "data", "samples_data"):
            value = raw.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                rows.extend(item for item in value if isinstance(item, Mapping))
    rows.append(payload)
    return rows


def _market_tide_slope(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    mean_x = sum(item[0] for item in points) / len(points)
    mean_y = sum(item[1] for item in points) / len(points)
    denominator = sum((item[0] - mean_x) ** 2 for item in points)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def market_tide_trend_snapshot(
    payload: Mapping[str, Any] | None,
    *,
    history: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build auditable 5/15/30m Market Tide trends from signed raw points."""

    source = dict(payload or {})
    candidates = [*history, *_market_tide_raw_rows(source)]
    by_time: dict[datetime, dict[str, Any]] = {}
    rejected_call_put_only = 0
    for row in candidates:
        timestamp = _market_tide_timestamp(
            row.get("timestamp")
            or row.get("time")
            or row.get("tape_time")
            or row.get("captured_at")
        )
        directional = _market_tide_directional_value(row)
        if timestamp is None or directional is None:
            if directional is None and (
                row.get("net_call_premium") is not None
                or row.get("net_put_premium") is not None
            ):
                rejected_call_put_only += 1
            continue
        value, basis = directional
        by_time[timestamp] = {
            "timestamp": timestamp.isoformat(),
            "timestamp_ms": int(timestamp.timestamp() * 1_000),
            "directional_value": value,
            "basis": basis,
        }
    points = [by_time[key] for key in sorted(by_time)]
    if not points:
        return {
            "available": False,
            "directional_data_available": False,
            "bias": "neutral",
            "bias_basis": None,
            "point_count": 0,
            "points": [],
            "windows": {
                f"{minutes}m": {
                    "window_minutes": minutes,
                    "sample_count": 0,
                    "direction": "unknown",
                    "slope": None,
                    "acceleration": None,
                    "same_direction_points": 0,
                    "insufficient_data": True,
                }
                for minutes in (5, 15, 30)
            },
            "recent_6_slope": None,
            "recent_6_acceleration": None,
            "same_direction_points": 0,
            "insufficient_data": True,
            "rejected_call_put_only_points": rejected_call_put_only,
        }

    latest = points[-1]
    latest_value = float(latest["directional_value"])
    bias = "bull" if latest_value > 0 else "bear" if latest_value < 0 else "neutral"

    def window_summary(minutes: int) -> dict[str, Any]:
        latest_ms = int(latest["timestamp_ms"])
        cutoff = latest_ms - minutes * 60_000
        rows = [item for item in points if int(item["timestamp_ms"]) >= cutoff]
        coordinates = [
            (
                (int(item["timestamp_ms"]) - latest_ms) / 60_000,
                float(item["directional_value"]),
            )
            for item in rows[-6:]
        ]
        slope = _market_tide_slope(coordinates)
        differences = [
            (coordinates[index][0], coordinates[index][1] - coordinates[index - 1][1])
            for index in range(1, len(coordinates))
        ]
        acceleration = _market_tide_slope(differences)
        latest_direction = (
            "bull"
            if float(rows[-1]["directional_value"]) > 0
            else "bear"
            if float(rows[-1]["directional_value"]) < 0
            else "neutral"
        )
        consecutive = 0
        for item in reversed(rows):
            item_value = float(item["directional_value"])
            item_direction = "bull" if item_value > 0 else "bear" if item_value < 0 else "neutral"
            if item_direction != latest_direction:
                break
            consecutive += 1
        return {
            "window_minutes": minutes,
            "sample_count": len(rows),
            "direction": latest_direction,
            "start_value": rows[0]["directional_value"],
            "end_value": rows[-1]["directional_value"],
            "change": round(
                float(rows[-1]["directional_value"])
                - float(rows[0]["directional_value"]),
                6,
            ),
            "slope": round(slope, 8) if slope is not None else None,
            "acceleration": (
                round(acceleration, 8) if acceleration is not None else None
            ),
            "same_direction_points": consecutive,
            "insufficient_data": len(rows) < 2,
        }

    windows = {f"{minutes}m": window_summary(minutes) for minutes in (5, 15, 30)}
    recent = points[-6:]
    recent_coordinates = [
        (
            (int(item["timestamp_ms"]) - int(latest["timestamp_ms"])) / 60_000,
            float(item["directional_value"]),
        )
        for item in recent
    ]
    recent_slope = _market_tide_slope(recent_coordinates)
    recent_differences = [
        (
            recent_coordinates[index][0],
            recent_coordinates[index][1] - recent_coordinates[index - 1][1],
        )
        for index in range(1, len(recent_coordinates))
    ]
    recent_acceleration = _market_tide_slope(recent_differences)
    same_direction = 0
    for item in reversed(points):
        value = float(item["directional_value"])
        item_bias = "bull" if value > 0 else "bear" if value < 0 else "neutral"
        if item_bias != bias:
            break
        same_direction += 1
    return {
        "available": True,
        "directional_data_available": True,
        "bias": bias,
        "bias_basis": str(latest["basis"]),
        "point_count": len(points),
        "points": points[-31:],
        "windows": windows,
        "recent_6_slope": (
            round(recent_slope, 8) if recent_slope is not None else None
        ),
        "recent_6_acceleration": (
            round(recent_acceleration, 8)
            if recent_acceleration is not None
            else None
        ),
        "same_direction_points": same_direction,
        "insufficient_data": len(points) < 2,
        "rejected_call_put_only_points": rejected_call_put_only,
    }


def record_market_tide_event(event: Any) -> int:
    """Retain bounded signed Tide points supplied by the audited event stream."""

    channel = str(
        event.get("channel", "") if isinstance(event, Mapping) else getattr(event, "channel", "")
    )
    if channel.partition(":")[0] != "market_tide":
        return 0
    values = (
        dict(event.get("values") or {})
        if isinstance(event, Mapping)
        else dict(getattr(event, "values", {}) or {})
    )
    raw = (
        dict(event.get("raw") or {})
        if isinstance(event, Mapping)
        else dict(getattr(event, "raw", {}) or {})
    )
    event_time_ms = (
        event.get("event_time_ms")
        if isinstance(event, Mapping)
        else getattr(event, "event_time_ms", None)
    )
    timestamp = raw.get("timestamp") or event_time_ms
    point = {**values, "timestamp": timestamp}
    if _market_tide_timestamp(timestamp) is None:
        return 0
    with _MARKET_TIDE_STREAM_LOCK:
        trend = market_tide_trend_snapshot(
            {"points": [*(_market_tide_raw_rows(raw)), point]},
            history=_MARKET_TIDE_STREAM_POINTS,
        )
        _MARKET_TIDE_STREAM_POINTS[:] = list(trend.get("points") or [])[-31:]
        return len(_MARKET_TIDE_STREAM_POINTS)


def market_tide_stream_history() -> list[dict[str, Any]]:
    with _MARKET_TIDE_STREAM_LOCK:
        return [dict(item) for item in _MARKET_TIDE_STREAM_POINTS]


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


def _unusual_whales_payload(
    item: Mapping[str, Any], *, key: str, provider_symbol: str, label: str
) -> dict[str, Any]:
    price = _number(item.get("price"))
    previous_close = _number(item.get("previous_close"))
    day_open = _number(item.get("open"))
    day_high = _number(item.get("high"))
    day_low = _number(item.get("low"))
    change = price - previous_close if price is not None and previous_close not in (None, 0) else None
    change_percent = change / previous_close * 100 if change is not None else None
    intraday = (price - day_open) / day_open * 100 if price is not None and day_open not in (None, 0) else None
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
        "source_timestamp": item.get("tape_time"),
        "market_time": item.get("market_time"),
        "total_volume": item.get("total_volume"),
        "available": price is not None and price > 0,
        "proxy": True,
        "source": "unusual_whales_stock_state",
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
        unusual_whales_client: UnusualWhalesMarketClient | None = None,
        *,
        cache_seconds: int = 5,
        stale_seconds: int = 900,
    ) -> None:
        self.client = client
        self.quote_service = quote_service
        self.unusual_whales_client = unusual_whales_client
        self.cache_seconds = cache_seconds
        self.stale_seconds = stale_seconds
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._market_tide_history: list[dict[str, Any]] = []

    def _enhance_market_tide(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload or {})
        raw_bias = result.get("bias")
        trend = market_tide_trend_snapshot(
            result,
            history=[*self._market_tide_history, *market_tide_stream_history()],
        )
        self._market_tide_history = list(trend.get("points") or [])[-31:]
        current_point_available = bool(
            _market_tide_timestamp(result.get("timestamp") or result.get("time"))
            and _market_tide_directional_value(result) is not None
        )
        result.update(
            {
                "raw_provider_bias": raw_bias,
                "bias": trend.get("bias") if current_point_available else "neutral",
                "bias_basis": (
                    trend.get("bias_basis") if current_point_available else None
                ),
                "directional_data_available": current_point_available,
                "trend": trend,
                "trend_data_insufficient": bool(trend.get("insufficient_data")),
            }
        )
        return result

    def snapshot(self, repository: MonitorRepository, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC).replace(tzinfo=None)
        with self._lock:
            age = time.monotonic() - self._cached_at
            clock_session = us_market_session(current)
            cache_seconds = self.cache_seconds if clock_session["realtime_expected"] else max(60, self.cache_seconds)
            if self._cached is not None and age <= cache_seconds:
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
        provider_symbols = [provider_symbol for provider_symbol, _, _ in TARGET_QUOTES.values()]
        unusual_whales_configured = bool(
            self.unusual_whales_client and self.unusual_whales_client.configured()
        )
        unusual_states: dict[str, dict[str, Any]] = {}
        market_tide: dict[str, Any] = {
            "available": False,
            "source": "unavailable",
        }
        if unusual_whales_configured and self.unusual_whales_client is not None:
            with ThreadPoolExecutor(max_workers=2) as executor:
                state_future = executor.submit(
                    self.unusual_whales_client.stock_states,
                    provider_symbols,
                )
                tide_future = executor.submit(self.unusual_whales_client.market_tide)
                try:
                    unusual_states = state_future.result()
                except Exception:
                    unusual_states = {}
                try:
                    market_tide = tide_future.result()
                except Exception:
                    market_tide = {
                        "available": False,
                        "source": "unusual_whales_market_tide",
                    }
        market_tide = self._enhance_market_tide(market_tide)
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
            if provider_symbol not in cached_quotes and provider_symbol not in unusual_states
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
            unusual_state = unusual_states.get(provider_symbol)
            quote = cached_quotes.get(provider_symbol)
            if unusual_state is not None:
                entry = _unusual_whales_payload(
                    unusual_state,
                    key=key,
                    provider_symbol=provider_symbol,
                    label=label,
                )
            elif quote is not None:
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
                    realtime_proxy = entries[key] if entries[key].get("available") else None
                    exact["rsi_14_1h"] = entries[key].get("rsi_14_1h")
                    if realtime_proxy is not None:
                        exact["realtime_proxy"] = realtime_proxy
                        exact["market_time"] = realtime_proxy.get("market_time")
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
        tide_bias = str(market_tide.get("bias") or "neutral")
        if market_tide.get("available") and market_tide.get(
            "directional_data_available"
        ):
            sentiment_score += 4 if tide_bias == "bull" else -4 if tide_bias == "bear" else 0
        sentiment_score = _clamp(sentiment_score, 0, 100)
        sentiment = "euphoria" if sentiment_score >= 68 else "fear" if sentiment_score <= 35 else "neutral"
        direction = "bull" if average_change >= 0.35 else "bear" if average_change <= -0.35 else "neutral"
        events = macro_event_calendar(now)
        session = us_market_session(
            now,
            upstream_sessions=tuple(
                str(item.get("market_time") or "") for item in unusual_states.values()
            ),
        )
        available = sum(bool(entries[key].get("available")) for key in ("NDX", "SPX", "DJI", "RUT"))
        return {
            "version": "macro_market_context_v2",
            "available": available >= 2,
            "stale": False,
            "captured_at": now.replace(tzinfo=UTC).isoformat(),
            "market_session": session,
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
            "market_tide": market_tide,
            "providers": {
                "unusual_whales_configured": unusual_whales_configured,
                "unusual_whales_quotes": len(unusual_states),
                "unusual_whales_tide": bool(market_tide.get("available")),
            },
            "source_note": "现金指数保留原指数口径；Unusual Whales 提供盘前/盘中/盘后 ETF 实时代理与 Market Tide，失败时自动回退 Finnhub、Yahoo 和映射合约。",
        }


def unavailable_snapshot(now: datetime) -> dict[str, Any]:
    return {
        "version": "macro_market_context_v2",
        "available": False,
        "stale": False,
        "captured_at": now.replace(tzinfo=UTC).isoformat(),
        "market_session": us_market_session(now),
        "indices": [],
        "market_rsi": None,
        "vix": {"available": False, "value": None, "source": "unavailable", "proxy": False},
        "breadth": {"available": False, "advancers": 0, "decliners": 0, "unchanged": 0, "total": 0, "advance_decline_ratio": None},
        "sectors": [],
        "macro_assets": [],
        "sentiment": {"key": "neutral", "label": "数据不足", "score": 50.0, "direction": "neutral", "average_index_change_percent": 0.0},
        "events": macro_event_calendar(now),
        "market_tide": {"available": False, "source": "unavailable"},
        "providers": {
            "unusual_whales_configured": False,
            "unusual_whales_quotes": 0,
            "unusual_whales_tide": False,
        },
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
            "market_session": dict((snapshot or {}).get("market_session") or {}),
            "market_tide": dict((snapshot or {}).get("market_tide") or {}),
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

    session = snapshot.get("market_session") or {}
    session_key = str(session.get("key") or "")
    if session_key == "premarket":
        add("market_session", "盘前流动性折价", -3, session_key)
    elif session_key == "postmarket":
        add("market_session", "盘后流动性折价", -4, session_key)
    elif session_key == "closed":
        add("market_session", "美股休市", -6, session_key)

    market_tide = snapshot.get("market_tide") or {}
    tide_bias = str(market_tide.get("bias") or "neutral")
    if (
        market_tide.get("available")
        and market_tide.get("directional_data_available")
        and tide_bias in {"bull", "bear"}
    ):
        tide_aligned = (long_side and tide_bias == "bull") or (
            not long_side and tide_bias == "bear"
        )
        add(
            "market_tide",
            "期权资金潮共振" if tide_aligned else "期权资金潮逆向",
            3 if tide_aligned else -4,
            tide_bias,
        )

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
        "market_session": session,
        "market_tide": market_tide,
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
