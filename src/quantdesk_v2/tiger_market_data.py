"""Official Tiger historical market data and persistence services.

Tiger bars are stored as US cash-equity reference data.  They are never written
to the Binance ``klines`` table and never silently replaced with Binance bars.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session, aliased

from .models import (
    ReferenceMarketBar,
    ReferenceMarketDataQuality,
    Security,
    SecuritySymbolMapping,
    utcnow,
)

TIGER_SOURCE = "tiger_openapi"
SUPPORTED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "1d"})
SUPPORTED_SESSIONS = frozenset({"pre_market", "regular", "after_hours", "overnight"})
SUPPORTED_ADJUSTMENTS = frozenset({"none", "forward"})
VERIFIED_MAPPING_STATES = frozenset({"VERIFIED", "MANUAL"})
RESEARCH_MAPPING_STATES = VERIFIED_MAPPING_STATES | {"AUTO"}
RESEARCH_SECURITY_STATES = frozenset({"AUTO_VERIFIED", "VERIFIED"})


class TigerMarketDataError(RuntimeError):
    """Redacted upstream/data failure safe for logs and API output."""

    def __init__(self, category: str):
        super().__init__("Tiger market data request failed")
        self.category = category


class TigerQuoteApi(Protocol):
    def get_bars_by_page(self, **kwargs: Any) -> Any: ...

    def get_trading_calendar(
        self, market: Any, begin_date: str | None = None, end_date: str | None = None
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class TigerBar:
    symbol: str
    timeframe: str
    trade_session: str
    adjustment: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal | None
    received_at: datetime
    source_version: str = "tigeropen_v3"

    @property
    def valid_ohlc(self) -> bool:
        return (
            self.open > 0
            and self.high > 0
            and self.low > 0
            and self.close > 0
            and self.volume >= 0
            and self.low <= min(self.open, self.close)
            and self.high >= max(self.open, self.close)
            and self.low <= self.high
        )


@dataclass(frozen=True, slots=True)
class TigerTradingDay:
    trading_date: date
    day_type: str


@dataclass(frozen=True, slots=True)
class BarQualityReport:
    source: str
    symbol: str
    timeframe: str
    trade_session: str
    adjustment: str
    expected_bars: int
    actual_bars: int
    gap_count: int
    duplicate_count: int
    invalid_ohlc_count: int
    newest_closed_time: int | None
    age_seconds: int | None
    completeness_ratio: Decimal
    status: str
    reason_codes: tuple[str, ...]
    evaluated_at: datetime

    def audit_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["completeness_ratio"] = str(self.completeness_ratio)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        return payload


@dataclass(frozen=True, slots=True)
class VerifiedMarketLink:
    security_id: int
    underlying_symbol: str
    contract_symbol: str
    tiger_mapping_id: int
    binance_mapping_id: int


def _normalized_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp_ms(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized <= 0:
        return None
    if normalized < 10_000_000_000:
        normalized *= 1000
    return normalized


def _decimal(value: object, *, allow_zero: bool = False) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not normalized.is_finite():
        return None
    if normalized < 0 or (normalized == 0 and not allow_zero):
        return None
    return normalized


def _records(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict(orient="records")
        return [item for item in converted if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    raise TigerMarketDataError("invalid_response")


def _timeframe_delta(timeframe: str) -> timedelta:
    values = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
    }
    try:
        return values[timeframe]
    except KeyError as exc:
        raise ValueError("unsupported Tiger timeframe") from exc


def closed_tiger_bars(
    bars: Sequence[TigerBar], *, cutoff: datetime
) -> tuple[TigerBar, ...]:
    """Return only bars whose complete interval ended by ``cutoff``.

    Tiger can include the currently forming bar in a historical response.  A
    forming OHLC value is not deterministic and must never enter the strategy
    data set, otherwise a restart can evaluate a different box for the same
    timestamp.
    """

    cutoff_ms = int(_normalized_datetime(cutoff).timestamp() * 1000)
    return tuple(item for item in bars if item.close_time <= cutoff_ms)


class TigerBarClient:
    """Thin, deterministic adapter around Tiger's official ``QuoteClient``."""

    _periods = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "60min",
        "1d": "day",
    }
    _rights = {"none": "nr", "forward": "br"}
    _sessions = {
        "pre_market": "PreMarket",
        "regular": "Regular",
        "after_hours": "AfterHours",
        "overnight": "OverNight",
    }

    def __init__(self, quote_api: TigerQuoteApi, *, source_version: str = "tigeropen_v3"):
        self.quote_api = quote_api
        self.source_version = source_version

    def bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        begin_at: datetime,
        end_at: datetime,
        trade_session: str = "regular",
        adjustment: str = "none",
        total: int = 10_000,
        page_size: int = 1_000,
    ) -> tuple[TigerBar, ...]:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol or len(normalized_symbol) > 32:
            raise ValueError("invalid Tiger symbol")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported Tiger timeframe")
        if trade_session not in SUPPORTED_SESSIONS:
            raise ValueError("unsupported Tiger trade session")
        if adjustment not in SUPPORTED_ADJUSTMENTS:
            raise ValueError("unsupported Tiger adjustment")
        start = _normalized_datetime(begin_at)
        end = _normalized_datetime(end_at)
        if end <= start:
            raise ValueError("end_at must be after begin_at")
        if not 1 <= total <= 100_000:
            raise ValueError("total must be between 1 and 100000")
        if not 1 <= page_size <= 1_200:
            raise ValueError("page_size must be between 1 and 1200")

        try:
            payload = self.quote_api.get_bars_by_page(
                symbol=normalized_symbol,
                period=self._periods[timeframe],
                begin_time=int(start.timestamp() * 1000),
                end_time=int(end.timestamp() * 1000),
                total=total,
                page_size=page_size,
                right=self._rights[adjustment],
                time_interval=1,
                trade_session=self._sessions[trade_session],
            )
        except TigerMarketDataError:
            raise
        except Exception as exc:
            raise TigerMarketDataError("upstream") from exc

        received_at = datetime.now(UTC)
        delta_ms = int(_timeframe_delta(timeframe).total_seconds() * 1000)
        parsed: list[TigerBar] = []
        for item in _records(payload):
            row_symbol = str(item.get("symbol") or normalized_symbol).strip().upper()
            open_time = _timestamp_ms(item.get("time"))
            open_price = _decimal(item.get("open"))
            high = _decimal(item.get("high"))
            low = _decimal(item.get("low"))
            close = _decimal(item.get("close"))
            volume = _decimal(item.get("volume"), allow_zero=True)
            amount = _decimal(item.get("amount"), allow_zero=True)
            if (
                row_symbol != normalized_symbol
                or open_time is None
                or open_price is None
                or high is None
                or low is None
                or close is None
                or volume is None
            ):
                continue
            parsed.append(
                TigerBar(
                    symbol=normalized_symbol,
                    timeframe=timeframe,
                    trade_session=trade_session,
                    adjustment=adjustment,
                    open_time=open_time,
                    close_time=open_time + delta_ms,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    received_at=received_at,
                    source_version=self.source_version,
                )
            )
        parsed.sort(key=lambda item: item.open_time)
        return tuple(parsed)


