from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from starlette.requests import Request

from quantdesk_v2 import ai_monitor
from quantdesk_v2.interfaces.api.ai_monitor import (
    _score_policy_out,
    update_finnhub_usage,
    update_score_policy,
    update_unusual_whales_usage,
)
from quantdesk_v2.models import AdminSetting, Base, RealtimeMarketFeatureSnapshot
from quantdesk_v2.schemas import (
    AiMonitorFinnhubUsageUpdate,
    AiMonitorScorePolicyUpdate,
    AiMonitorUnusualWhalesUsageUpdate,
)


def _legacy_flow() -> dict:
    return {
        "score": 70.0,
        "data_quality": 0.8,
        "directional_data_available": True,
        "fresh": True,
        "hard_conflict": False,
    }


def test_missing_enhanced_domains_do_not_fabricate_neutral_scores() -> None:
    result = ai_monitor.apply_enhanced_market_domains(
        _legacy_flow(),
        None,
        direction="long",
    )

    assert result["score"] == 70.0
    assert result["domains"]["option_flow"]["score"] is None
    assert result["domains"]["gex"]["available"] is False
    assert result["domains"]["institutional_flow"]["available"] is False


def test_available_domains_are_renormalized_without_requiring_every_feed() -> None:
    result = ai_monitor.apply_enhanced_market_domains(
        _legacy_flow(),
        {
            "feature_version": "test_v1",
            "option_flow": {
                "available": True,
                "fresh": True,
                "long_score": 90.0,
                "data_quality": 1.0,
            },
            "gex": {},
            "institutional_flow": {},
        },
        direction="long",
    )

    assert result["score"] == 79.3333
    assert result["data_quality"] == 0.8933
    assert result["domains"]["gex"]["score"] is None


def test_quote_halt_and_event_checks_emit_observed_codes_for_binance_execution() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    quality = ai_monitor.signal_market_quality(
        {
            "evaluated_at": now_ms - 3_600_000,
            "prediction_features": {"quality_score": 0.9},
        },
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        maximum_market_age_seconds=120,
        minimum_feature_quality=0.7,
        requires_prediction_features=True,
        enhanced_feature={
            "quote": {
                "last_price": 100.0,
                "bid": 99.0,
                "ask": 101.0,
                "spread_bps": 200.0,
                "quote_age_ms": 250,
            },
            "halt_status": "halted",
            "data_coverage": 0.9,
            "stale_fields": [],
            "feature_version": "test_v1",
        },
        risk_events=[{"event_type": "FOMC", "risk_level": "critical"}],
    )
    gates = ai_monitor.stable_gate_summary(
        quality,
        _legacy_flow(),
        evaluated_at=now,
    )

    assert gates["status"] == "degraded"
    assert gates["blocking_reasons"] == []
    assert set(gates["observed_blocking_reasons"]) >= {
        "REFERENCE_SPREAD_TOO_WIDE",
        "SYMBOL_HALTED_OR_COOLDOWN",
        "HIGH_IMPACT_EVENT_WINDOW",
    }


def test_closed_hourly_bar_remains_valid_until_replacement_bar_can_close() -> None:
    now = datetime(2026, 8, 16, 12, 45, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)

    quality = ai_monitor.signal_market_quality(
        {
            # 11:00 open -> 12:00 close; at 12:45 it is still the latest
            # possible closed 1h bar and must not be rejected as stale.
            "evaluated_at": now_ms - 105 * 60 * 1000,
            "prediction_features": {"quality_score": 0.9},
        },
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        maximum_market_age_seconds=120,
        minimum_feature_quality=0.7,
        requires_prediction_features=True,
        enhanced_feature={
            "quote": {
                "bid": 99.99,
                "ask": 100.01,
                "quote_age_ms": 250,
            },
            "halt_status": "clear",
            "data_coverage": 1.0,
            "stale_fields": [],
        },
    )

    assert quality["checks"]["kline_fresh"] is True
    assert quality["bar_age_seconds"] == 45 * 60
    assert quality["maximum_bar_age_seconds"] >= 65 * 60


