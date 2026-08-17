from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

from sqlalchemy import create_engine, text

from quantdesk_v2.models import (
    MarketStreamEvent,
    OpportunityGateDecision,
    OpportunityMarketSnapshot,
    RealtimeMarketFeatureSnapshot,
)
from quantdesk_v2.unusual_whales_runtime import (
    DEFAULT_CHANNEL_FLAGS,
    UnusualWhalesRuntime,
    cleanup_market_data_retention,
    stream_subscriptions,
    validate_stream_subscriptions,
)
from quantdesk_v2.unusual_whales_stream import UnusualWhalesStreamEvent


def _event(
    event_id: str,
    *,
    channel: str = "price",
    symbol: str = "AAPL",
    received_at_ms: int = 1_000_000,
    event_time_ms: int | None = 999_900,
) -> UnusualWhalesStreamEvent:
    return UnusualWhalesStreamEvent(
        channel=channel,
        event_type=channel.partition(":")[0],
        symbol=symbol,
        event_time_ms=event_time_ms,
        received_at_ms=received_at_ms,
        event_id=event_id,
        values={"price": 190.25},
        raw={"ticker": symbol, "price": 190.25},
        quality={"available": True, "valid": True, "issues": []},
    )


def _runtime(**kwargs: Any) -> UnusualWhalesRuntime:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    return UnusualWhalesRuntime(
        engine,
        lambda: "test-key",
        ("AAPLUSDT", "MSFTUSDT"),
        websocket_enabled=False,
        **kwargs,
    )


def test_rest_recovery_defaults_to_five_minutes() -> None:
    runtime = _runtime()

    assert runtime.recovery_poll_seconds == 5 * 60


def test_market_session_gate_drops_events_while_us_market_is_closed() -> None:
    runtime = _runtime(market_open_checker=lambda: False)

    runtime.on_event(_event("closed-event"))

    assert runtime._queue.empty()
    health = runtime.health_snapshot()
    assert health["collection"]["market_open"] is False
    assert health["collection"]["active"] is False
    assert health["rest"]["status"] == "market_closed"


def test_stream_subscriptions_maps_flow_alerts_and_ticker_net_flow() -> None:
    flags = {key: False for key in DEFAULT_CHANNEL_FLAGS}
    flags.update({"flow_alerts": True, "net_flow": True})

    subscriptions = stream_subscriptions(
        flags,
        ("aaplusdt", " MSFT ", "", "USDT"),
    )

    assert subscriptions == ("flow-alerts", "net_flow:AAPL", "net_flow:MSFT")
    assert "net_flow" not in subscriptions


def test_contract_validation_supports_base_channels_and_aliases() -> None:
    accepted, missing = validate_stream_subscriptions(
        ("price:AAPL", "net_flow:AAPL", "flow-alerts", "gex"),
        ("price", "net_flow", "flow_alerts"),
    )

    assert accepted == ("flow-alerts", "net_flow:AAPL", "price:AAPL")
    assert missing == ("gex",)


