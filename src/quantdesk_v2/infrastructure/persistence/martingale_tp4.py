"""Durable, tenant-scoped Shadow ledger for Martingale TP4 baskets."""

from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...application.martingale_tp4.market_gate import (
    BinanceExecutionQuote,
    TigerReferenceQuote,
)
from ...application.martingale_tp4.shadow import (
    ShadowEvaluation,
    ShadowTransition,
    apply_shadow_evaluation,
    basket_from_payload,
    basket_payload,
    evaluate_shadow_tick,
)
from ...domain.martingale_tp4 import MartingaleTp4Config
from ...domain.martingale_tp4_engine import (
    BasketSnapshot,
    DecisionAction,
    Direction,
)
from ...models import (
    StrategyBasketCycle,
    StrategyBasketEvent,
    StrategyBasketLeg,
    StrategyDeployment,
    StrategySignal,
    utcnow,
)

_EVENT_NAMESPACE = uuid.UUID("1d17a4d9-29db-4c6c-ac87-cc1898536206")
_ACTIVE_STATES = (
    "arming",
    "opening",
    "open",
    "adding",
    "exiting",
    "recovery_required",
    "failed_closed",
)


@dataclass(frozen=True, slots=True)
class RestoredShadowBasket:
    cycle: StrategyBasketCycle | None
    basket: BasketSnapshot
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class RecordedShadowTransition:
    signal: StrategySignal
    cycle: StrategyBasketCycle | None
    idempotent: bool


@dataclass(frozen=True, slots=True)
class ProcessedShadowTick:
    evaluation: ShadowEvaluation
    transition: ShadowTransition
    recorded: RecordedShadowTransition


def _active_key(deployment_id: int, contract_symbol: str) -> str:
    return f"martingale_tp4:{deployment_id}:{contract_symbol.strip().upper()}"


def _runtime(deployment: StrategyDeployment) -> dict:
    value = deployment.runtime_state_json
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _same_structural_state(
    restored: BasketSnapshot,
    transition_before: BasketSnapshot,
    *,
    active_cycle: bool,
) -> bool:
    """Compare durable identity while allowing a fresh mark-to-market value."""

    restored_legs = tuple(
        (item.leg_index, item.direction, item.quantity, item.entry_price)
        for item in restored.legs
    )
    transition_legs = tuple(
        (item.leg_index, item.direction, item.quantity, item.entry_price)
        for item in transition_before.legs
    )
    if restored_legs != transition_legs:
        return False
    if active_cycle:
        return (
            restored.box_high == transition_before.box_high
            and restored.box_low == transition_before.box_low
            and restored.previous_bid == transition_before.previous_bid
        )
    if restored.previous_bid is not None:
        return restored.previous_bid == transition_before.previous_bid
    return True


def _latest_basket_payload(
    db: Session,
    cycle_id: int,
) -> dict | None:
    event = db.scalar(
        select(StrategyBasketEvent)
        .where(StrategyBasketEvent.cycle_id == cycle_id)
        .order_by(StrategyBasketEvent.sequence_no.desc())
        .limit(1)
    )
    if event is None or not isinstance(event.payload_json, dict):
        return None
    payload = event.payload_json.get("basket_after")
    return payload if isinstance(payload, dict) else None


def restore_shadow_basket(
    db: Session,
    deployment: StrategyDeployment,
    *,
    contract_symbol: str,
    for_update: bool = False,
) -> RestoredShadowBasket:
    """Restore the exact active leg state, including pre-entry cross memory."""

    statement = select(StrategyBasketCycle).where(
        StrategyBasketCycle.user_id == deployment.user_id,
        StrategyBasketCycle.deployment_id == deployment.id,
        StrategyBasketCycle.active_key == _active_key(deployment.id, contract_symbol),
        StrategyBasketCycle.state.in_(_ACTIVE_STATES),
    )
    if for_update:
        statement = statement.with_for_update()
    cycle = db.scalar(statement)
    if cycle is not None:
        payload = _latest_basket_payload(db, cycle.id)
        if payload is None:
            raise RuntimeError("active martingale basket is missing its state event")
        return RestoredShadowBasket(
            cycle=cycle,
            basket=basket_from_payload(payload),
            realized_pnl=Decimal(cycle.realized_pnl),
        )

    runtime = _runtime(deployment)
    idle_states = runtime.get("martingale_tp4_idle_states")
    payload = (
        idle_states.get(contract_symbol.strip().upper())
        if isinstance(idle_states, dict)
        else None
    )
    return RestoredShadowBasket(
        cycle=None,
        basket=basket_from_payload(payload) if isinstance(payload, dict) else BasketSnapshot(),
        realized_pnl=Decimal("0"),
    )


