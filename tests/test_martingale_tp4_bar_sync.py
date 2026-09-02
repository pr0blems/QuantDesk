from __future__ import annotations

from datetime import UTC, datetime, timedelta

from quantdesk_v2 import martingale_tp4_bar_sync as bar_sync
from quantdesk_v2.martingale_tp4_bar_sync import (
    DAILY_MINIMUM_BARS,
    MAX_STREAMS_PER_CYCLE,
    SIGNAL_WARMUP_BARS,
    MartingaleTigerBarSync,
    TigerBarDiscovery,
    TigerBarStreamRequirement,
    TigerBarStreamSyncResult,
    incremental_begin_at,
    requirements_for_link,
    stream_poll_seconds,
)
from quantdesk_v2.strategy_catalog import ENGINE_PARAMETER_SCHEMAS
from quantdesk_v2.tiger_market_data import VerifiedMarketLink

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


def _parameters(**overrides: int | float) -> dict[str, int | float]:
    values = {
        item["key"]: item["default"]
        for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
    }
    values.update(overrides)
    return values


def _link() -> VerifiedMarketLink:
    return VerifiedMarketLink(
        security_id=7,
        underlying_symbol="AMD",
        contract_symbol="AMDUSDT",
        tiger_mapping_id=8,
        binance_mapping_id=9,
    )


def test_stream_requirements_cover_full_box_search_and_daily_atr() -> None:
    requirements = requirements_for_link(
        {
            "engine_key": "martingale_tp4",
            "parameters": _parameters(
                BoxTimeFrameMinutes=5,
                BoxLength=200,
                AutoBoxRange=1,
                AutoBoxRangeDailyATRperiod=90,
            ),
        },
        _link(),
    )

    assert [(item.timeframe, item.expected_bars) for item in requirements] == [
        ("5m", SIGNAL_WARMUP_BARS),
        ("1d", 92),
    ]
    assert all(item.trade_session == "regular" for item in requirements)
    assert all(item.adjustment == "none" for item in requirements)


def test_fixed_box_does_not_request_unused_daily_stream() -> None:
    requirements = requirements_for_link(
        {
            "engine_key": "martingale_tp4",
            "parameters": _parameters(AutoBoxRange=0),
        },
        _link(),
    )

    assert len(requirements) == 1
    assert requirements[0].timeframe == "15m"
    assert requirements[0].expected_bars == SIGNAL_WARMUP_BARS


def test_incremental_window_overlaps_last_persisted_bar() -> None:
    requirement = TigerBarStreamRequirement(
        security_id=7,
        symbol="AMD",
        timeframe="15m",
        trade_session="regular",
        adjustment="none",
        expected_bars=SIGNAL_WARMUP_BARS,
    )
    latest = int((NOW - timedelta(minutes=15)).timestamp() * 1000)

    assert incremental_begin_at(
        requirement,
        latest_open_time=latest,
        now=NOW,
    ) == NOW - timedelta(minutes=30)


def test_initial_window_has_calendar_slack_and_polling_is_bounded() -> None:
    intraday = TigerBarStreamRequirement(
        security_id=7,
        symbol="AMD",
        timeframe="1m",
        trade_session="regular",
        adjustment="none",
        expected_bars=SIGNAL_WARMUP_BARS,
    )
    daily = TigerBarStreamRequirement(
        security_id=7,
        symbol="AMD",
        timeframe="1d",
        trade_session="regular",
        adjustment="none",
        expected_bars=DAILY_MINIMUM_BARS,
    )

    assert incremental_begin_at(intraday, latest_open_time=None, now=NOW) == (
        NOW - timedelta(minutes=10_000)
    )
    assert incremental_begin_at(daily, latest_open_time=None, now=NOW) == (
        NOW - timedelta(days=240)
    )
    assert stream_poll_seconds("1m") == 30
    assert stream_poll_seconds("15m") == 300
    assert stream_poll_seconds("1d") == 3_600


def test_sync_cycle_is_rate_limited_and_reports_blocked_quality(monkeypatch) -> None:
    requirements = tuple(
        TigerBarStreamRequirement(
            security_id=index,
            symbol=f"S{index}",
            timeframe="15m",
            trade_session="regular",
            adjustment="none",
            expected_bars=SIGNAL_WARMUP_BARS,
        )
        for index in range(MAX_STREAMS_PER_CYCLE + 2)
    )

    class _Session:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    saved: list[dict[str, object]] = []
    calls: list[str] = []
    monkeypatch.setattr(bar_sync, "Session", _Session)
    monkeypatch.setattr(
        bar_sync,
        "discover_running_shadow_streams",
        lambda _db: TigerBarDiscovery(requirements, 1, 0, 0),
    )
    monkeypatch.setattr(
        bar_sync,
        "_save_sync_summary",
        lambda _engine, summary: saved.append(summary),
    )
    service = MartingaleTigerBarSync(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )

    def sync_stream(requirement, *, now):
        del now
        calls.append(requirement.symbol)
        blocked = requirement.symbol == "S0"
        return TigerBarStreamSyncResult(
            symbol=requirement.symbol,
            timeframe=requirement.timeframe,
            fetched_bars=1,
            closed_bars=1,
            stored_rows=1,
            quality_status="blocked" if blocked else "usable",
            reason_codes=("bar_coverage_incomplete",) if blocked else (),
        )

    monkeypatch.setattr(service, "sync_stream", sync_stream)

    summary = service.sync_once()

    assert len(calls) == MAX_STREAMS_PER_CYCLE
    assert summary["attempted_count"] == MAX_STREAMS_PER_CYCLE
    assert summary["deferred_count"] == 2
    assert summary["blocked_quality_count"] == 1
    assert summary["state"] == "degraded"
    assert saved == [summary]
