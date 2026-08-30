from __future__ import annotations

import pytest

from quantdesk_v2 import live_engine, paper_engine
from quantdesk_v2.domain.exit_policy import (
    EXIT_POLICY_VERSION,
    advance_profit_guard,
    evaluate_bar_exit,
    evaluate_mark_exit,
    resolve_exit_level_plan,
)


@pytest.mark.parametrize(
    ("direction", "expected_stop", "expected_target"),
    [(1, 97.0, 105.0), (-1, 103.0, 95.0)],
)
def test_atr_exit_plan_is_direction_symmetric(
    direction: int, expected_stop: float, expected_target: float
) -> None:
    plan = resolve_exit_level_plan(
        100,
        direction,
        stop_loss_pct=2,
        take_profit_pct=4,
        atr=2,
    )

    assert plan is not None
    assert plan.stop == pytest.approx(expected_stop)
    assert plan.target == pytest.approx(expected_target)
    assert plan.source == "atr"
    assert plan.snapshot()["version"] == EXIT_POLICY_VERSION


def test_strategy_risk_proposal_is_authoritative_across_execution_modes() -> None:
    config = {"stop_loss_pct": 2, "take_profit_pct": 4}
    evidence = {
        "risk_proposal": {"stop_distance": 7.5, "take_profit_distance": 12.5}
    }
    plan = resolve_exit_level_plan(
        100,
        -1,
        stop_loss_pct=2,
        take_profit_pct=4,
        atr=99,
        risk_proposal=evidence["risk_proposal"],
    )

    assert plan is not None
    expected = (107.5, 87.5)
    assert (plan.stop, plan.target) == expected
    assert paper_engine._signal_exit_levels(100, -1, 99, config, evidence) == expected
    assert live_engine._signal_exit_levels(100, -1, 99, config, evidence) == expected


def test_invalid_explicit_risk_proposal_fails_closed() -> None:
    plan = resolve_exit_level_plan(
        100,
        1,
        stop_loss_pct=2,
        take_profit_pct=4,
        risk_proposal={"stop_distance": -1, "take_profit_distance": 5},
    )

    assert plan is None

    impossible_short_target = resolve_exit_level_plan(
        100,
        -1,
        stop_loss_pct=2,
        take_profit_pct=4,
        risk_proposal={"stop_distance": 5, "take_profit_distance": 101},
    )
    assert impossible_short_target is None


def test_mark_exit_uses_loss_protection_before_take_profit() -> None:
    decision = evaluate_mark_exit(
        90,
        1,
        stop=95,
        target=105,
        liquidation=92,
    )

    assert decision is not None
    assert decision.reason == "liquidation"
    assert decision.price == 90


@pytest.mark.parametrize("direction", [1, -1])
def test_same_bar_stop_and_target_is_conservative_for_both_directions(
    direction: int,
) -> None:
    decision = evaluate_bar_exit(
        open_price=100,
        high=110,
        low=90,
        direction=direction,
        stop=95 if direction > 0 else 105,
        target=105 if direction > 0 else 95,
        liquidation=80 if direction > 0 else 120,
    )

    assert decision is not None
    assert decision.reason == "stop_loss"
    assert decision.price == (95 if direction > 0 else 105)


def test_bar_exit_prices_liquidation_gap_at_worse_open() -> None:
    decision = evaluate_bar_exit(
        open_price=75,
        high=78,
        low=70,
        direction=1,
        stop=90,
        target=110,
        liquidation=80,
    )

    assert decision is not None
    assert decision.reason == "liquidation"
    assert decision.price == 75


@pytest.mark.parametrize(
    ("direction", "favorable_mark", "reversal_mark", "expected_protected"),
    [(1, 110, 104.9, 105), (-1, 90, 95.1, 95)],
)
def test_profit_guard_is_direction_symmetric(
    direction: int,
    favorable_mark: float,
    reversal_mark: float,
    expected_protected: float,
) -> None:
    guard, should_exit = advance_profit_guard(
        entry_price=100,
        mark_price=favorable_mark,
        initial_stop=90 if direction > 0 else 110,
        direction=direction,
        previous=None,
        exit_cost_bps=16,
        observed_at=1,
    )

    assert guard is not None
    assert guard["protected_price"] == pytest.approx(expected_protected)
    assert should_exit is False

    persisted, should_exit = advance_profit_guard(
        entry_price=100,
        mark_price=reversal_mark,
        initial_stop=90 if direction > 0 else 110,
        direction=direction,
        previous=guard,
        exit_cost_bps=16,
        observed_at=2,
    )
    assert persisted == guard
    assert should_exit is True