class TigerTradingCalendarClient:
    def __init__(self, quote_api: TigerQuoteApi):
        self.quote_api = quote_api

    def days(self, begin: date, end: date) -> tuple[TigerTradingDay, ...]:
        if end < begin:
            raise ValueError("calendar end must not precede begin")
        try:
            payload = self.quote_api.get_trading_calendar("US", begin.isoformat(), end.isoformat())
        except Exception as exc:
            raise TigerMarketDataError("upstream") from exc
        parsed: list[TigerTradingDay] = []
        for item in _records(payload):
            try:
                trading_date = date.fromisoformat(str(item.get("date") or ""))
            except ValueError:
                continue
            day_type = str(item.get("type") or "TRADING").strip().upper()
            parsed.append(TigerTradingDay(trading_date=trading_date, day_type=day_type))
        return tuple(sorted(parsed, key=lambda item: item.trading_date))


def build_tiger_quote_api(
    *, tiger_id: str, account: str, private_key_path: Path, sandbox: bool = False
) -> TigerQuoteApi:
    """Construct the official SDK client without exposing its key material."""

    try:
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.quote.quote_client import QuoteClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise TigerMarketDataError("sdk_not_installed") from exc

    path = private_key_path.expanduser().resolve(strict=True)
    try:
        config = TigerOpenClientConfig(sandbox_debug=bool(sandbox))
        config.private_key = read_private_key(str(path))
        config.tiger_id = tiger_id.strip()
        config.account = account.strip()
        return QuoteClient(config, is_grab_permission=True)
    except Exception as exc:
        raise TigerMarketDataError("initialization") from exc


