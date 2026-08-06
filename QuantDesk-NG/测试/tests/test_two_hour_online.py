from __future__ import annotations

import pytest

from quantdesk.two_hour_online import compose_prediction
from quantdesk.two_hour_research import MultiHeadSelection


def _selection() -> MultiHeadSelection:
    return MultiHeadSelection(
        samples=748,
        precision=0.7553,
        coverage=0.027,
        direction_weight=0.5,
        terminal_weight=0.15,
        event_direction_weight=0.35,
        occurrence_weight=1.0,
        occurrence_threshold=0.75,
        confidence_threshold=0.72,
        target_precision=0.755,
    )


def test_two_hour_model_emits_direction_only_after_both_locked_gates() -> None:
    selected = compose_prediction(
        probability_up=0.82,
        event_probability=0.80,
        event_candidate=True,
        fresh=True,
        quality_score=1.0,
        barrier_bps=42,
        selection=_selection(),
    )
    abstained = compose_prediction(
        probability_up=0.82,
        event_probability=0.74,
        event_candidate=True,
        fresh=True,
        quality_score=1.0,
        barrier_bps=42,
        selection=_selection(),
    )

    assert selected["result"] == "long"
    assert selected["prediction_state"] == "calibrated"
    assert selected["selected"] is True
    assert abstained["result"] == "neutral"
    assert abstained["selected"] is False
    assert sum(
        selected[name]
        for name in ("long_probability", "short_probability", "neutral_probability")
    ) == pytest.approx(1.0)


def test_two_hour_model_abstains_for_stale_features() -> None:
    result = compose_prediction(
        probability_up=0.9,
        event_probability=0.9,
        event_candidate=True,
        fresh=False,
        quality_score=0.2,
        barrier_bps=10,
        selection=_selection(),
    )

    assert result["prediction_state"] == "data_insufficient"
    assert result["result"] == "neutral"
    assert result["neutral_probability"] == pytest.approx(0.8)
    assert result["target_bps"] == pytest.approx(20.0)
    assert "DATA_INSUFFICIENT" in result["reason_codes"]
