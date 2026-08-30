"""Deterministic conversion from a strategy decision to one order intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.execution import ExecutionMode, IntentAction, OrderIntent
from ..domain.exit_policy import DEFAULT_EXIT_POLICY, ExitLevelPlan
from ..domain.runtime import DecisionEnvelope, StrategyDecisionType, canonical_event_hash
from ..domain.trading import OrderSide


@dataclass(frozen=True, slots=True)
class EntryOrderPlan:
    """Exactly one entry intent plus the immutable exit levels it establishes."""

    decision_id: str
    intent: OrderIntent
    exit_levels: ExitLevelPlan | None

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent.intent_id,
            "idempotency_key": self.intent.idempotency_key,
            "decision_id": self.decision_id,
            "side": self.intent.side.value,
            "quantity": str(self.intent.quantity),
            "timeframe": self.intent.timeframe,
            "mode": self.intent.mode.value,
            "exit_levels": (
                self.exit_levels.snapshot() if self.exit_levels is not None else None
            ),
        }


def build_entry_order_plan(
    envelope: DecisionEnvelope,
    *,
    mode: ExecutionMode,
    quantity: Decimal,
    reference_price: Decimal,
    tenant_scope: str,
    user_scope: str,
    account_scope: str,
    deployment_scope: str,
    created_at: datetime,
    config: Mapping[str, Any] | None = None,
    market: str = "binance_usdm",
    atr: float | None = None,
) -> EntryOrderPlan | None:
    """Build a stable entry request; HOLD/SKIP/EXIT never create an entry intent."""

    if envelope.decision not in {
        StrategyDecisionType.LONG_ENTRY,
        StrategyDecisionType.SHORT_ENTRY,
    }:
        return None
    valid_until = envelope.valid_until
    if valid_until is None or valid_until <= created_at:
        return None
    direction = envelope.direction
    side = OrderSide.BUY if direction > 0 else OrderSide.SELL
    normalized_mode = ExecutionMode(mode)
    key = f"{normalized_mode.value}:{deployment_scope}:{envelope.decision_id}"
    intent_hash = canonical_event_hash(
        {
            "mode": normalized_mode.value,
            "deployment": deployment_scope,
            "decision_id": envelope.decision_id,
            "action": IntentAction.OPEN.value,
        }
    )
    intent = OrderIntent(
        intent_id=f"intent-{intent_hash}",
        idempotency_key=key,
        strategy_version_id=envelope.revision_fingerprint,
        tenant_scope=tenant_scope,
        user_scope=user_scope,
        account_scope=account_scope,
        deployment_scope=deployment_scope,
        mode=normalized_mode,
        market=market,
        symbol=envelope.symbol,
        timeframe=envelope.timeframe,
        action=IntentAction.OPEN,
        side=side,
        quantity=quantity,
        signal_time=envelope.event_time,
        valid_until=valid_until,
        created_at=created_at,
    )
    settings = dict(config or {})
    exit_levels = DEFAULT_EXIT_POLICY.resolve_levels(
        reference_price,
        direction,
        stop_loss_pct=_decimal(settings.get("stop_loss_pct"), "3"),
        take_profit_pct=_decimal(settings.get("take_profit_pct"), "5"),
        atr=atr,
        risk_proposal=envelope.risk_proposal or None,
    )
    return EntryOrderPlan(
        decision_id=envelope.decision_id,
        intent=intent,
        exit_levels=exit_levels,
    )


def _decimal(value: Any, fallback: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal(fallback)
    if not parsed.is_finite() or parsed <= 0:
        return Decimal(fallback)
    return parsed
