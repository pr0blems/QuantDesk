"""Deterministic baseline risk evaluator for the unified execution service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ..domain.execution import (
    ExecutionContext,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
)
from ..domain.trading import PositionDirection, PositionMode, PositionSide


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_open_positions: int = 20
    max_notional_to_equity: Decimal = Decimal("10")
    max_order_notional: Decimal | None = None
    max_order_quantity: Decimal | None = None
    allowed_symbols: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        for name in (
            "max_notional_to_equity",
            "max_order_notional",
            "max_order_quantity",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.allowed_symbols is not None:
            normalized = frozenset(str(symbol).strip().upper() for symbol in self.allowed_symbols)
            if "" in normalized:
                raise ValueError("allowed_symbols cannot contain an empty symbol")
            object.__setattr__(self, "allowed_symbols", normalized)


class DeterministicRiskEvaluator:
    """Conservative baseline checks before any broker-specific operation."""

    __slots__ = ("_clock", "_policy")

    def __init__(
        self,
        policy: RiskPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy or RiskPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def policy(self) -> RiskPolicy:
        """Expose the immutable policy used by deterministic evaluation."""

        return self._policy

    def evaluate(self, intent: OrderIntent, context: ExecutionContext) -> RiskDecision:
        reasons: list[str] = []
        policy = self._policy
        if policy.allowed_symbols is not None and intent.symbol not in policy.allowed_symbols:
            reasons.append("symbol_not_allowed")
        if context.account.can_trade is False:
            reasons.append("account_cannot_trade")
        if not intent.risk_reducing and context.account.wallet_balance <= 0:
            reasons.append("account_equity_unavailable")
        if (
            context.position_mode is PositionMode.ONE_WAY
            and intent.position_side is not PositionSide.BOTH
        ):
            reasons.append("position_mode_mismatch")
        if (
            context.position_mode is PositionMode.HEDGE
            and intent.position_side is PositionSide.BOTH
        ):
            reasons.append("position_mode_mismatch")

        try:
            approved_quantity = context.rules.quantity(intent.quantity)
        except ValueError:
            approved_quantity = None
            reasons.append("quantity_outside_instrument_rules")

        if approved_quantity is not None:
            notional = approved_quantity * context.quote.price
            if notional < context.rules.minimum_notional:
                reasons.append("minimum_notional_not_met")
            if policy.max_order_quantity is not None and approved_quantity > policy.max_order_quantity:
                reasons.append("order_quantity_limit")
            if policy.max_order_notional is not None and notional > policy.max_order_notional:
                reasons.append("order_notional_limit")
            if not intent.risk_reducing and context.account.wallet_balance > 0:
                current_exposure = _portfolio_notional(context)
                if current_exposure is None:
                    reasons.append("account_exposure_snapshot_incomplete")
                elif (
                    current_exposure + notional
                    > context.account.wallet_balance * policy.max_notional_to_equity
                ):
                    reasons.append("account_exposure_limit")
        else:
            notional = None

        if not intent.risk_reducing:
            open_positions = sum(position.quantity > 0 for position in context.account.positions)
            if open_positions >= policy.max_open_positions:
                reasons.append("position_count_limit")
        else:
            expected_direction = (
                PositionDirection.LONG
                if intent.side.value == "SELL"
                else PositionDirection.SHORT
            )
            matching_positions = tuple(
                position
                for position in context.account.positions
                if position.symbol == intent.symbol
                and position.direction is expected_direction
                and (
                    context.position_mode is PositionMode.ONE_WAY
                    or position.position_side is intent.position_side
                )
            )
            if not matching_positions:
                reasons.append("position_to_reduce_not_found")
            elif (
                context.position_mode is PositionMode.HEDGE
                and approved_quantity is not None
                and approved_quantity > sum(
                    (position.quantity for position in matching_positions),
                    start=Decimal(0),
                )
            ):
                reasons.append("reduce_quantity_exceeds_position")

        decided_at = self._clock()
        if reasons:
            return RiskDecision(
                intent_id=intent.intent_id,
                outcome=RiskOutcome.REJECTED,
                reason_codes=tuple(dict.fromkeys(reasons)),
                decided_at=decided_at,
                requested_quantity=intent.quantity,
                reference_price=context.quote.price,
            )
        return RiskDecision(
            intent_id=intent.intent_id,
            outcome=RiskOutcome.APPROVED,
            reason_codes=("approved",),
            decided_at=decided_at,
            requested_quantity=intent.quantity,
            approved_quantity=approved_quantity,
            reference_price=context.quote.price,
            notional=notional,
        )


def _portfolio_notional(context: ExecutionContext) -> Decimal | None:
    total = Decimal(0)
    for position in context.account.positions:
        if position.notional is not None:
            total += abs(position.notional)
            continue
        reference_price = position.mark_price or position.entry_price
        if reference_price is None:
            return None
        total += position.quantity * reference_price
    return total
