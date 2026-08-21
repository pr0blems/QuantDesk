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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from . import macro_market
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
PREDICTION_FACT_READ_BATCH_SIZE = 25
SCORE_HISTORY_READ_BATCH_SIZE = 1000
PREDICTION_FACT_PROJECTION_VERSION = "signal_features_v3"
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


def _feature_status(
    payload: Mapping[str, Any] | None,
    *,
    channel_enabled: bool | None = None,
) -> str:
    value = _mapping(payload)
    if channel_enabled is False:
        return "channel_disabled"
    if not value:
        return "not_captured_at_signal"
    if value.get("available") is False:
        reason = str(
            _first(
                value.get("reason"),
                _mapping(value.get("data_quality")).get("status"),
                "unavailable",
            )
        ).strip().lower()
        return reason[:32] or "unavailable"
    if value.get("fresh") is False:
        return "stale"
    return "available"


def _signal_session(signal_at: datetime | None, fallback: Any) -> str:
    if signal_at is None:
        return str(fallback or "unknown")[:16]
    aware = signal_at if signal_at.tzinfo is not None else signal_at.replace(tzinfo=UTC)
    return str(macro_market.us_market_session(aware).get("key") or fallback or "unknown")[:16]


def _quote_projection(
    quote_payload: Mapping[str, Any] | None,
    *,
    signal_at: datetime | None,
    market_quality: Mapping[str, Any],
) -> dict[str, Any]:
    quote = _mapping(quote_payload)
    bid = _decimal(quote.get("bid"))
    ask = _decimal(quote.get("ask"))
    last_price = _decimal(_first(quote.get("last_price"), quote.get("price")))
    spread_bps = _decimal(quote.get("spread_bps"))
    if spread_bps is None and bid is not None and ask is not None and bid > 0 and ask >= bid:
        midpoint = (bid + ask) / Decimal("2")
        if midpoint > 0:
            spread_bps = (ask - bid) / midpoint * Decimal("10000")

    # The snapshot records quote age at capture time. Prefer that immutable
    # value over recomputing from a timezone-naive database timestamp; the
    # latter made valid historical NBBO look hours stale after projection.
    explicit_age_ms = _decimal(quote.get("quote_age_ms"))
    quote_age_ms: int | None = (
        max(0, int(explicit_age_ms)) if explicit_age_ms is not None else None
    )
    received_at_ms = _decimal(
        _first(quote.get("quote_received_at_ms"), quote.get("received_at_ms"))
    )
    if quote_age_ms is None and received_at_ms is not None and signal_at is not None:
        received = int(received_at_ms)
        if 0 < received < 1_000_000_000_000:
            received *= 1_000
        aware = signal_at if signal_at.tzinfo is not None else signal_at.replace(tzinfo=UTC)
        quote_age_ms = max(0, int(aware.timestamp() * 1_000) - received)

    session = _signal_session(
        signal_at,
        _first(quote.get("market_session"), market_quality.get("market_session")),
    )
    default_max_age = 2_000 if session == "regular" else 10_000
    maximum_age_ms = int(
        _decimal(market_quality.get("maximum_quote_age_ms")) or default_max_age
    )
    maximum_spread_bps = _decimal(
        market_quality.get("maximum_spread_bps")
    ) or Decimal("80")
    has_nbbo = bool(
        bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
    )
    has_price = bool(last_price is not None or bid is not None or ask is not None)
    if not has_price:
        quality = "missing"
    elif bid is not None and ask is not None and not has_nbbo:
        quality = "blocked"
    elif has_nbbo and spread_bps is not None and spread_bps > maximum_spread_bps:
        quality = "blocked"
    elif has_nbbo and quote_age_ms is not None and quote_age_ms <= maximum_age_ms:
        quality = "passed"
    else:
        quality = "partial"
    return {
        "quality": quality,
        "source": str(_first(quote.get("source"), quote.get("provider"), "unknown"))[:32],
        "age_ms": quote_age_ms,
        "spread_bps": spread_bps,
        "market_session": session,
    }