def test_trade_price_only_is_rejected_as_non_executable_reference() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    quality = ai_monitor.signal_market_quality(
        {
            "evaluated_at": now_ms - 3_600_000,
            "prediction_features": {"quality_score": 0.9},
        },
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        maximum_market_age_seconds=120,
        minimum_feature_quality=0.7,
        requires_prediction_features=True,
        enhanced_feature={
            "quote": {"last_price": 100.0},
            "halt_status": "clear",
            "data_coverage": 0.55,
            "stale_fields": [],
        },
    )
    gates = ai_monitor.stable_gate_summary(quality, _legacy_flow(), evaluated_at=now)

    assert quality["quote_available"] is False
    assert quality["quote_rejection_reason"] == "REFERENCE_QUOTE_UNAVAILABLE"
    assert quality["checks"]["reference_quote_available"] is False
    assert quality["checks"]["quote_fresh"] is False
    assert quality["checks"]["spread_acceptable"] is False
    assert quality["checks"]["quote_sane"] is False
    assert gates["passed"] is True
    assert gates["blocking_reasons"] == []
    assert "OBSERVED_ONLY:REFERENCE_QUOTE_UNAVAILABLE" in gates["warnings"]
    assert gates["execution_safety_gate_applied"] is False


def test_signal_upgrade_tables_are_registered_in_metadata() -> None:
    expected = {
        "market_stream_events",
        "realtime_market_feature_snapshots",
        "market_risk_events",
        "opportunity_market_snapshots",
    }

    assert expected <= set(Base.metadata.tables)
    snapshot = Base.metadata.tables["opportunity_market_snapshots"]
    assert {
        "quote_snapshot_json",
        "option_flow_snapshot_json",
        "gex_snapshot_json",
        "institutional_flow_snapshot_json",
        "risk_gate_snapshot_json",
        "score_components_json",
        "feature_version",
        "weights_version",
        "decision_version",
    } <= set(snapshot.columns.keys())


def test_stream_event_feature_upsert_keeps_missing_flow_scores_unknown() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def scalar(self, _statement: object) -> None:
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

    session = FakeSession()
    event = {
        "channel": "option_trades:AAPL",
        "event_type": "option_trade",
        "symbol": "AAPL",
        "event_time_ms": 1_776_336_000_000,
        "received_at_ms": 1_776_336_000_250,
        "event_id": "trade-1",
        "values": {"premium": 12500.0},
        "raw": {"premium": 12500.0},
        "quality": {"valid": True, "stale": False, "age_ms": 250},
    }

    result = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        session,  # type: ignore[arg-type]
        event,
    )

    assert isinstance(result, RealtimeMarketFeatureSnapshot)
    assert result.option_flow_snapshot_json["available"] is True
    assert "score" not in result.option_flow_snapshot_json
    assert result.data_coverage == Decimal("0.12")


def test_stream_option_score_requires_execution_side_evidence() -> None:
    class FakeSession:
        def scalar(self, _statement: object) -> None:
            return None

        def add(self, _value: object) -> None:
            return None

    event = {
        "channel": "option_trades:AAPL",
        "event_type": "option_trade",
        "symbol": "AAPL",
        "event_time_ms": 1_776_336_000_000,
        "received_at_ms": 1_776_336_000_250,
        "event_id": "trade-directional",
        "values": {"premium": 12500.0},
        "raw": {"premium": 12500.0, "option_type": "call", "side": "ask"},
        "quality": {"valid": True, "stale": False, "age_ms": 250},
    }

    result = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        FakeSession(),  # type: ignore[arg-type]
        event,
    )

    assert result is not None
    assert result.option_flow_snapshot_json["long_score"] == 100.0
    assert result.option_flow_snapshot_json["short_score"] == 0.0
    assert result.option_flow_snapshot_json["classified_event_count"] == 1


