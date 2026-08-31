from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantdesk_v2 import backtest, shadow_worker
from quantdesk_v2.application.order_plans import build_entry_order_plan
from quantdesk_v2.application.semantic_replay import replay_mode_semantics
from quantdesk_v2.domain.execution import ExecutionMode
from quantdesk_v2.domain.runtime import build_decision_envelope, canonical_event_hash
from quantdesk_v2.strategy_evaluator import StrategyCandle, TimeframePolicy

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "cross_mode_semantics_v1.json").read_text(
        encoding="utf-8"
    )
)


class _ScriptedEvaluator:
    def __init__(self, signals: list[int], validity_seconds: int = 3_600) -> None:
        self.signals = signals
        self.validity_seconds = validity_seconds

    def evaluate_envelopes(
        self,
        _strategy_id,
        candles,
        _params,
        *,
        symbol,
        timeframe,
        revision_fingerprint,
    ):
        envelopes = []
        for candle, direction in zip(candles, self.signals, strict=True):
            event_time = datetime.fromtimestamp(candle.ts, tz=UTC)
            decision = {1: "LONG_ENTRY", -1: "SHORT_ENTRY"}.get(direction, "HOLD")
            envelopes.append(
                build_decision_envelope(
                    revision_fingerprint=revision_fingerprint,
                    event_id=canonical_event_hash(
                        {"symbol": symbol, "timeframe": timeframe, "ts": candle.ts}
                    ),
                    symbol=symbol,
                    timeframe=timeframe,
                    event_time=event_time,
                    decision=decision,
                    valid_until=event_time + timedelta(seconds=self.validity_seconds),
                )
            )
        return envelopes


def _fixture_candles(case: dict) -> list[StrategyCandle]:
    return [
        StrategyCandle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=100,
        )
        for row in case["candles"]
    ]


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


@pytest.mark.parametrize(
    "case",
    _FIXTURE["cases"],
    ids=[item["name"] for item in _FIXTURE["cases"]],
)
def test_fixed_golden_cases_match_backtest_paper_and_shadow(case: dict) -> None:
    candles = _fixture_candles(case)
    policy = TimeframePolicy("1h", 3_600, int(case["max_holding_bars"]))
    shared = {
        "strategy_id": "scripted_v1",
        "revision_fingerprint": "golden-revision-v1",
        "symbol": "TESTUSDT",
        "timeframe_policy": policy,
        "candles": candles,
        "params": {},
        "stop_loss_pct": float(case["stop_loss_pct"]),
        "take_profit_pct": float(case["take_profit_pct"]),
        "evaluator": _ScriptedEvaluator(case["signals"]),
    }
    results = {
        mode: replay_mode_semantics(mode, **shared)
        for mode in (
            ExecutionMode.BACKTEST,
            ExecutionMode.PAPER,
            ExecutionMode.SHADOW,
        )
    }

    assert len({item.semantic_signature for item in results.values()}) == 1
    oracle = results[ExecutionMode.BACKTEST]
    assert oracle.directions == tuple(case["signals"])
    assert oracle.exit_reasons == tuple(case["expected_exits"])

    backtest_result = backtest._run_engine(
        candles,
        {
            "strategy_id": "scripted_v1",
            "symbol": "TESTUSDT",
            "timeframe": "1h",
            "params": {},
            "initial_capital": 10_000,
            "position_size_pct": 10,
            "leverage": 2,
            "fee_bps": 0,
            "slippage_bps": 0,
            "stop_loss_pct": float(case["stop_loss_pct"]),
            "take_profit_pct": float(case["take_profit_pct"]),
            "max_holding_bars": int(case["max_holding_bars"]),
        },
        signals=list(case["signals"]),
        signal_valid_until=[candle.ts + 3_600 for candle in candles],
    )
    assert tuple(item["exit_reason"] for item in backtest_result["trades"]) == (
        oracle.exit_reasons
    )


def test_entry_plan_differs_by_delivery_mode_not_strategy_semantics() -> None:
    event_time = datetime(2026, 8, 31, 12, tzinfo=UTC)
    envelope = build_decision_envelope(
        revision_fingerprint="golden-revision-v1",
        event_id="golden-event-1",
        symbol="TESTUSDT",
        timeframe="1h",
        event_time=event_time,
        decision="SHORT_ENTRY",
        valid_until=event_time + timedelta(hours=1),
    )
    plans = {
        mode: build_entry_order_plan(
            envelope,
            mode=mode,
            quantity=Decimal("2"),
            reference_price=Decimal("100"),
            tenant_scope="tenant-1",
            user_scope="user-1",
            account_scope=f"{mode.value}-1",
            deployment_scope="deployment-1",
            created_at=event_time,
            config={"stop_loss_pct": 3, "take_profit_pct": 5},
        )
        for mode in (ExecutionMode.PAPER, ExecutionMode.SHADOW)
    }

    assert all(plan is not None for plan in plans.values())
    paper_plan = plans[ExecutionMode.PAPER]
    shadow_plan = plans[ExecutionMode.SHADOW]
    assert paper_plan is not None and shadow_plan is not None
    assert paper_plan.intent.side == shadow_plan.intent.side
    assert paper_plan.intent.timeframe == shadow_plan.intent.timeframe == "1h"
    assert paper_plan.exit_levels == shadow_plan.exit_levels
    assert paper_plan.intent.idempotency_key != shadow_plan.intent.idempotency_key
