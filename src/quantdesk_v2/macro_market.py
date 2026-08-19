"""US macro-market context for AI opportunity discovery.

The opportunity engine trades mapped equity contracts, but the market regime is
measured from independent US-market instruments.  Index cards deliberately
identify ETF proxies instead of presenting proxy prices as cash-index prices.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import time
from calendar import monthrange
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from . import indicators
from .finnhub import FinnhubClient, FinnhubClientError, FinnhubQuote
from .models import AdminSetting
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
    "BANKS": ("KBE", "", "银行"),
    "ENERGY": ("XLE", "", "能源"),
    "EQUAL_WEIGHT": ("RSP", "", "标普等权"),
    "HIGH_YIELD": ("HYG", "", "高收益债"),
    "OIL": ("USO", "", "原油"),
    "US10Y": ("TLT", "", "10Y 美债代理"),
    "US2Y": ("SHY", "", "2Y 美债代理"),
    "DXY": ("UUP", "", "美元指数代理"),
}
VIX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=5m"
MOVE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EMOVE?range=1mo&interval=1d"
TREASURY_NOMINAL_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?field_tdr_date_value={year}"
    "&type=daily_treasury_yield_curve&page&_format=csv"
)
TREASURY_REAL_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?field_tdr_date_value={year}"
    "&type=daily_treasury_real_yield_curve&page&_format=csv"
)
BIS_POLICY_RATE_URL = (
    "https://stats.bis.org/api/v1/data/WS_CBPOL/{series}"
    "?startPeriod={start_period}"
)
MACRO_TREASURY_CACHE_KEY = "macro_treasury_last_good_v1"
MACRO_CENTRAL_BANK_CACHE_KEY = "macro_central_bank_last_good_v1"
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


def _row_value(row: Mapping[str, Any], *keys: str) -> float | None:
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = _number(normalized.get(key.strip().lower()))
        if value is not None:
            return value
    return None


def _row_date(row: Mapping[str, Any]) -> date | None:
    raw = row.get("Date") or row.get("date")
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw), pattern).date()
        except (TypeError, ValueError):
            continue
    return None


def _ordered_treasury_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    dated = [(parsed, row) for row in rows if (parsed := _row_date(row)) is not None]
    dated.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in dated]


def _series_zscore(values: Sequence[float], *, window: int = 60) -> float | None:
    selected = [float(value) for value in values[:window] if math.isfinite(float(value))]
    if len(selected) < 10:
        return None
    deviation = statistics.pstdev(selected)
    if deviation <= 1e-9:
        return 0.0
    return round((selected[0] - statistics.fmean(selected)) / deviation, 4)


def _change_bps(values: Sequence[float], offset: int) -> float | None:
    if len(values) <= offset:
        return None
    return round((values[0] - values[offset]) * 100, 2)


def _yield_metric(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    label: str,
    columns: Sequence[str],
) -> dict[str, Any]:
    values = [value for row in rows if (value := _row_value(row, *columns)) is not None]
    latest_date = _row_date(rows[0]).isoformat() if rows and _row_date(rows[0]) else None
    if not values:
        return {
            "key": key,
            "label": label,
            "available": False,
            "value": None,
            "change_bps": {"1d": None, "5d": None, "20d": None},
            "zscore": None,
            "as_of": latest_date,
        }
    return {
        "key": key,
        "label": label,
        "available": True,
        "value": round(values[0], 4),
        "change_bps": {
            "1d": _change_bps(values, 1),
            "5d": _change_bps(values, 5),
            "20d": _change_bps(values, 20),
        },
        "zscore": _series_zscore(values),
        "as_of": latest_date,
    }


def _difference_metric(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    key: str,
    label: str,
) -> dict[str, Any]:
    left_value = _number(left.get("value"))
    right_value = _number(right.get("value"))
    available = left_value is not None and right_value is not None
    changes: dict[str, float | None] = {}
    for horizon in ("1d", "5d", "20d"):
        left_change = _number(dict(left.get("change_bps") or {}).get(horizon))
        right_change = _number(dict(right.get("change_bps") or {}).get(horizon))
        changes[horizon] = (
            round(left_change - right_change, 2)
            if left_change is not None and right_change is not None
            else None
        )
    return {
        "key": key,
        "label": label,
        "available": available,
        "value": round((left_value - right_value) * 100, 2) if available else None,
        "unit": "bps",
        "change_bps": changes,
        "as_of": left.get("as_of") or right.get("as_of"),
    }


def classify_rate_shock(curve: Mapping[str, Any]) -> dict[str, Any]:
    maturities = {
        str(item.get("key")): item
        for item in curve.get("nominal", [])
        if isinstance(item, Mapping)
    }
    two_change = _number(dict(maturities.get("2Y", {}).get("change_bps") or {}).get("5d"))
    ten_change = _number(dict(maturities.get("10Y", {}).get("change_bps") or {}).get("5d"))
    thirty_change = _number(dict(maturities.get("30Y", {}).get("change_bps") or {}).get("5d"))
    real_change = _number(dict(curve.get("real_10y", {}).get("change_bps") or {}).get("5d"))
    breakeven_change = _number(
        dict(curve.get("breakeven_10y", {}).get("change_bps") or {}).get("5d")
    )
    available = sum(value is not None for value in (two_change, ten_change, thirty_change)) >= 2
    regime = "insufficient_data"
    label = "收益率数据不足"
    severity = "unknown"
    reasons: list[str] = []
    impacts: list[str] = []
    if available:
        regime, label, severity = "stable", "利率环境平稳", "normal"
        if (
            two_change is not None
            and ten_change is not None
            and two_change >= 10
            and ten_change >= 10
        ):
            regime, label, severity = "hawkish_inflation", "鹰派 / 通胀冲击", "shock"
            reasons.append("2Y 与 10Y 收益率 5 日同步快速上升")
            impacts.append("利率敏感资产整体降杠杆，等待波动收敛后再入场")
        elif (
            ten_change is not None
            and ten_change >= 8
            and real_change is not None
            and real_change >= 8
        ):
            regime, label, severity = "real_rate_tightening", "实际利率收紧", "shock"
            reasons.append("10Y 名义与实际利率同步上升")
            impacts.append("科技、软件、加密等长久期资产估值压力最大")
        elif (
            two_change is not None
            and abs(two_change) < 5
            and max(ten_change or -999, thirty_change or -999) >= 8
        ):
            regime, label, severity = "term_premium_fiscal", "期限溢价 / 财政压力", "tightening"
            reasons.append("2Y 稳定而 10Y/30Y 长端利率上升")
            impacts.append("长久期成长股承压；银行需结合曲线陡峭化与信用风险判断")
        elif (
            breakeven_change is not None
            and breakeven_change >= 8
            and (real_change is None or abs(real_change) < 5)
        ):
            regime, label, severity = "inflation_expectation", "通胀预期抬升", "tightening"
            reasons.append("10Y 通胀预期 5 日明显上升、实际利率相对稳定")
            impacts.append("能源和资源股可能受益，高估值资产仍需防通胀估值压缩")
        elif max(ten_change or -999, thirty_change or -999) >= 6:
            regime, label, severity = "long_end_pressure", "长端利率压力上升", "tightening"
            reasons.append("长端收益率 5 日上升，但尚未达到冲击级别")
            impacts.append("提高成长股入场确认要求，不对全市场无差别看空")
    return {
        "available": available,
        "regime": regime,
        "label": label,
        "severity": severity,
        "changes_5d_bps": {
            "2Y": two_change,
            "10Y": ten_change,
            "30Y": thirty_change,
            "10Y_REAL": real_change,
            "10Y_BREAKEVEN": breakeven_change,
        },
        "reasons": reasons,
        "impacts": impacts,
    }


def treasury_curve_snapshot(
    nominal_rows: Sequence[Mapping[str, Any]],
    real_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    nominal = _ordered_treasury_rows(nominal_rows)
    real = _ordered_treasury_rows(real_rows)
    nominal_metrics = [
        _yield_metric(nominal, key="2Y", label="2Y 名义收益率", columns=("2 Yr", "2 YR")),
        _yield_metric(nominal, key="5Y", label="5Y 名义收益率", columns=("5 Yr", "5 YR")),
        _yield_metric(nominal, key="10Y", label="10Y 名义收益率", columns=("10 Yr", "10 YR")),
        _yield_metric(nominal, key="30Y", label="30Y 名义收益率", columns=("30 Yr", "30 YR")),
    ]
    by_key = {item["key"]: item for item in nominal_metrics}
    real_10y = _yield_metric(real, key="10Y_REAL", label="10Y 实际利率", columns=("10 Yr", "10 YR"))
    breakeven_10y = _difference_metric(
        by_key["10Y"], real_10y, key="10Y_BREAKEVEN", label="10Y 通胀预期"
    )
    curves = [
        _difference_metric(by_key["10Y"], by_key["2Y"], key="2S10S", label="2s10s 曲线"),
        _difference_metric(by_key["30Y"], by_key["5Y"], key="5S30S", label="5s30s 曲线"),
    ]
    result = {
        "available": all(item["available"] for item in nominal_metrics),
        "source": "us_treasury_official_daily_curve",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
        "as_of": nominal_metrics[0].get("as_of"),
        "nominal": nominal_metrics,
        "real_10y": real_10y,
        "breakeven_10y": breakeven_10y,
        "curves": curves,
        "coverage": {
            "available": sum(bool(item["available"]) for item in nominal_metrics)
            + int(bool(real_10y["available"])),
            "required": 5,
        },
    }
    result["shock"] = classify_rate_shock(result)
    return result


def _unavailable_treasury_curve(reason: str = "官方收益率暂不可用") -> dict[str, Any]:
    blank = {
        "available": False,
        "value": None,
        "change_bps": {"1d": None, "5d": None, "20d": None},
        "zscore": None,
        "as_of": None,
    }
    result = {
        "available": False,
        "source": "unavailable",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
        "reason": reason,
        "as_of": None,
        "nominal": [{**blank, "key": key, "label": f"{key} 名义收益率"} for key in ("2Y", "5Y", "10Y", "30Y")],
        "real_10y": {**blank, "key": "10Y_REAL", "label": "10Y 实际利率"},
        "breakeven_10y": {**blank, "key": "10Y_BREAKEVEN", "label": "10Y 通胀预期"},
        "curves": [
            {**blank, "key": "2S10S", "label": "2s10s 曲线", "unit": "bps"},
            {**blank, "key": "5S30S", "label": "5s30s 曲线", "unit": "bps"},
        ],
        "coverage": {"available": 0, "required": 5},
    }
    result["shock"] = classify_rate_shock(result)
    return result


def global_central_bank_matrix(
    now: datetime,
    policy_rates: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Combine BIS policy rates with auditable official meeting metadata."""

    rows = [
        {
            "key": "FED",
            "label": "美联储",
            "policy_rate": "3.50%–3.75%",
            "midpoint": 3.625,
            "last_action": "2026-07-29 维持不变",
            "vote_split": "9–3（3 票倾向加息 25bp）",
            "next_meeting": "2026-09-15 / 09-16",
            "market_path": {"available": False, "label": "SOFR / Fed Funds 期货路径未接入"},
            "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
        },
        {
            "key": "ECB",
            "label": "欧洲央行",
            "policy_rate": "存款 2.25% / MRO 2.40%",
            "midpoint": 2.25,
            "last_action": "2026-07-23 维持不变",
            "vote_split": "官方公报未披露逐票分歧",
            "next_meeting": "2026-09-10",
            "market_path": {"available": False, "label": "€STR 期货路径未接入"},
            "source_url": "https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.mp260723~29f24d99bc.en.html",
        },
        {
            "key": "BOJ",
            "label": "日本央行",
            "policy_rate": "隔夜拆借约 1.00%",
            "midpoint": 1.0,
            "last_action": "2026-07-31 维持目标",
            "vote_split": "以官方会议记录为准",
            "next_meeting": "按 BOJ 官方会议日程",
            "market_path": {"available": False, "label": "TONA 期货路径未接入"},
            "source_url": "https://www.boj.or.jp/en/mopo/mpmdeci/state_2026/index.htm",
        },
    ]
    policy_rates = policy_rates or {}
    for row in rows:
        observed = policy_rates.get(str(row["key"]))
        if not isinstance(observed, Mapping) or not observed.get("available"):
            row["rate_source"] = "official_meeting_fallback"
            row["rate_status"] = "fallback"
            continue
        row.update(
            {
                "policy_rate": str(observed.get("policy_rate") or row["policy_rate"]),
                "midpoint": float(observed.get("midpoint") or row["midpoint"]),
                "last_action": str(observed.get("last_action") or row["last_action"]),
                "rate_as_of": observed.get("as_of"),
                "rate_source": "bis_official_policy_rate",
                "rate_source_url": observed.get("source_url"),
                "rate_status": "live",
                "change_5d_bps": observed.get("change_5d_bps"),
            }
        )
    fed = rows[0]["midpoint"]
    available_rates = sum(row.get("rate_status") == "live" for row in rows)
    latest_periods = [str(row.get("rate_as_of")) for row in rows if row.get("rate_as_of")]
    return {
        "available": True,
        "as_of": max(latest_periods) if latest_periods else now.date().isoformat(),
        "source": "bis_official_policy_rate" if available_rates else "official_meeting_fallback",
        "source_url": "https://data.bis.org/topics/CBPOL",
        "coverage": {"available": available_rates, "required": len(rows)},
        "rows": rows,
        "spreads": [
            {"key": "FED_ECB", "label": "Fed–ECB 政策利差", "value_bps": round((fed - rows[1]["midpoint"]) * 100, 2), "change_5d_bps": None},
            {"key": "FED_BOJ", "label": "Fed–BOJ 政策利差", "value_bps": round((fed - rows[2]["midpoint"]) * 100, 2), "change_5d_bps": None},
        ],
        "market_path_coverage": {"available": 0, "required": 3, "reason": "尚未接入政策利率期货数据源"},
    }


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


