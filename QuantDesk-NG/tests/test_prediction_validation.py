from __future__ import annotations

import pytest

from quantdesk.prediction_validation import summarize_rows


def _row(*, result: str, actual: str, probability: float, completed_at: int, net: float = 0) -> dict:
    return {
        "model_key": "test-model",
        "model_version": 1,
        "horizon_seconds": 14_400,
        "result": result,
        "actual_result": actual,
        "long_probability": probability if result == "long" else 0.1,
        "short_probability": probability if result == "short" else 0.1,
        "directional_return_bps": net,
        "completed_at_ms": completed_at,
    }


def test_validation_counts_neutral_outcomes_as_failed_directional_calls() -> None:
    rows = [
        _row(result="long", actual="long", probability=0.8, completed_at=200, net=18),
        _row(result="short", actual="neutral", probability=0.7, completed_at=201, net=-12),
        _row(result="neutral", actual="neutral", probability=0.8, completed_at=202),
    ]

    result = summarize_rows(rows, window_start_ms=100, evaluated_until_ms=300)

    assert len(result) == 1
    metric = result[0]
    assert metric["completed_outcomes"] == 3
    assert metric["directional_predictions"] == 2
    assert metric["correct_directional"] == 1
    assert metric["coverage_ratio"] == pytest.approx(2 / 3)
    assert metric["directional_accuracy"] == 0.5
    assert metric["brier_score"] == 0.265
    assert metric["mean_net_return_bps"] == 3.0
    assert metric["status"] == "collecting"


def test_validation_excludes_labels_completed_outside_replay_window() -> None:
    rows = [
        _row(result="long", actual="long", probability=0.9, completed_at=99),
        _row(result="long", actual="long", probability=0.9, completed_at=100),
        _row(result="long", actual="long", probability=0.9, completed_at=301),
    ]

    result = summarize_rows(rows, window_start_ms=100, evaluated_until_ms=300)

    assert result[0]["completed_outcomes"] == 1
    assert result[0]["last_completed_at_ms"] == 100
