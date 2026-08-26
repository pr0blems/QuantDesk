from __future__ import annotations

import pytest

from quantdesk_v2.strategy_routes import _validate_passed_evidence


def test_oos_and_stress_evidence_are_fail_closed() -> None:
    _validate_passed_evidence(
        "oos",
        {
            "net_after_cost": 1.0,
            "positive_months": 5,
            "largest_symbol_profit_share": 0.2,
            "max_drawdown_within_limit": True,
        },
    )
    _validate_passed_evidence(
        "stress",
        {"double_cost_net_after_cost": 0.01, "parameter_stable": True},
    )

    with pytest.raises(ValueError, match="单一品种"):
        _validate_passed_evidence(
            "oos",
            {
                "net_after_cost": 1.0,
                "positive_months": 5,
                "largest_symbol_profit_share": 0.2001,
                "max_drawdown_within_limit": True,
            },
        )


def test_shadow_and_paper_minimum_samples_are_enforced() -> None:
    with pytest.raises(ValueError, match="28 天"):
        _validate_passed_evidence(
            "shadow",
            {
                "duration_days": 27.9,
                "decision_consistency": 1.0,
                "incident_count": 0,
                "latency_slo_met": True,
            },
        )
    with pytest.raises(ValueError, match="100 笔"):
        _validate_passed_evidence(
            "paper",
            {
                "trade_count": 99,
                "net_after_cost": 10,
                "slippage_within_stress": True,
                "incident_count": 0,
            },
        )


def test_fault_drill_requires_every_recovery_scenario() -> None:
    required = {
        "order_timeout_after_submit",
        "crash_before_ack",
        "partial_fill_disconnect",
        "protection_partial_failure",
        "user_stream_gap",
        "rest_reconciliation_failure",
        "database_commit_failure",
        "worker_lease_handover",
        "service_restart",
    }
    _validate_passed_evidence(
        "fault_drill",
        {"scenarios": [{"code": code, "passed": True} for code in sorted(required)]},
    )
    with pytest.raises(ValueError, match="service_restart"):
        _validate_passed_evidence(
            "fault_drill",
            {
                "scenarios": [
                    {"code": code, "passed": True}
                    for code in sorted(required - {"service_restart"})
                ]
            },
        )