class _RestClient:
    def __init__(self, *, contract=None) -> None:
        self.contract = contract or ["price", "market_tide", "net_flow"]
        self.quote_symbols: list[str] = []
        self.state_symbols: list[str] = []

    def websocket_channels(self):
        return {"channels": list(self.contract), "quality": {"available": True}}

    def stock_quotes(self, symbols):
        self.quote_symbols.extend(symbols)
        return {
            "AAPL": {
                "symbol": "AAPL",
                "price": 190.25,
                "bid": 190.2,
                "ask": 190.3,
                "bid_size": 100,
                "ask_size": 80,
                "quote_time_ms": 1_786_636_351_248,
                "quality": {
                    "available": True,
                    "valid": True,
                    "source_time_ms": 1_786_636_351_248,
                    "issues": [],
                },
            }
        }

    def stock_states(self, symbols):
        self.state_symbols.extend(symbols)
        return {
            "MSFT": {
                "symbol": "MSFT",
                "price": 420.5,
                "volume": 1234,
                "market_time": "regular",
                "quality": {
                    "available": True,
                    "valid": True,
                    "source_time_ms": 1_786_636_351_248,
                    "issues": [],
                },
            }
        }

    def market_tide(self):
        return {
            "timestamp": "2026-08-16T14:30:00Z",
            "net_call_premium": 900,
            "net_put_premium": 200,
            "net_premium": 700,
            "bias": "bull",
            "quality": {
                "available": True,
                "valid": True,
                "source_time_ms": 1_786_636_351_248,
                "issues": [],
            },
        }

    def vix_term_structure(self, *, history_days):
        assert history_days == 30
        return {
            "latest": {"front_month": "18.4"},
            "history": [],
            "quality": {
                "available": True,
                "valid": True,
                "source_time_ms": 1_786_636_351_248,
                "issues": [],
            },
        }

    def gex_levels(self, symbol):
        return {
            "symbol": symbol,
            "call_wall": 200,
            "put_wall": 180,
            "quality": {"available": True, "valid": True, "issues": []},
        }

    def off_lit_price_levels(self, symbol):
        return {
            "symbol": symbol,
            "total_lit_volume": 100,
            "total_off_lit_volume": 200,
            "off_lit_ratio": 2 / 3,
            "levels": [],
            "quality": {"available": True, "valid": True, "issues": []},
        }


def test_runtime_validates_official_contract_without_disabling_on_empty() -> None:
    client = _RestClient(contract=["price", "market_tide", "net_flow"])
    runtime = _runtime(rest_client=client)

    assert runtime._refresh_channel_contract() is True
    contract = runtime.health_snapshot()["rest"]["channel_contract"]
    assert contract["verified"] is True
    assert "price" in contract["active"]
    assert "gex" in contract["missing"]
    assert "net_flow:AAPL" in contract["active"]

    client.contract = []
    assert runtime._refresh_channel_contract() is False
    restored = runtime.stream.health_snapshot()["subscriptions"]
    assert "gex" in restored
    assert "price" in restored


def test_recovery_snapshot_enqueues_quotes_fallback_states_and_global_context() -> None:
    client = _RestClient()
    runtime = _runtime(
        rest_client=client,
        rest_snapshot_symbol_limit=2,
        rest_detail_symbol_limit=1,
    )

    runtime._poll_recovery_snapshot("startup")

    queued = []
    while not runtime._queue.empty():
        queued.append(runtime._queue.get_nowait())
    channels = {event.channel for event in queued}
    assert "price:AAPL" in channels
    assert "price:MSFT" in channels
    assert "market_tide" in channels
    assert "vix_term_structure" in channels
    assert "gex:AAPL" in channels
    assert "off_lit_price_levels" in channels
    assert client.quote_symbols == ["AAPL", "MSFT"]
    assert client.state_symbols == ["MSFT"]

    rest = runtime.health_snapshot()["rest"]
    assert rest["recovery_runs"] == 1
    assert rest["recovery_quote_rows"] == 1
    assert rest["recovery_state_rows"] == 1
    assert rest["recovery_events"] == 6
    assert rest["last_recovery_reason"] == "startup"
    assert rest["status"] == "ready"