def _signal_decision(transition: ShadowTransition) -> str:
    decision = transition.evaluation.decision
    if decision.action in {DecisionAction.OPEN, DecisionAction.ADD}:
        return "LONG_ENTRY" if decision.direction == Direction.BUY else "SHORT_ENTRY"
    if decision.action in {
        DecisionAction.CLOSE_ALL,
        DecisionAction.CLOSE_DIRECTION,
        DecisionAction.CLOSE_PAIR,
    }:
        return "EXIT"
    return "HOLD"


def _signal_status(transition: ShadowTransition) -> str:
    if transition.fills:
        return "executed"
    source = transition.evaluation.source_decision.action
    if source in {DecisionAction.OPEN, DecisionAction.ADD}:
        return "risk_rejected"
    return "approved"


def _aggregate_cycle(cycle: StrategyBasketCycle, transition: ShadowTransition) -> None:
    basket = transition.basket_after
    gross = sum((item.quantity for item in basket.legs), Decimal("0"))
    net = sum(
        (
            item.quantity if item.direction == Direction.BUY else -item.quantity
            for item in basket.legs
        ),
        Decimal("0"),
    )
    weighted = (
        sum((item.entry_price * item.quantity for item in basket.legs), Decimal("0"))
        / gross
        if gross
        else None
    )
    cycle.gross_quantity = gross
    cycle.net_quantity = net
    cycle.weighted_cost = weighted
    cycle.realized_pnl = Decimal(cycle.realized_pnl) + transition.realized_pnl_delta
    cycle.unrealized_pnl = sum(
        (item.unrealized_pnl for item in basket.legs), Decimal("0")
    )
    cycle.box_high = basket.box_high
    cycle.box_low = basket.box_low
    cycle.version += 1
    cycle.updated_at = utcnow()
    if basket.legs:
        cycle.state = "open"
        cycle.opened_at = cycle.opened_at or utcnow()
    else:
        cycle.state = "closed"
        cycle.active_key = None
        cycle.closed_at = utcnow()


def _record_legs(
    db: Session,
    cycle: StrategyBasketCycle,
    transition: ShadowTransition,
    *,
    occurred_at: datetime,
) -> None:
    decision_action = transition.evaluation.decision.action
    close_action = {
        DecisionAction.CLOSE_ALL: "exit",
        DecisionAction.CLOSE_DIRECTION: "reduce",
        DecisionAction.CLOSE_PAIR: "overlap_close",
    }.get(decision_action)
    for fill in transition.fills:
        action = fill.action if close_action is None else close_action
        identity = hashlib.sha256(
            f"{transition.evaluation.idempotency_key}:{fill.leg_index}:{action}".encode()
        ).hexdigest()
        db.add(
            StrategyBasketLeg(
                cycle_id=cycle.id,
                user_id=cycle.user_id,
                leg_index=fill.leg_index,
                action=action,
                direction="long" if fill.direction == Direction.BUY else "short",
                position_side="LONG" if fill.direction == Direction.BUY else "SHORT",
                planned_quantity=fill.quantity,
                filled_quantity=fill.quantity,
                planned_price=fill.price,
                average_fill_price=fill.price,
                intent_id=identity,
                exchange_order_id=f"shadow-{identity[:48]}",
                client_order_id=f"shadow-{identity[:56]}",
                fee=fill.fee,
                realized_pnl=fill.realized_pnl,
                state="filled",
                reason_code=transition.evaluation.decision.reason_code[:64],
                created_at=occurred_at,
                submitted_at=occurred_at,
                filled_at=occurred_at,
            )
        )


