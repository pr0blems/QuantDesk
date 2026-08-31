"""Persistent shadow evaluator using the same strategy evaluator as paper trading."""

from __future__ import annotations

import copy
import json
import math
import threading
import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import market_store
from .application.order_plans import EntryOrderPlan, build_entry_order_plan
from .application.risk import RiskPolicy
from .application.safety import PreflightPolicy
from .application.strategy_signals import evaluate_strategy_snapshot
from .domain.execution import ExecutionMode, ExecutionResult, ExecutionState
from .domain.runtime import decision_record_key, strategy_decision_id
from .domain.trading import AccountSnapshot, AccountType, InstrumentRules
from .infrastructure.persistence.executions import MySqlExecutionJournal
from .infrastructure.shadow_execution import ShadowExecutionRuntime
from .infrastructure.store_market_data import StoreMarketDataFeed
from .models import (
    StrategyDeployment,
    StrategyRevision,
    StrategySignal,
    UserStrategy,
    utcnow,
)
from .strategy_evaluator import StrategyEvaluationError, resolve_strategy_timing_policy

_LOOP_SECONDS = 5.0


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _timeframe(snapshot: dict[str, Any]) -> str:
    try:
        return resolve_strategy_timing_policy(snapshot).trigger_timeframe
    except StrategyEvaluationError:
        return "1h"


