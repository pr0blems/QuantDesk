from datetime import datetime, timedelta
from types import SimpleNamespace

from quantdesk_v2.ai_monitor import (
    _frozen_analytics_evidence,
    enhanced_opportunity_domain_score,
    frozen_gate_rejection_summary,
    frozen_market_ablation_summary,
    record_opportunity_gate_decision,
    stable_gate_summary,
)
from quantdesk_v2.models import Base, OpportunityGateDecision


def _outcome(
    prediction_id: str,
    *,
    signal_time: datetime,
    result: str,
    net_return: float,
    quote_passed: bool = True,
    quote_available: bool = True,
    option_available: bool = True,
    gex_available: bool = True,
    institutional_available: bool = True,
) -> dict:
    checks = {
        "price_available": True,
        "quote_fresh": quote_passed,
        "spread_acceptable": quote_passed,
        "quote_sane": True,
        "not_halted": True,
        "event_window_clear": True,
    }
    domains = {
        "option_flow": {
            "available": option_available,
            "fresh": option_available,
            "score": 75.0 if option_available else None,
            "hard_conflict": False,
        },
        "gex": {
            "available": gex_available,
            "fresh": gex_available,
            "score": 68.0 if gex_available else None,
            "hard_conflict": False,
        },
        "institutional_flow": {
            "available": institutional_available,
            "fresh": institutional_available,
            "score": 62.0 if institutional_available else None,
            "hard_conflict": False,
        },
    }
    return {
        "id": f"opportunity-{prediction_id}",
        "prediction_id": prediction_id,
        "symbol": "AAPL",
        "direction": "long",
        "signal_time": signal_time,
        "result": result,
        "net_directional_return_bps": net_return,
        "quote": (
            {
                "bid": 99.9,
                "ask": 100.1,
                "spread_bps": 20.0,
                "quote_age_ms": 250,
            }
            if quote_available
            else {}
        ),
        "data_quality": {"quote_available": quote_available},
        "flow": {"domains": domains},
        "score_components": {
            "option_flow": domains["option_flow"]["score"],
            "gex": domains["gex"]["score"],
            "institutional_flow": domains["institutional_flow"]["score"],
        },
        "gate_summary": {
            "passed": quote_passed,
            "checks": checks,
            "blocking_reasons": (
                []
                if quote_passed
                else ["REFERENCE_QUOTE_STALE", "REFERENCE_SPREAD_TOO_WIDE"]
            ),
        },
        "version": {"feature": "test-v1", "decision": "test-decision-v1"},
    }


def test_frozen_market_ablation_reports_nested_performance_and_rejections() -> None:
    start = datetime(2026, 8, 16, 9, 30)
    items = [
        _outcome(
            "p1",
            signal_time=start,
            result="win",
            net_return=10.0,
        ),
        _outcome(
            "p2",
            signal_time=start + timedelta(minutes=1),
            result="loss",
            net_return=-15.0,
            quote_passed=False,
        ),
        _outcome(
            "p3",
            signal_time=start + timedelta(minutes=2),
            result="win",
            net_return=3.0,
            option_available=False,
            gex_available=False,
            institutional_available=False,
        ),
        _outcome(
            "p4",
            signal_time=start + timedelta(minutes=3),
            result="loss",
            net_return=-5.0,
            quote_available=False,
            option_available=False,
            gex_available=False,
            institutional_available=False,
        ),
    ]

    result = frozen_market_ablation_summary(items)
    variants = {item["key"]: item for item in result["variants"]}

    assert result["schema_version"] == "frozen_market_ablation.v2"
    assert result["causal_replay"] is False
    assert result["causal_replay_status"] == "unavailable"
    assert variants["baseline"]["sample_count"] == 4
    assert variants["baseline"]["maximum_drawdown_bps"] == 17.0
    assert variants["quote_halt"]["available_count"] == 3
    assert variants["quote_halt"]["sample_count"] == 2
    assert variants["quote_halt"]["rejected_count"] == 1
    assert variants["quote_halt"]["average_net_return_bps"] == 6.5
    assert variants["option_flow"]["available_count"] == 2
    assert variants["option_flow"]["sample_count"] == 1
    assert variants["full"]["available_count"] == 2
    assert variants["full"]["sample_count"] == 1
    assert variants["full"]["rejection_reason_counts"] == {
        "REFERENCE_QUOTE_STALE": 1,
        "REFERENCE_SPREAD_TOO_WIDE": 1,
    }
    assert result["data_coverage"]["option_flow"]["coverage_rate"] == 50.0
    assert any(
        item["prediction_id"] == "p2"
        and "REFERENCE_QUOTE_STALE" in item["reasons"]
        for item in result["gate_rejections"]["items"]
    )