def test_writer_batches_events_and_reports_persistence_health(monkeypatch) -> None:
    batches: list[list[str]] = []

    def fake_ingest(_db, events):
        ids = [event.event_id for event in events]
        batches.append(ids)
        return {
            "accepted": len(ids) - (1 if "duplicate" in ids else 0),
            "duplicates": 1 if "duplicate" in ids else 0,
        }

    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales_runtime.ingest_market_stream_events",
        fake_ingest,
    )
    runtime = _runtime(batch_size=2, flush_seconds=0.05)
    runtime.on_event(_event("first"))
    runtime.on_event(_event("duplicate", channel="net_flow:AAPL"))
    runtime.on_event(_event("third", received_at_ms=1_000_200, event_time_ms=1_000_000))

    # Run the deterministic drain path without starting the websocket or a worker thread.
    runtime._stop.set()
    runtime._writer_loop()

    assert batches == [["first", "duplicate"], ["third"]]
    health = runtime.health_snapshot()
    assert health["writer"]["queue_depth"] == 0
    assert health["writer"]["persisted"] == 2
    assert health["writer"]["duplicates"] == 1
    assert health["writer"]["write_errors"] == 0
    assert health["writer"]["last_write_at_ms"] is not None
    assert health["writer"]["last_write_error"] is None

    channels = runtime.channel_health_snapshot()
    assert channels["price"]["received"] == 2
    assert channels["price"]["lag_ms"] == 200
    assert channels["net_flow"]["received"] == 1
    assert channels["net_flow"]["lag_ms"] == 100


def test_channel_health_marks_old_events_stale(monkeypatch) -> None:
    runtime = _runtime(channel_stale_ms=1_000)
    runtime.on_event(
        _event(
            "old",
            channel="flow-alerts",
            received_at_ms=10_000,
            event_time_ms=9_900,
        )
    )
    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales_runtime.time.time",
        lambda: 12.5,
    )

    snapshot = runtime.channel_health_snapshot()["flow-alerts"]

    assert snapshot["age_ms"] == 2_500
    assert snapshot["fresh"] is False
    assert snapshot["status"] == "stale"


def test_runtime_filters_symbols_outside_the_monitor_pool_but_keeps_market_tide() -> None:
    runtime = _runtime()

    runtime.on_event(_event("outside", symbol="TSLA"))
    runtime.on_event(_event("mapped", symbol="AAPL"))
    runtime.on_event(_event("global", channel="market_tide", symbol=""))

    assert runtime._queue.qsize() == 2
    assert runtime.health_snapshot()["writer"]["filtered"] == 1
    assert runtime.channel_health_snapshot()["price"]["filtered"] == 1


def test_writer_records_value_errors_without_losing_queue_accounting(monkeypatch) -> None:
    def reject_batch(_db, _events):
        raise ValueError("invalid canonical event")

    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales_runtime.ingest_market_stream_events",
        reject_batch,
    )
    runtime = _runtime(batch_size=10, flush_seconds=0.05)
    runtime.on_event(_event("bad"))
    runtime._stop.set()

    runtime._writer_loop()

    health = runtime.health_snapshot()["writer"]
    assert health["queue_depth"] == 0
    assert health["persisted"] == 0
    assert health["write_errors"] == 1
    assert health["last_write_error"] == "ValueError"


def test_sqlite_leadership_fallback_elects_exactly_one_runtime() -> None:
    first = _runtime()
    second = _runtime()
    try:
        assert first._try_acquire_leadership() is True
        assert second._try_acquire_leadership() is False
        assert first.health_snapshot()["leadership"]["status"] == "leader"
        assert second.health_snapshot()["leadership"]["status"] == "standby"
        assert (
            first.health_snapshot()["leadership"]["mode"]
            == "process_local_sqlite_fallback"
        )

        first._release_leadership()
        assert second._try_acquire_leadership() is True
    finally:
        first._release_leadership()
        second._release_leadership()


def test_standby_runtime_takes_over_after_leader_releases() -> None:
    first = _runtime()
    second = _runtime(leadership_poll_seconds=0.05)
    activated = Event()
    second._activate_leader = activated.set  # type: ignore[method-assign]
    try:
        assert first._try_acquire_leadership() is True
        second.start()
        assert second.health_snapshot()["leadership"]["status"] == "standby"

        first._release_leadership()

        assert activated.wait(0.5) is True
        assert second.health_snapshot()["leadership"]["status"] == "leader"
    finally:
        first._release_leadership()
        second.stop(join_timeout=0.5)


