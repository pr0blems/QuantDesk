"""Revision-pinned deployment control plane and reproducibility evidence."""

from __future__ import annotations

import copy
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import get_current_user
from .models import (
    AuditLog,
    LiveOrderIntent,
    StrategyDeployment,
    StrategyRevision,
    StrategyRunManifest,
    StrategySignal,
    User,
    UserStrategy,
    utcnow,
)
from .strategy_artifacts import add_run_manifest
from .strategy_lifecycle import SHADOW_ELIGIBLE_STATUSES

router = APIRouter(prefix="/api/v2", tags=["strategy-deployments"])


class ShadowDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    expected_version: int = Field(ge=1)
    mode: Literal["shadow"]
    name: str = Field(min_length=1, max_length=100)
    symbols: list[str] = Field(min_length=1, max_length=100)
    confirmed: Literal[True]


class DeploymentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_status: Literal["created", "running", "paused", "stopped", "error"]
    reason: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


class DeploymentRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    target_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


def _audit(
    db: Session,
    request: Request,
    action: str,
    user_id: int,
    deployment: StrategyDeployment,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type="strategy_deployment",
            resource_id=deployment.public_id,
            ip_address=request.client.host[:45] if request.client else None,
            metadata_json=metadata,
            created_at=utcnow(),
        )
    )


def _deployment_out(deployment: StrategyDeployment) -> dict[str, Any]:
    return {
        "id": deployment.public_id,
        "strategy_id": deployment.strategy_id,
        "strategy_revision_id": deployment.strategy_revision_id,
        "mode": deployment.mode,
        "target_account_id": deployment.target_account_id,
        "name": deployment.name,
        "status": deployment.status,
        "universe_override": copy.deepcopy(deployment.universe_override_json),
        "risk_override": copy.deepcopy(deployment.risk_override_json),
        "runtime_state": copy.deepcopy(deployment.runtime_state_json),
        "last_evaluated_bar_time": deployment.last_evaluated_bar_time,
        "last_error_code": deployment.last_error_code,
        "started_at": deployment.started_at,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
    }


def _manifest_out(manifest: StrategyRunManifest) -> dict[str, Any]:
    return {
        "id": manifest.public_id,
        "deployment_id": manifest.deployment_id,
        "strategy_revision_id": manifest.strategy_revision_id,
        "mode": manifest.mode,
        "data_set_id": manifest.data_set_id,
        "engine_version": manifest.engine_version,
        "cost_model_version": manifest.cost_model_version,
        "fill_model_version": manifest.fill_model_version,
        "risk_policy_version": manifest.risk_policy_version,
        "manifest": copy.deepcopy(manifest.manifest_json),
        "manifest_hash": manifest.manifest_hash,
        "created_at": manifest.created_at,
    }