def evaluate_bar_quality(
    bars: Sequence[TigerBar],
    *,
    symbol: str,
    timeframe: str,
    trade_session: str,
    adjustment: str,
    expected_bars: int,
    maximum_age_seconds: int,
    now: datetime | None = None,
) -> BarQualityReport:
    if expected_bars < 1:
        raise ValueError("expected_bars must be positive")
    if maximum_age_seconds < 0:
        raise ValueError("maximum_age_seconds must not be negative")
    evaluated_at = _normalized_datetime(now or datetime.now(UTC))
    selected = [
        item
        for item in bars
        if item.symbol == symbol
        and item.timeframe == timeframe
        and item.trade_session == trade_session
        and item.adjustment == adjustment
        and item.close_time <= int(evaluated_at.timestamp() * 1000)
    ]
    unique_times = {item.open_time for item in selected}
    duplicate_count = max(0, len(selected) - len(unique_times))
    invalid_count = sum(not item.valid_ohlc for item in selected)
    actual = len(unique_times)
    gap_count = max(0, expected_bars - actual)
    newest = max((item.close_time for item in selected), default=None)
    age_seconds = (
        max(0, int(evaluated_at.timestamp() - newest / 1000)) if newest is not None else None
    )
    completeness = min(Decimal("1"), Decimal(actual) / Decimal(expected_bars)).quantize(
        Decimal("0.000001")
    )
    reasons: list[str] = []
    if actual < expected_bars:
        reasons.append("bar_coverage_incomplete")
    if duplicate_count:
        reasons.append("duplicate_bars")
    if invalid_count:
        reasons.append("invalid_ohlc")
    if newest is None:
        reasons.append("no_closed_bars")
    elif age_seconds is not None and age_seconds > maximum_age_seconds:
        reasons.append("newest_bar_stale")
    return BarQualityReport(
        source=TIGER_SOURCE,
        symbol=symbol,
        timeframe=timeframe,
        trade_session=trade_session,
        adjustment=adjustment,
        expected_bars=expected_bars,
        actual_bars=actual,
        gap_count=gap_count,
        duplicate_count=duplicate_count,
        invalid_ohlc_count=invalid_count,
        newest_closed_time=newest,
        age_seconds=age_seconds,
        completeness_ratio=completeness,
        status="usable" if not reasons else "blocked",
        reason_codes=tuple(reasons),
        evaluated_at=evaluated_at,
    )


