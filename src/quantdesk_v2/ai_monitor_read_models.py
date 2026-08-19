"""Incremental read-model projections for AI monitor list and analytics queries.

The source tables remain the audit authority.  These projections are disposable:
they can be rebuilt after a migration without changing opportunity or prediction
history.  Runtime callers safely no-op while a rolling deployment has not yet
applied migration 0059.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from .models import (
    AiMonitorOpportunity,
    AiMonitorOpportunityCurrent,
    AiMonitorPrediction,
    AiMonitorPredictionFact,
    AiMonitorScoreHistory,
    OpportunityGateDecision,
    OpportunityMarketSnapshot,
    utcnow,
)

READ_MODEL_TABLES = frozenset(
    {
        "ai_monitor_prediction_facts",
        "ai_monitor_opportunity_current",
        "ai_monitor_score_history",
    }
)
PROJECTION_FLUSH_BATCH_SIZE = 100
SCORE_HISTORY_READ_BATCH_SIZE = 1000
_availability_cache: dict[int, tuple[float, bool]] = {}
_availability_lock = threading.Lock()


def read_models_available(db: Session, *, refresh: bool = False) -> bool:
    """Return whether all projection tables exist, with a short engine cache."""

    engine = db.get_bind()
    cache_key = id(engine)
    now = time.monotonic()
    with _availability_lock:
        cached = _availability_cache.get(cache_key)
        if not refresh and cached is not None and now - cached[0] < 300:
            return cached[1]
    # Inspect through the Session connection. Inspecting the Engine may borrow
    # and roll back the same DBAPI connection used by an in-memory SQLite
    # transaction, which made a forced availability refresh discard pending
    # projection writes during tests and maintenance rebuilds.
    available = READ_MODEL_TABLES.issubset(
        set(inspect(db.connection()).get_table_names())
    )
    with _availability_lock:
        _availability_cache[cache_key] = (now, available)
    return available


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _first(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _domain_score(domains: Mapping[str, Any], key: str) -> Decimal | None:
    value = domains.get(key)
    if isinstance(value, Mapping):
        value = _first(value.get("score"), value.get("directional_score"))
    return _decimal(value)


def _quote_quality(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"healthy", "passed", "ok", "live"}:
        return "passed"
    if normalized in {"degraded", "partial", "last_trade_only", "stale"}:
        return "partial"
    if normalized in {"blocked", "rejected", "invalid"}:
        return "blocked"
    return "missing"


def _event_risk(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"blocked", "critical"}:
        return "blocked"
    if normalized in {"warning", "high", "medium"}:
        return "warning"
    return "clear"


def _projection_inputs(
    opportunity: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None,
) -> dict[str, Any]:
    opportunity_evidence = _mapping(opportunity.evidence_json)
    prediction_evidence = _mapping(prediction.evidence_json if prediction else {})
    evidence = {**opportunity_evidence, **prediction_evidence}
    scores = _mapping(evidence.get("signal_scores"))
    components = _mapping(evidence.get("score_components"))
    domains = _mapping(evidence.get("enhanced_domain_scoring")).get("domains")
    domains = _mapping(domains)
    flow = _mapping(evidence.get("market_flow"))
    quality = _mapping(evidence.get("market_quality"))
    data_quality = _mapping(evidence.get("data_quality"))
    versions = _mapping(evidence.get("version"))
    market_environment = _mapping(evidence.get("market_environment"))
    event_gate = _mapping(evidence.get("event_gate"))
    gate = _mapping(evidence.get("gate_summary"))
    market = _mapping(evidence.get("market"))
    option_flow = _mapping(flow.get("option_flow"))
    gex = _mapping(flow.get("gex"))
    institutional = _mapping(flow.get("institutional_flow"))

    coverage = _decimal(
        _first(
            data_quality.get("coverage"),
            data_quality.get("data_coverage"),
            quality.get("data_coverage"),
        )
    )
    if coverage is not None and coverage <= 1:
        coverage *= 100
    blocking = gate.get("blocking_reasons") or gate.get("failed_checks") or []
    if isinstance(blocking, Mapping):
        blocking = [str(key) for key, passed in blocking.items() if passed is False]
    elif not isinstance(blocking, list):
        blocking = [str(blocking)] if blocking else []
    primary_blocker = str(blocking[0])[:191] if blocking else None

    return {
        "evidence": evidence,
        "scores": scores,
        "components": components,
        "domains": domains,
        "gate": gate,
        "primary_blocker": primary_blocker,
        "market_session": str(
            _first(
                quality.get("market_session"),
                evidence.get("market_session"),
                _mapping(market_environment.get("market_session")).get("key"),
                "unknown",
            )
        )[:16],
        "quote_quality": _quote_quality(
            _first(quality.get("data_status"), data_quality.get("status"))
        ),
        "event_risk": _event_risk(
            _first(
                event_gate.get("risk_level"),
                _mapping(market_environment.get("events")).get("risk_level"),
            )
        ),
        "data_coverage": coverage,
        "news_score": _decimal(_first(scores.get("news"), opportunity.news_score)),
        "technical_score": _decimal(
            _first(
                scores.get("indicator"), components.get("technical"), opportunity.indicator_score
            )
        ),
        "market_context_score": _domain_score(domains, "macro_market"),
        "option_flow_score": _decimal(
            _first(option_flow.get("score"), _domain_score(domains, "options_flow"))
        ),
        "gex_score": _decimal(_first(gex.get("score"), _domain_score(domains, "gex"))),
        "institutional_flow_score": _decimal(
            _first(
                institutional.get("score"),
                _domain_score(domains, "institutional_flow"),
            )
        ),
        "market_flow_score": _decimal(
            _first(scores.get("market_flow"), flow.get("score"), components.get("market_flow"))
        ),
        "combined_score": _decimal(_first(scores.get("combined"), opportunity.combined_score)),
        # Binance mapped-contract ticker is the only execution/mark/settlement
        # price authority. Finnhub/UW quotes remain feature evidence only.
        "current_price": _decimal(market.get("price")),
        "weights_version": str(_first(versions.get("weights"), "unknown"))[:32],
        "feature_version": str(_first(versions.get("feature"), "unknown"))[:32],
        "decision_version": str(_first(versions.get("decision"), "unknown"))[:32],
    }


def refresh_prediction_facts(
    db: Session,
    *,
    user_id: int | None = None,
    limit: int = 2000,
) -> int:
    statement = (
        select(AiMonitorPrediction, AiMonitorOpportunity, OpportunityMarketSnapshot)
        .join(
            AiMonitorOpportunity,
            AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
        )
        .outerjoin(
            OpportunityMarketSnapshot,
            OpportunityMarketSnapshot.opportunity_id == AiMonitorOpportunity.id,
        )
        .order_by(AiMonitorPrediction.updated_at.desc(), AiMonitorPrediction.id.desc())
        .limit(max(1, min(int(limit), 10000)))
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    rows = list(db.execute(statement).all())
    if not rows:
        return 0
    prediction_ids = [prediction.id for prediction, _opportunity, _snapshot in rows]
    existing = {
        fact.prediction_id: fact
        for fact in db.scalars(
            select(AiMonitorPredictionFact).where(
                AiMonitorPredictionFact.prediction_id.in_(prediction_ids)
            )
        ).all()
    }
    now = utcnow()
    changed = 0
    for prediction, opportunity, snapshot in rows:
        fact = existing.get(prediction.id)
        if fact is not None and fact.source_updated_at >= prediction.updated_at:
            continue
        projection = _projection_inputs(opportunity, prediction)
        snapshot_complete = snapshot is not None
        invalid_reason = None
        if prediction.entry_price is None:
            invalid_reason = "missing_entry_price"
        elif prediction.status == "completed" and prediction.exit_price is None:
            invalid_reason = "incomplete_settlement"
        elif not snapshot_complete:
            invalid_reason = "legacy_missing_snapshot"
        values = {
            "opportunity_id": opportunity.id,
            "user_id": prediction.user_id,
            "symbol": prediction.symbol,
            "contract_symbol": prediction.contract_symbol,
            "direction": prediction.direction,
            "timeframe": prediction.timeframe,
            "opportunity_status": opportunity.status,
            "prediction_status": prediction.status,
            "result": prediction.result,
            "net_result": prediction.net_result,
            "market_session": projection["market_session"],
            "quote_quality": projection["quote_quality"],
            "event_risk": projection["event_risk"],
            "data_coverage": projection["data_coverage"],
            "news_score": _decimal(_first(prediction.signal_news_score, projection["news_score"])),
            "technical_score": _decimal(
                _first(prediction.signal_indicator_score, projection["technical_score"])
            ),
            "market_context_score": projection["market_context_score"],
            "option_flow_score": projection["option_flow_score"],
            "gex_score": projection["gex_score"],
            "institutional_flow_score": projection["institutional_flow_score"],
            "market_flow_score": projection["market_flow_score"],
            "combined_score": _decimal(prediction.confidence_score),
            "price_source": "binance",
            "entry_price": prediction.entry_price,
            "exit_price": prediction.exit_price,
            "gross_return_bps": prediction.directional_return_bps,
            "net_return_bps": prediction.net_directional_return_bps,
            "mfe_bps": prediction.max_favorable_bps,
            "mae_bps": prediction.max_adverse_bps,
            "estimated_cost_bps": prediction.estimated_cost_bps,
            "exit_reason": prediction.exit_reason,
            "weights_version": projection["weights_version"],
            "feature_version": projection["feature_version"],
            "decision_version": projection["decision_version"],
            "settlement_version": prediction.settlement_version,
            "readiness_status": prediction.readiness_status,
            "calibration_sample_count": int(prediction.calibration_sample_count or 0),
            "expected_gross_edge_bps": prediction.expected_gross_edge_bps,
            "expected_edge_lower_bound_bps": prediction.expected_edge_lower_bound_bps,
            "snapshot_complete": snapshot_complete,
            "invalid_reason": invalid_reason,
            "signal_at": prediction.predicted_at,
            "due_at": prediction.due_at,
            "exit_at": prediction.exit_at,
            "settled_at": prediction.completed_at,
            "source_updated_at": prediction.updated_at,
            "updated_at": now,
        }
        if fact is None:
            fact = AiMonitorPredictionFact(
                prediction_id=prediction.id,
                created_at=now,
                **values,
            )
            db.add(fact)
        else:
            for key, value in values.items():
                setattr(fact, key, value)
        changed += 1
        if changed % PROJECTION_FLUSH_BATCH_SIZE == 0:
            db.flush()
    return changed


def _lifecycle_status(
    opportunity: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None,
    projection: Mapping[str, Any],
) -> str:
    if prediction is not None and prediction.status == "pending" and prediction.entry_price:
        return "triggered"
    if prediction is not None and prediction.status == "unavailable":
        return "data_error"
    gate = _mapping(projection.get("gate"))
    if gate.get("passed") is True or opportunity.status == "discovered":
        return "ready"
    if projection.get("primary_blocker"):
        return "blocked"
    return "candidate"


def refresh_current_opportunities(
    db: Session,
    *,
    user_id: int | None = None,
) -> int:
    now = utcnow()
    statement = (
        select(AiMonitorOpportunity, AiMonitorPrediction)
        .outerjoin(
            AiMonitorPrediction,
            AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
        )
        .where(
            AiMonitorOpportunity.status.in_(("candidate", "discovered")),
            AiMonitorOpportunity.expires_at > now,
            or_(
                AiMonitorPrediction.id.is_(None),
                AiMonitorPrediction.status != "completed",
            ),
        )
        .order_by(AiMonitorOpportunity.discovered_at.desc(), AiMonitorOpportunity.id.desc())
    )
    if user_id is not None:
        statement = statement.where(AiMonitorOpportunity.user_id == user_id)
    source_rows = list(db.execute(statement).all())
    latest: dict[tuple[int, str], tuple[AiMonitorOpportunity, AiMonitorPrediction | None]] = {}
    for opportunity, prediction in source_rows:
        key = (opportunity.user_id, opportunity.contract_symbol.strip().upper())
        latest.setdefault(key, (opportunity, prediction))

    current_statement = select(AiMonitorOpportunityCurrent)
    if user_id is not None:
        current_statement = current_statement.where(AiMonitorOpportunityCurrent.user_id == user_id)
    current_rows = list(db.scalars(current_statement).all())
    existing = {(row.user_id, row.contract_symbol): row for row in current_rows}
    changed = 0
    for key, row in list(existing.items()):
        if key not in latest:
            db.delete(row)
            changed += 1

    for key, (opportunity, prediction) in latest.items():
        projection = _projection_inputs(opportunity, prediction)
        row = existing.get(key)
        values = {
            "opportunity_id": opportunity.id,
            "prediction_id": prediction.id if prediction is not None else None,
            "user_id": opportunity.user_id,
            "symbol": opportunity.symbol,
            "contract_symbol": key[1],
            "direction": opportunity.direction,
            "timeframe": opportunity.timeframe,
            "lifecycle_status": _lifecycle_status(opportunity, prediction, projection),
            "opportunity_status": opportunity.status,
            "prediction_status": prediction.status if prediction is not None else None,
            "primary_blocker": projection["primary_blocker"],
            "news_score": projection["news_score"],
            "technical_score": projection["technical_score"],
            "market_flow_score": projection["market_flow_score"],
            "combined_score": projection["combined_score"],
            "data_coverage": projection["data_coverage"],
            "price_source": "binance",
            "entry_price": prediction.entry_price if prediction is not None else None,
            "current_price": projection["current_price"],
            "discovered_at": opportunity.discovered_at,
            "expires_at": opportunity.expires_at,
            "score_updated_at": opportunity.updated_at,
            "updated_at": now,
        }
        if row is None:
            db.add(AiMonitorOpportunityCurrent(row_version=1, **values))
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.row_version = int(row.row_version or 0) + 1
        changed += 1
        if changed % PROJECTION_FLUSH_BATCH_SIZE == 0:
            db.flush()
    return changed


def _five_minute_bucket(value: datetime) -> datetime:
    return value.replace(minute=value.minute - value.minute % 5, second=0, microsecond=0)


def refresh_score_history(
    db: Session,
    *,
    user_id: int | None = None,
    limit: int = 5000,
) -> int:
    max_id_statement = select(func.max(AiMonitorScoreHistory.gate_decision_id))
    if user_id is not None:
        max_id_statement = max_id_statement.where(AiMonitorScoreHistory.user_id == user_id)
    max_processed_id = int(db.scalar(max_id_statement) or 0)
    remaining = max(1, min(int(limit), 100000))
    cursor_id = max_processed_id
    changed = 0
    while remaining > 0:
        batch_limit = min(SCORE_HISTORY_READ_BATCH_SIZE, remaining)
        statement = (
            select(OpportunityGateDecision)
            .where(OpportunityGateDecision.id > cursor_id)
            .order_by(OpportunityGateDecision.id)
            .limit(batch_limit)
        )
        if user_id is not None:
            statement = statement.where(OpportunityGateDecision.user_id == user_id)
        decisions = list(db.scalars(statement).all())
        if not decisions:
            break

        opportunity_ids = sorted({decision.opportunity_id for decision in decisions})
        sampled_times = [_five_minute_bucket(decision.decision_at) for decision in decisions]
        existing_statement = select(AiMonitorScoreHistory).where(
            AiMonitorScoreHistory.opportunity_id.in_(opportunity_ids),
            AiMonitorScoreHistory.sampled_at >= min(sampled_times),
            AiMonitorScoreHistory.sampled_at <= max(sampled_times),
        )
        if user_id is not None:
            existing_statement = existing_statement.where(
                AiMonitorScoreHistory.user_id == user_id
            )
        existing = {
            (row.opportunity_id, row.sampled_at): row
            for row in db.scalars(existing_statement).all()
        }

        for decision in decisions:
            sampled_at = _five_minute_bucket(decision.decision_at)
            key = (decision.opportunity_id, sampled_at)
            row = existing.get(key)
            scores = _mapping(decision.score_components_json)
            market_flow = _mapping(decision.market_flow_snapshot_json)
            domains = _mapping(scores.get("domains"))
            coverage = _decimal(
                _first(
                    _mapping(decision.data_quality_json).get("coverage"),
                    _mapping(decision.data_quality_json).get("data_coverage"),
                )
            )
            if coverage is not None and coverage <= 1:
                coverage *= 100
            blockers = list(decision.blocking_reasons_json or [])
            values = {
                "gate_decision_id": decision.id,
                "user_id": decision.user_id,
                "direction": decision.direction,
                "gate_status": decision.gate_status,
                "primary_blocker": str(blockers[0])[:191] if blockers else None,
                "news_score": _decimal(
                    _first(scores.get("news"), scores.get("news_score"))
                ),
                "technical_score": _decimal(
                    _first(scores.get("technical"), scores.get("indicator"))
                ),
                "market_context_score": _domain_score(domains, "macro_market"),
                "option_flow_score": _decimal(
                    _first(
                        _mapping(market_flow.get("option_flow")).get("score"),
                        _domain_score(domains, "options_flow"),
                    )
                ),
                "gex_score": _decimal(
                    _first(
                        _mapping(market_flow.get("gex")).get("score"),
                        _domain_score(domains, "gex"),
                    )
                ),
                "institutional_flow_score": _decimal(
                    _first(
                        _mapping(market_flow.get("institutional_flow")).get("score"),
                        _domain_score(domains, "institutional_flow"),
                    )
                ),
                "market_flow_score": _decimal(
                    _first(scores.get("market_flow"), market_flow.get("score"))
                ),
                "combined_score": _decimal(
                    _first(scores.get("combined"), scores.get("combined_score"))
                ),
                "data_coverage": coverage,
                "feature_version": decision.feature_version,
                "weights_version": decision.weights_version,
                "decision_version": decision.decision_version,
            }
            if row is None:
                row = AiMonitorScoreHistory(
                    opportunity_id=decision.opportunity_id,
                    sampled_at=sampled_at,
                    **values,
                )
                db.add(row)
                existing[key] = row
            else:
                for field, value in values.items():
                    setattr(row, field, value)
            changed += 1
            if changed % PROJECTION_FLUSH_BATCH_SIZE == 0:
                db.flush()

        db.flush()
        cursor_id = max(decision.id for decision in decisions)
        remaining -= len(decisions)
        if len(decisions) < batch_limit:
            break
    return changed


def refresh_ai_monitor_read_models(
    db: Session,
    *,
    user_id: int | None = None,
    prediction_limit: int = 2000,
    score_limit: int = 5000,
    force_availability_check: bool = False,
) -> dict[str, int | bool]:
    """Refresh all projections; safe during rolling deployments before 0059."""

    if not read_models_available(db, refresh=force_availability_check):
        return {
            "available": False,
            "prediction_facts": 0,
            "current_opportunities": 0,
            "score_history": 0,
        }
    # Each projection owns a separate primary-key sequence. Flush between stages
    # so later SELECTs never trigger an implicit autoflush while the identity map
    # still contains transient rows from the previous projection. This keeps the
    # rebuild idempotent on SQLite tests and avoids noisy identity replacement
    # warnings on long-lived production sessions.
    prediction_facts = refresh_prediction_facts(
        db,
        user_id=user_id,
        limit=prediction_limit,
    )
    db.flush()
    current_opportunities = refresh_current_opportunities(db, user_id=user_id)
    db.flush()
    score_history = refresh_score_history(db, user_id=user_id, limit=score_limit)
    db.flush()
    return {
        "available": True,
        "prediction_facts": prediction_facts,
        "current_opportunities": current_opportunities,
        "score_history": score_history,
    }


def reconcile_ai_monitor_read_models(
    db: Session,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Compare disposable projections with the authoritative source ledger."""

    if not read_models_available(db):
        return {"available": False, "ready": False, "reason": "tables_missing"}
    prediction_filters = (
        [] if user_id is None else [AiMonitorPrediction.user_id == user_id]
    )
    fact_filters = [] if user_id is None else [AiMonitorPredictionFact.user_id == user_id]
    current_filters = (
        [] if user_id is None else [AiMonitorOpportunityCurrent.user_id == user_id]
    )
    source_prediction_count = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPrediction)
            .where(*prediction_filters)
        )
        or 0
    )
    fact_count = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPredictionFact)
            .where(*fact_filters)
        )
        or 0
    )
    missing_facts = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPrediction)
            .outerjoin(
                AiMonitorPredictionFact,
                AiMonitorPredictionFact.prediction_id == AiMonitorPrediction.id,
            )
            .where(*prediction_filters, AiMonitorPredictionFact.id.is_(None))
        )
        or 0
    )
    stale_facts = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPredictionFact)
            .join(
                AiMonitorPrediction,
                AiMonitorPrediction.id == AiMonitorPredictionFact.prediction_id,
            )
            .where(
                *fact_filters,
                AiMonitorPredictionFact.source_updated_at
                < AiMonitorPrediction.updated_at,
            )
        )
        or 0
    )
    orphan_facts = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPredictionFact)
            .outerjoin(
                AiMonitorPrediction,
                AiMonitorPrediction.id == AiMonitorPredictionFact.prediction_id,
            )
            .where(*fact_filters, AiMonitorPrediction.id.is_(None))
        )
        or 0
    )
    price_source_violations = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorPredictionFact)
            .where(*fact_filters, AiMonitorPredictionFact.price_source != "binance")
        )
        or 0
    ) + int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorOpportunityCurrent)
            .where(
                *current_filters,
                AiMonitorOpportunityCurrent.price_source != "binance",
            )
        )
        or 0
    )
    now = utcnow()
    active_statement = (
        select(
            AiMonitorOpportunity.user_id,
            AiMonitorOpportunity.contract_symbol,
        )
        .outerjoin(
            AiMonitorPrediction,
            AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
        )
        .where(
            AiMonitorOpportunity.status.in_(("candidate", "discovered")),
            AiMonitorOpportunity.expires_at > now,
            or_(
                AiMonitorPrediction.id.is_(None),
                AiMonitorPrediction.status != "completed",
            ),
        )
        .distinct()
    )
    if user_id is not None:
        active_statement = active_statement.where(AiMonitorOpportunity.user_id == user_id)
    expected_current_count = len(db.execute(active_statement).all())
    current_count = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorOpportunityCurrent)
            .where(*current_filters)
        )
        or 0
    )
    invalid_reasons = {
        str(reason or "none"): int(count)
        for reason, count in db.execute(
            select(
                AiMonitorPredictionFact.invalid_reason,
                func.count(AiMonitorPredictionFact.id),
            )
            .where(*fact_filters)
            .group_by(AiMonitorPredictionFact.invalid_reason)
        ).all()
    }
    ready = all(
        (
            source_prediction_count == fact_count,
            missing_facts == 0,
            stale_facts == 0,
            orphan_facts == 0,
            expected_current_count == current_count,
            price_source_violations == 0,
        )
    )
    return {
        "available": True,
        "ready": ready,
        "user_id": user_id,
        "source_prediction_count": source_prediction_count,
        "prediction_fact_count": fact_count,
        "missing_facts": missing_facts,
        "stale_facts": stale_facts,
        "orphan_facts": orphan_facts,
        "expected_current_count": expected_current_count,
        "current_count": current_count,
        "price_source_violations": price_source_violations,
        "invalid_reasons": invalid_reasons,
        "checked_at": now,
    }
