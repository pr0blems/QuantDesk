"""Pure, exchange-neutral position exit calculations.

The functions in this module deliberately have no database, broker, or clock
dependencies.  Backtests, paper trading, and live trading can therefore use the
same direction validation, trigger ordering, and profit-protection arithmetic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

EXIT_POLICY_VERSION = "unified_exit_v1"
EXIT_DECISION_VERSION = "unified_exit_decision_v1"
PROFIT_GUARD_VERSION = "risk_unit_profit_guard_v1"

_EXIT_REASON_PRIORITY = {
    "liquidation": 10,
    "stop_loss": 20,
    "take_profit": 30,
    "live_profit_guard": 40,
    "profit_guard": 40,
    "strategy_reversal": 50,
    "max_holding_bars": 60,
    "end_of_data": 70,
}

_EXIT_REASON_SOURCE = {
    "liquidation": "exchange_risk",
    "stop_loss": "price_barrier",
    "take_profit": "price_barrier",
    "live_profit_guard": "profit_guard",
    "profit_guard": "profit_guard",
    "strategy_reversal": "strategy_signal",
    "max_holding_bars": "holding_policy",
    "end_of_data": "data_boundary",
}


@dataclass(frozen=True, slots=True)
class ExitLevelPlan:
    """Immutable stop/target levels captured when a position opens."""

    entry_price: float
    direction: int
    stop: float
    target: float
    source: str

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop)

    @property
    def reward_distance(self) -> float:
        return abs(self.target - self.entry_price)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": EXIT_POLICY_VERSION,
            "source": self.source,
            "entry_price": self.entry_price,
            "direction": self.direction,
            "stop": self.stop,
            "target": self.target,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
        }


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """One deterministic exit selected from a market observation."""

    price: float
    reason: str
    priority: int
    source: str
    observed_at: int | None = None

    def snapshot(
        self,
        *,
        mode: str | None = None,
        execution_price: Any = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": EXIT_DECISION_VERSION,
            "policy_version": EXIT_POLICY_VERSION,
            "reason": self.reason,
            "source": self.source,
            "priority": self.priority,
            "trigger_price": self.price,
            "observed_at": self.observed_at,
        }
        if mode is not None:
            payload["mode"] = str(mode).strip().lower()
        parsed_execution = _finite_positive(execution_price)
        if parsed_execution is not None:
            payload["execution_price"] = parsed_execution
        return payload


@dataclass(frozen=True, slots=True)
class ExitDecisionComparison:
    """Normalized comparison between two execution-mode decisions."""

    reason_matches: bool
    priority_matches: bool
    trigger_price_delta_bps: float | None

    @property
    def matches(self) -> bool:
        return (
            self.reason_matches
            and self.priority_matches
            and (
                self.trigger_price_delta_bps is None
                or self.trigger_price_delta_bps <= 0.01
            )
        )


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    """Single exit-policy facade shared by replay, paper, shadow and live."""

    version: str = EXIT_POLICY_VERSION
    decision_version: str = EXIT_DECISION_VERSION

    def resolve_levels(
        self,
        entry_price: Any,
        direction: int,
        *,
        stop_loss_pct: Any,
        take_profit_pct: Any,
        atr: Any = None,
        risk_proposal: Mapping[str, Any] | None = None,
        atr_stop_multiplier: float = 1.5,
        atr_take_profit_multiplier: float = 2.5,
    ) -> ExitLevelPlan | None:
        return resolve_exit_level_plan(
            entry_price,
            direction,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            atr=atr,
            risk_proposal=risk_proposal,
            atr_stop_multiplier=atr_stop_multiplier,
            atr_take_profit_multiplier=atr_take_profit_multiplier,
        )

    def decision_for_reason(
        self,
        reason: str,
        price: Any,
        *,
        observed_at: int | None = None,
    ) -> ExitDecision | None:
        return decision_for_reason(reason, price, observed_at=observed_at)

    def evaluate_mark(
        self,
        mark_price: Any,
        direction: int,
        *,
        stop: Any = None,
        target: Any = None,
        liquidation: Any = None,
        observed_at: int | None = None,
    ) -> ExitDecision | None:
        return evaluate_mark_exit(
            mark_price,
            direction,
            stop=stop,
            target=target,
            liquidation=liquidation,
            observed_at=observed_at,
        )

    def evaluate_bar(
        self,
        *,
        open_price: Any,
        high: Any,
        low: Any,
        direction: int,
        stop: Any = None,
        target: Any = None,
        liquidation: Any = None,
        observed_at: int | None = None,
    ) -> ExitDecision | None:
        return evaluate_bar_exit(
            open_price=open_price,
            high=high,
            low=low,
            direction=direction,
            stop=stop,
            target=target,
            liquidation=liquidation,
            observed_at=observed_at,
        )

    def select(
        self,
        *,
        price: Any,
        observed_at: int | None = None,
        market_decision: ExitDecision | None = None,
        profit_guard_exit: bool = False,
        strategy_reversal: bool = False,
        holding_period_expired: bool = False,
    ) -> ExitDecision | None:
        return select_runtime_exit(
            price=price,
            observed_at=observed_at,
            market_decision=market_decision,
            profit_guard_exit=profit_guard_exit,
            strategy_reversal=strategy_reversal,
            holding_period_expired=holding_period_expired,
        )

    def advance_profit_guard(self, **kwargs: Any) -> tuple[dict[str, Any] | None, bool]:
        return advance_profit_guard(**kwargs)


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def decision_for_reason(
    reason: str,
    price: Any,
    *,
    observed_at: int | None = None,
) -> ExitDecision | None:
    """Build a standard decision without changing an engine's chosen reason."""

    normalized_reason = str(reason or "").strip().lower()
    parsed_price = _finite_positive(price)
    if not normalized_reason or parsed_price is None:
        return None
    return ExitDecision(
        price=parsed_price,
        reason=normalized_reason,
        priority=_EXIT_REASON_PRIORITY.get(normalized_reason, 90),
        source=_EXIT_REASON_SOURCE.get(normalized_reason, "engine_control"),
        observed_at=int(observed_at) if observed_at is not None else None,
    )


