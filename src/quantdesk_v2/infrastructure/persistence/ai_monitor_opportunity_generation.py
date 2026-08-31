"""Persistence adapter for opportunity-candidate admission state."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
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


def persist_candidate_opportunity(
    db: Session,
    *,
    existing: AiMonitorOpportunity | None,
    existing_prediction: AiMonitorPrediction | None,
    existing_status: str | None,
    user_id: int,
    analysis_run_id: int,
    symbol: str,
    contract_symbol: str,
    direction: str,
    timeframe: str,
    signal_confirmed: bool,
    news_score: float,
    indicator_score: float,
    combined_score: float,
    matched_indicator_keys: Sequence[str],
    news_ids: Sequence[str],
    frozen_batch_ids: Sequence[str],
    frozen_model_call_ids: Sequence[int],
    evidence: Mapping[str, Any],
    dedup_key: str,
    now: datetime,
    expires_at: datetime,
    has_new_material_news: bool,
    merge_expiration: Callable[..., datetime],
) -> tuple[AiMonitorOpportunity, bool]:
    """Create or refresh one candidate without owning scoring authority."""

    if existing is None:
        opportunity = AiMonitorOpportunity(
            public_id=str(uuid.uuid4()),
            user_id=user_id,
            analysis_run_id=analysis_run_id,
            symbol=symbol,
            contract_symbol=contract_symbol or symbol,
            direction=direction,
            status="discovered" if signal_confirmed else "candidate",
            timeframe=timeframe,
            news_score=Decimal(str(news_score)),
            indicator_score=Decimal(str(indicator_score)),
            combined_score=Decimal(str(combined_score)),
            matched_indicator_keys_json=list(matched_indicator_keys),
            news_ids_json=list(news_ids),
            news_ai_batch_ids_json=list(frozen_batch_ids),
            news_ai_model_call_ids_json=list(frozen_model_call_ids),
            evidence_json=dict(evidence),
            dedup_key=dedup_key,
            discovered_at=now,
            expires_at=expires_at,
        )
        db.add(opportunity)
        return opportunity, True

    opportunity = existing
    opportunity.analysis_run_id = analysis_run_id
    opportunity.contract_symbol = contract_symbol or symbol
    opportunity.status = "discovered" if signal_confirmed else "candidate"
    opportunity.news_score = Decimal(str(news_score))
    opportunity.indicator_score = Decimal(str(indicator_score))
    opportunity.combined_score = Decimal(str(combined_score))
    opportunity.matched_indicator_keys_json = list(matched_indicator_keys)
    opportunity.news_ids_json = list(news_ids)
    opportunity.evidence_json = dict(evidence)
    newly_confirmed = bool(signal_confirmed and existing_status != "discovered")
    if existing_prediction is None and newly_confirmed:
        opportunity.discovered_at = now
    opportunity.expires_at = merge_expiration(
        opportunity.expires_at,
        expires_at,
        has_prediction=existing_prediction is not None,
        has_new_material_news=has_new_material_news,
        newly_confirmed=newly_confirmed,
    )
    opportunity.updated_at = now
    return opportunity, False


def create_opportunity_prediction(
    db: Session,
    *,
    user_id: int,
    opportunity_id: int,
    symbol: str,
    contract_symbol: str,
    direction: str,
    timeframe: str,
    entry_price: float,
    confidence_score: float,
    news_score: float,
    indicator_score: float,
    estimated_cost_bps: float,
    settlement_version: str,
    readiness_status: str,
    calibration_sample_count: int,
    expected_gross_edge_bps: float | None,
    expected_edge_lower_bound_bps: float | None,
    evidence: Mapping[str, Any],
    predicted_at: datetime,
    due_at: datetime,
) -> AiMonitorPrediction:
    """Persist one immutable prediction fact for an admitted opportunity."""

    prediction = AiMonitorPrediction(
        public_id=str(uuid.uuid4()),
        user_id=user_id,
        opportunity_id=opportunity_id,
        symbol=symbol,
        contract_symbol=contract_symbol,
        direction=direction,
        timeframe=timeframe,
        status="pending" if entry_price > 0 else "unavailable",
        confidence_score=Decimal(str(confidence_score)),
        signal_news_score=Decimal(str(news_score)),
        signal_indicator_score=Decimal(str(indicator_score)),
        estimated_cost_bps=Decimal(str(estimated_cost_bps)),
        settlement_version=settlement_version,
        readiness_status=readiness_status,
        calibration_sample_count=calibration_sample_count,
        expected_gross_edge_bps=(
            Decimal(str(expected_gross_edge_bps))
            if expected_gross_edge_bps is not None
            else None
        ),
        expected_edge_lower_bound_bps=(
            Decimal(str(expected_edge_lower_bound_bps))
            if expected_edge_lower_bound_bps is not None
            else None
        ),
        entry_price=Decimal(str(entry_price)) if entry_price > 0 else None,
        evidence_json=dict(evidence),
        predicted_at=predicted_at,
        due_at=due_at,
        completed_at=predicted_at if entry_price <= 0 else None,
    )
    db.add(prediction)
    return prediction
