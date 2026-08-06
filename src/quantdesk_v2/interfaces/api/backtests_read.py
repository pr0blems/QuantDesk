"""Tenant-scoped read API for persisted backtest runs."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_current_user
from ...infrastructure.persistence.backtests import BacktestQueryRepository
from ...models import User
from .backtest_presenters import backtest_run_detail, backtest_run_summary

router = APIRouter()


@router.get("/backtests")
def list_backtests(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=12, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    runs = BacktestQueryRepository(db).list_for_user(user.id, limit=limit)
    return {"items": [backtest_run_summary(run) for run in runs]}


@router.get("/backtests/{run_id}")
def get_backtest(
    run_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    run = BacktestQueryRepository(db).get_for_user(user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return backtest_run_detail(run)