def test_trade_only_update_does_not_refresh_existing_nbbo() -> None:
    bucket_at = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
    snapshot = RealtimeMarketFeatureSnapshot(
        symbol="AAPL",
        bucket_at=bucket_at,
        market_session="regular",
        bid=Decimal("99.99"),
        ask=Decimal("100.01"),
        quote_age_ms=100,
        quote_snapshot_json={
            "bid": 99.99,
            "ask": 100.01,
            "quote_age_ms": 100,
            "quote_received_at_ms": 1_776_336_000_100,
            "received_at_ms": 1_776_336_000_100,
        },
        data_coverage=Decimal("0.7000"),
        stale_fields_json=[],
        quality_json={},
        feature_version=ai_monitor.MARKET_FEATURE_VERSION,
        captured_at=bucket_at,
    )

    class FakeSession:
        def scalar(self, _statement: object) -> RealtimeMarketFeatureSnapshot:
            return snapshot

        def add(self, _value: object) -> None:
            raise AssertionError("existing snapshot should be reused")

    event = {
        "channel": "price:AAPL",
        "event_type": "trade",
        "symbol": "AAPL",
        "event_time_ms": 1_776_336_030_000,
        "received_at_ms": 1_776_336_030_250,
        "event_id": "trade-only",
        "values": {"price": 100.05},
        "raw": {},
        "quality": {"valid": True, "stale": False, "age_ms": 250},
    }

    result = ai_monitor.upsert_realtime_market_feature_from_stream_event(
        FakeSession(),  # type: ignore[arg-type]
        event,
    )

    assert result is snapshot
    quote = result.quote_snapshot_json
    assert quote["quote_received_at_ms"] == 1_776_336_000_100
    assert quote["received_at_ms"] == 1_776_336_000_100
    assert quote["quote_age_ms"] == 100
    assert result.quote_age_ms == 100
    assert quote["last_trade_received_at_ms"] == 1_776_336_030_250
    assert quote["last_price"] == 100.05


def test_admin_signal_policy_defaults_to_record_only_and_rejects_bad_weights() -> None:
    class FakeSession:
        def get(self, _model: object, _key: str) -> object:
            return SimpleNamespace(
                version=4,
                value_json={
                    "mode": "invalid",
                    "weights": {"news": 1.0},
                    "thresholds": {"min_data_coverage": 9},
                },
            )

    policy = ai_monitor.unusual_whales_signal_policy(FakeSession())  # type: ignore[arg-type]

    assert policy["mode"] == "record"
    assert policy["score_enabled"] is False
    assert policy["hard_gate_enabled"] is False
    assert policy["weights"] == ai_monitor.DEFAULT_UNUSUAL_WHALES_WEIGHTS
    assert policy["thresholds"]["min_data_coverage"] == 1.0
    assert policy["weights_version"] == "uw_weights_v4"


def test_unpublished_signal_policy_uses_recommended_weights_and_version_zero() -> None:
    class EmptySession:
        def get(self, _model: object, _key: str) -> None:
            return None

    policy = ai_monitor.unusual_whales_signal_policy(EmptySession())  # type: ignore[arg-type]

    assert policy["weights"] == {
        "news": 0.20,
        "technical": 0.30,
        "market_context": 0.10,
        "options_flow": 0.20,
        "gex": 0.10,
        "institutional_flow": 0.10,
    }
    assert policy["published_version"] == 0
    assert policy["weights_version"] == "uw_weights_v0"
    assert policy["decision_version"] == "uw_record_v0"


