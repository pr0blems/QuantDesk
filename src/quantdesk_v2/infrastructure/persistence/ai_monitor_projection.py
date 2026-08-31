"""SQLAlchemy adapter for the AI Monitor current-opportunity projection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...application.ai_monitor.opportunity_projection import (
    OpportunityProjectionLagging,
    OpportunityProjectionUnavailable,
)
from ...models import (
    AiMonitorOpportunity,
    AiMonitorOpportunityCurrent,
    AiMonitorPrediction,
)
from .ai_monitor_read_models import read_models_available


class SqlAlchemyOpportunityProjectionReader:
    """Read current opportunities without falling back to source tables."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def current_page(
        self,
        *,
        user_id: int,
        limit: int,
        page: int,
        now: datetime,
    ) -> dict[str, Any]:
        db = self._db
        if not read_models_available(db):
            raise OpportunityProjectionUnavailable(
                "AI Monitor 查询投影尚未部署"
            )
        base_conditions = (
            AiMonitorOpportunityCurrent.user_id == user_id,
            AiMonitorOpportunityCurrent.expires_at > now,
        )
        total = int(
            db.scalar(
                select(func.count())
                .select_from(AiMonitorOpportunityCurrent)
                .where(*base_conditions)
            )
            or 0
        )
        if total == 0:
            source_active = int(
                db.scalar(
                    select(func.count())
                    .select_from(AiMonitorOpportunity)
                    .where(
                        AiMonitorOpportunity.user_id == user_id,
                        AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                        AiMonitorOpportunity.expires_at > now,
                    )
                )
                or 0
            )
            if source_active:
                raise OpportunityProjectionLagging(
                    "AI Monitor 查询投影正在追赶，请稍后重试"
                )

        total_pages = max(1, (total + limit - 1) // limit)
        current_page = min(page, total_pages)
        projections = list(
            db.scalars(
                select(AiMonitorOpportunityCurrent)
                .where(*base_conditions)
                .order_by(
                    AiMonitorOpportunityCurrent.discovered_at.desc(),
                    AiMonitorOpportunityCurrent.opportunity_id.desc(),
                )
                .offset((current_page - 1) * limit)
                .limit(limit)
            ).all()
        )
        opportunity_ids = [item.opportunity_id for item in projections]
        prediction_ids = [
            item.prediction_id
            for item in projections
            if item.prediction_id is not None
        ]
        opportunities_by_id = (
            {
                item.id: item
                for item in db.scalars(
                    select(AiMonitorOpportunity).where(
                        AiMonitorOpportunity.id.in_(opportunity_ids)
                    )
                ).all()
            }
            if opportunity_ids
            else {}
        )
        predictions_by_id = (
            {
                item.id: item
                for item in db.scalars(
                    select(AiMonitorPrediction).where(
                        AiMonitorPrediction.id.in_(prediction_ids)
                    )
                ).all()
            }
            if prediction_ids
            else {}
        )
        rows = [
            (
                opportunities_by_id[item.opportunity_id],
                predictions_by_id.get(item.prediction_id),
            )
            for item in projections
            if item.opportunity_id in opportunities_by_id
        ]
        direction_counts = {
            str(direction): int(count)
            for direction, count in db.execute(
                select(
                    AiMonitorOpportunityCurrent.direction,
                    func.count(AiMonitorOpportunityCurrent.id),
                )
                .where(*base_conditions)
                .group_by(AiMonitorOpportunityCurrent.direction)
            ).all()
        }
        prediction_counts = {
            str(status): int(count)
            for status, count in db.execute(
                select(
                    AiMonitorOpportunityCurrent.prediction_status,
                    func.count(AiMonitorOpportunityCurrent.id),
                )
                .where(*base_conditions)
                .group_by(AiMonitorOpportunityCurrent.prediction_status)
            ).all()
            if status is not None
        }
        return {
            "rows": rows,
            "direction_counts": {
                "long": direction_counts.get("long", 0),
                "short": direction_counts.get("short", 0),
            },
            "settlement_counts": {
                "total": total,
                "pending": prediction_counts.get("pending", 0),
                "unavailable": prediction_counts.get("unavailable", 0),
            },
            "pagination": {
                "page": current_page,
                "page_size": limit,
                "total": total,
                "total_pages": total_pages,
                "has_previous": current_page > 1,
                "has_next": current_page < total_pages,
            },
        }
