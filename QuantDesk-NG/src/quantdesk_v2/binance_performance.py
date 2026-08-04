from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from .binance_client import (
    BinanceAccountSnapshot,
    BinanceIncomeHistory,
    BinanceIncomeRecord,
)

INCLUDED_INCOME_TYPES = ("REALIZED_PNL", "FUNDING_FEE", "COMMISSION")
_INCLUDED_INCOME_TYPE_SET = frozenset(INCLUDED_INCOME_TYPES)
HistoryStatus = Literal[
    "available",
    "history_limited",
    "history_unavailable",
    "future_month",
    "not_configured",
    "request_failed",
]


@dataclass(slots=True)
class _Bucket:
    realized_pnl: Decimal = Decimal(0)
    funding_fee: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    realized_records: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_profit: Decimal = Decimal(0)
    gross_loss_abs: Decimal = Decimal(0)
    days: dict[date, _Bucket] = field(default_factory=dict)

    @property
    def net_income(self) -> Decimal:
        return self.realized_pnl + self.funding_fee + self.commission

    def add(self, record: BinanceIncomeRecord, local_date: date, *, include_day: bool) -> None:
        if record.income_type == "REALIZED_PNL":
            self.realized_pnl += record.income
            self.realized_records += 1
            if record.income > 0:
                self.wins += 1
                self.gross_profit += record.income
            elif record.income < 0:
                self.losses += 1
                self.gross_loss_abs += abs(record.income)
            else:
                self.breakeven += 1
        elif record.income_type == "FUNDING_FEE":
            self.funding_fee += record.income
        elif record.income_type == "COMMISSION":
            self.commission += record.income

        if include_day:
            self.days.setdefault(local_date, _Bucket()).add(record, local_date, include_day=False)


def month_window_ms(month: str, timezone_offset_minutes: int) -> tuple[int, int]:
    """Return [start, end) UTC milliseconds for a calendar month at a fixed offset."""

    year, month_number = (int(part) for part in month.split("-", maxsplit=1))
    start_local_as_utc = datetime(year, month_number, 1, tzinfo=UTC)
    if month_number == 12:
        end_local_as_utc = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end_local_as_utc = datetime(year, month_number + 1, 1, tzinfo=UTC)
    offset = timedelta(minutes=timezone_offset_minutes)
    start_utc = start_local_as_utc - offset
    end_utc = end_local_as_utc - offset
    return int(start_utc.timestamp() * 1_000), int(end_utc.timestamp() * 1_000)


def history_status_for_month(
    month: str,
    timezone_offset_minutes: int,
    now: datetime,
) -> HistoryStatus:
    start_ms, end_ms = month_window_ms(month, timezone_offset_minutes)
    now_utc = _as_utc(now)
    now_ms = int(now_utc.timestamp() * 1_000)
    if start_ms > now_ms:
        return "future_month"

    cutoff_ms = int(_subtract_calendar_months(now_utc, 3).timestamp() * 1_000)
    if end_ms <= cutoff_ms:
        return "history_unavailable"
    if start_ms < cutoff_ms:
        return "history_limited"
    return "available"


def empty_binance_performance(
    *,
    month: str,
    timezone_offset_minutes: int,
    generated_at: datetime,
    configured: bool,
    connected: bool,
    history_status: HistoryStatus,
    error_category: str | None,
    account: BinanceAccountSnapshot | None = None,
) -> dict[str, Any]:
    return {
        "source": "binance_income",
        "scope": "current_user",
        "configured": configured,
        "connected": connected,
        "generated_at": _as_utc(generated_at),
        "month": month,
        "timezone_offset_minutes": timezone_offset_minutes,
        "timezone_label": _timezone_label(timezone_offset_minutes),
        "history_status": history_status,
        "history_complete": False,
        "month_complete": _month_complete(month, timezone_offset_minutes, generated_at),
        "data_as_of": None,
        "account": _account_payload(account) if account is not None else None,
        "income_basis": "realized_pnl_plus_funding_fee_plus_commission",
        "aggregation_policy": "per_asset_no_conversion",
        "included_income_types": list(INCLUDED_INCOME_TYPES),
        "excluded_income_types": [],
        "records_received": 0,
        "records_included": 0,
        "pages_fetched": 0,
        "assets": [],
        "error_category": error_category,
    }