def test_score_policy_update_preserves_non_weight_platform_sections() -> None:
    original = {
        "enabled": True,
        "mode": "gate",
        "rest_enabled": False,
        "websocket_enabled": True,
        "channels": {
            "price": True,
            "trading_halts": False,
            "interval_flow": True,
            "net_flow": False,
            "market_tide": True,
            "gex": True,
            "lit_trades": False,
            "off_lit_trades": True,
            "flow_alerts": True,
            "option_trades": False,
        },
        "thresholds": {
            "quote_age_regular_ms": 1_700,
            "quote_age_extended_ms": 9_000,
            "spread_hard_max_bps": 70.0,
            "source_divergence_max_bps": 32.0,
            "min_data_coverage": 0.82,
            "event_block_before_minutes": 40,
            "event_block_after_minutes": 18,
            "halt_cooldown_minutes": 17,
        },
        "weights": {
            "news": 0.30,
            "technical": 0.25,
            "market_context": 0.15,
            "options_flow": 0.12,
            "gex": 0.08,
            "institutional_flow": 0.10,
        },
        "retention": {
            "raw_event_days": 20,
            "feature_snapshot_days": 110,
            "cleanup_interval_minutes": 45,
            "cleanup_batch_size": 2_500,
            "cleanup_max_batches": 11,
        },
    }
    setting = AdminSetting(
        key=ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY,
        value_json=original,
        version=7,
        updated_by=1,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.committed = False

        def get(self, model: object, key: str) -> object | None:
            if model is AdminSetting and key == setting.key:
                return setting
            return None

        def scalar(self, _statement: object) -> object:
            return setting

        def add(self, row: object) -> None:
            self.added.append(row)

        def commit(self) -> None:
            self.committed = True

        def refresh(self, _row: object) -> None:
            return None

    database = FakeSession()
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v2/ai-monitor/score-policy",
            "headers": [(b"x-quantdesk-user-id", b"1")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    recommended = {
        "news": 0.20,
        "technical": 0.30,
        "market_context": 0.10,
        "options_flow": 0.20,
        "gex": 0.10,
        "institutional_flow": 0.10,
    }
    body = update_score_policy(
        AiMonitorScorePolicyUpdate(weights=recommended),
        request,
        database,  # type: ignore[arg-type]
        SimpleNamespace(id=1, is_admin=True),  # type: ignore[arg-type]
    )

    assert database.committed is True
    assert setting.version == 8
    assert setting.value_json["mode"] == "score"
    assert setting.value_json["weights"] == recommended
    for preserved in (
        "enabled",
        "rest_enabled",
        "websocket_enabled",
        "channels",
        "thresholds",
        "retention",
    ):
        assert setting.value_json[preserved] == original[preserved]
    assert body["score_enabled"] is True
    assert body["hard_gate_enabled"] is False
    assert body["weights_version"] == "uw_weights_v8"
    assert body["weights"] == recommended
    assert _score_policy_out(database, can_edit=False)["can_edit"] is False  # type: ignore[arg-type]


def test_platform_usage_switch_is_persisted_and_applied_immediately() -> None:
    setting = AdminSetting(
        key=ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY,
        value_json={"enabled": True},
        version=2,
        updated_by=1,
    )

    class FakeSession:
        def scalar(self, _statement: object) -> object:
            return setting

        def get(self, model: object, key: str) -> object | None:
            if model is AdminSetting and key == setting.key:
                return setting
            return None

        def add(self, _row: object) -> None:
            return None

        def commit(self) -> None:
            return None

        def refresh(self, _row: object) -> None:
            return None

    applied: list[dict] = []
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v2/ai-monitor/unusual-whales-enabled",
            "headers": [(b"x-quantdesk-user-id", b"1")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    apply_unusual_whales_runtime_config=applied.append,
                )
            ),
        }
    )
    database = FakeSession()

    result = update_unusual_whales_usage(
        AiMonitorUnusualWhalesUsageUpdate(enabled=False),
        request,
        database,  # type: ignore[arg-type]
        SimpleNamespace(id=1, is_admin=True),  # type: ignore[arg-type]
    )

    assert setting.version == 3
    assert setting.value_json["enabled"] is False
    assert applied[0]["enabled"] is False
    assert result["enabled"] is False
    assert result["effective_mode"] == "disabled"
    assert result["refresh_interval_seconds"] == 5 * 60


