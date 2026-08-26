"""Operational safety monitor for worker, revision and execution incidents."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import (
    LiveOrderIntent,
    RuntimeIncident,
    StrategyDeployment,
    StrategyRevision,
    TradingControlLatch,
    WorkerHeartbeat,
    utcnow,
)

WORKER_STALE_AFTER = timedelta(seconds=30)
MONITOR_INTERVAL_SECONDS = 10.0


def _upsert_incident(
    db: Session,
    *,
    dedup_key: str,
    severity: str,
    category: str,
    source_type: str,
    source_key: str,
    title: str,
    details: dict[str, Any],
) -> RuntimeIncident:
    now = utcnow()
    incident = db.scalar(
        select(RuntimeIncident).where(RuntimeIncident.dedup_key == dedup_key).with_for_update()
    )
    if incident is None:
        incident = RuntimeIncident(
            dedup_key=dedup_key,
            severity=severity,
            category=category,
            source_type=source_type,
            source_key=source_key,
            title=title,
            status="open",
            details_json=details,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(incident)
        return incident
    incident.severity = severity
    incident.title = title
    incident.details_json = details
    incident.last_seen_at = now
    incident.occurrence_count += 1
    incident.updated_at = now
    if incident.status == "resolved":
        incident.status = "open"
        incident.resolved_by_user_id = None
        incident.resolved_at = None
        incident.resolution_note = None
    return incident


def _resolve_automatic_incident(db: Session, dedup_key: str, note: str) -> None:
    incident = db.scalar(
        select(RuntimeIncident)
        .where(
            RuntimeIncident.dedup_key == dedup_key,
            RuntimeIncident.status.in_(("open", "acknowledged")),
        )
        .with_for_update()
    )
    if incident is None:
        return
    now = utcnow()
    incident.status = "resolved"
    incident.resolved_at = now
    incident.resolution_note = note
    incident.updated_at = now


def _engage_infrastructure_latch(
    db: Session,
    *,
    scope_type: str,
    scope_key: str,
    reason_code: str,
    reason_text: str,
) -> None:
    latch = db.scalar(
        select(TradingControlLatch)
        .where(
            TradingControlLatch.owner_scope == "global",
            TradingControlLatch.scope_type == scope_type,
            TradingControlLatch.scope_key == scope_key,
        )
        .with_for_update()
    )
    now = utcnow()
    if latch is None:
        db.add(
            TradingControlLatch(
                owner_user_id=None,
                owner_scope="global",
                scope_type=scope_type,
                scope_key=scope_key,
                engaged=True,
                reason_code=reason_code,
                reason_text=reason_text,
                version=1,
                changed_by_user_id=None,
                changed_at=now,
                created_at=now,
            )
        )
        return
    if latch.engaged and latch.reason_code == reason_code:
        return
    latch.engaged = True
    latch.reason_code = reason_code
    latch.reason_text = reason_text
    latch.version += 1
    latch.changed_by_user_id = None
    latch.changed_at = now


def _latest_workers(db: Session) -> dict[str, WorkerHeartbeat]:
    rows = db.scalars(
        select(WorkerHeartbeat).order_by(
            WorkerHeartbeat.worker_type,
            WorkerHeartbeat.last_seen_at.desc(),
            WorkerHeartbeat.id.desc(),
        )
    ).all()
    latest: dict[str, WorkerHeartbeat] = {}
    for row in rows:
        latest.setdefault(row.worker_type, row)
    return latest


def inspect_runtime(engine: Engine, *, live_enabled: bool) -> None:
    """Evaluate operational invariants once and persist incident transitions."""

    now = utcnow()
    with Session(engine) as db:
        latest = _latest_workers(db)
        required = {"market", "shadow", "paper", "ai"}
        if live_enabled:
            required.add("live")
        for worker_type in sorted(required):
            row = latest.get(worker_type)
            age_seconds = (
                max(0.0, (now - row.last_seen_at).total_seconds()) if row is not None else None
            )
            healthy = bool(
                row is not None
                and row.status == "running"
                and age_seconds is not None
                and age_seconds <= WORKER_STALE_AFTER.total_seconds()
            )
            dedup_key = f"worker-unavailable:{worker_type}"
            if healthy:
                _resolve_automatic_incident(db, dedup_key, "Worker heartbeat has recovered")
                continue
            severity = "P0" if worker_type in {"market", "live"} else "P1"
            _upsert_incident(
                db,
                dedup_key=dedup_key,
                severity=severity,
                category="worker_unavailable",
                source_type="worker",
                source_key=worker_type,
                title=f"{worker_type} worker 不可用",
                details={
                    "status": row.status if row is not None else "missing",
                    "age_seconds": age_seconds,
                    "last_seen_at": row.last_seen_at.isoformat() if row is not None else None,
                },
            )
            if worker_type == "market":
                _engage_infrastructure_latch(
                    db,
                    scope_type="data_source",
                    scope_key="market_data",
                    reason_code="automatic_market_worker_unavailable",
                    reason_text="行情 Worker 心跳超时，自动冻结新增风险；恢复后需人工复核并解除",
                )
            elif worker_type == "live":
                _engage_infrastructure_latch(
                    db,
                    scope_type="broker_connection",
                    scope_key="binance-usdm",
                    reason_code="automatic_live_worker_unavailable",
                    reason_text="实盘 Worker 心跳超时，自动冻结新增风险；恢复后需人工复核并解除",
                )

        unknown_count = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(LiveOrderIntent.status == "unknown")
            )
            or 0
        )
        if unknown_count:
            _upsert_incident(
                db,
                dedup_key="live-order-intents:unknown",
                severity="P0",
                category="execution_unknown",
                source_type="execution",
                source_key="live-order-intents",
                title="存在交易所状态未知的实盘订单意图",
                details={"unknown_intent_count": unknown_count},
            )
            _engage_infrastructure_latch(
                db,
                scope_type="broker_connection",
                scope_key="binance-usdm",
                reason_code="automatic_execution_state_unknown",
                reason_text="存在状态未知的实盘订单，自动冻结新增风险；必须先完成交易所对账",
            )
        else:
            _resolve_automatic_incident(
                db,
                "live-order-intents:unknown",
                "No unknown live order intents remain",
            )

        invalid_revision_count = int(
            db.scalar(
                select(func.count(StrategyDeployment.id))
                .join(
                    StrategyRevision,
                    StrategyRevision.id == StrategyDeployment.strategy_revision_id,
                )
                .where(
                    StrategyDeployment.mode == "live",
                    StrategyDeployment.status.in_(("created", "running")),
                    StrategyRevision.lifecycle_status.not_in(("micro_live", "live")),
                )
            )
            or 0
        )
        if invalid_revision_count:
            _upsert_incident(
                db,
                dedup_key="live-deployments:unapproved-revision",
                severity="P0",
                category="revision_gate_bypass",
                source_type="deployment",
                source_key="live",
                title="存在未获实盘授权的运行中修订",
                details={"invalid_deployment_count": invalid_revision_count},
            )
            _engage_infrastructure_latch(
                db,
                scope_type="global",
                scope_key="*",
                reason_code="automatic_live_revision_gate_bypass",
                reason_text="检测到实盘部署修订未通过晋级门槛，自动冻结全部新增风险",
            )
        else:
            _resolve_automatic_incident(
                db,
                "live-deployments:unapproved-revision",
                "All active live deployments are bound to approved revisions",
            )
        db.commit()


def ops_loop(engine: Engine, stop_event: threading.Event, *, live_enabled: bool) -> None:
    """Run continuous operational invariant checks."""

    print("[ops] operational safety monitor started")
    # Give concurrently-starting workers one full heartbeat window.
    if stop_event.wait(WORKER_STALE_AFTER.total_seconds()):
        return
    while not stop_event.is_set():
        try:
            inspect_runtime(engine, live_enabled=live_enabled)
        except Exception as exc:
            print(f"[ops] inspection failed: {type(exc).__name__}")
        stop_event.wait(MONITOR_INTERVAL_SECONDS)