def select_runtime_exit(
    *,
    price: Any,
    observed_at: int | None = None,
    market_decision: ExitDecision | None = None,
    profit_guard_exit: bool = False,
    strategy_reversal: bool = False,
    holding_period_expired: bool = False,
) -> ExitDecision | None:
    """Select one exit using the common risk-first runtime priority."""

    if market_decision is not None:
        return market_decision
    if profit_guard_exit:
        return decision_for_reason("profit_guard", price, observed_at=observed_at)
    if strategy_reversal:
        return decision_for_reason(
            "strategy_reversal", price, observed_at=observed_at
        )
    if holding_period_expired:
        return decision_for_reason(
            "max_holding_bars", price, observed_at=observed_at
        )
    return None


def compare_exit_decisions(
    reference: ExitDecision | None,
    candidate: ExitDecision | None,
) -> ExitDecisionComparison:
    """Compare mode decisions without treating two missing exits as a match."""

    if reference is None or candidate is None:
        return ExitDecisionComparison(False, False, None)
    price_base = max(abs(reference.price), abs(candidate.price))
    price_delta_bps = (
        abs(reference.price - candidate.price) / price_base * 10_000
        if price_base > 0
        else None
    )
    return ExitDecisionComparison(
        reason_matches=reference.reason == candidate.reason,
        priority_matches=reference.priority == candidate.priority,
        trigger_price_delta_bps=price_delta_bps,
    )


