from __future__ import annotations

from quantdesk_v2 import backtest, shadow_worker
from quantdesk_v2.application.semantic_replay import replay_mode_semantics
from quantdesk_v2.domain.execution import ExecutionMode
from quantdesk_v2.strategy_evaluator import StrategyCandle, TimeframePolicy


def _history() -> list[StrategyCandle]:
    closes = [10, 10, 10, 12, 8, 7, 9]
    return [
        StrategyCandle(
            ts=index * 3_600,
            open=close,
            high=close + 0.25,
            low=close - 0.25,
            close=close,
            volume=100,
        )
        for index, close in enumerate(closes)
    ]


def test_same_history_has_identical_direction_and_exit_semantics() -> None:
    candles = _history()
    policy = TimeframePolicy("1h", 3_600, 0)
    shared = {
        "strategy_id": "ma_cross",
        "revision_fingerprint": "revision-ma-cross-v1",
        "symbol": "TESTUSDT",
        "timeframe_policy": policy,
        "candles": candles,
        "params": {"fast_period": 2, "slow_period": 3},
        "stop_loss_pct": 50,
        "take_profit_pct": 50,
    }

    results = {
        mode: replay_mode_semantics(mode, **shared)
        for mode in (
            ExecutionMode.BACKTEST,
            ExecutionMode.PAPER,
            ExecutionMode.SHADOW,
        )
    }

    signatures = {item.semantic_signature for item in results.values()}
    assert len(signatures) == 1
    assert results[ExecutionMode.BACKTEST].exit_reasons == (
        "strategy_reversal",
        "end_of_data",
    )
    decision_hashes = {
        tuple(decision.semantic_hash for decision in item.decisions)
        for item in results.values()
    }
    assert len(decision_hashes) == 1
    for mode, result in results.items():
        assert all(
            snapshot["mode"] == mode.value
            for snapshot in result.delivery_snapshots()
        )


def test_shadow_timeframe_uses_the_shared_policy() -> None:
    assert shadow_worker._timeframe(
        {
            "strategy_kind": "source_strategy",
            "source_validation": {"trigger_timeframe": "15m"},
            "risk_defaults": {"max_holding_bars": 8},
        }
    ) == "15m"


def test_backtest_engine_matches_the_cross_mode_semantic_oracle() -> None:
    candles = _history()
    oracle = replay_mode_semantics(
        ExecutionMode.BACKTEST,
        strategy_id="ma_cross",
        revision_fingerprint="revision-ma-cross-v1",
        symbol="TESTUSDT",
        timeframe_policy=TimeframePolicy("1h", 3_600, 0),
        candles=candles,
        params={"fast_period": 2, "slow_period": 3},
        stop_loss_pct=50,
        take_profit_pct=50,
    )
    result = backtest._run_engine(
        candles,
        {
            "strategy_id": "ma_cross",
            "symbol": "TESTUSDT",
            "timeframe": "1h",
            "params": {"fast_period": 2, "slow_period": 3},
            "initial_capital": 10_000,
            "position_size_pct": 10,
            "leverage": 2,
            "fee_bps": 0,
            "slippage_bps": 0,
            "stop_loss_pct": 50,
            "take_profit_pct": 50,
            "max_holding_bars": 0,
        },
    )

    assert tuple(item["exit_reason"] for item in result["trades"]) == (
        oracle.exit_reasons
    )