def _valid_until(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    while timestamp >= 100_000_000_000:
        timestamp /= 1000
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(tzinfo=None)


def _decision_id(
    revision: StrategyRevision,
    symbol: str,
    timeframe: str,
    signal_time: int,
    decision: str,
) -> str:
    fingerprint = revision.source_hash or revision.spec_hash or f"strategy-revision:{revision.id}"
    return strategy_decision_id(
        fingerprint,
        symbol,
        timeframe,
        signal_time,
        decision,
    )


def _decimal(value: object, fallback: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal(fallback)
    if not parsed.is_finite() or parsed <= 0:
        return Decimal(fallback)
    return parsed


def _instrument_rules(symbol: str) -> InstrumentRules:
    """Conservative exchange-neutral rules for a no-network Shadow fill."""

    return InstrumentRules(
        symbol=symbol,
        quantity_step=Decimal("0.000001"),
        minimum_quantity=Decimal("0.000001"),
        maximum_quantity=Decimal("1000000000"),
        price_tick=Decimal("0.000001"),
        minimum_notional=Decimal("0.01"),
    )


def _shadow_account(config: dict[str, Any], now: datetime) -> AccountSnapshot:
    balance = _decimal(
        config.get("initial_capital", config.get("initial_balance", 10_000)),
        "10000",
    )
    return AccountSnapshot(
        account_type=AccountType.USD_M_FUTURES,
        can_trade=True,
        wallet_balance=balance,
        available_balance=balance,
        unrealized_pnl=Decimal(0),
        currency="USDT",
        updated_at=now,
        observed_at=now,
    )


def _shadow_quantity(
    account: AccountSnapshot,
    price: Decimal,
    rules: InstrumentRules,
    config: dict[str, Any],
) -> Decimal:
    position_pct = min(_decimal(config.get("position_size_pct", 10), "10"), Decimal(100))
    leverage = min(_decimal(config.get("leverage", 2), "2"), Decimal(20))
    requested_notional = account.wallet_balance * position_pct / Decimal(100) * leverage
    requested_notional = max(requested_notional, rules.minimum_notional)
    raw = requested_notional / price
    stepped = (raw / rules.quantity_step).to_integral_value(rounding=ROUND_DOWN)
    quantity = stepped * rules.quantity_step
    if quantity < rules.minimum_quantity:
        quantity = rules.minimum_quantity
    if quantity > rules.maximum_quantity:
        quantity = rules.maximum_quantity
    return quantity


def _execution_snapshot(
    plan: EntryOrderPlan | None,
    result: ExecutionResult | None,
) -> dict[str, Any]:
    if plan is None:
        return {
            "mode": "shadow",
            "approved": False,
            "network_write": False,
            "order_intent": None,
            "execution_state": "not_applicable",
        }
    return {
        "mode": "shadow",
        "approved": result is not None and result.state is ExecutionState.FILLED,
        "network_write": False,
        "order_intent": plan.snapshot(),
        "execution_state": result.state.value if result is not None else "not_executed",
        "error_code": result.error_code if result is not None else None,
        "broker_order": (
            {
                "client_order_id": result.broker_order.reference.client_order_id,
                "exchange_order_id": result.broker_order.exchange_order_id,
                "status": result.broker_order.status.value,
                "executed_quantity": str(result.broker_order.executed_quantity),
                "average_price": (
                    str(result.broker_order.average_price)
                    if result.broker_order.average_price is not None
                    else None
                ),
            }
            if result is not None and result.broker_order is not None
            else None
        ),
    }


def _evaluate_deployment(
    db: Session,
    engine: Engine,
    deployment: StrategyDeployment,
    revision: StrategyRevision,
    strategy: UserStrategy,
) -> None:
    snapshot = copy.deepcopy(revision.snapshot_json or {})
    symbols = sorted(
        {
            str(symbol).strip().upper()
            for symbol in (deployment.universe_override_json or {}).get("symbols", [])
            if str(symbol).strip()
        }
    )
    runtime = copy.deepcopy(deployment.runtime_state_json or {})
    last_by_symbol = dict(runtime.get("last_bar_by_symbol") or {})
    evaluations = int(runtime.get("evaluations") or 0)
    decisions = int(runtime.get("decisions") or 0)
    order_intents = int(runtime.get("order_intents") or 0)
    config = copy.deepcopy(deployment.risk_override_json or {})
    now = datetime.now(UTC)
    feed = StoreMarketDataFeed()
    account = _shadow_account(config, now)
    rules = {symbol: _instrument_rules(symbol) for symbol in symbols}
    execution = ShadowExecutionRuntime(
        account=account,
        feed=feed,
        rules=rules,
        tenant_scope=f"tenant-{deployment.user_id}",
        user_scope=f"user-{deployment.user_id}",
        account_scope=f"shadow-{deployment.id}",
        physical_account_id=f"shadow-wallet-{deployment.id}",
        slippage_bps=_decimal(config.get("slippage_bps", 2), "2"),
        risk_policy=RiskPolicy(
            max_open_positions=max(1, min(int(config.get("max_positions", 20)), 20))
        ),
        preflight_policy=PreflightPolicy(
            max_quote_age_seconds=300,
            max_account_age_seconds=300,
        ),
        idempotency=MySqlExecutionJournal(engine),
        clock=lambda: now,
    )
    for symbol in symbols:
        evaluated = evaluate_strategy_snapshot(
            snapshot,
            symbol,
            config,
            load_klines=market_store.get_klines,
        )
        direction, atr, basis, signal_time, evidence = evaluated.execution_tuple()
        if signal_time is None or int(last_by_symbol.get(symbol) or 0) >= int(signal_time):
            continue
        envelope = evaluated.envelope
        timeframe = envelope.timeframe if envelope is not None else _timeframe(snapshot)
        decision = (
            envelope.decision.value
            if envelope is not None
            else {1: "LONG_ENTRY", -1: "SHORT_ENTRY"}.get(direction, "HOLD")
        )
        decision_id = (
            envelope.decision_id
            if envelope is not None
            else _decision_id(revision, symbol, timeframe, int(signal_time), decision)
        )
        idempotency_key = decision_record_key("shadow", deployment.id, decision_id)
        exists = db.scalar(
            select(StrategySignal.id).where(StrategySignal.idempotency_key == idempotency_key)
        )
        if exists is None:
            plan = None
            result = None
            reference_price = _decimal(evidence.get("reference_price"), "0")
            has_reference_price = reference_price > 0
            if envelope is not None and has_reference_price:
                plan = build_entry_order_plan(
                    envelope,
                    mode=ExecutionMode.SHADOW,
                    quantity=_shadow_quantity(
                        account, reference_price, rules[symbol], config
                    ),
                    reference_price=reference_price,
                    tenant_scope=f"tenant-{deployment.user_id}",
                    user_scope=f"user-{deployment.user_id}",
                    account_scope=f"shadow-{deployment.id}",
                    deployment_scope=f"deployment-{deployment.id}",
                    created_at=envelope.event_time,
                    config=config,
                    atr=atr,
                )
            if plan is not None:
                result = execution.execute(plan.intent)
                order_intents += 1
            risk_snapshot = _execution_snapshot(plan, result)
            if envelope is not None and envelope.direction and not has_reference_price:
                risk_snapshot["execution_state"] = "reference_price_unavailable"
                risk_snapshot["error_code"] = "reference_price_unavailable"
            if result is not None and result.state is ExecutionState.FILLED:
                status = "executed"
            elif result is not None:
                status = "risk_rejected"
            elif envelope is not None and envelope.direction and envelope.valid_until is not None:
                status = "expired" if envelope.valid_until <= now else "risk_rejected"
            else:
                status = "approved"
            reasons = list(envelope.reason_codes) if envelope is not None else list(basis[-8:])
            confidence = envelope.confidence if envelope is not None else None
            db.add(
                StrategySignal(
                    public_id=str(uuid.uuid4()),
                    user_id=deployment.user_id,
                    deployment_id=deployment.id,
                    strategy_revision_id=revision.id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_bar_time=int(signal_time),
                    decision=decision,
                    confidence=confidence,
                    status=status,
                    valid_until=(
                        envelope.valid_until.replace(tzinfo=None)
                        if envelope is not None and envelope.valid_until is not None
                        else _valid_until((evidence or {}).get("valid_until"))
                    ),
                    reason_codes_json=[str(item)[:64] for item in reasons[:32]],
                    evidence_json=_json_safe(
                        {
                            **copy.deepcopy(evidence or {}),
                            "decision_id": decision_id,
                            "basis": basis,
                            "strategy_public_id": strategy.public_id,
                            "strategy_version": revision.version,
                        }
                    ),
                    risk_decision_json=_json_safe(risk_snapshot),
                    idempotency_key=idempotency_key,
                    created_at=utcnow(),
                )
            )
            decisions += 1
        evaluations += 1
        last_by_symbol[symbol] = int(signal_time)
        deployment.last_evaluated_bar_time = max(
            int(deployment.last_evaluated_bar_time or 0), int(signal_time)
        )
    runtime.update(
        {
            "decision_protocol_version": "decision_envelope_v1",
            "evaluations": evaluations,
            "decisions": decisions,
            "order_intents": order_intents,
            "last_bar_by_symbol": last_by_symbol,
            "last_cycle_at": utcnow().isoformat() + "Z",
            "network_writes": 0,
        }
    )
    deployment.runtime_state_json = runtime
    deployment.last_error_code = None
    deployment.updated_at = utcnow()


def tick(engine: Engine) -> int:
    completed = 0
    with Session(engine) as db:
        deployments = db.scalars(
            select(StrategyDeployment).where(
                StrategyDeployment.mode == "shadow",
                StrategyDeployment.status == "running",
            )
        ).all()
        for deployment in deployments:
            revision = db.get(StrategyRevision, deployment.strategy_revision_id)
            strategy = db.get(UserStrategy, deployment.strategy_id)
            if revision is None or strategy is None:
                deployment.status = "error"
                deployment.last_error_code = "shadow_revision_unavailable"
                deployment.updated_at = utcnow()
                continue
            try:
                _evaluate_deployment(db, engine, deployment, revision, strategy)
                db.commit()
                completed += 1
            except IntegrityError:
                db.rollback()
            except Exception as exc:
                db.rollback()
                current = db.get(StrategyDeployment, deployment.id)
                if current is not None:
                    current.last_error_code = f"shadow_{type(exc).__name__.lower()}"[:64]
                    current.updated_at = utcnow()
                    db.commit()
        return completed


def shadow_loop(engine: Engine, stop_event: threading.Event) -> None:
    print("[shadow] revision-pinned shadow evaluator started")
    while not stop_event.wait(_LOOP_SECONDS):
        tick(engine)
