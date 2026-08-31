"""Stable presentation and mutation helpers shared by AI Monitor routers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ...models import AiMonitorRun, AuditLog, NewsAiBatch, User


def utc_out(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_expected_user(request: Request, user: User) -> None:
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
            detail="authenticated user changed; sign in again before updating AI monitor",
        )


def add_ai_monitor_audit(
    db: Session,
    request: Request,
    user_id: int,
    action: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type="ai_monitor",
            resource_id=resource_id,
            ip_address=request.client.host[:45] if request.client else None,
            metadata_json=metadata,
        )
    )


def run_out(run: AiMonitorRun, batch: NewsAiBatch | None = None) -> dict[str, Any]:
    summary = dict(run.summary_json or {})
    if batch is not None:
        summary.setdefault("market_sentiment", batch.market_sentiment)
        summary.setdefault(
            "market_confidence",
            float(batch.market_confidence) if batch.market_confidence is not None else None,
        )
        summary.setdefault("market_summary", batch.market_summary)
        summary.setdefault("model_name", batch.model_name)
    return {
        "id": run.public_id,
        "run_type": run.run_type,
        "status": run.status,
        "input_count": int(run.input_count),
        "matched_count": int(run.matched_count),
        "summary": summary,
        "error_message": run.error_message,
        "news_batch_id": run.news_batch_id,
        "started_at": utc_out(run.started_at),
        "completed_at": utc_out(run.completed_at),
        "created_at": utc_out(run.created_at),
        "updated_at": utc_out(run.updated_at),
    }