def test_missing_market_modules_are_unavailable_instead_of_neutral() -> None:
    item = {
        "id": "opportunity-legacy",
        "prediction_id": "prediction-legacy",
        "signal_time": datetime(2026, 8, 16, 9, 30),
        "result": "win",
        "net_directional_return_bps": 12.0,
        "quote": {},
        "flow": {},
        "gate_summary": {},
        "data_quality": {},
        # A mutable live value must never backfill signal-time coverage.
        "current_quote": {"bid": 99.9, "ask": 100.1, "quote_age_ms": 50},
    }

    result = frozen_market_ablation_summary([item])
    variants = {entry["key"]: entry for entry in result["variants"]}

    assert variants["baseline"]["status"] == "available"
    assert variants["quote_halt"]["status"] == "unavailable"
    assert variants["quote_halt"]["hit_rate"] is None
    assert variants["full"]["status"] == "unavailable"
    assert result["data_coverage"]["quote_halt"]["status"] == "unavailable"


def _complete_point_in_time_outcome() -> dict:
    signal_at = datetime(2026, 8, 16, 9, 30, 2)
    flow = {
        "hard_conflict": False,
        "domains": {
            "option_flow": {
                "available": True,
                "fresh": True,
                "score": 75.0,
                "data_quality": 1.0,
                "hard_conflict": False,
            },
            "gex": {
                "available": True,
                "fresh": True,
                "score": 68.0,
                "data_quality": 1.0,
                "hard_conflict": False,
            },
            "institutional_flow": {
                "available": True,
                "fresh": True,
                "score": 62.0,
                "data_quality": 1.0,
                "hard_conflict": False,
            },
        },
    }
    weights = {
        "news": 0.30,
        "technical": 0.25,
        "market_context": 0.15,
        "options_flow": 0.12,
        "gex": 0.08,
        "institutional_flow": 0.10,
    }
    score = enhanced_opportunity_domain_score(
        news_score=80.0,
        technical_score=70.0,
        market_environment={"available": True, "adjustment": 2.0},
        market_flow=flow,
        policy={"mode": "gate", "weights": weights, "weights_version": "weights-v1"},
    )
    checks = {
        "price_available": True,
        "ticker_fresh": True,
        "kline_fresh": True,
        "feature_quality": True,
        "reference_quote_available": True,
        "quote_fresh": True,
        "spread_acceptable": True,
        "quote_sane": True,
        "not_halted": True,
        "data_coverage": True,
        "event_window_clear": True,
    }
    gate = stable_gate_summary(
        {
            "checks": checks,
            "quote_available": True,
            "halt_status": "clear",
            "data_status": "live",
            "data_coverage": 1.0,
            "stale_fields": [],
        },
        flow,
        evaluated_at=datetime(2026, 8, 16, 9, 30, 1),
        policy_mode="gate",
    )
    return {
        "id": "opportunity-causal",
        "prediction_id": "prediction-causal",
        "symbol": "AAPL",
        "direction": "long",
        "signal_time": signal_at,
        "result": "win",
        "net_directional_return_bps": 12.0,
        "market_snapshot_source": "opportunity_market_snapshot",
        "quote": {
            "bid": 99.9,
            "ask": 100.1,
            "spread_bps": 20.0,
            "quote_age_ms": 100,
        },
        "data_quality": {
            "quote_available": True,
            "status": "live",
            "coverage": 1.0,
        },
        "flow": flow,
        "score_components": {
            "policy_mode": "gate",
            "enhanced_domains": score["domains"],
            "enhanced_configured_weights": weights,
            "enhanced_effective_weights": score["effective_weights"],
            "enhanced_score": score["score"],
            "combined": score["score"],
        },
        "gate_summary": gate,
        "version": {
            "feature": "feature-v1",
            "weights": "weights-v1",
            "decision": "decision-v1",
        },
        "point_in_time_replay": {
            "snapshot_captured_at": "2026-08-16T09:30:00",
            "market_feature_snapshot_id": 7,
            "feature_snapshot": {
                "id": 7,
                "captured_at": "2026-08-16T09:29:50",
                "halt_status": "clear",
                "data_coverage": 1.0,
                "stale_fields": [],
                "quality": {},
                "feature_version": "feature-v1",
            },
            "gate_decision": {
                "market_feature_snapshot_id": 7,
                "selected": True,
                "gate_status": "passed",
                "decision_at": "2026-08-16T09:30:01",
                "risk_gate_snapshot": gate,
                "feature_version": "feature-v1",
                "weights_version": "weights-v1",
                "decision_version": "decision-v1",
            },
        },
    }