def test_finnhub_usage_switch_is_persisted_and_applied_immediately() -> None:
    from quantdesk_v2.finnhub_quotes import FINNHUB_USAGE_SETTING_KEY

    setting = AdminSetting(
        key=FINNHUB_USAGE_SETTING_KEY,
        value_json={"enabled": True, "market_open_only": True},
        version=2,
        updated_by=1,
    )

    class FakeSession:
        def scalar(self, _statement: object) -> object:
            return setting

        def get(self, model: object, key: str) -> object | None:
            if model is AdminSetting and key == setting.key:
                return setting
            return None

        def add(self, _row: object) -> None:
            return None

        def commit(self) -> None:
            return None

        def refresh(self, _row: object) -> None:
            return None

    applied: list[dict] = []
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v2/ai-monitor/finnhub-enabled",
            "headers": [(b"x-quantdesk-user-id", b"1")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
            "app": SimpleNamespace(
                state=SimpleNamespace(apply_finnhub_runtime_config=applied.append)
            ),
        }
    )

    result = update_finnhub_usage(
        AiMonitorFinnhubUsageUpdate(enabled=False),
        request,
        FakeSession(),  # type: ignore[arg-type]
        SimpleNamespace(id=1, is_admin=True),  # type: ignore[arg-type]
    )

    assert setting.version == 3
    assert setting.value_json == {"enabled": False, "market_open_only": True}
    assert applied == [{"enabled": False, "market_open_only": True}]
    assert result["finnhub_enabled"] is False
    assert result["finnhub"]["storage"] == "finnhub_quote_snapshots"


def test_six_domain_score_renormalizes_only_real_quality_weighted_evidence() -> None:
    policy = {
        "mode": "score",
        "weights": ai_monitor.DEFAULT_UNUSUAL_WHALES_WEIGHTS,
        "weights_version": "uw_weights_v7",
    }
    result = ai_monitor.enhanced_opportunity_domain_score(
        news_score=80,
        technical_score=60,
        market_environment={"available": True, "adjustment": 4},
        market_flow={
            "domains": {
                "option_flow": {
                    "available": True,
                    "fresh": True,
                    "score": 90,
                    "data_quality": 0.5,
                },
                "gex": {"available": False, "score": None},
                "institutional_flow": {
                    "available": True,
                    "fresh": True,
                    "score": 20,
                    "data_quality": 1.0,
                },
            }
        },
        policy=policy,
    )

    assert result["score"] == 63.75
    assert "gex" not in result["effective_weights"]
    assert result["domains"]["market_context"]["score"] == 60.0
    assert result["missing_domains"] == ["gex"]


def test_record_mode_observes_external_quote_failures_without_blocking_binance() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    quality = {
        "checks": {
            "price_available": True,
            "ticker_fresh": True,
            "kline_fresh": True,
            "feature_quality": True,
            "reference_quote_available": True,
            "quote_fresh": True,
            "spread_acceptable": False,
            "quote_sane": True,
            "not_halted": True,
            "event_window_clear": False,
        },
        "quote_available": True,
        "data_status": "blocked",
    }
    flow = {**_legacy_flow(), "legacy_hard_conflict": False}

    recorded = ai_monitor.stable_gate_summary(
        quality, flow, evaluated_at=now, policy_mode="record"
    )
    scored = ai_monitor.stable_gate_summary(
        quality, flow, evaluated_at=now, policy_mode="score"
    )
    gated = ai_monitor.stable_gate_summary(
        quality, flow, evaluated_at=now, policy_mode="gate"
    )

    assert recorded["passed"] is True
    assert scored["passed"] is True
    assert recorded["blocking_reasons"] == []
    assert scored["blocking_reasons"] == []
    assert "OBSERVED_ONLY:REFERENCE_SPREAD_TOO_WIDE" in recorded["warnings"]
    assert "REFERENCE_SPREAD_TOO_WIDE" in recorded["observed_blocking_reasons"]
    assert "HIGH_IMPACT_EVENT_WINDOW" in recorded["observed_blocking_reasons"]
    assert gated["passed"] is True
    assert gated["blocking_reasons"] == []
    assert "OBSERVED_ONLY:REFERENCE_SPREAD_TOO_WIDE" in gated["warnings"]
    assert "OBSERVED_ONLY:HIGH_IMPACT_EVENT_WINDOW" in gated["warnings"]