def _append_cycle_event(
    db: Session,
    cycle: StrategyBasketCycle,
    transition: ShadowTransition,
    *,
    occurred_at: datetime,
) -> None:
    sequence = int(
        db.scalar(
            select(func.max(StrategyBasketEvent.sequence_no)).where(
                StrategyBasketEvent.cycle_id == cycle.id
            )
        )
        or 0
    ) + 1
    db.add(
        StrategyBasketEvent(
            public_id=str(
                uuid.uuid5(
                    _EVENT_NAMESPACE,
                    f"{cycle.public_id}:{transition.evaluation.idempotency_key}",
                )
            ),
            cycle_id=cycle.id,
            user_id=cycle.user_id,
            sequence_no=sequence,
            event_type="shadow_transition",
            actor_type="strategy_engine",
            reason_code=transition.evaluation.decision.reason_code[:64],
            payload_json=transition.audit_payload(),
            occurred_at=occurred_at,
            created_at=utcnow(),
        )
    )


def _cycle_sequence(db: Session, deployment_id: int, contract_symbol: str) -> int:
    return int(
        db.scalar(
            select(func.max(StrategyBasketCycle.cycle_seq)).where(
                StrategyBasketCycle.deployment_id == deployment_id,
                StrategyBasketCycle.contract_symbol == contract_symbol,
            )
        )
        or 0
    ) + 1