def test_complete_frozen_rows_enable_causal_point_in_time_ablation() -> None:
    result = frozen_market_ablation_summary([_complete_point_in_time_outcome()])

    assert result["causal_replay"] is True
    assert result["causal_replay_status"] == "available"
    assert result["method"] == "point_in_time_frozen_signal_replay"
    assert result["causal_replay_coverage"] == {
        "available_count": 1,
        "missing_count": 0,
        "coverage_rate": 100.0,
        "unavailable_reason_counts": {},
    }


def test_future_feature_makes_causal_replay_explicitly_unavailable() -> None:
    item = _complete_point_in_time_outcome()
    item["point_in_time_replay"]["feature_snapshot"]["captured_at"] = (
        "2026-08-16T09:30:03"
    )

    result = frozen_market_ablation_summary([item])

    assert result["causal_replay"] is False
    assert result["causal_replay_status"] == "unavailable"
    assert result["causal_replay_coverage"]["unavailable_reason_counts"][
        "FEATURE_AFTER_SIGNAL"
    ] == 1


def test_immutable_market_snapshot_takes_precedence_over_prediction_fallback() -> None:
    snapshot = SimpleNamespace(
        quote_snapshot_json={"bid": 100.0, "ask": 100.1, "quote_age_ms": 100},
        option_flow_snapshot_json={"available": True, "score": 77.0},
        gex_snapshot_json={"available": True, "score": 66.0},
        institutional_flow_snapshot_json={"available": True, "score": 55.0},
        risk_gate_snapshot_json={"passed": True, "checks": {"not_halted": True}},
        score_components_json={"combined": 81.0},
        data_quality_json={"quote_available": True, "coverage": 1.0},
        feature_version="snapshot-feature-v1",
        weights_version="snapshot-weights-v1",
        decision_version="snapshot-decision-v1",
    )

    result = _frozen_analytics_evidence(
        {"quote": {"bid": 1.0, "ask": 2.0}, "version": {"feature": "fallback"}},
        snapshot,  # type: ignore[arg-type]
    )

    assert result["snapshot_source"] == "opportunity_market_snapshot"
    assert result["quote"]["bid"] == 100.0
    assert result["flow"]["option_flow"]["score"] == 77.0
    assert result["version"] == {
        "api": "ai_opportunity.v3",
        "feature": "snapshot-feature-v1",
        "weights": "snapshot-weights-v1",
        "decision": "snapshot-decision-v1",
    }