def _projection_inputs(
    opportunity: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None,
    snapshot: OpportunityMarketSnapshot | None = None,
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
    frozen_quote = _mapping(snapshot.quote_snapshot_json if snapshot is not None else {})
    frozen_option_flow = _mapping(
        snapshot.option_flow_snapshot_json if snapshot is not None else {}
    )
    frozen_gex = _mapping(snapshot.gex_snapshot_json if snapshot is not None else {})
    frozen_institutional = _mapping(
        snapshot.institutional_flow_snapshot_json if snapshot is not None else {}
    )
    option_flow = frozen_option_flow or option_flow
    gex = frozen_gex or gex
    institutional = frozen_institutional or institutional
    signal_at = prediction.predicted_at if prediction is not None else opportunity.discovered_at
    quote_projection = _quote_projection(
        frozen_quote or _mapping(quality.get("quote")) or _mapping(evidence.get("quote")),
        signal_at=signal_at,
        market_quality=quality,
    )
    uw_policy = _mapping(evidence.get("unusual_whales_policy"))
    channels = _mapping(uw_policy.get("channels"))

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
        "market_session": quote_projection["market_session"],
        "quote_quality": quote_projection["quality"],
        "quote_source": quote_projection["source"],
        "quote_age_ms": quote_projection["age_ms"],
        "quote_spread_bps": quote_projection["spread_bps"],
        "option_flow_status": _feature_status(
            option_flow,
            channel_enabled=channels.get("option_trades") if channels else None,
        ),
        "gex_status": _feature_status(
            gex,
            channel_enabled=channels.get("gex") if channels else None,
        ),
        "institutional_flow_status": _feature_status(institutional),
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
    id_statement = (
        select(AiMonitorPrediction.id, AiMonitorPrediction.updated_at)
        .order_by(AiMonitorPrediction.updated_at.desc(), AiMonitorPrediction.id.desc())
        .limit(max(1, min(int(limit), 10000)))
    )
    if user_id is not None:
        id_statement = id_statement.where(AiMonitorPrediction.user_id == user_id)
    prediction_rows = list(db.execute(id_statement).all())
    prediction_ids = [row.id for row in prediction_rows]
    if not prediction_ids:
        return 0

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

    # Projection-version upgrades only need the immutable feature snapshot.
    # Avoid re-reading the duplicated ~400 KB opportunity/prediction audit JSON
    # when the authoritative prediction has not changed.
    snapshot_only_ids = [
        row.id
        for row in prediction_rows
        if (fact := existing.get(row.id)) is not None
        and fact.source_updated_at >= row.updated_at
        and fact.projection_version != PREDICTION_FACT_PROJECTION_VERSION
    ]
    for offset in range(0, len(snapshot_only_ids), PREDICTION_FACT_READ_BATCH_SIZE):
        batch_ids = snapshot_only_ids[offset : offset + PREDICTION_FACT_READ_BATCH_SIZE]
        snapshot_rows = db.execute(
            select(
                AiMonitorPrediction.id,
                AiMonitorPrediction.predicted_at,
                OpportunityMarketSnapshot.id,
                OpportunityMarketSnapshot.quote_snapshot_json,
                OpportunityMarketSnapshot.option_flow_snapshot_json,
                OpportunityMarketSnapshot.gex_snapshot_json,
                OpportunityMarketSnapshot.institutional_flow_snapshot_json,
            )
            .outerjoin(
                OpportunityMarketSnapshot,
                OpportunityMarketSnapshot.opportunity_id
                == AiMonitorPrediction.opportunity_id,
            )
            .where(AiMonitorPrediction.id.in_(batch_ids))
        ).all()
        for row in snapshot_rows:
            fact = existing[row[0]]
            quote = _quote_projection(
                _mapping(row[3]),
                signal_at=row[1],
                market_quality={},
            )
            fact.market_session = quote["market_session"]
            fact.quote_quality = quote["quality"]
            fact.quote_source = (
                quote["source"] if quote["source"] != "unknown" else fact.quote_source
            )
            fact.quote_age_ms = quote["age_ms"]
            fact.quote_spread_bps = quote["spread_bps"]
            for field, payload in (
                ("option_flow_status", row[4]),
                ("gex_status", row[5]),
                ("institutional_flow_status", row[6]),
            ):
                status = _feature_status(_mapping(payload))
                current = str(getattr(fact, field) or "")
                if status != "not_captured_at_signal" or not current:
                    setattr(fact, field, status)
            fact.snapshot_complete = row[2] is not None
            fact.projection_version = PREDICTION_FACT_PROJECTION_VERSION
            fact.updated_at = now
            changed += 1
        db.flush()

    full_prediction_ids = [
        row.id
        for row in prediction_rows
        if (fact := existing.get(row.id)) is None
        or fact.source_updated_at < row.updated_at
    ]

    # Evidence and immutable market snapshots can contain large audit JSON.
    # Fetch a bounded group at a time so a rebuild cannot turn into one giant
    # result set that times out while MySQL is still writing it to the client.
    def source_rows():
        for offset in range(
            0,
            len(full_prediction_ids),
            PREDICTION_FACT_READ_BATCH_SIZE,
        ):
            batch_ids = full_prediction_ids[
                offset : offset + PREDICTION_FACT_READ_BATCH_SIZE
            ]
            statement = (
                select(
                    AiMonitorPrediction,
                    AiMonitorOpportunity,
                    OpportunityMarketSnapshot,
                )
                .join(
                    AiMonitorOpportunity,
                    AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id,
                )
                .outerjoin(
                    OpportunityMarketSnapshot,
                    OpportunityMarketSnapshot.opportunity_id == AiMonitorOpportunity.id,
                )
                .where(AiMonitorPrediction.id.in_(batch_ids))
            )
            yield from db.execute(statement).all()

    for prediction, opportunity, snapshot in source_rows():
        fact = existing.get(prediction.id)
        projection = _projection_inputs(opportunity, prediction, snapshot)
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
            "quote_source": projection["quote_source"],
            "quote_age_ms": projection["quote_age_ms"],
            "quote_spread_bps": projection["quote_spread_bps"],
            "option_flow_status": projection["option_flow_status"],
            "gex_status": projection["gex_status"],
            "institutional_flow_status": projection["institutional_flow_status"],
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
            "projection_version": PREDICTION_FACT_PROJECTION_VERSION,
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
