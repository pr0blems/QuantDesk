from __future__ import annotations

import pytest

import quantdesk_v2.backtest as backtest
from quantdesk_v2.strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    StrategyCandle,
    StrategyEvaluationError,
    resolve_legacy_strategy_timeframe,
    resolve_strategy_timing_policy,
    resolve_strategy_trigger_timeframe,
    strategy_timeframe_seconds,
)


def _candles(closes: list[float]) -> list[StrategyCandle]:
    return [
        StrategyCandle(
            ts=index * 3_600,
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=100,
        )
        for index, close in enumerate(closes)
    ]


def test_public_evaluator_preserves_legacy_ma_cross_signals() -> None:
    candles = _candles([10, 10, 10, 12, 8, 7])

    signals = DEFAULT_STRATEGY_EVALUATOR.evaluate_legacy(
        "ma_cross", candles, {"fast_period": 2, "slow_period": 3}
    )

    assert signals == [0, 0, 0, 1, 0, -1]
    assert backtest._Candle is StrategyCandle
    assert backtest._build_signals(
        "ma_cross", candles, {"fast_period": 2, "slow_period": 3}
    ) == signals


@pytest.mark.parametrize(
    ("snapshot", "config", "expected"),
    [
        ({}, {}, "4h"),
        ({"timeframe": "15m"}, {"strategy_timeframe": "1h"}, "15m"),
        ({"timeframes": {"trigger": "1h"}}, {}, "1h"),
        ({}, {"strategy_timeframe": "1h"}, "1h"),
        ({}, {"timeframe": "15m"}, "15m"),
    ],
)
def test_legacy_timeframe_resolution_is_explicit_and_backward_compatible(
    snapshot: dict, config: dict, expected: str
) -> None:
    assert resolve_legacy_strategy_timeframe(snapshot, config) == expected
    assert strategy_timeframe_seconds(expected) in {900, 3_600, 14_400}


@pytest.mark.parametrize("invalid", ["5m", "", None, 60])
def test_explicit_invalid_legacy_timeframe_fails_closed(invalid: object) -> None:
    with pytest.raises(StrategyEvaluationError, match="timeframe"):
        resolve_legacy_strategy_timeframe({"timeframe": invalid})


def test_unknown_legacy_engine_is_rejected() -> None:
    with pytest.raises(StrategyEvaluationError, match="unsupported legacy strategy"):
        DEFAULT_STRATEGY_EVALUATOR.evaluate_legacy("unknown", _candles([1, 2, 3]), {})


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {
                "strategy_kind": "source_strategy",
                "source_validation": {"trigger_timeframe": "1h"},
            },
            "1h",
        ),
        (
            {
                "strategy_kind": "source_strategy",
                "source_validation": {
                    "data_requirements": {"trigger_timeframe": "15m"}
                },
            },
            "15m",
        ),
        (
            {
                "strategy_kind": "full_strategy",
                "spec": {"timeframes": {"trigger": "4h"}},
            },
            "4h",
        ),
    ],
)
def test_trigger_timeframe_resolution_is_shared_by_all_strategy_kinds(
    snapshot: dict, expected: str
) -> None:
    assert resolve_strategy_trigger_timeframe(snapshot) == expected


def test_timing_policy_prefers_immutable_strategy_and_entry_values() -> None:
    snapshot = {
        "strategy_kind": "full_strategy",
        "spec": {
            "timeframes": {"trigger": "1h"},
            "exit": {"max_holding_bars": 24},
        },
    }

    policy = resolve_strategy_timing_policy(
        snapshot,
        {"max_holding_bars": 12},
        evidence={"risk_proposal": {"max_holding_bars": 6}},
    )

    assert policy.snapshot() == {
        "trigger_timeframe": "1h",
        "timeframe_seconds": 3_600,
        "max_holding_bars": 6,
        "max_holding_seconds": 21_600,
    }
    assert not policy.expired(opened_at=1_000, observed_at=22_599)
    assert policy.expired(opened_at=1_000, observed_at=22_600)


def test_captured_timing_policy_is_not_changed_by_account_edits() -> None:
    policy = resolve_strategy_timing_policy(
        {"strategy_kind": "legacy_signal", "timeframe": "4h"},
        {"max_holding_bars": 99},
        captured={
            "trigger_timeframe": "15m",
            "timeframe_seconds": 900,
            "max_holding_bars": 2,
        },
    )

    assert policy.trigger_timeframe == "15m"
    assert policy.max_holding_bars == 2
    assert policy.expired(opened_at=0, observed_at=1_800)


def test_timing_policy_matches_public_max_holding_range() -> None:
    snapshot = {"strategy_kind": "legacy_signal", "timeframe": "1h"}

    policy = resolve_strategy_timing_policy(
        snapshot,
        {"max_holding_bars": 50_000},
    )

    assert policy.max_holding_bars == 50_000
    with pytest.raises(StrategyEvaluationError, match="outside"):
        resolve_strategy_timing_policy(snapshot, {"max_holding_bars": 50_001})