class TigerMarketDataRepository:
    def __init__(self, db: Session):
        self.db = db

    def upsert_bars(self, bars: Sequence[TigerBar], *, security_id: int) -> int:
        if not bars:
            return 0
        rows = [
            {
                "source": TIGER_SOURCE,
                "asset_class": "US_EQUITY",
                "security_id": security_id,
                "symbol": item.symbol,
                "timeframe": item.timeframe,
                "trade_session": item.trade_session,
                "adjustment": item.adjustment,
                "open_time": item.open_time,
                "close_time": item.close_time,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
                "amount": item.amount,
                "received_at": item.received_at.replace(tzinfo=None),
                "source_version": item.source_version,
            }
            for item in bars
        ]
        statement = mysql_insert(ReferenceMarketBar).values(rows)
        statement = statement.on_duplicate_key_update(
            security_id=statement.inserted.security_id,
            close_time=statement.inserted.close_time,
            open=statement.inserted.open,
            high=statement.inserted.high,
            low=statement.inserted.low,
            close=statement.inserted.close,
            volume=statement.inserted.volume,
            amount=statement.inserted.amount,
            received_at=statement.inserted.received_at,
            source_version=statement.inserted.source_version,
        )
        return int(self.db.execute(statement).rowcount or 0)

    def load_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        trade_session: str,
        adjustment: str,
        begin_time: int,
        end_time: int,
    ) -> tuple[TigerBar, ...]:
        if end_time <= begin_time:
            raise ValueError("end_time must be greater than begin_time")
        rows = self.db.scalars(
            select(ReferenceMarketBar)
            .where(
                ReferenceMarketBar.source == TIGER_SOURCE,
                ReferenceMarketBar.symbol == symbol.strip().upper(),
                ReferenceMarketBar.timeframe == timeframe,
                ReferenceMarketBar.trade_session == trade_session,
                ReferenceMarketBar.adjustment == adjustment,
                ReferenceMarketBar.open_time >= begin_time,
                ReferenceMarketBar.open_time < end_time,
                ReferenceMarketBar.close_time <= end_time,
            )
            .order_by(ReferenceMarketBar.open_time)
        ).all()
        return tuple(
            TigerBar(
                symbol=row.symbol,
                timeframe=row.timeframe,
                trade_session=row.trade_session,
                adjustment=row.adjustment,
                open_time=row.open_time,
                close_time=row.close_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                amount=row.amount,
                received_at=(
                    row.received_at.replace(tzinfo=UTC)
                    if row.received_at.tzinfo is None
                    else row.received_at.astimezone(UTC)
                ),
                source_version=row.source_version,
            )
            for row in rows
        )

    def load_latest_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        trade_session: str,
        adjustment: str,
        end_time: int,
        limit: int,
    ) -> tuple[TigerBar, ...]:
        """Load a bounded closed-bar tail in chronological order."""

        if limit < 1 or limit > 10_000:
            raise ValueError("latest bar limit must be between 1 and 10000")
        rows = self.db.scalars(
            select(ReferenceMarketBar)
            .where(
                ReferenceMarketBar.source == TIGER_SOURCE,
                ReferenceMarketBar.symbol == symbol.strip().upper(),
                ReferenceMarketBar.timeframe == timeframe,
                ReferenceMarketBar.trade_session == trade_session,
                ReferenceMarketBar.adjustment == adjustment,
                ReferenceMarketBar.close_time <= end_time,
            )
            .order_by(ReferenceMarketBar.open_time.desc())
            .limit(limit)
        ).all()
        rows.reverse()
        return tuple(
            TigerBar(
                symbol=row.symbol,
                timeframe=row.timeframe,
                trade_session=row.trade_session,
                adjustment=row.adjustment,
                open_time=row.open_time,
                close_time=row.close_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                amount=row.amount,
                received_at=(
                    row.received_at.replace(tzinfo=UTC)
                    if row.received_at.tzinfo is None
                    else row.received_at.astimezone(UTC)
                ),
                source_version=row.source_version,
            )
            for row in rows
        )

    def latest_open_time(
        self,
        *,
        symbol: str,
        timeframe: str,
        trade_session: str,
        adjustment: str,
    ) -> int | None:
        value = self.db.scalar(
            select(func.max(ReferenceMarketBar.open_time)).where(
                ReferenceMarketBar.source == TIGER_SOURCE,
                ReferenceMarketBar.symbol == symbol.strip().upper(),
                ReferenceMarketBar.timeframe == timeframe,
                ReferenceMarketBar.trade_session == trade_session,
                ReferenceMarketBar.adjustment == adjustment,
            )
        )
        return int(value) if value is not None else None

    def save_quality(self, report: BarQualityReport) -> ReferenceMarketDataQuality:
        row = self.db.scalar(
            select(ReferenceMarketDataQuality).where(
                ReferenceMarketDataQuality.source == report.source,
                ReferenceMarketDataQuality.symbol == report.symbol,
                ReferenceMarketDataQuality.timeframe == report.timeframe,
                ReferenceMarketDataQuality.trade_session == report.trade_session,
                ReferenceMarketDataQuality.adjustment == report.adjustment,
            )
        )
        if row is None:
            row = ReferenceMarketDataQuality(
                source=report.source,
                symbol=report.symbol,
                timeframe=report.timeframe,
                trade_session=report.trade_session,
                adjustment=report.adjustment,
            )
            self.db.add(row)
        row.expected_bars = report.expected_bars
        row.actual_bars = report.actual_bars
        row.gap_count = report.gap_count
        row.duplicate_count = report.duplicate_count
        row.invalid_ohlc_count = report.invalid_ohlc_count
        row.newest_closed_time = report.newest_closed_time
        row.age_seconds = report.age_seconds
        row.completeness_ratio = report.completeness_ratio
        row.status = report.status
        row.reason_codes_json = list(report.reason_codes)
        row.evaluated_at = report.evaluated_at.replace(tzinfo=None)
        self.db.flush()
        return row

    def dataset_hash(
        self,
        *,
        symbol: str,
        timeframe: str,
        trade_session: str,
        adjustment: str,
        begin_time: int,
        end_time: int,
    ) -> str:
        rows = self.db.scalars(
            select(ReferenceMarketBar)
            .where(
                ReferenceMarketBar.source == TIGER_SOURCE,
                ReferenceMarketBar.symbol == symbol,
                ReferenceMarketBar.timeframe == timeframe,
                ReferenceMarketBar.trade_session == trade_session,
                ReferenceMarketBar.adjustment == adjustment,
                ReferenceMarketBar.open_time >= begin_time,
                ReferenceMarketBar.open_time < end_time,
            )
            .order_by(ReferenceMarketBar.open_time)
        ).all()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                json.dumps(
                    [
                        row.open_time,
                        str(row.open),
                        str(row.high),
                        str(row.low),
                        str(row.close),
                        str(row.volume),
                        str(row.amount) if row.amount is not None else None,
                    ],
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TigerBackfillResult:
    security_id: int
    symbol: str
    timeframe: str
    trade_session: str
    adjustment: str
    fetched_bars: int
    stored_rows: int
    quality: BarQualityReport


class TigerBarBackfillService:
    """Fetch, validate and persist one source-qualified Tiger bar stream."""

    def __init__(
        self,
        client: TigerBarClient,
        repository: TigerMarketDataRepository,
    ) -> None:
        self.client = client
        self.repository = repository

    def backfill(
        self,
        *,
        security_id: int,
        symbol: str,
        timeframe: str,
        begin_at: datetime,
        end_at: datetime,
        trade_session: str,
        adjustment: str,
        expected_bars: int | None,
        maximum_age_seconds: int,
        total: int = 10_000,
    ) -> TigerBackfillResult:
        requested = self.client.bars(
            symbol,
            timeframe=timeframe,
            begin_at=begin_at,
            end_at=end_at,
            trade_session=trade_session,
            adjustment=adjustment,
            total=total,
        )
        bars = closed_tiger_bars(
            requested,
            cutoff=min(_normalized_datetime(end_at), datetime.now(UTC)),
        )
        if not bars:
            raise ValueError("Tiger 未返回可用于回测的已收盘 K 线")
        stored = self.repository.upsert_bars(bars, security_id=security_id)
        report = evaluate_bar_quality(
            bars,
            symbol=symbol.strip().upper(),
            timeframe=timeframe,
            trade_session=trade_session,
            adjustment=adjustment,
            expected_bars=expected_bars if expected_bars is not None else len(bars),
            maximum_age_seconds=maximum_age_seconds,
        )
        self.repository.save_quality(report)
        return TigerBackfillResult(
            security_id=security_id,
            symbol=symbol.strip().upper(),
            timeframe=timeframe,
            trade_session=trade_session,
            adjustment=adjustment,
            fetched_bars=len(bars),
            stored_rows=stored,
            quality=report,
        )


def ensure_tiger_security_mapping(
    db: Session, security: Security, *, now: datetime | None = None
) -> SecuritySymbolMapping:
    """Attach Tiger's cash symbol to the existing security-master identity."""

    observed_at = (now or utcnow()).replace(tzinfo=None)
    symbol = security.symbol.strip().upper()
    row = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.source == TIGER_SOURCE,
            SecuritySymbolMapping.source_symbol == symbol,
        )
    )
    research_eligible = (
        security.exchange == "US"
        and security.is_active
        and security.security_type in {"COMMON_STOCK", "ETF"}
        and security.verification_status in RESEARCH_SECURITY_STATES
    )
    strictly_verified = research_eligible and security.verification_status == "VERIFIED"
    mapping_status = (
        "VERIFIED"
        if strictly_verified
        else "AUTO"
        if research_eligible
        else "REVIEW_REQUIRED"
    )
    if row is None:
        row = SecuritySymbolMapping(
            security_id=security.id,
            source=TIGER_SOURCE,
            source_symbol=symbol,
            normalized_symbol=symbol,
            mapping_status=mapping_status,
            mapping_method="security_master_symbol",
            source_status="ACTIVE",
            underlying_type="US_EQUITY",
            monitor_enabled=True,
            strategy_enabled=research_eligible,
            live_trading_enabled=False,
            source_metadata_json={"provider": "Tiger Open API"},
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(row)
    else:
        row.security_id = security.id
        row.normalized_symbol = symbol
        if row.mapping_status != "MANUAL":
            row.mapping_status = mapping_status
        row.source_status = "ACTIVE"
        row.monitor_enabled = True
        row.strategy_enabled = research_eligible
        row.last_seen_at = observed_at
        row.updated_at = observed_at
    db.flush()
    return row


def resolve_verified_market_link(
    db: Session, *, underlying_symbol: str, contract_symbol: str
) -> VerifiedMarketLink | None:
    underlying = underlying_symbol.strip().upper()
    contract = contract_symbol.strip().upper()
    tiger = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.source == TIGER_SOURCE,
            SecuritySymbolMapping.source_symbol == underlying,
        )
    )
    binance = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.source == "binance_tradfi",
            SecuritySymbolMapping.source_symbol == contract,
        )
    )
    if (
        tiger is None
        or binance is None
        or tiger.security_id != binance.security_id
        or tiger.mapping_status not in VERIFIED_MAPPING_STATES
        or binance.mapping_status not in VERIFIED_MAPPING_STATES
        or tiger.source_status != "ACTIVE"
        or binance.source_status != "TRADING"
        or not tiger.strategy_enabled
        or not binance.strategy_enabled
    ):
        return None
    return VerifiedMarketLink(
        security_id=int(tiger.security_id),
        underlying_symbol=underlying,
        contract_symbol=contract,
        tiger_mapping_id=int(tiger.id),
        binance_mapping_id=int(binance.id),
    )