def build_binance_performance(
    *,
    snapshot: BinanceAccountSnapshot,
    history: BinanceIncomeHistory,
    month: str,
    timezone_offset_minutes: int,
    generated_at: datetime,
) -> dict[str, Any]:
    offset = timedelta(minutes=timezone_offset_minutes)
    asset_buckets: dict[str, _Bucket] = {}
    excluded_types: set[str] = set()
    included_records = 0
    latest_time_ms: int | None = None

    for record in history.records:
        if record.income_type not in _INCLUDED_INCOME_TYPE_SET:
            excluded_types.add(record.income_type)
            continue
        included_records += 1
        latest_time_ms = max(latest_time_ms or record.time_ms, record.time_ms)
        local_date = (datetime.fromtimestamp(record.time_ms / 1_000, UTC) + offset).date()
        asset_buckets.setdefault(record.asset, _Bucket()).add(record, local_date, include_day=True)

    # Account snapshots may list every supported collateral asset with a zero
    # unrealized value. Those rows are not performance sources and would turn the
    # UI asset picker into a misleading list of empty assets.
    unrealized_by_asset = {
        asset: value for asset, value in snapshot.unrealized_pnl_by_asset if value != 0
    }
    all_assets = sorted(set(asset_buckets) | set(unrealized_by_asset))
    assets: list[dict[str, Any]] = []
    for asset in all_assets:
        bucket = asset_buckets.get(asset, _Bucket())
        assets.append(
            {
                "asset": asset,
                "net_income": _number(bucket.net_income),
                "realized_pnl": _number(bucket.realized_pnl),
                "funding_fee": _number(bucket.funding_fee),
                "commission": _number(bucket.commission),
                "current_unrealized_pnl": (
                    _number(unrealized_by_asset[asset]) if asset in unrealized_by_asset else None
                ),
                "realized_records": bucket.realized_records,
                "wins": bucket.wins,
                "losses": bucket.losses,
                "breakeven": bucket.breakeven,
                "win_rate_pct": _win_rate(bucket),
                "profit_factor": _profit_factor(bucket),
                "profit_factor_status": _profit_factor_status(bucket),
                "gross_profit": _number(bucket.gross_profit),
                "gross_loss_abs": _number(bucket.gross_loss_abs),
                "days": [
                    _day_payload(day, day_bucket) for day, day_bucket in sorted(bucket.days.items())
                ],
            }
        )

    response = empty_binance_performance(
        month=month,
        timezone_offset_minutes=timezone_offset_minutes,
        generated_at=generated_at,
        configured=True,
        connected=True,
        history_status="available",
        error_category=None,
        account=snapshot,
    )
    response.update(
        {
            "history_complete": history.complete,
            "data_as_of": (
                datetime.fromtimestamp(latest_time_ms / 1_000, UTC)
                if latest_time_ms is not None
                else None
            ),
            "excluded_income_types": sorted(excluded_types),
            "records_received": len(history.records),
            "records_included": included_records,
            "pages_fetched": history.pages_fetched,
            "assets": assets,
        }
    )
    return response


def _day_payload(day: date, bucket: _Bucket) -> dict[str, Any]:
    return {
        "date": day,
        "net_income": _number(bucket.net_income),
        "realized_pnl": _number(bucket.realized_pnl),
        "funding_fee": _number(bucket.funding_fee),
        "commission": _number(bucket.commission),
        "realized_records": bucket.realized_records,
        "wins": bucket.wins,
        "losses": bucket.losses,
        "breakeven": bucket.breakeven,
    }


def _account_payload(snapshot: BinanceAccountSnapshot) -> dict[str, Any]:
    return {
        "account_type": snapshot.account_type,
        "wallet_balance": _number(snapshot.wallet_balance),
        "available_balance": _number(snapshot.available_balance),
        "unrealized_pnl": _number(snapshot.unrealized_pnl),
        "currency": snapshot.currency,
        "updated_at": snapshot.updated_at,
    }


def _win_rate(bucket: _Bucket) -> float | None:
    decisive = bucket.wins + bucket.losses
    return round(bucket.wins * 100 / decisive, 8) if decisive else None


def _profit_factor(bucket: _Bucket) -> float | None:
    if bucket.gross_loss_abs == 0:
        return None
    return _number(bucket.gross_profit / bucket.gross_loss_abs)


def _profit_factor_status(bucket: _Bucket) -> str:
    if bucket.realized_records == 0:
        return "no_trades"
    if bucket.gross_loss_abs == 0:
        return "no_losses"
    return "available"


def _number(value: Decimal) -> float:
    return float(value)


def _month_complete(month: str, timezone_offset_minutes: int, now: datetime) -> bool:
    _, end_ms = month_window_ms(month, timezone_offset_minutes)
    return end_ms <= int(_as_utc(now).timestamp() * 1_000)


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    absolute_month = value.year * 12 + (value.month - 1) - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=UTC)
    last_day = (next_month - timedelta(days=1)).day
    return value.replace(year=year, month=month, day=min(value.day, last_day))


def _timezone_label(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(offset_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