def record_shadow_transition(
    db: Session,
    deployment: StrategyDeployment,
    config: MartingaleTp4Config,
    transition: ShadowTransition,
    *,
    occurred_at: datetime,
    signal_time_ms: int,
    account_balance: Decimal,
    box_time: int | None = None,
) -> RecordedShadowTransition:
    """Append one idempotent Shadow transition inside the caller transaction."""

    locked_deployment = db.scalar(
        select(StrategyDeployment)
        .where(
            StrategyDeployment.id == deployment.id,
            StrategyDeployment.user_id == deployment.user_id,
        )
        .with_for_update()
    )
    if locked_deployment is None:
        raise RuntimeError("martingale shadow deployment no longer exists")
    deployment = locked_deployment
    key = transition.evaluation.idempotency_key
    existing = db.scalar(
        select(StrategySignal).where(
            StrategySignal.user_id == deployment.user_id,
            StrategySignal.idempotency_key == key,
        )
    )
    if existing is not None:
        cycle = db.scalar(
            select(StrategyBasketCycle).where(
                StrategyBasketCycle.user_id == deployment.user_id,
                StrategyBasketCycle.deployment_id == deployment.id,
                StrategyBasketCycle.active_key
                == _active_key(deployment.id, config.market_data.contract_symbol),
            )
        )
        return RecordedShadowTransition(existing, cycle, True)

    restored = restore_shadow_basket(
        db,
        deployment,
        contract_symbol=config.market_data.contract_symbol,
        for_update=True,
    )
    cycle = restored.cycle
    if not _same_structural_state(
        restored.basket,
        transition.basket_before,
        active_cycle=cycle is not None,
    ):
        raise RuntimeError(
            "stale martingale shadow transition; restore and evaluate the latest basket"
        )
    if cycle is None and transition.fills:
        if transition.evaluation.decision.action != DecisionAction.OPEN:
            raise RuntimeError("cannot apply a non-opening fill without an active basket")
        cycle = StrategyBasketCycle(
            user_id=deployment.user_id,
            deployment_id=deployment.id,
            strategy_revision_id=deployment.strategy_revision_id,
            underlying_symbol=config.market_data.underlying_symbol,
            contract_symbol=config.market_data.contract_symbol,
            mode=config.parameters.mode,
            cycle_seq=_cycle_sequence(
                db, deployment.id, config.market_data.contract_symbol
            ),
            state="opening",
            box_high=transition.basket_before.box_high,
            box_low=transition.basket_before.box_low,
            box_time=box_time,
            max_risk=(
                account_balance * config.live_risk.max_cycle_loss_pct / Decimal("100")
            ),
            active_key=_active_key(deployment.id, config.market_data.contract_symbol),
            version=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(cycle)
        db.flush()

    if cycle is not None:
        _record_legs(db, cycle, transition, occurred_at=occurred_at)
        _aggregate_cycle(cycle, transition)
        _append_cycle_event(db, cycle, transition, occurred_at=occurred_at)

    evidence = transition.audit_payload()
    signal = StrategySignal(
        public_id=str(uuid.uuid5(_EVENT_NAMESPACE, key)),
        user_id=deployment.user_id,
        deployment_id=deployment.id,
        strategy_revision_id=deployment.strategy_revision_id,
        symbol=config.market_data.contract_symbol,
        timeframe=config.parameters.box.timeframe,
        signal_bar_time=signal_time_ms,
        decision=_signal_decision(transition),
        status=_signal_status(transition),
        reason_codes_json=[transition.evaluation.decision.reason_code[:64]],
        evidence_json=evidence,
        risk_decision_json=evidence["market_gate"],
        idempotency_key=key,
        created_at=utcnow(),
    )
    db.add(signal)

    runtime = _runtime(deployment)
    idle_states = runtime.get("martingale_tp4_idle_states")
    if not isinstance(idle_states, dict):
        idle_states = {}
    contract = config.market_data.contract_symbol
    if transition.basket_after.legs:
        idle_states.pop(contract, None)
    else:
        idle_states[contract] = basket_payload(transition.basket_after)
    runtime.update(
        {
            "martingale_tp4_engine_version": "martingale_tp4_shadow_v1",
            "martingale_tp4_idle_states": idle_states,
            "last_martingale_event_key": key,
            "last_martingale_decision": transition.evaluation.decision.action.value,
            "last_martingale_reason_code": transition.evaluation.decision.reason_code,
            "last_martingale_tick_at": occurred_at.isoformat(),
            "network_writes": 0,
        }
    )
    deployment.runtime_state_json = runtime
    deployment.last_evaluated_bar_time = max(
        int(deployment.last_evaluated_bar_time or 0), signal_time_ms
    )
    deployment.last_error_code = None
    deployment.updated_at = utcnow()
    db.flush()
    return RecordedShadowTransition(signal, cycle, False)


def process_shadow_tick(
    db: Session,
    deployment: StrategyDeployment,
    config: MartingaleTp4Config,
    *,
    tiger: TigerReferenceQuote | None,
    binance: BinanceExecutionQuote | None,
    mapping_verified: bool,
    point_size: Decimal,
    hour: int,
    account_balance: Decimal,
    event_id: str,
    occurred_at: datetime,
    leverage: Decimal = Decimal("1"),
    execution_point_size: Decimal | None = None,
    box_high: Decimal | None = None,
    box_low: Decimal | None = None,
    box_time: int | None = None,
    fee_bps: Decimal = Decimal("0"),
    manual_direction: Direction | None = None,
    manual_quantity: Decimal | None = None,
) -> ProcessedShadowTick:
    """Restore, evaluate, virtually fill, and append one atomic Shadow tick."""

    restored = restore_shadow_basket(
        db,
        deployment,
        contract_symbol=config.market_data.contract_symbol,
        for_update=True,
    )
    basket = restored.basket
    if not basket.legs and (box_high is not None or box_low is not None):
        if box_high is None or box_low is None:
            raise ValueError("box_high and box_low must be supplied together")
        basket = replace(basket, box_high=box_high, box_low=box_low)
    evaluation = evaluate_shadow_tick(
        config,
        basket,
        tiger=tiger,
        binance=binance,
        mapping_verified=mapping_verified,
        point_size=point_size,
        hour=hour,
        account_balance=account_balance,
        deployment_scope=deployment.public_id,
        event_id=event_id,
        now=occurred_at,
        leverage=leverage,
        execution_point_size=execution_point_size,
        manual_direction=manual_direction,
        manual_quantity=manual_quantity,
    )
    transition = apply_shadow_evaluation(
        evaluation,
        binance=binance,
        tiger_bid=tiger.bid if tiger is not None else None,
        fee_bps=fee_bps,
    )
    recorded = record_shadow_transition(
        db,
        deployment,
        config,
        transition,
        occurred_at=occurred_at,
        signal_time_ms=int(occurred_at.timestamp() * 1000),
        account_balance=account_balance,
        box_time=box_time,
    )
    return ProcessedShadowTick(evaluation, transition, recorded)


def serialize_recorded_transition(result: RecordedShadowTransition) -> dict:
    evidence = result.signal.evidence_json or {}
    return {
        "idempotent": result.idempotent,
        "signal_id": result.signal.public_id,
        "cycle_id": result.cycle.public_id if result.cycle is not None else None,
        "decision": result.signal.decision,
        "status": result.signal.status,
        "reason_codes": list(result.signal.reason_codes_json or []),
        "transition": copy.deepcopy(evidence),
        "network_write": False,
    }