def _fetch_move(timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        MOVE_URL,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 QuantDesk/2"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS origin
            raw = response.read(512 * 1024)
        chart = json.loads(raw)["chart"]["result"][0]
        meta = chart["meta"]
        price = _number(meta.get("regularMarketPrice"))
        closes = [
            value
            for raw_value in (((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            if (value := _number(raw_value)) is not None
        ]
        previous = closes[-2] if len(closes) >= 2 else _number(meta.get("chartPreviousClose"))
        if price is None or price <= 0:
            raise ValueError("missing MOVE price")
        change_percent = (
            (price - previous) / previous * 100 if previous not in (None, 0) else None
        )
        return {
            "available": True,
            "value": round(price, 4),
            "change_percent": round(change_percent, 4) if change_percent is not None else None,
            "source_timestamp": meta.get("regularMarketTime"),
            "source": "yahoo_finance_move",
            "proxy": False,
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"available": False, "value": None, "source": "unavailable", "proxy": False}


def _fetch_treasury_csv(url: str, timeout_seconds: float) -> list[dict[str, str]]:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "home.treasury.gov":
        raise ValueError("unsupported Treasury data origin")
    request = Request(  # noqa: S310 - validated HTTPS Treasury origin
        url,
        headers={"Accept": "text/csv", "User-Agent": "Mozilla/5.0 QuantDesk/2"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS origin
        raw = response.read(2 * 1024 * 1024)
    content = raw.decode("utf-8-sig")
    rows = [dict(row) for row in csv.DictReader(io.StringIO(content))]
    if not rows:
        raise ValueError("empty Treasury curve")
    return rows


def _fetch_bis_policy_csv(
    series: str,
    start_period: str,
    timeout_seconds: float,
) -> list[dict[str, str]]:
    """Fetch one allowlisted BIS policy-rate series through the SDMX API."""

    if series not in {"D.US", "M.XM", "D.JP"}:
        raise ValueError("unsupported BIS policy-rate series")
    url = BIS_POLICY_RATE_URL.format(series=series, start_period=start_period)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "stats.bis.org":
        raise ValueError("unsupported BIS data origin")
    request = Request(  # noqa: S310 - validated HTTPS BIS origin
        url,
        headers={
            "Accept": "application/vnd.sdmx.data+csv;version=1.0.0",
            "User-Agent": "QuantDesk/2 macro-policy-rates",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed BIS origin
        raw = response.read(2 * 1024 * 1024)
    rows = [
        dict(row)
        for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if _number(row.get("OBS_VALUE")) is not None and row.get("TIME_PERIOD")
    ]
    if not rows:
        raise ValueError("empty BIS policy-rate series")
    return rows


def _bis_policy_metric(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    label: str,
) -> dict[str, Any]:
    observations: list[tuple[str, float]] = []
    for row in rows:
        period = str(row.get("TIME_PERIOD") or "").strip()
        value = _number(row.get("OBS_VALUE"))
        if period and value is not None:
            observations.append((period, value))
    observations.sort(key=lambda item: item[0])
    if not observations:
        return {"key": key, "label": label, "available": False}
    latest_period, latest_value = observations[-1]
    prior_different: tuple[str, float] | None = None
    for period, value in reversed(observations[:-1]):
        if not math.isclose(value, latest_value, abs_tol=0.000001):
            prior_different = (period, value)
            break
    if prior_different is None:
        last_action = f"回看期内维持 {latest_value:.3f}%"
        action_bps = 0.0
    else:
        action_bps = round((latest_value - prior_different[1]) * 100, 2)
        action_direction = "上调" if action_bps > 0 else "下调"
        last_action = f"{latest_period} {action_direction}至 {latest_value:.3f}%"
    five_step_change = None
    if len(observations) > 5:
        five_step_change = round((latest_value - observations[-6][1]) * 100, 2)
    return {
        "key": key,
        "label": label,
        "available": True,
        "policy_rate": f"{latest_value:.3f}%",
        "midpoint": round(latest_value, 4),
        "as_of": latest_period,
        "last_action": last_action,
        "last_action_bps": action_bps,
        "change_5d_bps": five_step_change,
        "source": "bis_official_policy_rate",
        "source_url": "https://data.bis.org/topics/CBPOL",
    }


def _fetch_daily_return_snapshot(
    provider_symbol: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Read medium-term ETF performance used by persistent regime checks."""

    quoted = quote(provider_symbol, safe="-^=.")
    query = urlencode({"range": "3mo", "interval": "1d"})
    request = Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quoted}?{query}",
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 QuantDesk/2"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS origin
            raw = response.read(512 * 1024)
        chart = json.loads(raw)["chart"]["result"][0]
        closes = [
            value
            for raw_value in (((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            if (value := _number(raw_value)) is not None and value > 0
        ]
        if len(closes) < 21:
            raise ValueError("daily history is incomplete")

        def performance(offset: int) -> float | None:
            if len(closes) <= offset or closes[-1 - offset] <= 0:
                return None
            return round((closes[-1] / closes[-1 - offset] - 1) * 100, 4)

        return {
            "available": True,
            "symbol": provider_symbol,
            "change_5d_percent": performance(5),
            "change_20d_percent": performance(20),
            "samples": len(closes),
            "source": "yahoo_daily_market_structure",
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
        return {
            "available": False,
            "symbol": provider_symbol,
            "change_5d_percent": None,
            "change_20d_percent": None,
            "samples": 0,
            "source": "unavailable",
        }


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


def _snapshot_items(snapshot: Mapping[str, Any], key: str) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("key")): item
        for item in snapshot.get(key, [])
        if isinstance(item, Mapping)
    }


def capital_retreat_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Confirm broad risk withdrawal only when at least two independent legs agree."""

    indices = _snapshot_items(snapshot, "indices")
    assets = _snapshot_items(snapshot, "macro_assets")
    fund_flow = snapshot.get("equity_fund_flows")
    fund_flow = dict(fund_flow) if isinstance(fund_flow, Mapping) else {}
    flow_weeks = _number(fund_flow.get("consecutive_outflow_weeks"))
    flow_met = flow_weeks is not None and flow_weeks >= 2
    checks: list[dict[str, Any]] = [
        {
            "key": "fund_outflow",
            "label": "股票资金连续 2–4 周净流出",
            "available": flow_weeks is not None,
            "met": flow_met,
            "value": flow_weeks,
            "detail": (
                f"已连续 {int(flow_weeks)} 周净流出"
                if flow_weeks is not None
                else "基金流向周频数据尚未接入，不参与确认"
            ),
        }
    ]
    structure = dict(snapshot.get("market_structure") or {})
    persistence = dict(structure.get("breadth_persistence") or {})
    spx_change = _number(indices.get("SPX", {}).get("change_percent"))
    equal_change = _number(assets.get("EQUAL_WEIGHT", {}).get("change_percent"))
    breadth = dict(snapshot.get("breadth") or {})
    breadth_ratio = _number(breadth.get("advance_decline_ratio"))
    relative_5d = _number(persistence.get("relative_5d_percent"))
    relative_20d = _number(persistence.get("relative_20d_percent"))
    breadth_available = bool(persistence.get("available")) and breadth_ratio is not None
    breadth_met = bool(
        breadth_available
        and relative_5d is not None
        and relative_20d is not None
        and relative_5d <= -0.5
        and relative_20d <= -0.5
        and breadth_ratio < 0.8
    )
    checks.append(
        {
            "key": "breadth_divergence",
            "label": "等权与上涨家数持续弱于市值指数",
            "available": breadth_available,
            "met": breadth_met,
            "value": {
                "equal_weight_change_percent": equal_change,
                "spx_change_percent": spx_change,
                "advance_decline_ratio": breadth_ratio,
                "relative_5d_percent": relative_5d,
                "relative_20d_percent": relative_20d,
            },
            "detail": (
                f"等权相对标普 5日 {relative_5d:+.2f}% / 20日 {relative_20d:+.2f}% · A/D {breadth_ratio:.2f}"
                if breadth_available
                else "等权 5/20 日相对强弱或市场宽度数据不足"
            ),
        }
    )
    credit_history = dict(structure.get("credit_dollar") or {})
    hyg_change = _number(credit_history.get("high_yield_5d_percent"))
    dxy_change = _number(credit_history.get("dollar_5d_percent"))
    if hyg_change is None:
        hyg_change = _number(assets.get("HIGH_YIELD", {}).get("change_percent"))
    if dxy_change is None:
        dxy_change = _number(assets.get("DXY", {}).get("change_percent"))
    vix = dict(snapshot.get("vix") or {})
    move = dict(snapshot.get("move") or {})
    vix_change = _number(vix.get("change_percent"))
    move_change = _number(move.get("change_percent"))
    credit_stress = hyg_change is not None and hyg_change <= -0.8
    vol_stress = (vix_change is not None and vix_change >= 4) or (
        move_change is not None and move_change >= 2
    )
    dollar_stress = dxy_change is not None and dxy_change >= 0.5
    stress_components = sum((credit_stress, vol_stress, dollar_stress))
    credit_available = sum(
        value is not None for value in (hyg_change, dxy_change, vix_change, move_change)
    ) >= 3
    checks.append(
        {
            "key": "credit_vol_dollar",
            "label": "信用利差、MOVE/VIX、美元同步走强",
            "available": credit_available,
            "met": bool(credit_available and stress_components >= 2),
            "value": {
                "hyg_change_percent": hyg_change,
                "vix_change_percent": vix_change,
                "move_change_percent": move_change,
                "dxy_change_percent": dxy_change,
                "stress_components": stress_components,
            },
            "detail": f"信用 {int(credit_stress)} / 波动 {int(vol_stress)} / 美元 {int(dollar_stress)}",
        }
    )
    available_count = sum(bool(item["available"]) for item in checks)
    met_count = sum(bool(item["available"] and item["met"]) for item in checks)
    confirmed = available_count >= 2 and met_count >= 2
    return {
        "version": "capital_retreat_confirmation_v1",
        "confirmed": confirmed,
        "label": "美股全面撤资已确认" if confirmed else "尚未确认美股全面撤资",
        "met_count": met_count,
        "available_count": available_count,
        "required_count": 2,
        "checks": checks,
    }


def macro_entry_policy(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    shock = dict(dict(snapshot.get("treasury_curve") or {}).get("shock") or {})
    retreat = dict(snapshot.get("capital_retreat") or {})
    events = dict(snapshot.get("events") or {})
    vix_value = _number(dict(snapshot.get("vix") or {}).get("value"))
    imminent_critical = any(
        isinstance(item, Mapping)
        and item.get("impact") == "critical"
        and 0 <= float(item.get("hours_until") or 999) <= 6
        for item in events.get("items", [])
    )
    state = "normal"
    label = "正常环境"
    threshold_delta = 0.0
    long_multiplier = 1.0
    short_multiplier = 1.0
    pause_new_trend_longs = False
    reasons: list[str] = []
    if imminent_critical or (
        retreat.get("confirmed") and vix_value is not None and vix_value >= 30
    ):
        state = "major_event_credit"
        label = "重大事件 / 信用压力"
        threshold_delta = 10.0
        long_multiplier = 0.0
        short_multiplier = 0.5
        pause_new_trend_longs = True
        reasons.append("重大事件窗口或信用压力达到暂停新增顺势多单条件")
    elif shock.get("severity") == "shock" or retreat.get("confirmed"):
        state = "rate_liquidity_shock"
        label = "利率 / 流动性冲击"
        threshold_delta = 10.0
        long_multiplier = 0.25
        short_multiplier = 0.5
        reasons.append(str(shock.get("label") or retreat.get("label") or label))
    elif shock.get("severity") == "tightening" or (
        vix_value is not None and vix_value >= 24
    ):
        state = "tightening"
        label = "偏紧环境"
        threshold_delta = 5.0
        long_multiplier = 0.6
        short_multiplier = 0.75
        reasons.append(str(shock.get("label") or "波动率与金融条件偏紧"))
    else:
        reasons.append("未触发利率、流动性或重大事件升级条件")
    return {
        "version": "macro_entry_policy_v1",
        "state": state,
        "label": label,
        "threshold_delta": threshold_delta,
        "position_multiplier": long_multiplier,
        "long_position_multiplier": long_multiplier,
        "short_position_multiplier": short_multiplier,
        "pause_new_trend_longs": pause_new_trend_longs,
        "reasons": reasons,
    }


def sector_macro_impacts(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    curve = dict(snapshot.get("treasury_curve") or {})
    shock = dict(curve.get("shock") or {})
    real_change = _number(
        dict(dict(curve.get("real_10y") or {}).get("change_bps") or {}).get("5d")
    )
    breakeven_change = _number(
        dict(dict(curve.get("breakeven_10y") or {}).get("change_bps") or {}).get("5d")
    )
    curves = {
        str(item.get("key")): item
        for item in curve.get("curves", [])
        if isinstance(item, Mapping)
    }
    curve_2s10s = _number(curves.get("2S10S", {}).get("value"))
    assets = _snapshot_items(snapshot, "macro_assets")
    dxy_change = _number(assets.get("DXY", {}).get("change_percent"))
    oil_change = _number(assets.get("OIL", {}).get("change_percent"))
    hyg_change = _number(assets.get("HIGH_YIELD", {}).get("change_percent"))
    vix_value = _number(dict(snapshot.get("vix") or {}).get("value"))
    retreat = bool(dict(snapshot.get("capital_retreat") or {}).get("confirmed"))

    def impact(key: str, label: str, points: float, reasons: list[str]) -> dict[str, Any]:
        bounded = round(_clamp(points, -20, 10), 2)
        return {
            "key": key,
            "label": label,
            "adjustment": bounded,
            "state": "supportive" if bounded >= 3 else "adverse" if bounded <= -5 else "neutral",
            "reasons": reasons or ["关键宏观因子未形成明显方向"],
        }

    tech_points = 0.0
    tech_reasons: list[str] = []
    if real_change is not None and real_change >= 5:
        tech_points -= min(12, real_change * 0.6)
        tech_reasons.append(f"10Y 实际利率 5 日 +{real_change:.1f}bp")
    if dxy_change is not None and dxy_change >= 0.25:
        tech_points -= min(5, dxy_change * 4)
        tech_reasons.append(f"美元代理 +{dxy_change:.2f}%")
    crypto_points = tech_points * 1.2
    crypto_reasons = [*tech_reasons]
    if shock.get("severity") == "shock":
        crypto_points -= 3
        crypto_reasons.append("利率冲击放大高波动资产折价")
    bank_points = 3.0 if curve_2s10s is not None and curve_2s10s > 0 else 0.0
    bank_reasons = [f"2s10s {curve_2s10s:+.1f}bp"] if curve_2s10s is not None else []
    if retreat:
        bank_points -= 10
        bank_reasons.append("资金撤退确认，信用风险压过曲线利好")
    energy_points = 0.0
    energy_reasons: list[str] = []
    if breakeven_change is not None and breakeven_change >= 5:
        energy_points += min(6, breakeven_change * 0.4)
        energy_reasons.append(f"10Y 通胀预期 5 日 +{breakeven_change:.1f}bp")
    if oil_change is not None:
        energy_points += _clamp(oil_change * 1.5, -6, 6)
        energy_reasons.append(f"原油代理 {oil_change:+.2f}%")
    small_points = 0.0
    small_reasons: list[str] = []
    if hyg_change is not None and hyg_change <= -0.4:
        small_points -= min(8, abs(hyg_change) * 5)
        small_reasons.append(f"高收益债代理 {hyg_change:+.2f}%")
    if vix_value is not None and vix_value >= 25:
        small_points -= min(8, (vix_value - 20) * 0.8)
        small_reasons.append(f"VIX {vix_value:.1f}")
    return [
        impact("TECH", "科技 / 软件", tech_points, tech_reasons),
        impact("SEMIS", "半导体", tech_points * 0.85, tech_reasons),
        impact("CRYPTO", "加密关联", crypto_points, crypto_reasons),
        impact("BANKS", "银行", bank_points, bank_reasons),
        impact("ENERGY", "能源 / 资源", energy_points, energy_reasons),
        impact("SMALL_CAP", "小盘股", small_points, small_reasons),
        impact("BROAD", "其他行业", tech_points * 0.25, tech_reasons[:1]),
    ]


class MacroMarketService:
    """Rate-bounded market-regime cache with database fallbacks."""

    def __init__(
        self,
        client: FinnhubClient,
        quote_service: Any | None = None,
        unusual_whales_client: UnusualWhalesMarketClient | None = None,
        *,
        engine: Engine | None = None,
        cache_seconds: int = 5,
        finnhub_enabled: bool = True,
        unusual_whales_enabled: bool = True,
        unusual_whales_cache_seconds: int = 5 * 60,
        stale_seconds: int = 900,
    ) -> None:
        self.client = client
        self.engine = engine
        self.quote_service = quote_service
        self.unusual_whales_client = unusual_whales_client
        self.cache_seconds = cache_seconds
        self.finnhub_enabled = bool(finnhub_enabled)
        self.unusual_whales_enabled = bool(unusual_whales_enabled)
        self.unusual_whales_cache_seconds = max(
            5 * 60, int(unusual_whales_cache_seconds)
        )
        self.stale_seconds = stale_seconds
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._market_tide_history: list[dict[str, Any]] = []
        self._unusual_whales_cached_at = 0.0
        self._unusual_whales_cached_states: dict[str, dict[str, Any]] = {}
        self._unusual_whales_cached_tide: dict[str, Any] = {
            "available": False,
            "source": "unavailable",
        }
        self._treasury_cached_at = 0.0
        self._treasury_cached: dict[str, Any] | None = None
        self._treasury_persistence_loaded = False
        self._treasury_last_success_at = ""
        self._central_bank_cached_at = 0.0
        self._central_bank_cached: dict[str, Any] | None = None
        self._central_bank_persistence_loaded = False
        self._central_bank_last_success_at = ""
        self._market_structure_cached_at = 0.0
        self._market_structure_cached: dict[str, Any] | None = None

    def _load_persisted_snapshot(self, key: str) -> dict[str, Any] | None:
        if self.engine is None:
            return None
        try:
            session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
            with session_factory() as db:
                setting = db.get(AdminSetting, key)
                value = setting.value_json if setting is not None else None
                return dict(value) if isinstance(value, Mapping) else None
        except Exception:
            return None

    def _persist_snapshot(self, key: str, value: Mapping[str, Any]) -> None:
        if self.engine is None:
            return
        try:
            session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
            with session_factory() as db:
                setting = db.get(AdminSetting, key)
                payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
                if setting is None:
                    setting = AdminSetting(
                        key=key,
                        value_json=payload,
                        version=1,
                        updated_by=None,
                    )
                    db.add(setting)
                else:
                    setting.value_json = payload
                    setting.version = int(setting.version or 0) + 1
                db.commit()
        except Exception:
            # Persistence is a durability layer. A database hiccup must not
            # invalidate the already successful provider response in memory.
            return

    def set_unusual_whales_enabled(self, enabled: bool) -> None:
        """Apply the platform switch and invalidate the combined market snapshot."""

        normalized = bool(enabled)
        with self._lock:
            if normalized == self.unusual_whales_enabled:
                return
            self.unusual_whales_enabled = normalized
            self._cached = None
            self._cached_at = 0.0
            if not normalized:
                self._unusual_whales_cached_at = 0.0
                self._unusual_whales_cached_states = {}
                self._unusual_whales_cached_tide = {
                    "available": False,
                    "source": "disabled",
                }

    def set_finnhub_enabled(self, enabled: bool) -> None:
        """Apply the platform cash-quote switch and invalidate market context."""

        normalized = bool(enabled)
        with self._lock:
            if normalized == self.finnhub_enabled:
                return
            self.finnhub_enabled = normalized
            self._cached = None
            self._cached_at = 0.0

    def _unusual_whales_snapshot(
        self,
        provider_symbols: Sequence[str],
        *,
        allow_refresh: bool = True,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], bool]:
        """Return one shared REST snapshot, refreshing it at most every five minutes."""

        configured = bool(
            self.unusual_whales_enabled
            and self.unusual_whales_client
            and self.unusual_whales_client.configured()
        )
        if not configured or self.unusual_whales_client is None:
            return (
                {},
                {
                    "available": False,
                    "source": "disabled" if not self.unusual_whales_enabled else "unavailable",
                },
                configured,
            )
        if not allow_refresh:
            return (
                dict(self._unusual_whales_cached_states),
                dict(self._unusual_whales_cached_tide),
                configured,
            )
        age = time.monotonic() - self._unusual_whales_cached_at
        if self._unusual_whales_cached_at and age < self.unusual_whales_cache_seconds:
            return (
                dict(self._unusual_whales_cached_states),
                dict(self._unusual_whales_cached_tide),
                configured,
            )

        states = dict(self._unusual_whales_cached_states)
        tide = dict(self._unusual_whales_cached_tide)
        state_succeeded = False
        tide_succeeded = False
        with ThreadPoolExecutor(max_workers=2) as executor:
            state_future = executor.submit(
                self.unusual_whales_client.stock_states,
                provider_symbols,
            )
            tide_future = executor.submit(self.unusual_whales_client.market_tide)
            try:
                states = state_future.result()
                state_succeeded = True
            except Exception:
                state_succeeded = False
            try:
                tide = tide_future.result()
                tide_succeeded = True
            except Exception:
                tide_succeeded = False
        if state_succeeded:
            self._unusual_whales_cached_states = dict(states)
        if tide_succeeded:
            self._unusual_whales_cached_tide = dict(tide)
        self._unusual_whales_cached_at = time.monotonic()
        return states, tide, configured

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

    def _treasury_snapshot(self, now: datetime) -> dict[str, Any]:
        """Read official daily curves without putting a slow origin on the 5s UI path."""

        if not self._treasury_persistence_loaded:
            self._treasury_cached = self._load_persisted_snapshot(
                MACRO_TREASURY_CACHE_KEY
            )
            self._treasury_last_success_at = str(
                (self._treasury_cached or {}).get("last_success_at") or ""
            )
            self._treasury_persistence_loaded = True

        age = time.monotonic() - self._treasury_cached_at
        if self._treasury_cached is not None and age < 6 * 60 * 60:
            return dict(self._treasury_cached)
        year = now.year
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                nominal_future = executor.submit(
                    _fetch_treasury_csv,
                    TREASURY_NOMINAL_URL.format(year=year),
                    max(5.0, float(self.client.timeout_seconds)),
                )
                real_future = executor.submit(
                    _fetch_treasury_csv,
                    TREASURY_REAL_URL.format(year=year),
                    max(5.0, float(self.client.timeout_seconds)),
                )
                result = treasury_curve_snapshot(
                    nominal_future.result(),
                    real_future.result(),
                )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, csv.Error):
            if self._treasury_cached is not None:
                stale = dict(self._treasury_cached)
                stale["stale"] = True
                stale["cache_age_seconds"] = round(age, 1)
                # Back off after an origin failure instead of retrying the
                # Treasury endpoint on every five-second market-context read.
                self._treasury_cached = dict(stale)
                self._treasury_cached_at = time.monotonic()
                return stale
            result = _unavailable_treasury_curve()
        if result.get("available"):
            self._treasury_last_success_at = datetime.now(UTC).isoformat()
            result["last_success_at"] = self._treasury_last_success_at
            result["stale"] = False
            self._persist_snapshot(MACRO_TREASURY_CACHE_KEY, result)
        self._treasury_cached = dict(result)
        self._treasury_cached_at = time.monotonic()
        return result

    def _central_bank_snapshot(self, now: datetime) -> dict[str, Any]:
        """Refresh official BIS policy rates at low frequency with DB fallback."""

        if not self._central_bank_persistence_loaded:
            self._central_bank_cached = self._load_persisted_snapshot(
                MACRO_CENTRAL_BANK_CACHE_KEY
            )
            self._central_bank_last_success_at = str(
                (self._central_bank_cached or {}).get("last_success_at") or ""
            )
            self._central_bank_persistence_loaded = True

        age = time.monotonic() - self._central_bank_cached_at
        if self._central_bank_cached is not None and age < 12 * 60 * 60:
            return dict(self._central_bank_cached)
        start_daily = (now.date() - timedelta(days=400)).isoformat()
        start_monthly = (now.date() - timedelta(days=400)).strftime("%Y-%m")
        specs = {
            "FED": ("D.US", start_daily, "美联储"),
            "ECB": ("M.XM", start_monthly, "欧洲央行"),
            "BOJ": ("D.JP", start_daily, "日本央行"),
        }
        metrics: dict[str, dict[str, Any]] = {}
        timeout_seconds = min(
            12.0,
            max(5.0, float(getattr(self.client, "timeout_seconds", 5.0))),
        )
        with ThreadPoolExecutor(max_workers=len(specs)) as executor:
            pending = {
                executor.submit(
                    _fetch_bis_policy_csv,
                    series,
                    start_period,
                    timeout_seconds,
                ): (key, label)
                for key, (series, start_period, label) in specs.items()
            }
            for future in as_completed(pending):
                key, label = pending[future]
                try:
                    metrics[key] = _bis_policy_metric(
                        future.result(),
                        key=key,
                        label=label,
                    )
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, csv.Error):
                    continue
        if metrics:
            result = global_central_bank_matrix(now, metrics)
            self._central_bank_last_success_at = datetime.now(UTC).isoformat()
            result["last_success_at"] = self._central_bank_last_success_at
            result["stale"] = False
            self._central_bank_cached = dict(result)
            self._central_bank_cached_at = time.monotonic()
            self._persist_snapshot(MACRO_CENTRAL_BANK_CACHE_KEY, result)
            return result
        if self._central_bank_cached is not None:
            stale = dict(self._central_bank_cached)
            stale["stale"] = True
            stale["cache_age_seconds"] = round(age, 1)
            self._central_bank_cached_at = time.monotonic()
            return stale
        result = global_central_bank_matrix(now)
        result["stale"] = True
        result["reason"] = "BIS 暂不可用，当前显示官方会议快照"
        self._central_bank_cached = dict(result)
        self._central_bank_cached_at = time.monotonic()
        return result

    def _market_structure_snapshot(self) -> dict[str, Any]:
        """Cache 5/20-day cross-asset confirmation inputs for fifteen minutes."""

        age = time.monotonic() - self._market_structure_cached_at
        if self._market_structure_cached is not None and age < 15 * 60:
            return dict(self._market_structure_cached)
        timeout_seconds = max(5.0, float(getattr(self.client, "timeout_seconds", 5.0)))
        symbols = ("RSP", "SPY", "HYG", "UUP")
        with ThreadPoolExecutor(max_workers=len(symbols)) as executor:
            pending = {
                symbol: executor.submit(
                    _fetch_daily_return_snapshot,
                    symbol,
                    timeout_seconds,
                )
                for symbol in symbols
            }
            histories = {symbol: future.result() for symbol, future in pending.items()}
        rsp_5d = _number(histories["RSP"].get("change_5d_percent"))
        rsp_20d = _number(histories["RSP"].get("change_20d_percent"))
        spy_5d = _number(histories["SPY"].get("change_5d_percent"))
        spy_20d = _number(histories["SPY"].get("change_20d_percent"))
        breadth_available = None not in (rsp_5d, rsp_20d, spy_5d, spy_20d)
        result = {
            "source": "daily_cross_asset_confirmation",
            "breadth_persistence": {
                "available": breadth_available,
                "relative_5d_percent": round(rsp_5d - spy_5d, 4) if breadth_available else None,
                "relative_20d_percent": round(rsp_20d - spy_20d, 4) if breadth_available else None,
                "equal_weight_5d_percent": rsp_5d,
                "equal_weight_20d_percent": rsp_20d,
                "spx_proxy_5d_percent": spy_5d,
                "spx_proxy_20d_percent": spy_20d,
            },
            "credit_dollar": {
                "available": bool(histories["HYG"].get("available") and histories["UUP"].get("available")),
                "high_yield_5d_percent": histories["HYG"].get("change_5d_percent"),
                "dollar_5d_percent": histories["UUP"].get("change_5d_percent"),
            },
            "raw": histories,
        }
        self._market_structure_cached = dict(result)
        self._market_structure_cached_at = time.monotonic()
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
        session = us_market_session(now)
        regular_market_open = session["key"] == "regular"
        provider_symbols = [provider_symbol for provider_symbol, _, _ in TARGET_QUOTES.values()]
        unusual_states, market_tide, unusual_whales_configured = (
            self._unusual_whales_snapshot(
                provider_symbols,
                allow_refresh=regular_market_open,
            )
        )
        market_tide = self._enhance_market_tide(market_tide)
        cached_quotes: dict[str, FinnhubQuote] = {}
        if self.finnhub_enabled and self.quote_service is not None:
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
        if self.finnhub_enabled and regular_market_open and missing_provider_symbols:
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

        with ThreadPoolExecutor(max_workers=5) as executor:
            vix_future = executor.submit(_fetch_vix, self.client.timeout_seconds)
            move_future = executor.submit(_fetch_move, self.client.timeout_seconds)
            treasury_future = executor.submit(self._treasury_snapshot, now)
            central_bank_future = executor.submit(self._central_bank_snapshot, now)
            structure_future = executor.submit(self._market_structure_snapshot)
            vix = vix_future.result()
            move = move_future.result()
            treasury_curve = treasury_future.result()
            central_banks = central_bank_future.result()
            market_structure = structure_future.result()
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
        result = {
            "version": "macro_market_context_v3",
            "available": available >= 2,
            "stale": False,
            "captured_at": now.replace(tzinfo=UTC).isoformat(),
            "market_session": session,
            "indices": [entries[key] for key in ("NDX", "SPX", "DJI", "RUT")],
            "market_rsi": entries["NDX"].get("rsi_14_1h"),
            "vix": vix,
            "move": move,
            "breadth": breadth,
            "sectors": [entries[key] for key in ("TECH", "SEMIS", "CRYPTO", "BANKS", "ENERGY")],
            "macro_assets": [
                entries[key]
                for key in (
                    "US10Y",
                    "US2Y",
                    "DXY",
                    "EQUAL_WEIGHT",
                    "HIGH_YIELD",
                    "OIL",
                )
            ],
            "treasury_curve": treasury_curve,
            "market_structure": market_structure,
            "central_banks": central_banks,
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
                "finnhub_enabled": self.finnhub_enabled,
                "finnhub_configured": self.client.configured,
                "finnhub_market_open_only": True,
                "unusual_whales_enabled": self.unusual_whales_enabled,
                "unusual_whales_configured": unusual_whales_configured,
                "unusual_whales_quotes": len(unusual_states),
                "unusual_whales_tide": bool(market_tide.get("available")),
                "unusual_whales_refresh_seconds": self.unusual_whales_cache_seconds,
                "collection_market_open": regular_market_open,
                "treasury_direct_yields": bool(treasury_curve.get("available")),
            },
            "source_note": "收益率取美国财政部官方日曲线；Finnhub 与 Unusual Whales 仅在美股常规交易时段采集。Binance 映射合约始终是交易与结算主价格。",
        }
        result["capital_retreat"] = capital_retreat_snapshot(result)
        result["entry_policy"] = macro_entry_policy(result)
        result["sector_impacts"] = sector_macro_impacts(result)
        captured_at = str(result["captured_at"])
        structure_available = bool(
            market_structure.get("breadth_persistence", {}).get("available")
            or market_structure.get("credit_dollar", {}).get("available")
        )
        result["data_sources"] = [
            {
                "key": "binance",
                "label": "Binance 映射合约",
                "role": "交易与结算主价格",
                "tier": "primary_execution",
                "status": "healthy" if ticker_map else "degraded",
                "source": "Binance Futures",
                "cadence": "实时行情 / K 线闭合更新",
                "last_success_at": captured_at if ticker_map else None,
                "next_refresh_at": None,
                "stable": True,
            },
            {
                "key": "treasury",
                "label": "美国国债直接收益率",
                "role": "宏观利率基线",
                "tier": "official_primary",
                "status": "stale" if treasury_curve.get("stale") else "healthy" if treasury_curve.get("available") else "unavailable",
                "source": "U.S. Department of the Treasury",
                "source_url": treasury_curve.get("source_url"),
                "cadence": "交易日约 15:30 ET 发布；系统每 6 小时检查",
                "as_of": treasury_curve.get("as_of"),
                "last_success_at": treasury_curve.get("last_success_at") or self._treasury_last_success_at or None,
                "next_refresh_at": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
                "stable": True,
            },
            {
                "key": "bis_policy",
                "label": "全球央行政策利率",
                "role": "政策利率与跨央行利差",
                "tier": "official_primary",
                "status": (
                    "stale"
                    if central_banks.get("stale")
                    else "healthy"
                    if central_banks.get("coverage", {}).get("available")
                    == central_banks.get("coverage", {}).get("required")
                    else "degraded"
                    if central_banks.get("coverage", {}).get("available")
                    else "fallback"
                ),
                "source": "Bank for International Settlements",
                "source_url": central_banks.get("source_url"),
                "cadence": "BIS 每周中更新；系统每 12 小时检查",
                "as_of": central_banks.get("as_of"),
                "last_success_at": central_banks.get("last_success_at") or self._central_bank_last_success_at or None,
                "next_refresh_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
                "stable": True,
            },
            {
                "key": "finnhub",
                "label": "Finnhub 美股现货",
                "role": "盘中现货交叉验证",
                "tier": "realtime_reference",
                "status": "healthy" if self.finnhub_enabled and self.client.configured and regular_market_open else "scheduled" if self.finnhub_enabled and self.client.configured else "disabled",
                "source": "Finnhub",
                "cadence": "仅美股常规盘按实时采集服务轮询",
                "last_success_at": captured_at if cached_quotes else None,
                "next_refresh_at": None,
                "stable": bool(self.client.configured),
            },
            {
                "key": "unusual_whales",
                "label": "Unusual Whales",
                "role": "Market Tide 与补充盘口",
                "tier": "supplementary_realtime",
                "status": "healthy" if unusual_states or market_tide.get("available") else "scheduled" if unusual_whales_configured and not regular_market_open else "disabled" if not self.unusual_whales_enabled else "degraded",
                "source": "Unusual Whales",
                "cadence": "仅美股常规盘每 5 分钟",
                "last_success_at": captured_at if unusual_states or market_tide.get("available") else None,
                "next_refresh_at": (datetime.now(UTC) + timedelta(seconds=self.unusual_whales_cache_seconds)).isoformat() if regular_market_open and unusual_whales_configured else None,
                "stable": bool(unusual_whales_configured),
            },
            {
                "key": "market_proxy",
                "label": "跨资产与波动率代理",
                "role": "VIX / MOVE / RSP / HYG / UUP 补充确认",
                "tier": "non_blocking_fallback",
                "status": "healthy" if structure_available and vix.get("available") else "degraded",
                "source": "Yahoo Finance public chart fallback",
                "cadence": "15 分钟；失败不阻断交易，只保留旧快照",
                "last_success_at": captured_at if structure_available or vix.get("available") else None,
                "next_refresh_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "stable": False,
            },
        ]
        return result


def unavailable_snapshot(now: datetime) -> dict[str, Any]:
    result = {
        "version": "macro_market_context_v3",
        "available": False,
        "stale": False,
        "captured_at": now.replace(tzinfo=UTC).isoformat(),
        "market_session": us_market_session(now),
        "indices": [],
        "market_rsi": None,
        "vix": {"available": False, "value": None, "source": "unavailable", "proxy": False},
        "move": {"available": False, "value": None, "source": "unavailable", "proxy": False},
        "breadth": {"available": False, "advancers": 0, "decliners": 0, "unchanged": 0, "total": 0, "advance_decline_ratio": None},
        "sectors": [],
        "macro_assets": [],
        "treasury_curve": _unavailable_treasury_curve(),
        "market_structure": {
            "source": "unavailable",
            "breadth_persistence": {"available": False},
            "credit_dollar": {"available": False},
        },
        "central_banks": global_central_bank_matrix(now),
        "sentiment": {"key": "neutral", "label": "数据不足", "score": 50.0, "direction": "neutral", "average_index_change_percent": 0.0},
        "events": macro_event_calendar(now),
        "market_tide": {"available": False, "source": "unavailable"},
        "providers": {
            "finnhub_enabled": False,
            "finnhub_configured": False,
            "finnhub_market_open_only": True,
            "unusual_whales_enabled": False,
            "unusual_whales_configured": False,
            "unusual_whales_quotes": 0,
            "unusual_whales_tide": False,
        },
        "source_note": "大盘行情暂不可用，机会评分未应用宏观调整。",
    }
    result["capital_retreat"] = capital_retreat_snapshot(result)
    result["entry_policy"] = macro_entry_policy(result)
    result["sector_impacts"] = sector_macro_impacts(result)
    return result


def sector_key(symbol: str, sector: str | None = None, industry: str | None = None) -> str:
    normalized = symbol.upper()
    combined = f"{sector or ''} {industry or ''}".lower()
    if normalized in {"MSTR", "COIN", "MARA", "RIOT", "CLSK", "IBIT"} or any(
        token in combined for token in ("crypto", "bitcoin", "blockchain", "加密")
    ):
        return "CRYPTO"
    if any(token in combined for token in ("semiconductor", "半导体", "chip")):
        return "SEMIS"
    if any(token in combined for token in ("bank", "financial", "银行", "金融")):
        return "BANKS"
    if any(token in combined for token in ("energy", "oil", "gas", "mining", "能源", "石油", "资源")):
        return "ENERGY"
    if any(token in combined for token in ("small cap", "small-cap", "小盘")):
        return "SMALL_CAP"
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
            "version": "macro_directional_adjustment_v2",
            "available": False,
            "adjustment": 0.0,
            "resonance": "unknown",
            "resonance_label": "大盘数据不足",
            "sector_key": sector_key(symbol, sector, industry),
            "market_session": dict((snapshot or {}).get("market_session") or {}),
            "market_tide": dict((snapshot or {}).get("market_tide") or {}),
            "entry_policy": dict((snapshot or {}).get("entry_policy") or {}),
            "rate_shock": dict(dict((snapshot or {}).get("treasury_curve") or {}).get("shock") or {}),
            "capital_retreat": dict((snapshot or {}).get("capital_retreat") or {}),
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
    sector_impacts = {
        str(item.get("key")): item
        for item in snapshot.get("sector_impacts", [])
        if isinstance(item, Mapping)
    }
    selected_sector_impact = dict(
        sector_impacts.get(selected_sector) or sector_impacts.get("BROAD") or {}
    )
    sector_macro_adjustment = _number(selected_sector_impact.get("adjustment"))
    if sector_macro_adjustment is not None and abs(sector_macro_adjustment) >= 1:
        directional_sector_points = sector_macro_adjustment if long_side else -sector_macro_adjustment
        add(
            "sector_macro_sensitivity",
            f"{selected_sector_impact.get('label') or '行业'}宏观敏感度",
            _clamp(directional_sector_points, -12, 8),
            selected_sector_impact.get("reasons") or [],
        )
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
    global_policy = dict(snapshot.get("entry_policy") or {})
    position_multiplier = _number(
        global_policy.get(
            "long_position_multiplier" if long_side else "short_position_multiplier"
        )
    )
    direction_blocked = bool(long_side and global_policy.get("pause_new_trend_longs"))
    entry_policy = {
        **global_policy,
        "direction": direction,
        "position_multiplier": position_multiplier if position_multiplier is not None else 1.0,
        "entry_allowed": not direction_blocked,
        "blocked_reason": (
            "重大事件或信用压力环境暂停新增顺势多单" if direction_blocked else None
        ),
    }
    return {
        "version": "macro_directional_adjustment_v2",
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
        "entry_policy": entry_policy,
        "rate_shock": dict(dict(snapshot.get("treasury_curve") or {}).get("shock") or {}),
        "capital_retreat": dict(snapshot.get("capital_retreat") or {}),
        "sector_macro_impact": selected_sector_impact,
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
