"""Read-side persistence adapter for tenant-scoped backtest history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...models import BacktestRun


class BacktestQueryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_user(self, user_id: int, *, limit: int) -> list[BacktestRun]:
        return list(
            self._session.scalars(
                select(BacktestRun)
                .where(BacktestRun.user_id == user_id)
                .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
                .limit(limit)
            ).all()
        )

    def get_for_user(self, user_id: int, run_id: int) -> BacktestRun | None:
        return self._session.scalar(
            select(BacktestRun)
            .options(selectinload(BacktestRun.trades))
            .where(BacktestRun.id == run_id, BacktestRun.user_id == user_id)
        )

