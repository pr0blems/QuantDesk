from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from . import __version__

router = APIRouter(prefix="/api/v2")

# These roles own the continuous data, decision and simulated-execution loops.
# Keep the readiness gate separate from the API liveness probe: an operator can
# still inspect the API when a worker is unhealthy, while automation does not
# mistake a partially running stack for a ready trading environment.
REQUIRED_WORKER_ROLES = ("market", "news", "paper", "intelligence")


class WorkerReadiness(BaseModel):
    status: Literal["active", "missing", "stale"]
    heartbeat_age_seconds: int | None


class ReadinessOut(BaseModel):
    status: Literal["ok", "not_ready"]
    database: Literal["ok", "unavailable"]
    version: str
    workers: dict[str, WorkerReadiness]


@router.get("/health/live")
def liveness() -> dict[str, str]:
    """Cheap process-level liveness probe; it intentionally does not use the DB."""

    return {"status": "ok", "version": __version__}


def _utc_naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _worker_readiness(request: Request) -> tuple[dict[str, WorkerReadiness], bool]:
    settings = request.app.state.settings
    maximum_lag = max(settings.worker_heartbeat_seconds * 2, 15)
    statement = text(
        """
        SELECT worker_key, heartbeat_at, expires_at
          FROM worker_leases
         WHERE worker_key IN :worker_keys
        """
    ).bindparams(bindparam("worker_keys", expanding=True))
    with request.app.state.database_engine.connect() as connection:
        rows = connection.execute(
            statement,
            {"worker_keys": [f"quantdesk-ng:{role}" for role in REQUIRED_WORKER_ROLES]},
        ).mappings().all()
    by_key = {str(row["worker_key"]): row for row in rows}
    now = _utc_naive_now()
    workers: dict[str, WorkerReadiness] = {}
    all_active = True
    for role in REQUIRED_WORKER_ROLES:
        row = by_key.get(f"quantdesk-ng:{role}")
        if row is None:
            workers[role] = WorkerReadiness(status="missing", heartbeat_age_seconds=None)
            all_active = False
            continue
        heartbeat = row["heartbeat_at"]
        expires_at = row["expires_at"]
        if heartbeat.tzinfo is not None:
            heartbeat = heartbeat.astimezone(UTC).replace(tzinfo=None)
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
        age_seconds = max(0, int((now - heartbeat).total_seconds()))
        if expires_at <= now or age_seconds > maximum_lag:
            workers[role] = WorkerReadiness(status="stale", heartbeat_age_seconds=age_seconds)
            all_active = False
            continue
        workers[role] = WorkerReadiness(status="active", heartbeat_age_seconds=age_seconds)
    return workers, all_active


@router.get("/health/ready", response_model=ReadinessOut)
def readiness(request: Request):
    """Report whether DB-backed continuous services are safe to rely on."""

    try:
        workers, all_active = _worker_readiness(request)
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content=ReadinessOut(
                status="not_ready",
                database="unavailable",
                version=__version__,
                workers={},
            ).model_dump(),
        )
    payload = ReadinessOut(
        status="ok" if all_active else "not_ready",
        database="ok",
        version=__version__,
        workers=workers,
    )
    if not all_active:
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
