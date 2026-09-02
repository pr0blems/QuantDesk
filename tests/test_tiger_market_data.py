from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantdesk_v2.models import SecuritySymbolMapping
from quantdesk_v2.tiger_market_data import (
    TigerBar,
    TigerBarClient,
    TigerTradingCalendarClient,
    closed_tiger_bars,
    evaluate_bar_quality,
    resolve_research_contract_market_link,
    resolve_verified_contract_market_link,
)


class _QuoteApi:
    def __init__(self) -> None:
        self.bar_kwargs: dict[str, object] = {}

    def get_bars_by_page(self, **kwargs: object):
        self.bar_kwargs = kwargs
        return [
            {
                "symbol": "AMD",
                "time": 1_788_321_600_000,
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1_000,
                "amount": 100_500,
            },
            # Invalid and foreign rows are ignored at the source boundary.
            {
                "symbol": "AMD",
                "time": 1_788_322_500_000,
                "open": 0,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1_000,
            },
            {
                "symbol": "AAPL",
                "time": 1_788_322_500_000,
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1_000,
            },
        ]

    def get_trading_calendar(
        self, market: str, begin_date: str | None = None, end_date: str | None = None
    ):
        assert market == "US"
        assert begin_date == "2026-09-01"
        assert end_date == "2026-09-03"
        return [
            {"date": "2026-09-02", "type": "TRADING"},
            {"date": "2026-09-01", "type": "TRADING"},
        ]


class _MappingSession:
    def __init__(self, binance: SecuritySymbolMapping, tiger: SecuritySymbolMapping) -> None:
        self.rows = iter((binance, tiger))

    def scalar(self, _statement):
        return next(self.rows)


def _mapping_pair() -> tuple[SecuritySymbolMapping, SecuritySymbolMapping]:
    binance = SecuritySymbolMapping(
        id=1,
        security_id=10,
        source="binance_tradfi",
        source_symbol="AAPLUSDT",
        normalized_symbol="AAPL",
        mapping_status="AUTO",
        source_status="TRADING",
        strategy_enabled=True,
    )
    tiger = SecuritySymbolMapping(
        id=2,
        security_id=10,
        source="tiger_openapi",
        source_symbol="AAPL",
        normalized_symbol="AAPL",
        mapping_status="VERIFIED",
        source_status="ACTIVE",
        strategy_enabled=True,
    )
    return binance, tiger


def test_research_mapping_accepts_auto_binance_without_weakening_live_resolver() -> None:
    binance, tiger = _mapping_pair()
    strict = resolve_verified_contract_market_link(
        _MappingSession(binance, tiger),  # type: ignore[arg-type]
        contract_symbol="AAPLUSDT",
    )
    binance, tiger = _mapping_pair()
    research = resolve_research_contract_market_link(
        _MappingSession(binance, tiger),  # type: ignore[arg-type]
        contract_symbol="AAPLUSDT",
    )

    assert strict is None
    assert research is not None
    assert research.underlying_symbol == "AAPL"
    assert research.contract_symbol == "AAPLUSDT"


def test_tiger_bar_client_uses_official_source_semantics() -> None:
    api = _QuoteApi()
    client = TigerBarClient(api)
    bars = client.bars(
        "amd",
        timeframe="15m",
        begin_at=datetime(2026, 8, 1, tzinfo=UTC),
        end_at=datetime(2026, 9, 2, tzinfo=UTC),
        trade_session="regular",
        adjustment="none",
    )

    assert len(bars) == 1
    assert bars[0].symbol == "AMD"
    assert bars[0].timeframe == "15m"
    assert bars[0].close_time - bars[0].open_time == 15 * 60 * 1000
    assert bars[0].valid_ohlc is True
    assert api.bar_kwargs["period"] == "15min"
    assert api.bar_kwargs["right"] == "nr"
    assert api.bar_kwargs["trade_session"] == "Regular"


def test_tiger_calendar_is_sorted_and_source_driven() -> None:
    days = TigerTradingCalendarClient(_QuoteApi()).days(date(2026, 9, 1), date(2026, 9, 3))

    assert [item.trading_date for item in days] == [date(2026, 9, 1), date(2026, 9, 2)]


def _bar(open_time: int, *, close: str = "101") -> TigerBar:
    return TigerBar(
        symbol="AMD",
        timeframe="15m",
        trade_session="regular",
        adjustment="none",
        open_time=open_time,
        close_time=open_time + 900_000,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("1000"),
        amount=Decimal("100500"),
        received_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def test_quality_report_blocks_incomplete_duplicate_or_stale_streams() -> None:
    now = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    newest_open = int((now - timedelta(minutes=30)).timestamp() * 1000)
    first = _bar(newest_open - 900_000)
    duplicate = _bar(first.open_time)
    newest = _bar(newest_open)

    report = evaluate_bar_quality(
        [first, duplicate, newest],
        symbol="AMD",
        timeframe="15m",
        trade_session="regular",
        adjustment="none",
        expected_bars=3,
        maximum_age_seconds=60,
        now=now,
    )

    assert report.actual_bars == 2
    assert report.duplicate_count == 1
    assert report.gap_count == 1
    assert report.status == "blocked"
    assert report.reason_codes == (
        "bar_coverage_incomplete",
        "duplicate_bars",
        "newest_bar_stale",
    )


def test_quality_report_accepts_complete_fresh_closed_bars() -> None:
    now = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    last_open = int((now - timedelta(minutes=15)).timestamp() * 1000)
    bars = [_bar(last_open - index * 900_000) for index in reversed(range(3))]

    report = evaluate_bar_quality(
        bars,
        symbol="AMD",
        timeframe="15m",
        trade_session="regular",
        adjustment="none",
        expected_bars=3,
        maximum_age_seconds=1,
        now=now,
    )

    assert report.status == "usable"
    assert report.completeness_ratio == Decimal("1.000000")
    assert report.reason_codes == ()


def test_client_rejects_unsupported_adjustment_instead_of_silent_fallback() -> None:
    with pytest.raises(ValueError, match="adjustment"):
        TigerBarClient(_QuoteApi()).bars(
            "AMD",
            timeframe="15m",
            begin_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2026, 9, 2, tzinfo=UTC),
            trade_session="regular",
            adjustment="backward",
        )


def test_only_fully_closed_tiger_bars_can_enter_strategy_storage() -> None:
    now = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    closed = _bar(int((now - timedelta(minutes=15)).timestamp() * 1000))
    forming = _bar(int(now.timestamp() * 1000))

    selected = closed_tiger_bars((closed, forming), cutoff=now)

    assert selected == (closed,)
