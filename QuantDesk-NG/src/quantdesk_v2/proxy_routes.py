"""Authenticated Clash-style proxy management API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import get_current_user, require_admin_write
from .models import AuditLog, ProxyNode, ProxySubscription, User
from .proxy_management import (
    _node,
    add_manual_node,
    apply_test_result,
    import_subscription,
    runtime_setting,
    set_runtime,
)
from .schemas import ProxyNodeCreate, ProxyRuntimeUpdate, ProxySubscriptionCreate

router = APIRouter(prefix="/api/v2/proxy", tags=["proxy"])


def _audit(
    db: Session,
    request: Request,
    user: User,
    action: str,
    resource_id: str,
    metadata: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="proxy",
            resource_id=resource_id,
            ip_address=request.client.host[:45] if request.client else None,
            metadata_json=metadata,
        )
    )


def _node_payload(node: ProxyNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "protocol": node.protocol,
        "host": node.host,
        "port": node.port,
        "has_credentials": bool(node.password_encrypted),
        "credential_fingerprint": node.credential_fingerprint,
        "enabled": node.enabled,
        "health_status": node.health_status,
        "last_latency_ms": node.last_latency_ms,
        "last_tested_at": node.last_tested_at,
        "last_error": node.last_error,
        "subscription_id": node.subscription_id,
    }


def _subscription_payload(subscription: ProxySubscription, count: int) -> dict[str, Any]:
    return {
        "id": subscription.id,
        "public_id": subscription.public_id,
        "name": subscription.name,
        "endpoint": subscription.endpoint,
        "has_authorization": bool(subscription.auth_encrypted),
        "enabled": subscription.enabled,
        "refresh_interval_minutes": subscription.refresh_interval_minutes,
        "source_format": subscription.source_format,
        "last_imported_at": subscription.last_imported_at,
        "last_error": subscription.last_error,
        "node_count": count,
    }


@router.get("/status")
def proxy_status(
    _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    setting = runtime_setting(db)
    nodes = list(db.scalars(select(ProxyNode).order_by(ProxyNode.name, ProxyNode.id)))
    subscriptions = list(
        db.scalars(select(ProxySubscription).order_by(ProxySubscription.created_at.desc()))
    )
    counts: dict[int, int] = {}
    for node in nodes:
        if node.subscription_id is not None:
            counts[node.subscription_id] = counts.get(node.subscription_id, 0) + 1
    return {
        "runtime": {
            "enabled": setting.enabled,
            "selection_mode": setting.selection_mode,
            "active_node_id": setting.active_node_id,
            "fallback_state": setting.fallback_state,
            "fallback_reason": setting.fallback_reason,
            "updated_at": setting.updated_at,
        },
        "nodes": [_node_payload(node) for node in nodes],
        "subscriptions": [
            _subscription_payload(item, counts.get(item.id, 0)) for item in subscriptions
        ],
    }


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
def create_subscription(
    payload: ProxySubscriptionCreate,
    request: Request,
    user: User = Depends(require_admin_write),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        subscription, imported, skipped = import_subscription(
            db,
            name=payload.name,
            content=payload.content.get_secret_value(),
            endpoint=payload.endpoint,
            authorization=(
                payload.authorization.get_secret_value() if payload.authorization else None
            ),
            enabled=payload.enabled,
            refresh_interval_minutes=payload.refresh_interval_minutes,
            master_key=request.app.state.settings.credential_master_key.get_secret_value(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.flush()
    _audit(
        db,
        request,
        user,
        "proxy_subscription_imported",
        subscription.public_id,
        {
            "imported_nodes": imported,
            "skipped_nodes": skipped,
            "source_format": subscription.source_format,
        },
    )
    db.commit()
    return {
        "subscription": _subscription_payload(subscription, imported),
        "imported_nodes": imported,
        "skipped_nodes": skipped,
    }


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
def create_node(
    payload: ProxyNodeCreate,
    request: Request,
    user: User = Depends(require_admin_write),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        node = add_manual_node(
            db,
            parsed=_node(
                name=payload.name,
                protocol=payload.protocol,
                host=payload.host,
                port=payload.port,
                username=payload.username,
                password=payload.password.get_secret_value() if payload.password else None,
            ),
            master_key=request.app.state.settings.credential_master_key.get_secret_value(),
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.flush()
    _audit(db, request, user, "proxy_node_created", str(node.id), {"protocol": node.protocol})
    db.commit()
    return _node_payload(node)


@router.post("/nodes/{node_id}/test")
def test_node(
    node_id: int,
    request: Request,
    user: User = Depends(require_admin_write),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    node = db.get(ProxyNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="proxy node not found")
    apply_test_result(db, node, request.app.state.settings.credential_master_key.get_secret_value())
    _audit(
        db,
        request,
        user,
        "proxy_node_tested",
        str(node.id),
        {"health_status": node.health_status, "latency_ms": node.last_latency_ms},
    )
    db.commit()
    return _node_payload(node)


@router.put("/runtime")
def update_runtime(
    payload: ProxyRuntimeUpdate,
    request: Request,
    user: User = Depends(require_admin_write),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        setting = set_runtime(
            db,
            enabled=payload.enabled,
            selection_mode=payload.selection_mode,
            active_node_id=payload.active_node_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _audit(
        db,
        request,
        user,
        "proxy_runtime_updated",
        "runtime",
        {"enabled": setting.enabled, "selection_mode": setting.selection_mode},
    )
    db.commit()
    return {
        "enabled": setting.enabled,
        "selection_mode": setting.selection_mode,
        "active_node_id": setting.active_node_id,
        "fallback_state": setting.fallback_state,
        "fallback_reason": setting.fallback_reason,
        "updated_at": setting.updated_at,
    }