def resolve_exit_level_plan(
    entry_price: Any,
    direction: int,
    *,
    stop_loss_pct: Any,
    take_profit_pct: Any,
    atr: Any = None,
    risk_proposal: Mapping[str, Any] | None = None,
    atr_stop_multiplier: float = 1.5,
    atr_take_profit_multiplier: float = 2.5,
) -> ExitLevelPlan | None:
    """Resolve a direction-safe stop and target from one ordered policy.

    A strategy risk proposal is authoritative when present.  Otherwise ATR is
    preferred, with configured percentages as the deterministic fallback.
    Invalid explicit proposals fail closed instead of silently changing risk.
    """

    entry = _finite_positive(entry_price)
    if entry is None or direction not in {-1, 1}:
        return None

    source = "configured_percentage"
    if risk_proposal is not None:
        stop_distance = _finite_positive(risk_proposal.get("stop_distance"))
        reward_distance = _finite_positive(
            risk_proposal.get("take_profit_distance")
        )
        if stop_distance is None or reward_distance is None:
            return None
        source = "strategy_risk_proposal"
    else:
        atr_value = _finite_positive(atr)
        if atr_value is not None:
            stop_distance = _finite_positive(atr_value * atr_stop_multiplier)
            reward_distance = _finite_positive(
                atr_value * atr_take_profit_multiplier
            )
            source = "atr"
        else:
            stop_pct = _finite_positive(stop_loss_pct)
            reward_pct = _finite_positive(take_profit_pct)
            if stop_pct is None or reward_pct is None:
                return None
            stop_distance = entry * stop_pct / 100
            reward_distance = entry * reward_pct / 100

    if stop_distance is None or reward_distance is None:
        return None
    stop = entry - direction * stop_distance
    target = entry + direction * reward_distance
    if not (
        math.isfinite(stop)
        and math.isfinite(target)
        and stop > 0
        and target > 0
        and (entry - stop) * direction > 0
        and (target - entry) * direction > 0
    ):
        if source == "strategy_risk_proposal":
            return None
        # A very large ATR can make a short target non-positive.  Preserve the
        # ATR stop when valid, but use the configured percentage for any invalid
        # leg exactly as the legacy engines did.
        stop_pct = _finite_positive(stop_loss_pct)
        reward_pct = _finite_positive(take_profit_pct)
        if stop <= 0 or not math.isfinite(stop) or (entry - stop) * direction <= 0:
            stop = entry - direction * entry * stop_pct / 100 if stop_pct else 0
        if (
            target <= 0
            or not math.isfinite(target)
            or (target - entry) * direction <= 0
        ):
            target = (
                entry + direction * entry * reward_pct / 100 if reward_pct else 0
            )
        if not (
            math.isfinite(stop)
            and math.isfinite(target)
            and stop > 0
            and target > 0
            and (entry - stop) * direction > 0
            and (target - entry) * direction > 0
        ):
            return None
    return ExitLevelPlan(entry, direction, stop, target, source)


def evaluate_mark_exit(
    mark_price: Any,
    direction: int,
    *,
    stop: Any = None,
    target: Any = None,
    liquidation: Any = None,
    observed_at: int | None = None,
) -> ExitDecision | None:
    """Evaluate one mark with loss protection taking precedence over profit."""

    mark = _finite_positive(mark_price)
    if mark is None or direction not in {-1, 1}:
        return None
    liquidation_price = _finite_positive(liquidation)
    stop_price = _finite_positive(stop)
    target_price = _finite_positive(target)
    if liquidation_price is not None and (
        mark <= liquidation_price if direction > 0 else mark >= liquidation_price
    ):
        return decision_for_reason("liquidation", mark, observed_at=observed_at)
    if stop_price is not None and (
        mark <= stop_price if direction > 0 else mark >= stop_price
    ):
        return decision_for_reason("stop_loss", mark, observed_at=observed_at)
    if target_price is not None and (
        mark >= target_price if direction > 0 else mark <= target_price
    ):
        return decision_for_reason("take_profit", mark, observed_at=observed_at)
    return None


def evaluate_bar_exit(
    *,
    open_price: Any,
    high: Any,
    low: Any,
    direction: int,
    stop: Any = None,
    target: Any = None,
    liquidation: Any = None,
    observed_at: int | None = None,
) -> ExitDecision | None:
    """Evaluate an OHLC bar using conservative, direction-symmetric ordering."""

    opened = _finite_positive(open_price)
    bar_high = _finite_positive(high)
    bar_low = _finite_positive(low)
    if (
        opened is None
        or bar_high is None
        or bar_low is None
        or direction not in {-1, 1}
    ):
        return None
    liquidation_price = _finite_positive(liquidation)
    stop_price = _finite_positive(stop)
    target_price = _finite_positive(target)

    if liquidation_price is not None and (
        opened <= liquidation_price if direction > 0 else opened >= liquidation_price
    ):
        return decision_for_reason("liquidation", opened, observed_at=observed_at)

    stop_hit = stop_price is not None and (
        bar_low <= stop_price if direction > 0 else bar_high >= stop_price
    )
    liquidation_hit = liquidation_price is not None and (
        bar_low <= liquidation_price if direction > 0 else bar_high >= liquidation_price
    )
    target_hit = target_price is not None and (
        bar_high >= target_price if direction > 0 else bar_low <= target_price
    )

    adverse_price: float | None = None
    adverse_reason: str | None = None
    if stop_hit and liquidation_hit:
        stop_is_first = (
            stop_price > liquidation_price
            if direction > 0
            else stop_price < liquidation_price
        )
        adverse_price = stop_price if stop_is_first else liquidation_price
        adverse_reason = "stop_loss" if stop_is_first else "liquidation"
    elif liquidation_hit:
        adverse_price = liquidation_price
        adverse_reason = "liquidation"
    elif stop_hit:
        adverse_price = stop_price
        adverse_reason = "stop_loss"

    if adverse_price is not None and adverse_reason is not None:
        price = (
            min(opened, adverse_price)
            if direction > 0
            else max(opened, adverse_price)
        )
        return decision_for_reason(adverse_reason, price, observed_at=observed_at)
    if target_hit and target_price is not None:
        price = (
            max(opened, target_price)
            if direction > 0
            else min(opened, target_price)
        )
        return decision_for_reason("take_profit", price, observed_at=observed_at)
    return None


