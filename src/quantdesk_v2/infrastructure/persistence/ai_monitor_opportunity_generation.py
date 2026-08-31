"""Persistence adapter for opportunity-candidate admission state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AiMonitorOpportunity, AiMonitorPrediction


def consumed_news_ids_by_direction(
    db: Session,
    *,
    user_id: int,
    predicted_since: datetime,
) -> dict[str, set[str]]:
    """Load prediction-backed news consumption once per direction."""

    result: dict[str, set[str]] = {}
    rows = db.execute(
        select(
            AiMonitorOpportunity.symbol,
            AiMonitorOpportunity.direction,
            AiMonitorOpportunity.news_ids_json,
        )
        .join(
            AiMonitorPrediction,
            AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
        )
        .where(
            AiMonitorOpportunity.user_id == user_id,
            AiMonitorPrediction.predicted_at >= predicted_since,
        )
    ).all()
    for _symbol, direction, news_ids in rows:
        result.setdefault(str(direction), set()).update(
            str(news_id) for news_id in (news_ids or []) if str(news_id)
        )
    return result


def active_candidate_keys(
    db: Session,
    *,
    user_id: int,
    now: datetime,
) -> set[tuple[str, str]]:
    """Load active symbol/direction keys that may retain their existing trigger."""

    return {
        (str(symbol), str(direction))
        for symbol, direction in db.execute(
            select(AiMonitorOpportunity.symbol, AiMonitorOpportunity.direction).where(
                AiMonitorOpportunity.user_id == user_id,
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at > now,
            )
        ).all()
    }