def resolve_verified_contract_market_link(
    db: Session,
    *,
    contract_symbol: str,
) -> VerifiedMarketLink | None:
    """Resolve one verified Binance contract to its Tiger cash symbol."""

    contract = contract_symbol.strip().upper()
    binance = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.source == "binance_tradfi",
            SecuritySymbolMapping.source_symbol == contract,
        )
    )
    if binance is None:
        return None
    tiger = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.security_id == binance.security_id,
            SecuritySymbolMapping.source == TIGER_SOURCE,
        )
    )
    if (
        tiger is None
        or tiger.mapping_status not in VERIFIED_MAPPING_STATES
        or binance.mapping_status not in VERIFIED_MAPPING_STATES
        or tiger.source_status != "ACTIVE"
        or binance.source_status != "TRADING"
        or not tiger.strategy_enabled
        or not binance.strategy_enabled
    ):
        return None
    return VerifiedMarketLink(
        security_id=int(binance.security_id),
        underlying_symbol=tiger.source_symbol.strip().upper(),
        contract_symbol=contract,
        tiger_mapping_id=int(tiger.id),
        binance_mapping_id=int(binance.id),
    )


def resolve_research_contract_market_link(
    db: Session,
    *,
    contract_symbol: str,
) -> VerifiedMarketLink | None:
    """Resolve a research-only Tiger/Binance link without weakening live gates.

    The security-master synchronizer can establish both sides of the mapping
    deterministically.  That ``AUTO`` state is sufficient for historical
    research because Tiger still validates the symbol while fetching bars, but
    it must never be used by the strict live resolver above.
    """

    contract = contract_symbol.strip().upper()
    binance = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.source == "binance_tradfi",
            SecuritySymbolMapping.source_symbol == contract,
        )
    )
    if binance is None:
        return None
    tiger = db.scalar(
        select(SecuritySymbolMapping).where(
            SecuritySymbolMapping.security_id == binance.security_id,
            SecuritySymbolMapping.source == TIGER_SOURCE,
        )
    )
    if (
        tiger is None
        or tiger.mapping_status not in RESEARCH_MAPPING_STATES
        or binance.mapping_status not in RESEARCH_MAPPING_STATES
        or tiger.source_status != "ACTIVE"
        or binance.source_status != "TRADING"
        or not tiger.strategy_enabled
        or not binance.strategy_enabled
    ):
        return None
    return VerifiedMarketLink(
        security_id=int(binance.security_id),
        underlying_symbol=tiger.source_symbol.strip().upper(),
        contract_symbol=contract,
        tiger_mapping_id=int(tiger.id),
        binance_mapping_id=int(binance.id),
    )


