"""Persistence adapter for opportunity-candidate admission state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AiMonitorOpportunity, AiMonitorPrediction


@dataclass(frozen=True)
class CandidatePersistenceState:
    existing: AiMonitorOpportunity | None
    existing_status: str | None
    existing_prediction: AiMonitorPrediction | None
    duplicated: bool
    frozen_batch_ids: list[str]
    frozen_model_call_ids: list[int]
    has_new_material_news: bool


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


def candidate_persistence_state(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    direction: str,
    dedup_key: str,
    news_ids: list[str],
    model_call_audit: dict[str, dict[str, Any]],
    now: datetime,
) -> CandidatePersistenceState:
    """Resolve candidate identity and freeze model-audit lineage before scoring."""

    active_for_symbol = db.scalars(
        select(AiMonitorOpportunity)
        .where(
            AiMonitorOpportunity.user_id == user_id,
            AiMonitorOpportunity.symbol == symbol,
            AiMonitorOpportunity.status.in_(("candidate", "discovered")),
            AiMonitorOpportunity.expires_at > now,
        )
        .order_by(
            AiMonitorOpportunity.updated_at.desc(),
            AiMonitorOpportunity.id.desc(),
        )
    ).all()
    existing = next(
        (item for item in active_for_symbol if item.direction == direction),
        None,
    )
    existing_status = existing.status if existing is not None else None
    existing_prediction = (
        db.scalar(
            select(AiMonitorPrediction).where(
                AiMonitorPrediction.opportunity_id == existing.id
            )
        )
        if existing is not None
        else None
    )
    if active_for_symbol and existing is None:
        for previous in active_for_symbol:
            previous.status = "expired"
            previous.expires_at = min(previous.expires_at, now)
            previous.updated_at = now
    duplicated = bool(
        existing is None
        and db.scalar(
            select(AiMonitorOpportunity.id).where(
                AiMonitorOpportunity.dedup_key == dedup_key
            )
        )
    )
    if existing is None:
        audit_rows = [
            model_call_audit[item]
            for item in news_ids
            if item in model_call_audit
        ]
        frozen_batch_ids = sorted({str(item["batch_id"]) for item in audit_rows})
        frozen_model_call_ids = sorted(
            {
                int(call_id)
                for item in audit_rows
                for call_id in item.get("call_ids", [])
            }
        )
    else:
        frozen_batch_ids = list(existing.news_ai_batch_ids_json or [])
        frozen_model_call_ids = [
            int(item) for item in (existing.news_ai_model_call_ids_json or [])
        ]
    existing_news_ids = (
        set(existing.news_ids_json or []) if existing is not None else set()
    )
    return CandidatePersistenceState(
        existing=existing,
        existing_status=existing_status,
        existing_prediction=existing_prediction,
        duplicated=duplicated,
        frozen_batch_ids=frozen_batch_ids,
        frozen_model_call_ids=frozen_model_call_ids,
        has_new_material_news=bool(set(news_ids) - existing_news_ids),
    )
