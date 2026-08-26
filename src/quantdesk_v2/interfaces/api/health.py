from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ... import __version__
from ...database import get_db
from ...dependencies import get_current_user, require_admin_write
from ...models import (
    AuditLog,
    LiveOrderIntent,
    RuntimeIncident,
    StrategyDeployment,
    StrategyRevision,
    TradingControlLatch,
    User,
    WorkerHeartbeat,
    utcnow,
)
from ...schemas import HealthOut

router = APIRouter()
WORKER_STALE_AFTER = timedelta(seconds=20)


class IncidentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    note: str = Field(min_length=10, max_length=500)
    confirmed: bool


@router.get("/health", response_model=HealthOut)
def health(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    settings = request.app.state.settings
    return HealthOut(
        status="ok",
        database="ok",
        version=__version__,
        database_dialect=db.bind.dialect.name if db.bind else "unknown",
        tls_required=settings.db_ssl_required,
    )


@router.get("/ready")
def ready(
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """API process readiness; worker health is reported separately."""

    try:
        db.execute(text("SELECT 1"))
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="数据库或迁移状态不可用") from None
    return {
        "status": "ready",
        "database": "ready",
        "schema_revision": str(revision),
        "version": __version__,
    }


@router.get("/system/trading-readiness")
def trading_readiness(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Explain whether infrastructure can safely accept new trading risk."""

    now = utcnow()
    rows = db.scalars(
        select(WorkerHeartbeat).order_by(
            WorkerHeartbeat.worker_type,
            WorkerHeartbeat.last_seen_at.desc(),
        )
    ).all()
    newest: dict[str, WorkerHeartbeat] = {}
    for row in rows:
        newest.setdefault(row.worker_type, row)
    required = {"market", "shadow", "paper", "ai", "ops"}
    if request.app.state.settings.binance_live_trading_enabled:
        required.add("live")
    workers: list[dict[str, Any]] = []
    worker_blockers: list[str] = []
    for worker_type in sorted(required | set(newest)):
        row = newest.get(worker_type)
        age_seconds = (
            max(0.0, (now - row.last_seen_at).total_seconds()) if row is not None else None
        )
        fresh = bool(
            row is not None
            and row.status == "running"
            and age_seconds is not None
            and age_seconds <= WORKER_STALE_AFTER.total_seconds()
        )
        if worker_type in required and not fresh:
            worker_blockers.append(f"worker_{worker_type}_unavailable")
        workers.append(
            {
                "worker_type": worker_type,
                "required": worker_type in required,
                "status": row.status if row is not None else "missing",
                "fresh": fresh,
                "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                "release_version": row.release_version if row is not None else None,
                "instance_key": row.instance_key if row is not None else None,
                "last_seen_at": row.last_seen_at if row is not None else None,
            }
        )
    controls = db.scalars(
        select(TradingControlLatch)
        .where(
            TradingControlLatch.engaged.is_(True),
            or_(
                TradingControlLatch.owner_scope == "global",
                TradingControlLatch.owner_user_id == user.id,
            ),
        )
        .order_by(TradingControlLatch.scope_type, TradingControlLatch.scope_key)
    ).all()
    invalid_live_count = int(
        db.scalar(
            select(func.count(StrategyDeployment.id))
            .select_from(StrategyDeployment)
            .join(
                StrategyRevision,
                StrategyRevision.id == StrategyDeployment.strategy_revision_id,
            )
            .where(
                StrategyDeployment.user_id == user.id,
                StrategyDeployment.mode == "live",
                StrategyDeployment.status.in_(("created", "running")),
                StrategyRevision.lifecycle_status.not_in(("micro_live", "live")),
            )
        )
        or 0
    )
    blockers = list(worker_blockers)
    if controls:
        blockers.append("kill_switch_engaged")
    if invalid_live_count:
        blockers.append("live_deployment_revision_not_approved")
    open_p0_count = int(
        db.scalar(
            select(func.count(RuntimeIncident.id)).where(
                RuntimeIncident.severity == "P0",
                RuntimeIncident.status.in_(("open", "acknowledged")),
            )
        )
        or 0
    )
    if open_p0_count:
        blockers.append("open_p0_incident")
    return {
        "ready_for_new_risk": not blockers,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "workers": workers,
        "engaged_controls": [
            {
                "scope_type": row.scope_type,
                "scope_key": row.scope_key,
                "reason_code": row.reason_code,
                "reason": row.reason_text,
                "changed_at": row.changed_at,
            }
            for row in controls
        ],
        "invalid_live_deployment_count": invalid_live_count,
        "open_p0_incident_count": open_p0_count,
        "checked_at": now,
    }


def _incident_out(row: RuntimeIncident) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "dedup_key": row.dedup_key,
        "severity": row.severity,
        "category": row.category,
        "source_type": row.source_type,
        "source_key": row.source_key,
        "title": row.title,
        "status": row.status,
        "details": row.details_json,
        "occurrence_count": row.occurrence_count,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
        "resolution_note": row.resolution_note,
        "updated_at": row.updated_at,
    }


@router.get("/system/incidents")
def list_runtime_incidents(
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
    incident_status: str | None = None,
) -> dict[str, Any]:
    statement = select(RuntimeIncident)
    if incident_status is not None:
        if incident_status not in {"open", "acknowledged", "resolved"}:
            raise HTTPException(status_code=422, detail="事故状态参数无效")
        statement = statement.where(RuntimeIncident.status == incident_status)
    rows = db.scalars(
        statement.order_by(
            RuntimeIncident.status,
            RuntimeIncident.severity,
            RuntimeIncident.last_seen_at.desc(),
        ).limit(500)
    ).all()
    return {"items": [_incident_out(row) for row in rows]}


def _incident_action(
    db: Session,
    incident_id: str,
    payload: IncidentActionRequest,
    admin: User,
    *,
    action: str,
) -> RuntimeIncident:
    if payload.confirmed is not True:
        raise HTTPException(status_code=422, detail="必须显式确认事故处理操作")
    incident = db.scalar(
        select(RuntimeIncident).where(RuntimeIncident.public_id == incident_id).with_for_update()
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="未找到运行事故")
    now = utcnow()
    if action == "acknowledge":
        if incident.status == "resolved":
            raise HTTPException(status_code=409, detail="已解决事故不能再次确认")
        incident.status = "acknowledged"
        incident.acknowledged_by_user_id = admin.id
        incident.acknowledged_at = now
    else:
        incident.status = "resolved"
        incident.resolved_by_user_id = admin.id
        incident.resolved_at = now
        incident.resolution_note = payload.note
    incident.updated_at = now
    db.add(
        AuditLog(
            user_id=admin.id,
            action=f"runtime.incident.{action}",
            resource_type="runtime_incident",
            resource_id=incident.public_id,
            metadata_json={"note": payload.note, "dedup_key": incident.dedup_key},
            created_at=now,
        )
    )
    db.commit()
    return incident


@router.post("/system/incidents/{incident_id}/acknowledge")
def acknowledge_runtime_incident(
    incident_id: str,
    payload: IncidentActionRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    return {
        "incident": _incident_out(
            _incident_action(db, incident_id, payload, admin, action="acknowledge")
        )
    }


@router.post("/system/incidents/{incident_id}/resolve")
def resolve_runtime_incident(
    incident_id: str,
    payload: IncidentActionRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    return {
        "incident": _incident_out(
            _incident_action(db, incident_id, payload, admin, action="resolve")
        )
    }


@router.get("/system/metrics", response_class=PlainTextResponse)
def system_metrics(
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> PlainTextResponse:
    """Prometheus text exposition for the safety control plane."""

    now = utcnow()
    latest: dict[str, WorkerHeartbeat] = {}
    for row in db.scalars(
        select(WorkerHeartbeat).order_by(
            WorkerHeartbeat.worker_type,
            WorkerHeartbeat.last_seen_at.desc(),
        )
    ).all():
        latest.setdefault(row.worker_type, row)
    lines = [
        "# HELP quantdesk_worker_up Worker heartbeat is fresh and running.",
        "# TYPE quantdesk_worker_up gauge",
    ]
    for worker_type, row in sorted(latest.items()):
        age = max(0.0, (now - row.last_seen_at).total_seconds())
        up = int(row.status == "running" and age <= WORKER_STALE_AFTER.total_seconds())
        lines.append(f'quantdesk_worker_up{{worker="{worker_type}"}} {up}')
    engaged = int(
        db.scalar(
            select(func.count(TradingControlLatch.id)).where(TradingControlLatch.engaged.is_(True))
        )
        or 0
    )
    open_p0 = int(
        db.scalar(
            select(func.count(RuntimeIncident.id)).where(
                RuntimeIncident.severity == "P0",
                RuntimeIncident.status.in_(("open", "acknowledged")),
            )
        )
        or 0
    )
    unknown_intents = int(
        db.scalar(select(func.count(LiveOrderIntent.id)).where(LiveOrderIntent.status == "unknown"))
        or 0
    )
    lines.extend(
        (
            "# TYPE quantdesk_trading_controls_engaged gauge",
            f"quantdesk_trading_controls_engaged {engaged}",
            "# TYPE quantdesk_runtime_incidents_p0_open gauge",
            f"quantdesk_runtime_incidents_p0_open {open_p0}",
            "# TYPE quantdesk_live_order_intents_unknown gauge",
            f"quantdesk_live_order_intents_unknown {unknown_intents}",
        )
    )
    return PlainTextResponse("\n".join(lines) + "\n")