def _owned_deployment(
    db: Session,
    user_id: int,
    public_id: str,
    *,
    for_update: bool = False,
) -> StrategyDeployment | None:
    statement = select(StrategyDeployment).where(
        StrategyDeployment.user_id == user_id,
        StrategyDeployment.public_id == public_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


@router.get("/deployments")
def list_deployments(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    mode: Literal["paper", "shadow", "live"] | None = None,
) -> dict[str, Any]:
    statement = select(StrategyDeployment).where(StrategyDeployment.user_id == user.id)
    if mode is not None:
        statement = statement.where(StrategyDeployment.mode == mode)
    rows = db.scalars(statement.order_by(StrategyDeployment.created_at.desc())).all()
    return {"items": [_deployment_out(row) for row in rows]}


@router.post(
    "/strategies/{strategy_public_id}/deployments",
    status_code=status.HTTP_201_CREATED,
)
def create_shadow_deployment(
    strategy_public_id: str,
    payload: ShadowDeploymentRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    deployment_id = str(uuid.UUID(payload.request_id))
    existing = _owned_deployment(db, user.id, deployment_id)
    if existing is not None:
        return {"deployment": _deployment_out(existing), "idempotent": True}
    strategy = db.scalar(
        select(UserStrategy)
        .where(
            UserStrategy.user_id == user.id,
            UserStrategy.public_id == strategy_public_id,
            UserStrategy.status == "active",
        )
        .with_for_update()
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="未找到该策略")
    if strategy.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="策略版本已变化，请刷新后重试")
    revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.user_id == user.id,
            StrategyRevision.version == strategy.version,
        )
    )
    if revision is None:
        raise HTTPException(status_code=409, detail="当前策略缺少不可变 revision")
    if revision.lifecycle_status not in SHADOW_ELIGIBLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前 revision 尚未取得影子运行资格",
                "lifecycle_status": revision.lifecycle_status,
                "required_statuses": sorted(SHADOW_ELIGIBLE_STATUSES),
            },
        )
    symbols = sorted(
        {str(symbol).strip().upper() for symbol in payload.symbols if str(symbol).strip()}
    )
    if not symbols or any(len(symbol) > 32 for symbol in symbols):
        raise HTTPException(status_code=422, detail="影子运行品种列表无效")
    deployment = StrategyDeployment(
        public_id=deployment_id,
        user_id=user.id,
        strategy_id=strategy.id,
        strategy_revision_id=revision.id,
        mode="shadow",
        name=payload.name,
        status="running",
        universe_override_json={"symbols": symbols},
        risk_override_json=copy.deepcopy(strategy.risk_defaults_json),
        runtime_state_json={
            "decision_protocol_version": "strategy_decision_v2",
            "evaluations": 0,
            "decisions": 0,
            "order_intents": 0,
        },
        started_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(deployment)
    add_run_manifest(
        db,
        deployment,
        revision,
        data_set_id="realtime:point-in-time",
        extra={"symbols": symbols, "network_writes": False},
    )
    _audit(
        db,
        request,
        "strategy.deployment.shadow.create",
        user.id,
        deployment,
        {"revision_id": revision.id, "strategy_version": revision.version},
    )
    db.commit()
    return {"deployment": _deployment_out(deployment), "idempotent": False}


@router.get("/deployments/{deployment_id}/manifest")
def get_deployment_manifest(
    deployment_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    deployment = _owned_deployment(db, user.id, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="未找到部署记录")
    manifest = db.scalar(
        select(StrategyRunManifest).where(
            StrategyRunManifest.deployment_id == deployment.id,
            StrategyRunManifest.user_id == user.id,
        )
    )
    if manifest is None:
        raise HTTPException(status_code=409, detail="该旧部署缺少可复现运行清单")
    return _manifest_out(manifest)


def _transition_shadow_deployment(
    deployment_id: str,
    action: Literal["pause", "resume"],
    payload: DeploymentTransitionRequest,
    request: Request,
    db: Session,
    user: User,
) -> dict[str, Any]:
    deployment = _owned_deployment(db, user.id, deployment_id, for_update=True)
    if deployment is None:
        raise HTTPException(status_code=404, detail="未找到部署记录")
    if deployment.mode != "shadow":
        raise HTTPException(status_code=409, detail="模拟盘和实盘请使用对应账户控制接口")
    if deployment.status != payload.expected_status:
        raise HTTPException(status_code=409, detail="部署状态已变化，请刷新后重试")
    target = "paused" if action == "pause" else "running"
    if action == "resume" and deployment.status not in {"paused", "error"}:
        raise HTTPException(status_code=409, detail="只有暂停或异常的影子部署可以恢复")
    if action == "pause" and deployment.status != "running":
        raise HTTPException(status_code=409, detail="只有运行中的影子部署可以暂停")
    deployment.status = target
    deployment.last_error_code = None
    deployment.updated_at = utcnow()
    if action == "resume":
        deployment.started_at = utcnow()
    _audit(
        db,
        request,
        f"strategy.deployment.shadow.{action}",
        user.id,
        deployment,
        {"reason": payload.reason},
    )
    db.commit()
    return _deployment_out(deployment)


@router.post("/deployments/{deployment_id}/pause")
def pause_shadow_deployment(
    deployment_id: str,
    payload: DeploymentTransitionRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _transition_shadow_deployment(deployment_id, "pause", payload, request, db, user)


@router.post("/deployments/{deployment_id}/resume")
def resume_shadow_deployment(
    deployment_id: str,
    payload: DeploymentTransitionRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _transition_shadow_deployment(deployment_id, "resume", payload, request, db, user)


@router.post("/deployments/{deployment_id}/rollback")
def rollback_shadow_deployment(
    deployment_id: str,
    payload: DeploymentRollbackRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    replacement_id = str(uuid.UUID(payload.request_id))
    existing = _owned_deployment(db, user.id, replacement_id)
    if existing is not None:
        return {"deployment": _deployment_out(existing), "idempotent": True}
    deployment = _owned_deployment(db, user.id, deployment_id, for_update=True)
    if deployment is None:
        raise HTTPException(status_code=404, detail="未找到部署记录")
    if deployment.mode != "shadow":
        raise HTTPException(status_code=409, detail="该接口仅用于影子部署回滚")
    revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.user_strategy_id == deployment.strategy_id,
            StrategyRevision.user_id == user.id,
            StrategyRevision.version == payload.target_version,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="未找到目标策略 revision")
    if revision.lifecycle_status not in SHADOW_ELIGIBLE_STATUSES:
        raise HTTPException(status_code=409, detail="目标 revision 尚未取得影子运行资格")
    deployment.status = "stopped"
    deployment.updated_at = utcnow()
    replacement = StrategyDeployment(
        public_id=replacement_id,
        user_id=user.id,
        strategy_id=deployment.strategy_id,
        strategy_revision_id=revision.id,
        mode="shadow",
        name=f"{deployment.name} · 回滚 v{revision.version}"[:100],
        status="paused",
        universe_override_json=copy.deepcopy(deployment.universe_override_json),
        risk_override_json=copy.deepcopy(deployment.risk_override_json),
        runtime_state_json={"replaces_deployment_id": deployment.public_id},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(replacement)
    add_run_manifest(
        db,
        replacement,
        revision,
        data_set_id="realtime:point-in-time",
        extra={"replaces_deployment_id": deployment.public_id},
    )
    _audit(
        db,
        request,
        "strategy.deployment.shadow.rollback",
        user.id,
        replacement,
        {
            "source_deployment_id": deployment.public_id,
            "target_version": revision.version,
            "reason": payload.reason,
        },
    )
    db.commit()
    return {"deployment": _deployment_out(replacement), "idempotent": False}


@router.get("/deployments/{deployment_id}/reconciliation")
def deployment_reconciliation(
    deployment_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    deployment = _owned_deployment(db, user.id, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="未找到部署记录")
    signal_count = int(
        db.scalar(
            select(func.count(StrategySignal.id)).where(
                StrategySignal.user_id == user.id,
                StrategySignal.deployment_id == deployment.id,
            )
        )
        or 0
    )
    unknown_orders = 0
    if deployment.mode == "live" and deployment.target_account_id is not None:
        unknown_orders = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(
                    LiveOrderIntent.user_id == user.id,
                    LiveOrderIntent.live_account_id == deployment.target_account_id,
                    LiveOrderIntent.status.in_(("created", "submitted", "unknown")),
                )
            )
            or 0
        )
    blockers = []
    if unknown_orders:
        blockers.append("unresolved_exchange_orders")
    if deployment.last_error_code:
        blockers.append("deployment_runtime_error")
    return {
        "deployment_id": deployment.public_id,
        "mode": deployment.mode,
        "status": "matched" if not blockers else "review_required",
        "blockers": blockers,
        "signal_count": signal_count,
        "unresolved_order_count": unknown_orders,
        "last_evaluated_bar_time": deployment.last_evaluated_bar_time,
        "last_error_code": deployment.last_error_code,
        "checked_at": utcnow(),
    }