def advance_profit_guard(
    *,
    entry_price: Any,
    mark_price: Any,
    initial_stop: Any,
    direction: int,
    previous: Mapping[str, Any] | None,
    exit_cost_bps: Any,
    observed_at: int,
    activation_r: Any = 0.5,
    trailing_activation_r: Any = 1.0,
    maximum_giveback_r: Any = 0.5,
    minimum_protected_r: Any = 0.0,
    cost_buffer_bps: float = 2.0,
    peak_write_step_r: float = 0.1,
) -> tuple[dict[str, Any] | None, bool]:
    """Advance a causal R-based cost lock/trailing guard."""

    entry = _finite_positive(entry_price)
    mark = _finite_positive(mark_price)
    stop = _finite_positive(initial_stop)
    if entry is None or mark is None or stop is None or direction not in {-1, 1}:
        return None, False
    risk_distance = abs(entry - stop)
    if risk_distance <= 0 or direction * (entry - stop) <= 0:
        return None, False

    def bounded(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(parsed, maximum))

    activation = bounded(activation_r, 0.5, 0.25, 2.0)
    trailing_activation = max(
        activation, bounded(trailing_activation_r, 1.0, 0.5, 4.0)
    )
    giveback = bounded(maximum_giveback_r, 0.5, 0.1, 1.5)
    protected_r = bounded(minimum_protected_r, 0.0, 0.0, 1.0)
    try:
        parsed_costs = float(exit_cost_bps)
    except (TypeError, ValueError, OverflowError):
        return None, False
    costs = max(16.0, parsed_costs) if math.isfinite(parsed_costs) else 16.0
    prior = dict(previous) if isinstance(previous, Mapping) else {}
    prior_peak = _finite_positive(prior.get("peak_price")) or entry
    if direction * (prior_peak - entry) < 0:
        prior_peak = entry
    observed_peak = max(prior_peak, mark) if direction > 0 else min(prior_peak, mark)
    favorable_distance = max(0.0, direction * (observed_peak - entry))
    peak_r = favorable_distance / risk_distance
    if peak_r < activation:
        return None, False

    cost_lock_distance = entry * (costs + cost_buffer_bps) / 10_000
    protected_distance = max(cost_lock_distance, protected_r * risk_distance)
    if peak_r >= trailing_activation:
        protected_distance = max(
            protected_distance, favorable_distance - giveback * risk_distance
        )
    protected_price = entry + direction * protected_distance
    should_exit = direction * (mark - protected_price) <= 0

    persisted_peak = _finite_positive(prior.get("peak_price")) or entry
    try:
        persisted_peak_r = float(prior.get("peak_r", 0.0))
    except (TypeError, ValueError, OverflowError):
        persisted_peak_r = 0.0
    threshold_crossed = (
        persisted_peak_r < activation <= peak_r
        or persisted_peak_r < trailing_activation <= peak_r
    )
    peak_advanced = direction * (observed_peak - persisted_peak) >= (
        risk_distance * peak_write_step_r
    )
    if prior and not threshold_crossed and not peak_advanced:
        persisted_protected = _finite_positive(prior.get("protected_price"))
        if persisted_protected is not None:
            should_exit = direction * (mark - persisted_protected) <= 0
        return prior, should_exit

    return (
        {
            "version": PROFIT_GUARD_VERSION,
            "entry_price": entry,
            "initial_stop": stop,
            "risk_distance": risk_distance,
            "peak_price": observed_peak,
            "peak_r": round(peak_r, 8),
            "protected_price": protected_price,
            "activation_r": activation,
            "trailing_activation_r": trailing_activation,
            "giveback_r": giveback,
            "cost_floor_bps": costs + cost_buffer_bps,
            "observed_at": int(observed_at),
        },
        should_exit,
    )


DEFAULT_EXIT_POLICY = ExitPolicy()
