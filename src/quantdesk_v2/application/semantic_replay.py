"""Cross-mode semantic oracle for deterministic strategy acceptance tests.

This module deliberately stops before order sizing, fees, broker adapters and
network writes.  Its only job is to prove that every execution mode interprets
the same immutable strategy revision and candle history as the same decisions
and exit reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..domain.execution import ExecutionMode
from ..domain.exit_policy import DEFAULT_EXIT_POLICY, ExitDecision, ExitPolicy
from ..domain.runtime import DecisionEnvelope, build_decision_envelope, canonical_event_hash
from ..strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    StrategyCandle,
    StrategyEvaluator,
    TimeframePolicy,
)


@dataclass(frozen=True, slots=True)
class SemanticReplayResult:
    mode: ExecutionMode
    decisions: tuple[DecisionEnvelope, ...]
    exits: tuple[DecisionEnvelope, ...]

    @property
    def directions(self) -> tuple[int, ...]:
        return tuple(item.direction for item in self.decisions)

    @property
    def exit_reasons(self) -> tuple[str, ...]:
        return tuple(
            str((item.exit_decision or {}).get("reason") or "")
            for item in self.exits
        )

    @property
    def semantic_signature(self) -> tuple[tuple[int, ...], tuple[str, ...]]:
        return self.directions, self.exit_reasons

    def delivery_snapshots(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            item.snapshot(mode=self.mode.value)
            for item in (*self.decisions, *self.exits)
        )


def _exit_envelope(
    *,
    reference: DecisionEnvelope,
    candle: StrategyCandle,
    exit_decision: ExitDecision,
) -> DecisionEnvelope:
    event_time = datetime.fromtimestamp(int(candle.ts), tz=UTC)
    reason = exit_decision.reason
    return build_decision_envelope(
        revision_fingerprint=reference.revision_fingerprint,
        event_id=canonical_event_hash(
            {
                "type": "position_exit",
                "source_event_id": reference.event_id,
                "symbol": reference.symbol,
                "timeframe": reference.timeframe,
                "event_time": event_time.isoformat(),
                "reason": reason,
            }
        ),
        symbol=reference.symbol,
        timeframe=reference.timeframe,
        event_time=event_time,
        decision="EXIT",
        reason_codes=(reason,),
        evidence={"source_decision_id": reference.decision_id},
        exit_decision=exit_decision.snapshot(),
    )


def replay_mode_semantics(
    mode: ExecutionMode | str,
    *,
    strategy_id: str,
    revision_fingerprint: str,
    symbol: str,
    timeframe_policy: TimeframePolicy,
    candles: list[StrategyCandle],
    params: dict[str, int | float],
    stop_loss_pct: float,
    take_profit_pct: float,
    evaluator: StrategyEvaluator = DEFAULT_STRATEGY_EVALUATOR,
    exit_policy: ExitPolicy = DEFAULT_EXIT_POLICY,
) -> SemanticReplayResult:
    """Replay one mode through the shared strategy and exit semantics."""

    execution_mode = ExecutionMode(mode)
    if not candles:
        return SemanticReplayResult(execution_mode, (), ())
    decisions = evaluator.evaluate_envelopes(
        strategy_id,
        candles,
        params,
        symbol=symbol,
        timeframe=timeframe_policy.trigger_timeframe,
        revision_fingerprint=revision_fingerprint,
    )
    pending_direction = 0
    pending_reference: DecisionEnvelope | None = None
    position: dict[str, Any] | None = None
    exits: list[DecisionEnvelope] = []

    for candle, envelope in zip(candles, decisions, strict=True):
        if pending_direction in {-1, 1} and pending_reference is not None:
            if position is not None and position["direction"] != pending_direction:
                selected = exit_policy.decision_for_reason(
                    "strategy_reversal", candle.open, observed_at=candle.ts
                )
                if selected is not None:
                    exits.append(
                        _exit_envelope(
                            reference=pending_reference,
                            candle=candle,
                            exit_decision=selected,
                        )
                    )
                position = None
            if position is None:
                levels = exit_policy.resolve_levels(
                    candle.open,
                    pending_direction,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                position = {
                    "direction": pending_direction,
                    "holding_bars": 0,
                    "stop": levels.stop if levels is not None else None,
                    "target": levels.target if levels is not None else None,
                    "reference": pending_reference,
                }

        if position is not None:
            position["holding_bars"] += 1
            market_exit = exit_policy.evaluate_bar(
                open_price=candle.open,
                high=candle.high,
                low=candle.low,
                direction=position["direction"],
                stop=position["stop"],
                target=position["target"],
                observed_at=candle.ts,
            )
            selected = exit_policy.select(
                price=candle.close,
                observed_at=candle.ts,
                market_decision=market_exit,
                holding_period_expired=(
                    timeframe_policy.max_holding_bars > 0
                    and position["holding_bars"]
                    >= timeframe_policy.max_holding_bars
                ),
            )
            if selected is not None:
                exits.append(
                    _exit_envelope(
                        reference=position["reference"],
                        candle=candle,
                        exit_decision=selected,
                    )
                )
                position = None

        pending_direction = envelope.direction
        pending_reference = envelope

    if position is not None:
        final_candle = candles[-1]
        selected = exit_policy.decision_for_reason(
            "end_of_data", final_candle.close, observed_at=final_candle.ts
        )
        if selected is not None:
            exits.append(
                _exit_envelope(
                    reference=position["reference"],
                    candle=final_candle,
                    exit_decision=selected,
                )
            )

    return SemanticReplayResult(
        execution_mode,
        tuple(decisions),
        tuple(exits),
    )
