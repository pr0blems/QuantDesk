from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2 import ai_monitor, macro_market
from quantdesk_v2.macro_market import market_tide_trend_snapshot
from quantdesk_v2.models import RealtimeMarketFeatureSnapshot


class _FeatureSession:
    def scalar(self, _statement: object) -> None:
        return None

    def add(self, _value: object) -> None:
        return None


def _event(
    *,
    channel: str = "option_trades:AAPL",
    event_type: str = "option_trade",
    event_time_ms: int = 1_787_321_600_000,
    values: dict | None = None,
    raw: dict | None = None,
) -> dict:
    return {
        "channel": channel,
        "event_type": event_type,
        "symbol": "AAPL",
        "event_time_ms": event_time_ms,
        "received_at_ms": event_time_ms + 100,
        "event_id": f"{channel}-{event_time_ms}",
        "values": values or {},
        "raw": raw or {},
        "quality": {"valid": True, "stale": False, "age_ms": 100},
    }


def test_option_flow_keeps_execution_opening_sweep_dte_and_multileg_evidence() -> None:
    event_time_ms = int(datetime(2026, 8, 17, 14, tzinfo=UTC).timestamp() * 1_000)
    event = _event(
        event_time_ms=event_time_ms,
        values={"premium": 10_000, "volume": 50, "open_interest": 500},
        raw={
            "premium": 10_000,
            "volume": 50,
            "open_interest": 500,
            "option_type": "call",
            "side": "ask",
            "is_opening": True,
            "is_sweep": True,
            "is_multileg": True,
            "expiration": "2026-08-27T14:00:00Z",
        },
    )

    snapshot = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        _FeatureSession(),  # type: ignore[arg-type]
        event,
    )

    assert isinstance(snapshot, RealtimeMarketFeatureSnapshot)
    flow = snapshot.option_flow_snapshot_json
    assert flow["ask_premium_share"] == 1.0
    assert flow["bid_premium_share"] == 0.0
    assert flow["ask_execution_share"] == 1.0
    assert flow["bid_execution_share"] == 0.0
    assert flow["opening_event_count"] == 1
    assert flow["opening_premium_share"] == 1.0
    assert flow["sweep_event_count"] == 1
    assert flow["sweep_premium_share"] == 1.0
    assert flow["multileg_event_count"] == 1
    assert flow["multileg_direction_discount"] == 0.35
    assert flow["raw_bullish_premium"] == 10_000
    assert flow["bullish_premium"] == 3_500
    assert flow["event_volume_oi_ratio_mean"] == 0.1
    assert flow["dte_mean"] == 10.0
    assert flow["long_score"] == 100.0
    assert flow["data_quality"] == 0.35
    assert flow["minute_series"][0]["direction"] == "long"
    assert flow["window_metrics"]["5m"]["insufficient_data"] is True
    assert snapshot.data_coverage == Decimal("0.12")


def test_option_flow_does_not_turn_call_put_totals_into_direction() -> None:
    event = _event(
        channel="net_flow:AAPL",
        event_type="net_flow",
        values={
            "net_call_premium": 900_000,
            "net_put_premium": 200_000,
            "net_volume": 40,
        },
        raw={"net_call_premium": 900_000, "net_put_premium": 200_000},
    )

    snapshot = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        _FeatureSession(),  # type: ignore[arg-type]
        event,
    )

    flow = snapshot.option_flow_snapshot_json
    assert flow["net_call_premium_latest"] == 900_000
    assert flow["net_put_premium_latest"] == 200_000
    assert flow["net_volume_latest"] == 40
    assert flow["classified_event_count"] == 0
    assert "long_score" not in flow
    assert "short_score" not in flow


def test_option_flow_windows_expose_persistence_slope_and_acceleration() -> None:
    current_ms = 1_787_321_600_000
    prior = []
    for minutes_ago, balance in ((25, 0.1), (20, 0.2), (15, 0.3), (10, 0.45), (5, 0.6)):
        prior.append(
            {
                "bucket_time_ms": current_ms - minutes_ago * 60_000,
                "event_count": 1,
                "classified_event_count": 1,
                "bullish_premium": (1 + balance) * 50,
                "bearish_premium": (1 - balance) * 50,
                "directional_balance": balance,
                "direction": "long",
            }
        )
    result = {
        "minute_series": prior,
        "event_count": 1,
        "classified_event_count": 1,
        "bullish_premium": 90,
        "bearish_premium": 10,
        "data_quality": 1.0,
    }

    ai_monitor._refresh_option_flow_series(  # noqa: SLF001 - focused feature test
        result,
        _event(event_time_ms=current_ms),
    )

    assert result["window_metrics"]["5m"]["sample_count"] == 2
    assert result["window_metrics"]["15m"]["sample_count"] == 4
    window = result["window_metrics"]["30m"]
    assert window["sample_count"] == 6
    assert window["same_direction_points"] == 6
    assert window["slope_last_6"] > 0
    assert window["acceleration_last_6"] is not None
    assert window["insufficient_data"] is False


