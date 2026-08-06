from __future__ import annotations

import pytest

from quantdesk_v2.optimization_governance import (
    build_consensus,
    evaluate_backtest_candidate,
    release_gate,
    validate_parameter_space,
)


def _space() -> dict:
    return {"fast_period": {"min": 5, "max": 15, "step": 5, "integer": True}}


def _metrics(**overrides: float) -> dict:
    values = {
        "total_return_pct": 12.0,
        "max_drawdown_pct": 4.0,
        "sharpe_ratio": 1.1,
        "trade_count": 24,
    }
    values.update(overrides)
    return values


def test_parameter_space_is_bounded_and_candidate_requires_real_metrics() -> None:
    space = validate_parameter_space(_space())
    accepted, evaluation, reason = evaluate_backtest_candidate(
        parameters={"fast_period": 10}, space=space, metrics=_metrics()
    )
    assert accepted is True
    assert reason is None
    assert evaluation["ranking_score"] == pytest.approx(19.0)

    accepted, _, reason = evaluate_backtest_candidate(
        parameters={"fast_period": 11}, space=space, metrics=_metrics()
    )
    assert accepted is False
    assert "approved step" in str(reason)

    accepted, _, reason = evaluate_backtest_candidate(
        parameters={"fast_period": 10}, space=space, metrics=_metrics(trade_count=9)
    )
    assert accepted is False
    assert "fewer than 10 trades" in str(reason)


def test_release_gate_never_approves_missing_or_collecting_metrics() -> None:
    passed, gate = release_gate(None)
    assert passed is False
    assert "no forward" in gate["reason"]

    passed, gate = release_gate({"status": "collecting", "directional_predictions": 999})
    assert passed is False
    assert "collecting" in gate["reason"]

    passed, gate = release_gate(
        {
            "status": "validated",
            "directional_predictions": 30,
            "directional_accuracy": 0.62,
            "brier_score": 0.18,
            "calibration_gap": 0.06,
        }
    )
    assert passed is True
    assert gate["reason"] == "passed"


def test_consensus_requires_independent_fresh_providers_and_can_observe() -> None:
    now = 1_000
    one_source = [
        {
            "provider_key": "market",
            "direction": "long",
            "probability": 0.9,
            "quality_score": 1,
            "as_of_ms": 900,
            "expires_at_ms": 1_100,
        }
    ]
    assert build_consensus(one_source, now_ms=now)["state"] == "insufficient_evidence"

    mixed = one_source + [
        {
            "provider_key": "news",
            "direction": "short",
            "probability": 0.88,
            "quality_score": 1,
            "as_of_ms": 900,
            "expires_at_ms": 1_100,
        }
    ]
    observed = build_consensus(mixed, now_ms=now)
    assert observed["state"] == "observe"
    assert observed["direction"] == "neutral"

    aligned = one_source + [
        {
            "provider_key": "news",
            "direction": "long",
            "probability": 0.8,
            "quality_score": 0.9,
            "as_of_ms": 900,
            "expires_at_ms": 1_100,
        }
    ]
    result = build_consensus(aligned, now_ms=now)
    assert result["state"] == "consensus"
    assert result["direction"] == "long"