def test_retention_prunes_only_unreferenced_replay_tiers_in_batches() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MarketStreamEvent.__table__.create(engine)
    RealtimeMarketFeatureSnapshot.__table__.create(engine)
    OpportunityMarketSnapshot.__table__.create(engine)
    OpportunityGateDecision.__table__.create(engine)
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    old = now.replace(tzinfo=None) - timedelta(days=120)
    with engine.begin() as connection:
        connection.execute(
            MarketStreamEvent.__table__.insert(),
            [
                {
                    "id": index + 1,
                    "provider": "unusual_whales",
                    "channel": "price",
                    "symbol": "AAPL",
                    "event_time": old,
                    "received_at": old,
                    "sequence_key": None,
                    "dedup_key": f"old-{index}",
                    "payload_json": {},
                    "schema_version": "uw_stream_v1",
                    "quality_status": "valid",
                }
                for index in range(3)
            ],
        )
        connection.execute(
            RealtimeMarketFeatureSnapshot.__table__.insert(),
            [
                {
                    "id": index + 1,
                    "symbol": symbol,
                    "bucket_at": old,
                    "market_session": "closed",
                    "halt_status": "clear",
                    "data_coverage": 1,
                    "stale_fields_json": [],
                    "quality_json": {},
                    "feature_version": "uw_features_v1",
                    "captured_at": old,
                }
                for index, symbol in enumerate(("AAPL", "MSFT", "NVDA"))
            ],
        )
        feature_ids = list(
            connection.scalars(
                text(
                    "SELECT id FROM realtime_market_feature_snapshots ORDER BY id"
                )
            )
        )
        protected_snapshot_id = int(feature_ids[1])
        protected_decision_id = int(feature_ids[2])
        connection.execute(
            text(
                """INSERT INTO opportunity_market_snapshots(
                       id,opportunity_id,user_id,market_feature_snapshot_id,captured_at,
                       quote_snapshot_json,option_flow_snapshot_json,gex_snapshot_json,
                       institutional_flow_snapshot_json,macro_snapshot_json,
                       risk_gate_snapshot_json,score_components_json,data_quality_json,
                       weights_version,feature_version,decision_version)
                   VALUES (1,1,1,:feature_id,:captured_at,'{}','{}','{}','{}','{}',
                           '{}','{}','{}','weights-v1','features-v1','decision-v1')"""
            ),
            {"feature_id": protected_snapshot_id, "captured_at": old},
        )
        connection.execute(
            text(
                """INSERT INTO opportunity_gate_decisions(
                       id,public_id,opportunity_id,user_id,analysis_run_id,
                       market_feature_snapshot_id,symbol,contract_symbol,direction,
                       gate_status,selected,decision_at,blocking_reasons_json,
                       warnings_json,risk_gate_snapshot_json,quote_snapshot_json,
                       market_flow_snapshot_json,score_components_json,data_quality_json,
                       feature_version,weights_version,decision_version,dedup_key,created_at)
                   VALUES (1,'decision-1',2,1,1,:feature_id,'NVDA','NVDAUSDT','long',
                           'passed',1,:decision_at,'[]','[]','{}','{}','{}','{}','{}',
                           'features-v1','weights-v1','decision-v1','decision-dedup',
                           :created_at)"""
            ),
            {
                "feature_id": protected_decision_id,
                "decision_at": old,
                "created_at": old,
            },
        )

    result = cleanup_market_data_retention(
        engine,
        now=now,
        raw_event_days=14,
        feature_snapshot_days=90,
        batch_size=100,
        max_batches=2,
    )

    assert result == {
        "deleted_events": 3,
        "deleted_features": 1,
        "event_backlog": 0,
        "feature_backlog": 0,
        "protected_features": 2,
    }
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT COUNT(*) FROM opportunity_market_snapshots")
        ) == 1
        assert connection.scalar(
            text("SELECT COUNT(*) FROM opportunity_gate_decisions")
        ) == 1
        assert connection.scalar(
            text("SELECT COUNT(*) FROM realtime_market_feature_snapshots")
        ) == 2