def test_disabled_mode_bypasses_unusual_whales_execution_gates() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    quality = {
        "checks": {
            "price_available": True,
            "ticker_fresh": True,
            "kline_fresh": True,
            "feature_quality": True,
            "reference_quote_available": False,
            "quote_fresh": False,
            "spread_acceptable": False,
            "quote_sane": False,
            "not_halted": False,
            "event_window_clear": False,
        },
        "quote_available": False,
        "data_status": "unavailable",
        "stale_fields": ["quote", "option_flow", "gex"],
    }
    result = ai_monitor.stable_gate_summary(
        quality,
        {**_legacy_flow(), "hard_conflict": True},
        evaluated_at=now,
        policy_mode="disabled",
    )

    assert result["passed"] is True
    assert result["blocking_reasons"] == []
    assert result["decision_checks"]["directional_conflict_clear"] is True
    assert result["execution_safety_gate_applied"] is False
    assert result["warnings"] == []


def test_record_mode_keeps_legacy_flow_score_while_retaining_raw_domains() -> None:
    result = ai_monitor.apply_enhanced_market_domains(
        _legacy_flow(),
        {
            "option_flow": {
                "available": True,
                "fresh": True,
                "long_score": 95,
                "data_quality": 1.0,
            }
        },
        direction="long",
        blend_into_legacy=False,
    )

    assert result["score"] == 70.0
    assert result["domains"]["option_flow"]["score"] == 95.0


def test_source_divergence_gate_exists_only_when_both_sources_exist() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    common = {
        "quote": {"last_price": 102.0},
        "data_coverage": 0.9,
        "halt_status": "clear",
    }
    kwargs = {
        "maximum_market_age_seconds": 120,
        "minimum_feature_quality": 0.7,
        "requires_prediction_features": False,
        "maximum_source_divergence_bps": 35.0,
    }

    divergent = ai_monitor.signal_market_quality(
        {"evaluated_at": now_ms - 3_600_000},
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        enhanced_feature=common,
        **kwargs,
    )
    absent = ai_monitor.signal_market_quality(
        {"evaluated_at": now_ms - 3_600_000},
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        enhanced_feature={"data_coverage": 0.9, "halt_status": "clear"},
        **kwargs,
    )

    assert divergent["checks"]["source_price_consistent"] is False
    assert divergent["source_divergence_bps"] == 200.0
    assert "source_price_consistent" not in absent["checks"]


def test_missing_coverage_metric_does_not_create_a_false_gate_failure() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    quality = ai_monitor.signal_market_quality(
        {"evaluated_at": now_ms - 3_600_000},
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        maximum_market_age_seconds=120,
        minimum_feature_quality=0.7,
        requires_prediction_features=False,
        enhanced_feature={
            "quote": {
                "last_price": 100.0,
                "bid": 99.99,
                "ask": 100.01,
                "spread_bps": 2.0,
                "quote_age_ms": 100,
            }
        },
        minimum_data_coverage=0.8,
    )

    assert "data_coverage" not in quality["checks"]
    assert ai_monitor.stable_gate_summary(
        quality, _legacy_flow(), evaluated_at=now, policy_mode="gate"
    )["passed"] is True