def list_research_contract_market_links(db: Session) -> tuple[VerifiedMarketLink, ...]:
    """List deterministic contract choices accepted by the research adapter."""

    binance = aliased(SecuritySymbolMapping)
    tiger = aliased(SecuritySymbolMapping)
    rows = db.execute(
        select(binance, tiger)
        .join(
            tiger,
            (tiger.security_id == binance.security_id)
            & (tiger.source == TIGER_SOURCE),
        )
        .where(
            binance.source == "binance_tradfi",
            binance.mapping_status.in_(RESEARCH_MAPPING_STATES),
            binance.source_status == "TRADING",
            binance.strategy_enabled.is_(True),
            tiger.mapping_status.in_(RESEARCH_MAPPING_STATES),
            tiger.source_status == "ACTIVE",
            tiger.strategy_enabled.is_(True),
        )
        .order_by(binance.source_symbol)
    ).all()
    return tuple(
        VerifiedMarketLink(
            security_id=int(binance_row.security_id),
            underlying_symbol=tiger_row.source_symbol.strip().upper(),
            contract_symbol=binance_row.source_symbol.strip().upper(),
            tiger_mapping_id=int(tiger_row.id),
            binance_mapping_id=int(binance_row.id),
        )
        for binance_row, tiger_row in rows
    )


def finite_float(value: Decimal) -> float:
    """Small boundary helper for SDK/report integrations."""

    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("value must be finite")
    return normalized
