"""Persistent shadow evaluator using the same strategy evaluator as paper trading."""

from __future__ import annotations

import copy
import json
import math
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .domain.runtime import decision_record_key, strategy_decision_id
from .models import (
    StrategyDeployment,
    StrategyRevision,
    StrategySignal,
    UserStrategy,
    utcnow,
)
from .paper_engine import _strategy_signal
from .strategy_evaluator import StrategyEvaluationError, resolve_legacy_strategy_timeframe

_LOOP_SECONDS = 5.0


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _timeframe(snapshot: dict[str, Any]) -> str:
    spec = snapshot.get("spec") or snapshot.get("spec_json")
    if isinstance(spec, dict):
        timeframes = spec.get("timeframes")
        if isinstance(timeframes, dict) and timeframes.get("trigger"):
            return str(timeframes["trigger"])
    validation = snapshot.get("source_validation") or snapshot.get("validation")
    if isinstance(validation, dict):
        requirements = validation.get("data_requirements")
        if isinstance(requirements, dict) and requirements.get("trigger_timeframe"):
            return str(requirements["trigger_timeframe"])
        if validation.get("trigger_timeframe"):
            return str(validation["trigger_timeframe"])
    try:
        return resolve_legacy_strategy_timeframe(snapshot)
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


def _evaluate_deployment(
    db: Session,
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
    account = {
        "id": deployment.id,
        "user_id": deployment.user_id,
        "deployment_id": deployment.id,
        "strategy_revision_id": revision.id,
        "deployment_mode": "shadow",
        "config_json": copy.deepcopy(deployment.risk_override_json or {}),
        "strategy_snapshot_json": snapshot,
    }
    for symbol in symbols:
        direction, _atr, basis, signal_time, evidence = _strategy_signal(
            account,
            symbol,
            snapshot,
        )
        if signal_time is None or int(last_by_symbol.get(symbol) or 0) >= int(signal_time):
            continue
        timeframe = _timeframe(snapshot)
        decision = str((evidence or {}).get("decision") or "")
        if decision not in {"LONG_ENTRY", "SHORT_ENTRY", "EXIT", "HOLD", "SKIP"}:
            decision = {1: "LONG_ENTRY", -1: "SHORT_ENTRY"}.get(direction, "HOLD")
        decision_id = _decision_id(
            revision,
            symbol,
            timeframe,
            int(signal_time),
            decision,
        )
        idempotency_key = decision_record_key("shadow", deployment.id, decision_id)
        exists = db.scalar(
            select(StrategySignal.id).where(StrategySignal.idempotency_key == idempotency_key)
        )
        order_intent = None
        if decision in {"LONG_ENTRY", "SHORT_ENTRY", "EXIT"}:
            order_intent = {
                "intent_id": decision_id,
                "symbol": symbol,
                "side": decision,
                "mode": "shadow",
                "network_write": False,
            }
        if exists is None:
            reasons = (evidence or {}).get("reason_codes")
            if not isinstance(reasons, list):
                reasons = [str(item) for item in basis[-8:]]
            confidence = (evidence or {}).get("confidence")
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
                    status="approved",
                    valid_until=_valid_until((evidence or {}).get("valid_until")),
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
                    risk_decision_json=_json_safe(
                        {
                            "mode": "shadow",
                            "approved": True,
                            "network_write": False,
                            "order_intent": order_intent,
                        }
                    ),
                    idempotency_key=idempotency_key,
                    created_at=utcnow(),
                )
            )
            decisions += 1
            if order_intent is not None:
                order_intents += 1
        evaluations += 1
        last_by_symbol[symbol] = int(signal_time)
        deployment.last_evaluated_bar_time = max(
            int(deployment.last_evaluated_bar_time or 0), int(signal_time)
        )
    runtime.update(
        {
            "decision_protocol_version": "strategy_decision_v2",
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
                _evaluate_deployment(db, deployment, revision, strategy)
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