def test_stale_nbbo_and_halt_are_observations_for_binance_execution() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    scan = {
        "evaluated_at": now_ms - 3_600_000,
        "prediction_features": {"quality_score": 0.9},
    }
    market = {"price": 100.0, "ts": now_ms}
    kwargs = {
        "maximum_market_age_seconds": 120,
        "minimum_feature_quality": 0.7,
        "requires_prediction_features": True,
        "maximum_quote_age_ms": 2_000,
        "maximum_spread_bps": 25.0,
    }
    stale = ai_monitor.signal_market_quality(
        scan,
        market,
        "1h",
        now,
        enhanced_feature={
            "quote": {
                "bid": 99.99,
                "ask": 100.01,
                "spread_bps": 2.0,
                "quote_age_ms": 2_001,
            },
            "halt_status": "clear",
        },
        **kwargs,
    )
    halted = ai_monitor.signal_market_quality(
        scan,
        market,
        "1h",
        now,
        enhanced_feature={
            "quote": {
                "bid": 99.99,
                "ask": 100.01,
                "spread_bps": 2.0,
                "quote_age_ms": 100,
            },
            "halt_status": "halted",
        },
        **kwargs,
    )

    for mode in ("record", "score", "gate"):
        stale_gate = ai_monitor.stable_gate_summary(
            stale, _legacy_flow(), evaluated_at=now, policy_mode=mode
        )
        halt_gate = ai_monitor.stable_gate_summary(
            halted, _legacy_flow(), evaluated_at=now, policy_mode=mode
        )
        assert stale_gate["passed"] is True
        assert stale_gate["blocking_reasons"] == []
        assert halt_gate["passed"] is True
        assert halt_gate["blocking_reasons"] == []
        assert "OBSERVED_ONLY:REFERENCE_QUOTE_STALE" in stale_gate["warnings"]
        assert "OBSERVED_ONLY:SYMBOL_HALTED_OR_COOLDOWN" in halt_gate["warnings"]


def test_quote_freshness_uses_capture_time_not_minute_bucket_start() -> None:
    now = datetime(2026, 8, 16, 12, 0, 59, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    quality = ai_monitor.signal_market_quality(
        {
            "evaluated_at": now_ms - 3_600_000,
            "prediction_features": {"quality_score": 0.9},
        },
        {"price": 100.0, "ts": now_ms},
        "1h",
        now,
        maximum_market_age_seconds=120,
        minimum_feature_quality=0.7,
        requires_prediction_features=True,
        maximum_quote_age_ms=2_000,
        enhanced_feature={
            "bucket_at": "2026-08-16T12:00:00+00:00",
            "captured_at": "2026-08-16T12:00:58+00:00",
            "quote": {
                "bid": 99.99,
                "ask": 100.01,
                "spread_bps": 2.0,
                "quote_age_ms": 100,
            },
            "halt_status": "clear",
        },
    )

    assert quality["snapshot_age_ms"] == 1_000.0
    assert quality["checks"]["quote_fresh"] is True


def test_transitional_external_quote_failures_are_observed_outside_gate_mode() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    base_checks = {
        "price_available": True,
        "ticker_fresh": True,
        "kline_fresh": True,
        "feature_quality": True,
    }
    missing_quote = ai_monitor.stable_gate_summary(
        {"checks": base_checks, "quote_available": False},
        _legacy_flow(),
        evaluated_at=now,
        policy_mode="record",
    )
    halted = ai_monitor.stable_gate_summary(
        {
            "checks": base_checks,
            "quote_available": True,
            "halt_status": "halted",
        },
        _legacy_flow(),
        evaluated_at=now,
        policy_mode="score",
    )

    assert missing_quote["blocking_reasons"] == []
    assert halted["blocking_reasons"] == []
    assert "OBSERVED_ONLY:REFERENCE_QUOTE_UNAVAILABLE" in missing_quote["warnings"]
    assert "OBSERVED_ONLY:SYMBOL_HALTED_OR_COOLDOWN" in halted["warnings"]