def test_gex_levels_and_off_lit_price_levels_are_retained_without_direction() -> None:
    gex = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        _FeatureSession(),  # type: ignore[arg-type]
        _event(
            channel="gex:AAPL",
            event_type="gex_snapshot",
            values={
                "call_wall": 210,
                "put_wall": 180,
                "gamma_flip": 195,
                "gamma_magnet": 202,
            },
            raw={"snapshot_kind": "gex_levels"},
        ),
    )
    off_lit = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        _FeatureSession(),  # type: ignore[arg-type]
        _event(
            channel="off_lit_price_levels",
            event_type="off_lit_snapshot",
            values={
                "total_lit_volume": 100,
                "total_off_lit_volume": 200,
                "off_lit_ratio": 2 / 3,
            },
            raw={
                "snapshot_kind": "off_lit_price_levels",
                "levels": [{"price": 190, "volume": 50}],
            },
        ),
    )

    assert gex.gex_snapshot_json["call_wall"] == 210
    assert gex.gex_snapshot_json["put_wall"] == 180
    assert gex.gex_snapshot_json["gamma_flip"] == 195
    assert gex.gex_snapshot_json["gamma_magnet"] == 202
    assert gex.gex_snapshot_json["levels_available"] is True
    assert "long_score" not in gex.gex_snapshot_json
    venue = off_lit.institutional_flow_snapshot_json
    assert venue["off_lit_ratio"] == 2 / 3
    assert venue["off_lit_price_levels"] == [{"price": 190, "volume": 50}]
    assert venue["price_levels_available"] is True
    assert "long_score" not in venue


def test_market_tide_trends_use_signed_volume_not_call_put_identity() -> None:
    start = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    points = [
        {
            "timestamp": (start + timedelta(minutes=index * 5)).isoformat(),
            "net_volume": value,
            "net_call_premium": 1_000_000 + index,
            "net_put_premium": 500_000 - index,
        }
        for index, value in enumerate((10, 20, 35, 55, 80, 110, 145))
    ]

    trend = market_tide_trend_snapshot({"points": points})

    assert trend["bias"] == "bull"
    assert trend["bias_basis"] == "net_volume"
    assert trend["same_direction_points"] == 7
    assert trend["windows"]["5m"]["sample_count"] == 2
    assert trend["windows"]["15m"]["sample_count"] == 4
    assert trend["windows"]["30m"]["sample_count"] == 7
    assert trend["recent_6_slope"] > 0
    assert trend["recent_6_acceleration"] > 0
    assert trend["insufficient_data"] is False

    call_put_only = market_tide_trend_snapshot(
        {
            "timestamp": start.isoformat(),
            "net_call_premium": 2_000_000,
            "net_put_premium": 100_000,
            "bias": "bull",
        }
    )
    assert call_put_only["directional_data_available"] is False
    assert call_put_only["bias"] == "neutral"
    assert call_put_only["rejected_call_put_only_points"] == 1


def test_market_tide_stream_events_feed_the_shared_rolling_history() -> None:
    macro_market._MARKET_TIDE_STREAM_POINTS.clear()  # noqa: SLF001
    session = _FeatureSession()
    start_ms = int(datetime(2026, 8, 17, 13, 30, tzinfo=UTC).timestamp() * 1_000)
    for index, value in enumerate((10, 25)):
        event_time_ms = start_ms + index * 5 * 60_000
        event = {
            "channel": "market_tide",
            "event_type": "market_tide_snapshot",
            "symbol": None,
            "event_time_ms": event_time_ms,
            "received_at_ms": event_time_ms + 100,
            "event_id": f"tide-{index}",
            "values": {
                "net_volume": value,
                "net_call_premium": 1_000,
                "net_put_premium": 500,
            },
            "raw": {"snapshot_kind": "market_tide"},
            "quality": {"valid": True, "stale": False},
        }
        assert ai_monitor.ingest_market_stream_event(session, event) is not None  # type: ignore[arg-type]

    history = macro_market.market_tide_stream_history()
    assert [item["directional_value"] for item in history] == [10, 25]
    trend = market_tide_trend_snapshot({}, history=history)
    assert trend["windows"]["5m"]["sample_count"] == 2
    assert trend["windows"]["5m"]["insufficient_data"] is False


def test_macro_service_overrides_unsafe_provider_bias_and_builds_history() -> None:
    macro_market._MARKET_TIDE_STREAM_POINTS.clear()  # noqa: SLF001
    service = macro_market.MacroMarketService(object())  # type: ignore[arg-type]
    start = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)

    unsafe = service._enhance_market_tide(  # noqa: SLF001
        {
            "available": True,
            "timestamp": start.isoformat(),
            "net_call_premium": 2_000,
            "net_put_premium": 100,
            "bias": "bull",
        }
    )
    assert unsafe["raw_provider_bias"] == "bull"
    assert unsafe["bias"] == "neutral"
    assert unsafe["directional_data_available"] is False

    first = service._enhance_market_tide(  # noqa: SLF001
        {
            "available": True,
            "timestamp": start.isoformat(),
            "net_volume": -20,
            "bias": "bull",
        }
    )
    second = service._enhance_market_tide(  # noqa: SLF001
        {
            "available": True,
            "timestamp": (start + timedelta(minutes=5)).isoformat(),
            "net_volume": -35,
            "bias": "bull",
        }
    )
    assert first["bias"] == "bear"
    assert first["bias_basis"] == "net_volume"
    assert first["trend_data_insufficient"] is True
    assert second["bias"] == "bear"
    assert second["trend"]["windows"]["5m"]["sample_count"] == 2
    assert second["trend"]["windows"]["5m"]["insufficient_data"] is False
