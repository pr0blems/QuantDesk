"""Deterministic Shadow transition logic for Martingale TP4.

Tiger remains the signal fact source. Binance bid/ask remains the execution,
mark-to-market, and exit fact source. This module performs no database or
broker I/O and can therefore be replayed after a process restart.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...domain.martingale_tp4 import MartingaleTp4Config
from ...domain.martingale_tp4_engine import (
    BasketLeg,
    BasketSnapshot,
    DecisionAction,
    Direction,
    EnginePolicy,
    MarketTick,
    StrategyDecision,
    evaluate_risk_reducing_decision,
    evaluate_tick,
)
from .market_gate import (
    BinanceExecutionQuote,
    MarketDataGateDecision,
    TigerReferenceQuote,
    evaluate_market_data_gate,
)

SHADOW_ENGINE_VERSION = "martingale_tp4_shadow_v1"
_TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ShadowFill:
    action: str
    leg_index: int
    direction: Direction
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class ShadowEvaluation:
    idempotency_key: str
    source_decision: StrategyDecision
    decision: StrategyDecision
    market_gate: MarketDataGateDecision
    marked_basket: BasketSnapshot


@dataclass(frozen=True, slots=True)
class ShadowTransition:
    evaluation: ShadowEvaluation
    basket_before: BasketSnapshot
    basket_after: BasketSnapshot
    fills: tuple[ShadowFill, ...]
    realized_pnl_delta: Decimal

    def audit_payload(self) -> dict[str, Any]:
        return {
            "engine_version": SHADOW_ENGINE_VERSION,
            "idempotency_key": self.evaluation.idempotency_key,
            "source_decision": decision_payload(self.evaluation.source_decision),
            "decision": decision_payload(self.evaluation.decision),
            "market_gate": market_gate_payload(self.evaluation.market_gate),
            "basket_before": basket_payload(self.basket_before),
            "basket_after": basket_payload(self.basket_after),
            "fills": [fill_payload(item) for item in self.fills],
            "realized_pnl_delta": str(self.realized_pnl_delta),
            "network_write": False,
        }


def shadow_idempotency_key(
    *,
    deployment_scope: str,
    contract_symbol: str,
    event_id: str,
) -> str:
    """Build a bounded, stable key from an upstream event identity."""

    scope = str(deployment_scope).strip()
    symbol = str(contract_symbol).strip().upper()
    event = str(event_id).strip()
    if not scope or not symbol or not event:
        raise ValueError("deployment_scope, contract_symbol, and event_id are required")
    digest = hashlib.sha256(f"{scope}\x1f{symbol}\x1f{event}".encode()).hexdigest()
    return f"mtp4-shadow:{digest}"


def mark_basket(
    basket: BasketSnapshot,
    quote: BinanceExecutionQuote,
) -> BasketSnapshot:
    """Mark each active leg against its executable closing side."""

    legs = tuple(
        replace(
            leg,
            unrealized_pnl=(
                (quote.bid - leg.entry_price) * leg.quantity
                if leg.direction == Direction.BUY
                else (leg.entry_price - quote.ask) * leg.quantity
            ),
        )
        for leg in basket.legs
    )
    favorable_high = basket.favorable_high
    favorable_low = basket.favorable_low
    if legs:
        favorable_high = (
            quote.bid if favorable_high is None else max(favorable_high, quote.bid)
        )
        favorable_low = (
            quote.ask if favorable_low is None else min(favorable_low, quote.ask)
        )
    return replace(
        basket,
        legs=legs,
        favorable_high=favorable_high,
        favorable_low=favorable_low,
    )


def _hold(
    reason_code: str,
    source: StrategyDecision,
    *,
    evidence: dict[str, Any] | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        action=DecisionAction.HOLD,
        reason_code=reason_code,
        effective_mode=source.effective_mode,
        evidence=evidence or {},
    )


def _gate_action(action: DecisionAction) -> str:
    if action == DecisionAction.OPEN:
        return "open"
    if action == DecisionAction.ADD:
        return "add"
    if action == DecisionAction.CLOSE_ALL:
        return "exit"
    if action in {DecisionAction.CLOSE_DIRECTION, DecisionAction.CLOSE_PAIR}:
        return "reduce"
    return "hold"


def _live_cycle_loss_exit(
    config: MartingaleTp4Config,
    basket: BasketSnapshot,
    account_balance: Decimal,
) -> StrategyDecision | None:
    if not basket.legs:
        return None
    basket_pnl = sum((item.unrealized_pnl for item in basket.legs), Decimal("0"))
    maximum_loss = account_balance * config.live_risk.max_cycle_loss_pct / Decimal("100")
    if basket_pnl > -maximum_loss:
        return None
    return StrategyDecision(
        action=DecisionAction.CLOSE_ALL,
        reason_code="live_cycle_loss_limit",
        effective_mode=config.parameters.mode,
        evidence={
            "basket_pnl": str(basket_pnl),
            "maximum_loss": str(maximum_loss),
            "account_balance": str(account_balance),
        },
    )


def _new_risk_limit_reason(
    config: MartingaleTp4Config,
    basket: BasketSnapshot,
    decision: StrategyDecision,
    binance: BinanceExecutionQuote | None,
    *,
    account_balance: Decimal,
    leverage: Decimal,
    execution_point_size: Decimal,
) -> tuple[str, dict[str, Any]] | None:
    if decision.action == DecisionAction.ADD and not config.live_risk.additions_enabled:
        return "live_additions_disabled", {}
    if (
        decision.action not in {DecisionAction.OPEN, DecisionAction.ADD}
        or decision.quantity is None
        or decision.direction is None
        or binance is None
    ):
        return None
    execution_spread_points = (binance.ask - binance.bid) / execution_point_size
    if execution_spread_points > config.parameters.execution.max_spread_points:
        return (
            "binance_execution_spread_too_wide",
            {
                "execution_spread_points": str(execution_spread_points),
                "maximum_spread_points": str(
                    config.parameters.execution.max_spread_points
                ),
                "execution_point_size": str(execution_point_size),
            },
        )
    entry_price = binance.ask if decision.direction == Direction.BUY else binance.bid
    current_notional = sum(
        (item.entry_price * item.quantity for item in basket.legs), Decimal("0")
    )
    projected_notional = current_notional + entry_price * decision.quantity
    evidence = {
        "current_notional": str(current_notional),
        "projected_notional": str(projected_notional),
        "leverage": str(leverage),
    }
    maximum_notional = config.live_risk.max_cycle_notional
    if maximum_notional is not None and projected_notional > maximum_notional:
        evidence["maximum_notional"] = str(maximum_notional)
        return "live_cycle_notional_limit", evidence
    projected_margin_pct = (
        projected_notional / leverage / account_balance * Decimal("100")
    )
    evidence["projected_margin_pct"] = str(projected_margin_pct)
    evidence["maximum_margin_pct"] = str(config.live_risk.max_cycle_margin_pct)
    if projected_margin_pct > config.live_risk.max_cycle_margin_pct:
        return "live_cycle_margin_limit", evidence
    return None


def evaluate_shadow_tick(
    config: MartingaleTp4Config,
    basket: BasketSnapshot,
    *,
    tiger: TigerReferenceQuote | None,
    binance: BinanceExecutionQuote | None,
    mapping_verified: bool,
    point_size: Decimal,
    hour: int,
    account_balance: Decimal,
    deployment_scope: str,
    event_id: str,
    now: datetime | None = None,
    leverage: Decimal = Decimal("1"),
    execution_point_size: Decimal | None = None,
    manual_direction: Direction | None = None,
    manual_quantity: Decimal | None = None,
) -> ShadowEvaluation:
    """Evaluate one source-qualified tick without submitting any order."""

    evaluated_at = now or datetime.now(UTC)
    if point_size <= 0:
        raise ValueError("point_size must be positive")
    if account_balance <= 0:
        raise ValueError("account_balance must be positive")
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    execution_tick_size = execution_point_size or point_size
    if execution_tick_size <= 0:
        raise ValueError("execution_point_size must be positive")

    marked = mark_basket(basket, binance) if binance is not None else basket
    source_decision: StrategyDecision | None = _live_cycle_loss_exit(
        config, marked, account_balance
    )

    # Risk-reducing decisions are based on the venue that owns the exposure.
    if source_decision is None and marked.legs and binance is not None:
        source_decision = evaluate_risk_reducing_decision(
            config.parameters,
            marked,
            MarketTick(
                bid=binance.bid,
                ask=binance.ask,
                point_size=execution_tick_size,
                hour=hour,
            ),
            policy=EnginePolicy.LIVE_SAFE,
        )

    # New exposure is based only on Tiger's reference-market signal.
    if source_decision is None and tiger is not None:
        source_decision = evaluate_tick(
            config.parameters,
            marked,
            MarketTick(
                bid=tiger.bid,
                ask=tiger.ask,
                point_size=point_size,
                hour=hour,
            ),
            account_balance=account_balance,
            policy=EnginePolicy.LIVE_SAFE,
            manual_direction=manual_direction,
            manual_quantity=manual_quantity,
            _allow_exit_evaluation=False,
            _enforce_entry_spread=False,
        )
    if source_decision is None:
        source_decision = StrategyDecision(
            action=DecisionAction.HOLD,
            reason_code=(
                "binance_quote_unavailable_for_shadow_exit"
                if marked.legs
                else "tiger_quote_unavailable_for_signal"
            ),
            effective_mode=config.parameters.mode,
        )

    action = _gate_action(source_decision.action)
    market_data = config.market_data
    gate = evaluate_market_data_gate(
        action=action,  # type: ignore[arg-type]
        mapping_verified=mapping_verified,
        tiger=tiger,
        binance=binance,
        maximum_tiger_age_seconds=Decimal(market_data.maximum_tiger_age_seconds),
        maximum_binance_age_seconds=Decimal(market_data.maximum_binance_age_seconds),
        maximum_clock_skew_seconds=Decimal(market_data.maximum_clock_skew_seconds),
        maximum_basis_bps=market_data.maximum_basis_bps,
        now=evaluated_at,
    )
    decision = source_decision
    risk_limit = _new_risk_limit_reason(
        config,
        marked,
        source_decision,
        binance,
        account_balance=account_balance,
        leverage=leverage,
        execution_point_size=execution_tick_size,
    )
    if action in {"open", "add"} and not gate.allowed:
        decision = _hold(
            "market_data_gate_blocked_new_risk",
            source_decision,
            evidence={
                "intended_action": source_decision.action.value,
                "intended_reason_code": source_decision.reason_code,
                "gate_reason_codes": list(gate.reason_codes),
            },
        )
    elif risk_limit is not None:
        reason_code, evidence = risk_limit
        decision = _hold(
            reason_code,
            source_decision,
            evidence={
                "intended_action": source_decision.action.value,
                "intended_reason_code": source_decision.reason_code,
                **evidence,
            },
        )

    return ShadowEvaluation(
        idempotency_key=shadow_idempotency_key(
            deployment_scope=deployment_scope,
            contract_symbol=config.market_data.contract_symbol,
            event_id=event_id,
        ),
        source_decision=source_decision,
        decision=decision,
        market_gate=gate,
        marked_basket=marked,
    )


def _close_pnl(leg: BasketLeg, price: Decimal) -> Decimal:
    if leg.direction == Direction.BUY:
        return (price - leg.entry_price) * leg.quantity
    return (leg.entry_price - price) * leg.quantity


def apply_shadow_evaluation(
    evaluation: ShadowEvaluation,
    *,
    binance: BinanceExecutionQuote | None,
    tiger_bid: Decimal | None,
    fee_bps: Decimal = Decimal("0"),
) -> ShadowTransition:
    """Apply approved Shadow intents to an immutable virtual basket."""

    if fee_bps < 0:
        raise ValueError("fee_bps must not be negative")
    before = evaluation.marked_basket
    decision = evaluation.decision
    legs = list(before.legs)
    fills: list[ShadowFill] = []
    realized_delta = Decimal("0")

    if decision.action in {DecisionAction.OPEN, DecisionAction.ADD}:
        if binance is None or decision.direction is None or decision.quantity is None:
            raise ValueError("approved new-risk decision requires a Binance quote and order terms")
        price = binance.ask if decision.direction == Direction.BUY else binance.bid
        leg_index = max((item.leg_index for item in legs), default=-1) + 1
        fee = price * decision.quantity * fee_bps / _TEN_THOUSAND
        legs.append(
            BasketLeg(
                leg_index=leg_index,
                direction=decision.direction,
                quantity=decision.quantity,
                entry_price=price,
            )
        )
        realized_delta -= fee
        fills.append(
            ShadowFill(
                action=decision.action.value,
                leg_index=leg_index,
                direction=decision.direction,
                quantity=decision.quantity,
                price=price,
                fee=fee,
                realized_pnl=-fee,
            )
        )
    elif decision.action in {
        DecisionAction.CLOSE_ALL,
        DecisionAction.CLOSE_DIRECTION,
        DecisionAction.CLOSE_PAIR,
    }:
        if binance is None:
            raise ValueError("approved exit decision requires a Binance quote")
        if decision.action == DecisionAction.CLOSE_ALL:
            closing = list(legs)
        elif decision.action == DecisionAction.CLOSE_DIRECTION:
            closing = [item for item in legs if item.direction == decision.direction]
        else:
            closing_indices = set(decision.leg_indices)
            closing = [item for item in legs if item.leg_index in closing_indices]
        closing_indices = {item.leg_index for item in closing}
        legs = [item for item in legs if item.leg_index not in closing_indices]
        for leg in closing:
            price = binance.bid if leg.direction == Direction.BUY else binance.ask
            gross = _close_pnl(leg, price)
            fee = price * leg.quantity * fee_bps / _TEN_THOUSAND
            net = gross - fee
            realized_delta += net
            fills.append(
                ShadowFill(
                    action=decision.action.value,
                    leg_index=leg.leg_index,
                    direction=leg.direction,
                    quantity=leg.quantity,
                    price=price,
                    fee=fee,
                    realized_pnl=net,
                )
            )

    if legs:
        after = replace(
            before,
            legs=tuple(legs),
            previous_bid=tiger_bid if tiger_bid is not None else before.previous_bid,
        )
        if binance is not None:
            after = mark_basket(after, binance)
    else:
        after = BasketSnapshot(
            previous_bid=tiger_bid if tiger_bid is not None else before.previous_bid
        )
    return ShadowTransition(
        evaluation=evaluation,
        basket_before=before,
        basket_after=after,
        fills=tuple(fills),
        realized_pnl_delta=realized_delta,
    )


def decision_payload(decision: StrategyDecision) -> dict[str, Any]:
    return {
        "action": decision.action.value,
        "reason_code": decision.reason_code,
        "effective_mode": decision.effective_mode,
        "direction": decision.direction.value if decision.direction is not None else None,
        "quantity": str(decision.quantity) if decision.quantity is not None else None,
        "target_price": str(decision.target_price) if decision.target_price is not None else None,
        "leg_indices": list(decision.leg_indices),
        "evidence": decision.evidence,
    }


def basket_payload(basket: BasketSnapshot) -> dict[str, Any]:
    return {
        "legs": [
            {
                "leg_index": item.leg_index,
                "direction": item.direction.value,
                "quantity": str(item.quantity),
                "entry_price": str(item.entry_price),
                "unrealized_pnl": str(item.unrealized_pnl),
            }
            for item in basket.legs
        ],
        "box_high": str(basket.box_high) if basket.box_high is not None else None,
        "box_low": str(basket.box_low) if basket.box_low is not None else None,
        "previous_bid": str(basket.previous_bid) if basket.previous_bid is not None else None,
        "favorable_high": (
            str(basket.favorable_high) if basket.favorable_high is not None else None
        ),
        "favorable_low": (
            str(basket.favorable_low) if basket.favorable_low is not None else None
        ),
    }


def basket_from_payload(payload: dict[str, Any]) -> BasketSnapshot:
    return BasketSnapshot(
        legs=tuple(
            BasketLeg(
                leg_index=int(item["leg_index"]),
                direction=Direction(str(item["direction"])),
                quantity=Decimal(str(item["quantity"])),
                entry_price=Decimal(str(item["entry_price"])),
                unrealized_pnl=Decimal(str(item.get("unrealized_pnl", "0"))),
            )
            for item in payload.get("legs", [])
        ),
        box_high=(Decimal(str(payload["box_high"])) if payload.get("box_high") else None),
        box_low=(Decimal(str(payload["box_low"])) if payload.get("box_low") else None),
        previous_bid=(
            Decimal(str(payload["previous_bid"])) if payload.get("previous_bid") else None
        ),
        favorable_high=(
            Decimal(str(payload["favorable_high"]))
            if payload.get("favorable_high")
            else None
        ),
        favorable_low=(
            Decimal(str(payload["favorable_low"]))
            if payload.get("favorable_low")
            else None
        ),
    )


def fill_payload(fill: ShadowFill) -> dict[str, Any]:
    return {
        "action": fill.action,
        "leg_index": fill.leg_index,
        "direction": fill.direction.value,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "realized_pnl": str(fill.realized_pnl),
    }


def market_gate_payload(gate: MarketDataGateDecision) -> dict[str, Any]:
    payload = asdict(gate)
    for key in (
        "basis_bps",
        "tiger_age_seconds",
        "binance_age_seconds",
        "clock_skew_seconds",
    ):
        value = payload[key]
        payload[key] = str(value) if value is not None else None
    payload["reason_codes"] = list(gate.reason_codes)
    payload["warning_codes"] = list(gate.warning_codes)
    # Ensure nested datetimes/Decimals from evidence remain JSON-safe.
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
