"""Shared HTTP-boundary helpers for authenticated API routers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ...models import AuditLog, User
from ...monitor import MonitorRepository, MonitorUnavailable


def monitor_repository(request: Request) -> MonitorRepository:
    settings = request.app.state.settings
    try:
        return MonitorRepository(
            request.app.state.database_engine,
            settings.monitor_symbols_config,
        )
    except MonitorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def require_expected_user(request: Request, user: User) -> None:
    """Block sensitive writes when a browser tab silently changes accounts."""

    expected = request.headers.get("X-QuantDesk-User-ID", "").strip()
    if not expected:
        raise HTTPException(status_code=428, detail="expected user identity is required")
    try:
        expected_user_id = int(expected)
    except ValueError:
        raise HTTPException(status_code=400, detail="expected user identity is invalid") from None
    if expected_user_id != user.id:
        raise HTTPException(
            status_code=409,
            detail="authenticated user changed; sign in again before updating credentials",
        )


def add_audit_log(
    db: Session,
    request: Request,
    action: str,
    user_id: int | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=request.client.host[:45] if request.client else None,
            metadata_json=metadata,
        )
    )
