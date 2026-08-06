from __future__ import annotations

import pytest

import quantdesk_v2.backtest as backtest
from quantdesk_v2.strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    StrategyCandle,
    StrategyEvaluationError,
    resolve_legacy_strategy_timeframe,
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