def test_gate_decision_table_and_same_scan_identity_are_stable() -> None:
    assert "opportunity_gate_decisions" in Base.metadata.tables
    columns = Base.metadata.tables["opportunity_gate_decisions"].columns
    assert {
        "analysis_run_id",
        "decision_at",
        "feature_captured_at",
        "blocking_reasons_json",
        "risk_gate_snapshot_json",
        "feature_version",
        "weights_version",
        "decision_version",
        "dedup_key",
    } <= set(columns.keys())

    class FakeSession:
        def __init__(self) -> None:
            self.new: set[object] = set()

        def scalar(self, _statement: object) -> None:
            return None

        def add(self, value: object) -> None:
            self.new.add(value)

    session = FakeSession()
    opportunity = SimpleNamespace(
        id=11,
        user_id=7,
        symbol="AAPL",
        contract_symbol="AAPLUSDT",
        direction="long",
    )
    decision_at = datetime(2026, 8, 16, 9, 31)
    evidence = {
        "confirmed": False,
        "gate_summary": {
            "status": "blocked",
            "passed": False,
            "blocking_reasons": ["reference_quote_stale"],
            "warnings": [],
        },
        "market_flow": {},
        "score_components": {"combined": 72.0},
        "data_quality": {"coverage": 0.5},
        "version": {
            "feature": "feature-v1",
            "weights": "weights-v1",
            "decision": "decision-v1",
        },
    }
    feature = {
        "id": 3,
        "captured_at": "2026-08-16T09:30:30Z",
        "quote": {"bid": 100.0, "ask": 100.1, "quote_age_ms": 31_000},
    }

    first = record_opportunity_gate_decision(
        session,  # type: ignore[arg-type]
        opportunity,  # type: ignore[arg-type]
        analysis_run_id=99,
        decision_at=decision_at,
        evidence=evidence,
        feature=feature,
    )
    duplicate = record_opportunity_gate_decision(
        session,  # type: ignore[arg-type]
        opportunity,  # type: ignore[arg-type]
        analysis_run_id=99,
        decision_at=decision_at + timedelta(seconds=1),
        evidence=evidence,
        feature=feature,
    )

    assert first is duplicate
    assert len(session.new) == 1
    assert first.gate_status == "blocked"
    assert first.selected is False
    assert first.blocking_reasons_json == ["REFERENCE_QUOTE_STALE"]
    assert first.feature_captured_at == datetime(2026, 8, 16, 9, 30, 30)
    assert first.dedup_key == duplicate.dedup_key


def test_observed_gate_rejection_summary_uses_only_frozen_rows() -> None:
    decision = OpportunityGateDecision(
        public_id="gate-1",
        opportunity_id=1,
        user_id=7,
        analysis_run_id=99,
        symbol="AAPL",
        contract_symbol="AAPLUSDT",
        direction="long",
        gate_status="blocked",
        selected=False,
        decision_at=datetime(2026, 8, 16, 9, 31),
        feature_captured_at=datetime(2026, 8, 16, 9, 30, 30),
        blocking_reasons_json=["REFERENCE_SPREAD_TOO_WIDE"],
        warnings_json=[],
        risk_gate_snapshot_json={},
        quote_snapshot_json={"bid": 99.0, "ask": 101.0, "quote_age_ms": 250},
        market_flow_snapshot_json={
            "domains": {
                "option_flow": {"available": True, "score": 70.0},
                "gex": {"available": False, "score": None},
            }
        },
        score_components_json={},
        data_quality_json={},
        feature_version="feature-v1",
        weights_version="weights-v1",
        decision_version="decision-v1",
        dedup_key="dedup-1",
    )

    result = frozen_gate_rejection_summary([decision], total_count=3)

    assert result["status"] == "available"
    assert result["total_count"] == 3
    assert result["loaded_count"] == 1
    assert result["truncated"] is True
    assert result["reason_counts"] == {"REFERENCE_SPREAD_TOO_WIDE": 1}
    assert result["data_coverage"]["quote_halt"]["coverage_rate"] == 100.0
    assert result["data_coverage"]["option_flow"]["coverage_rate"] == 100.0
    assert result["data_coverage"]["gex"]["coverage_rate"] == 0.0
    assert result["items"][0]["decision_at"] == datetime(2026, 8, 16, 9, 31)
