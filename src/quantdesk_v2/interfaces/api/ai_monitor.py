"""Authenticated AI monitoring workspace routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ... import ai_monitor, historical_replay
from ...database import get_db
from ...dependencies import get_current_user
from ...models import (
    AiModelConfig,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorReplayRun,
    AiMonitorRun,
    AuditLog,
    CompanyProfile,
    News,
    NewsAiBatch,
    NewsAiModelCall,
    Security,
    SecurityFinancialSnapshot,
    SecurityFundamentalAnalysis,
    User,
    utcnow,
)
from ...monitor import MonitorRepository, MonitorUnavailable
from ...schemas import (
    AiMonitorConfigUpdate,
    AiMonitorCostConfigUpdate,
    AiMonitorNewsAnalyzeRequest,
    AiMonitorReplayRequest,
    AiMonitorRunRequest,
)

router = APIRouter(prefix="/ai-monitor")


def _utc_out(value: datetime | None) -> datetime | None:
    """Attach the UTC offset stripped by MySQL's timezone-naive DateTime columns."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_expected_user(request: Request, user: User) -> None:
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


def _audit(
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


def _config_out(config: AiMonitorConfig | None) -> dict[str, Any]:
    data = ai_monitor.config_data(config)
    for key in ("last_news_run_at", "last_opportunity_run_at", "updated_at"):
        data[key] = _utc_out(data.get(key))
    return data


def _run_out(run: AiMonitorRun, batch: NewsAiBatch | None = None) -> dict[str, Any]:
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
        "started_at": _utc_out(run.started_at),
        "completed_at": _utc_out(run.completed_at),
        "created_at": _utc_out(run.created_at),
        "updated_at": _utc_out(run.updated_at),
    }


def _news_out(item: News) -> dict[str, Any]:
    return {
        "id": item.id,
        "ts": item.ts,
        "source": item.source,
        "lang": item.lang,
        "title": item.title,
        "title_zh": item.title_zh,
        "link": item.link,
        "summary": item.summary,
        "sentiment": item.sentiment,
        "rule_sentiment": item.rule_sentiment,
        "related_us_stocks": list(item.related_us_stocks or []),
        "related_industries": list(item.related_industries or []),
        "ai_sentiment": item.ai_sentiment,
        "ai_confidence": float(item.ai_confidence) if item.ai_confidence is not None else None,
        "ai_impact_strength": item.ai_impact_strength,
        "ai_time_horizon": item.ai_time_horizon,
        "ai_category": item.ai_category,
        "ai_reason": item.ai_reason,
        "ai_model": item.ai_model,
        "ai_analyzed_at": _utc_out(item.ai_analyzed_at),
    }


def _prediction_settlement_out(item: AiMonitorPrediction | None) -> dict[str, Any] | None:
    if item is None:
        return None
    now = utcnow()
    due_at = item.due_at
    grace_deadline = due_at + timedelta(hours=ai_monitor.PREDICTION_SETTLEMENT_GRACE_HOURS)
    if item.status == "completed":
        phase = "completed"
    elif item.status == "unavailable":
        phase = "unavailable"
    elif now < due_at:
        phase = "scheduled"
    elif now <= grace_deadline:
        phase = "awaiting_market_data"
    else:
        phase = "overdue"

    next_retry_at: datetime | None = None
    if item.status == "pending":
        last_updated_at = item.updated_at or item.predicted_at
        eligible_at = max(
            due_at,
            last_updated_at
            + timedelta(minutes=ai_monitor.PREDICTION_SETTLEMENT_RETRY_MINUTES),
        )
        next_retry_at = (
            eligible_at
            if eligible_at > now
            else now + timedelta(seconds=ai_monitor.PREDICTION_SETTLEMENT_POLL_SECONDS)
        )

    return {
        "status": item.status,
        "phase": phase,
        "due_at": _utc_out(due_at),
        "grace_deadline": _utc_out(grace_deadline),
        "last_attempt_at": _utc_out(item.updated_at),
        "next_retry_at": _utc_out(next_retry_at),
        "poll_interval_seconds": ai_monitor.PREDICTION_SETTLEMENT_POLL_SECONDS,
        "retry_interval_minutes": ai_monitor.PREDICTION_SETTLEMENT_RETRY_MINUTES,
        "grace_hours": ai_monitor.PREDICTION_SETTLEMENT_GRACE_HOURS,
        "price_timeframe": "15m",
    }


def _opportunity_out(
    item: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None = None,
    live_market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prediction_evidence = dict(prediction.evidence_json or {}) if prediction else {}
    prediction_signal_scores = prediction_evidence.get("signal_scores")
    prediction_signal_scores = (
        dict(prediction_signal_scores)
        if isinstance(prediction_signal_scores, dict)
        else {}
    )
    prediction_score_snapshot = prediction_evidence.get("score_snapshot")
    prediction_score_snapshot = (
        dict(prediction_score_snapshot)
        if isinstance(prediction_score_snapshot, dict)
        else {}
    )
    prediction_market_flow = prediction_evidence.get("market_flow")
    prediction_market_flow = (
        dict(prediction_market_flow)
        if isinstance(prediction_market_flow, dict)
        else {}
    )
    prediction_market_flow_score = prediction_signal_scores.get(
        "market_flow",
        prediction_score_snapshot.get("market_flow", prediction_market_flow.get("score")),
    )
    return {
        "id": item.public_id,
        "symbol": item.symbol,
        "contract_symbol": item.contract_symbol,
        "direction": item.direction,
        "status": item.status,
        "timeframe": item.timeframe,
        "news_score": float(item.news_score),
        "indicator_score": float(item.indicator_score),
        "combined_score": float(item.combined_score),
        "matched_indicator_keys": list(item.matched_indicator_keys_json or []),
        "news_ids": list(item.news_ids_json or []),
        "evidence": dict(item.evidence_json or {}),
        "prediction_status": prediction.status if prediction is not None else None,
        "prediction_settlement": _prediction_settlement_out(prediction),
        "prediction_result": prediction.result if prediction is not None else None,
        "prediction_entry_price": (
            float(prediction.entry_price)
            if prediction is not None and prediction.entry_price is not None
            else None
        ),
        "prediction_combined_score": (
            float(prediction.confidence_score) if prediction is not None else None
        ),
        "prediction_news_score": (
            float(prediction.signal_news_score)
            if prediction is not None and prediction.signal_news_score is not None
            else None
        ),
        "prediction_indicator_score": (
            float(prediction.signal_indicator_score)
            if prediction is not None and prediction.signal_indicator_score is not None
            else None
        ),
        "prediction_market_flow_score": (
            float(prediction_market_flow_score)
            if prediction_market_flow_score is not None
            else None
        ),
        "prediction_entry_gate": (
            ai_monitor.prediction_entry_gate_snapshot(prediction)
            if prediction is not None
            else None
        ),
        "virtual_position": (
            ai_monitor.virtual_position_snapshot(prediction, live_market)
            if prediction is not None
            else None
        ),
        "prediction_created_at": (
            _utc_out(prediction.predicted_at) if prediction is not None else None
        ),
        "prediction_due_at": (
            _utc_out(prediction.due_at) if prediction is not None else None
        ),
        "discovered_at": _utc_out(item.discovered_at),
        "expires_at": _utc_out(item.expires_at),
        "updated_at": _utc_out(item.updated_at),
    }


def _prediction_out(item: AiMonitorPrediction) -> dict[str, Any]:
    return {
        "id": item.public_id,
        "opportunity_id": item.opportunity_id,
        "symbol": item.symbol,
        "contract_symbol": item.contract_symbol,
        "direction": item.direction,
        "timeframe": item.timeframe,
        "status": item.status,
        "result": item.result,
        "confidence_score": float(item.confidence_score),
        "entry_price": float(item.entry_price) if item.entry_price is not None else None,
        "exit_price": float(item.exit_price) if item.exit_price is not None else None,
        "raw_return_bps": (float(item.raw_return_bps) if item.raw_return_bps is not None else None),
        "directional_return_bps": (
            float(item.directional_return_bps) if item.directional_return_bps is not None else None
        ),
        "signal_news_score": (
            float(item.signal_news_score) if item.signal_news_score is not None else None
        ),
        "signal_indicator_score": (
            float(item.signal_indicator_score) if item.signal_indicator_score is not None else None
        ),
        "estimated_cost_bps": float(item.estimated_cost_bps),
        "net_directional_return_bps": (
            float(item.net_directional_return_bps)
            if item.net_directional_return_bps is not None
            else None
        ),
        "net_result": item.net_result,
        "max_favorable_bps": (
            float(item.max_favorable_bps) if item.max_favorable_bps is not None else None
        ),
        "max_adverse_bps": (
            float(item.max_adverse_bps) if item.max_adverse_bps is not None else None
        ),
        "settlement_version": item.settlement_version,
        "readiness_status": item.readiness_status,
        "calibration_sample_count": int(item.calibration_sample_count),
        "expected_gross_edge_bps": (
            float(item.expected_gross_edge_bps)
            if item.expected_gross_edge_bps is not None
            else None
        ),
        "expected_edge_lower_bound_bps": (
            float(item.expected_edge_lower_bound_bps)
            if item.expected_edge_lower_bound_bps is not None
            else None
        ),
        "evidence": dict(item.evidence_json or {}),
        "predicted_at": _utc_out(item.predicted_at),
        "due_at": _utc_out(item.due_at),
        "completed_at": _utc_out(item.completed_at),
    }


def _next_run(last_run: datetime | None, interval_minutes: int, enabled: bool) -> datetime | None:
    if not enabled:
        return None
    return utcnow() if last_run is None else last_run + timedelta(minutes=interval_minutes)


@router.get("/overview")
def overview(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    config = db.get(AiMonitorConfig, user.id)
    settings = _config_out(config)
    now = utcnow()
    day_ago = int((datetime.now(UTC) - timedelta(hours=24)).timestamp())
    news_stats = (
        db.execute(
            select(
                func.count(News.id).label("total"),
                func.sum(News.ai_analyzed_at.is_not(None)).label("analyzed"),
                func.sum(News.ai_analyzed_at.is_(None)).label("pending"),
                func.sum((News.ts >= day_ago) & News.ai_analyzed_at.is_not(None)).label(
                    "analyzed_24h"
                ),
                func.max(News.ts).label("latest_ts"),
            )
        )
        .mappings()
        .one()
    )
    active_runs = int(
        db.scalar(
            select(func.count())
            .select_from(AiMonitorRun)
            .where(
                AiMonitorRun.user_id == user.id,
                AiMonitorRun.status.in_(ai_monitor.ACTIVE_RUN_STATUSES),
            )
        )
        or 0
    )
    active_opportunities = int(
        db.scalar(
            select(func.count(func.distinct(AiMonitorOpportunity.contract_symbol))).where(
                AiMonitorOpportunity.user_id == user.id,
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at > now,
            )
        )
        or 0
    )
    prediction_stats = (
        db.execute(
            select(
                func.count(AiMonitorPrediction.id).label("total"),
                func.sum(AiMonitorPrediction.status == "pending").label("pending"),
                func.sum(AiMonitorPrediction.status == "completed").label("completed"),
            ).where(AiMonitorPrediction.user_id == user.id)
        )
        .mappings()
        .one()
    )
    latest_run = db.scalar(
        select(AiMonitorRun)
        .where(AiMonitorRun.user_id == user.id)
        .order_by(AiMonitorRun.created_at.desc(), AiMonitorRun.id.desc())
        .limit(1)
    )
    model_configured = bool(
        db.scalar(
            select(AiModelConfig.id).where(
                AiModelConfig.user_id == user.id,
                AiModelConfig.is_enabled.is_(True),
                AiModelConfig.is_default.is_(True),
            )
        )
    )
    return {
        "config": settings,
        "scheduler": {
            "enabled": settings["enabled"],
            "active_runs": active_runs,
            "next_news_run_at": _utc_out(
                _next_run(
                    settings["last_news_run_at"],
                    settings["news_interval_minutes"],
                    settings["enabled"],
                )
            ),
            "next_opportunity_run_at": _utc_out(
                _next_run(
                    settings["last_opportunity_run_at"],
                    settings["opportunity_interval_minutes"],
                    settings["enabled"],
                )
            ),
        },
        "news": {
            "total": int(news_stats["total"] or 0),
            "analyzed": int(news_stats["analyzed"] or 0),
            "pending": int(news_stats["pending"] or 0),
            "analyzed_24h": int(news_stats["analyzed_24h"] or 0),
            "latest_ts": news_stats["latest_ts"],
        },
        "opportunities": {"active": active_opportunities},
        "predictions": {
            "total": int(prediction_stats["total"] or 0),
            "pending": int(prediction_stats["pending"] or 0),
            "completed": int(prediction_stats["completed"] or 0),
        },
        "model_configured": model_configured,
        "latest_run": _run_out(latest_run) if latest_run is not None else None,
        "updated_at": _utc_out(now),
    }


@router.get("/config")
def get_config(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _config_out(db.get(AiMonitorConfig, user.id))


@router.put("/config")
def update_config(
    payload: AiMonitorConfigUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_user(request, user)
    allowed = ai_monitor.valid_indicator_keys()
    unknown = sorted(set(payload.indicator_keys) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unsupported indicator: {unknown[0]}")
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    allowed_symbols = {
        item["contract_symbol"] for item in ai_monitor.monitor_symbol_catalog(repository)
    }
    unknown_symbols = sorted(set(payload.monitor_symbols) - allowed_symbols)
    if unknown_symbols:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported monitor symbol: {unknown_symbols[0]}",
        )
    config = db.get(AiMonitorConfig, user.id)
    if config is None:
        config = AiMonitorConfig(
            user_id=user.id,
            indicator_keys_json=list(payload.indicator_keys),
            monitor_symbols_json=list(payload.monitor_symbols),
        )
        db.add(config)
    config.enabled = payload.enabled
    config.news_interval_minutes = payload.news_interval_minutes
    config.opportunity_interval_minutes = payload.opportunity_interval_minutes
    config.news_lookback_hours = payload.news_lookback_hours
    config.timeframe = payload.timeframe
    config.indicator_keys_json = list(payload.indicator_keys)
    config.monitor_symbols_json = list(payload.monitor_symbols)
    config.minimum_news_confidence = Decimal(str(payload.minimum_news_confidence))
    config.minimum_news_mentions = payload.minimum_news_mentions
    config.minimum_indicator_score = Decimal(str(payload.minimum_indicator_score))
    config.minimum_combined_score = Decimal(str(payload.minimum_combined_score))
    config.maximum_market_age_seconds = payload.maximum_market_age_seconds
    config.minimum_feature_quality = Decimal(str(payload.minimum_feature_quality))
    config.minimum_market_flow_quality = Decimal(
        str(payload.minimum_market_flow_quality)
    )
    config.minimum_calibration_samples = payload.minimum_calibration_samples
    config.live_safety_margin_bps = Decimal(str(payload.live_safety_margin_bps))
    config.news_score_weight = Decimal(str(payload.news_score_weight))
    config.technical_score_weight = Decimal(str(payload.technical_score_weight))
    config.market_flow_score_weight = Decimal(str(payload.market_flow_score_weight))
    _audit(
        db,
        request,
        user.id,
        "ai_monitor.config.update",
        str(user.id),
        {
            "enabled": payload.enabled,
            "news_interval_minutes": payload.news_interval_minutes,
            "opportunity_interval_minutes": payload.opportunity_interval_minutes,
            "indicator_count": len(payload.indicator_keys),
            "monitor_symbol_count": len(payload.monitor_symbols),
            "monitor_scope": "selected" if payload.monitor_symbols else "all",
            "match_policy": ai_monitor.INDICATOR_MATCH_POLICY,
            "minimum_indicator_score": payload.minimum_indicator_score,
            "minimum_combined_score": payload.minimum_combined_score,
            "maximum_market_age_seconds": payload.maximum_market_age_seconds,
            "minimum_feature_quality": payload.minimum_feature_quality,
            "minimum_market_flow_quality": payload.minimum_market_flow_quality,
            "minimum_calibration_samples": payload.minimum_calibration_samples,
            "live_safety_margin_bps": payload.live_safety_margin_bps,
            "score_weights": {
                "news": payload.news_score_weight,
                "technical": payload.technical_score_weight,
                "market_flow": payload.market_flow_score_weight,
            },
        },
    )
    db.commit()
    db.refresh(config)
    return _config_out(config)


@router.put("/cost-config")
def update_cost_config(
    payload: AiMonitorCostConfigUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Persist cost assumptions used by virtual prediction statistics and gates."""

    _require_expected_user(request, user)
    config = db.get(AiMonitorConfig, user.id)
    if config is None:
        defaults = ai_monitor.default_config_data()
        config = AiMonitorConfig(
            user_id=user.id,
            indicator_keys_json=list(defaults["indicator_keys"]),
            monitor_symbols_json=list(defaults["monitor_symbols"]),
        )
        db.add(config)
    config.prediction_fee_enabled = payload.prediction_fee_enabled
    config.prediction_fee_bps_per_side = Decimal(
        str(payload.prediction_fee_bps_per_side)
    )
    config.prediction_slippage_enabled = payload.prediction_slippage_enabled
    config.prediction_slippage_bps_per_side = Decimal(
        str(payload.prediction_slippage_bps_per_side)
    )
    config.prediction_funding_enabled = payload.prediction_funding_enabled
    config.prediction_funding_bps_per_8h = Decimal(
        str(payload.prediction_funding_bps_per_8h)
    )
    _audit(
        db,
        request,
        user.id,
        "ai_monitor.cost_config.update",
        str(user.id),
        payload.model_dump(),
    )
    db.commit()
    db.refresh(config)
    return _config_out(config)


@router.get("/news")
def news(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    items = db.scalars(select(News).order_by(News.ts.desc(), News.id.desc()).limit(limit)).all()
    return {"items": [_news_out(item) for item in items], "total": len(items)}


@router.get("/runs")
def runs(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=60, ge=1, le=200),
) -> dict[str, Any]:
    items = db.scalars(
        select(AiMonitorRun)
        .where(AiMonitorRun.user_id == user.id)
        .order_by(AiMonitorRun.created_at.desc(), AiMonitorRun.id.desc())
        .limit(limit)
    ).all()
    batches = (
        {
            batch.id: batch
            for batch in db.scalars(
                select(NewsAiBatch).where(
                    NewsAiBatch.id.in_([item.news_batch_id for item in items if item.news_batch_id])
                )
            ).all()
        }
        if any(item.news_batch_id for item in items)
        else {}
    )
    return {"items": [_run_out(item, batches.get(item.news_batch_id or "")) for item in items]}


@router.get("/indicators")
def indicators(
    _: Annotated[User, Depends(get_current_user)],
    timeframe: str = Query(default="1h", pattern="^(15m|1h|4h)$"),
) -> dict[str, Any]:
    items = ai_monitor.indicator_catalog(timeframe)
    return {
        "items": items,
        "count": len(items),
        "match_policy": ai_monitor.INDICATOR_MATCH_POLICY,
        "templates": ai_monitor.indicator_templates(),
        "conflict_pairs": [],
        "groups": {
            key: sorted(values) for key, values in ai_monitor.INDICATOR_GROUPS.items()
        },
        "minimum_core_matches": 2,
        "non_blocking_keys": sorted(ai_monitor.NON_BLOCKING_INDICATOR_KEYS),
    }


@router.get("/symbols")
def monitor_symbols(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    items = ai_monitor.monitor_symbol_catalog(repository)
    return {"items": items, "count": len(items)}


@router.get("/opportunities")
def opportunities(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=300),
    include_expired: bool = False,
) -> dict[str, Any]:
    statement = select(AiMonitorOpportunity).where(AiMonitorOpportunity.user_id == user.id)
    if not include_expired:
        statement = statement.where(
            AiMonitorOpportunity.status.in_(("candidate", "discovered")),
            AiMonitorOpportunity.expires_at > utcnow(),
        )
    statement = statement.order_by(
        AiMonitorOpportunity.discovered_at.desc(),
        AiMonitorOpportunity.id.desc(),
    )
    items = db.scalars(statement.limit(300 if not include_expired else limit)).all()
    if not include_expired:
        unique: dict[str, AiMonitorOpportunity] = {}
        for item in items:
            instrument = (item.contract_symbol or item.symbol).strip().upper()
            unique.setdefault(instrument, item)
        items = list(unique.values())[:limit]
    prediction_by_opportunity_id = (
        {
            prediction.opportunity_id: prediction
            for prediction in db.scalars(
                select(AiMonitorPrediction).where(
                    AiMonitorPrediction.user_id == user.id,
                    AiMonitorPrediction.opportunity_id.in_([item.id for item in items]),
                )
            ).all()
        }
        if items
        else {}
    )
    live_tickers: dict[str, dict[str, Any]] = {}
    if items:
        try:
            repository = MonitorRepository(
                request.app.state.database_engine,
                request.app.state.settings.monitor_symbols_config,
            )
            live_tickers = repository.latest_tickers(
                [item.contract_symbol for item in items]
            )
        except MonitorUnavailable:
            live_tickers = {}
    return {
        "items": [
            _opportunity_out(
                item,
                prediction_by_opportunity_id.get(item.id),
                live_tickers.get((item.contract_symbol or "").upper()),
            )
            for item in items
        ]
    }


@router.get("/opportunities/{opportunity_id}/news")
def opportunity_news(
    opportunity_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.public_id == opportunity_id,
            AiMonitorOpportunity.user_id == user.id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    news_ids = [str(item) for item in (opportunity.news_ids_json or []) if str(item)]
    if not news_ids:
        return {"items": [], "total": 0}
    items = db.scalars(
        select(News)
        .where(News.id.in_(news_ids))
        .order_by(News.ts.desc(), News.id.desc())
    ).all()
    return {"items": [_news_out(item) for item in items], "total": len(items)}


@router.get("/opportunities/{opportunity_id}/model-calls")
def opportunity_model_calls(
    opportunity_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return exact persisted prompts and raw provider responses for an opportunity's news."""

    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.public_id == opportunity_id,
            AiMonitorOpportunity.user_id == user.id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    news_ids = [str(item) for item in (opportunity.news_ids_json or []) if str(item)]
    if not news_ids:
        return {
            "items": [],
            "total": 0,
            "retention_started": False,
            "note": "该机会没有关联新闻，无法定位模型调用记录。",
        }
    batch_ids = sorted(
        {
            str(item).strip()
            for item in (opportunity.news_ai_batch_ids_json or [])
            if str(item).strip()
        }
    )
    call_ids: list[int] = []
    for item in opportunity.news_ai_model_call_ids_json or []:
        try:
            call_id = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if call_id > 0:
            call_ids.append(call_id)
    call_ids = sorted(set(call_ids))
    if not batch_ids or not call_ids:
        return {
            "items": [],
            "total": 0,
            "retention_started": opportunity.news_ai_model_call_ids_json is not None,
            "note": (
                "该机会生成时没有可向当前账户披露的模型调用记录；"
                "历史机会或由其他账户完成的共享新闻分析不会回填原始调用。"
            ),
        }
    calls = db.scalars(
        select(NewsAiModelCall)
        .join(NewsAiBatch, NewsAiBatch.id == NewsAiModelCall.batch_id)
        .where(
            NewsAiBatch.started_by == user.id,
            NewsAiModelCall.batch_id.in_(batch_ids),
            NewsAiModelCall.id.in_(call_ids),
        )
        .order_by(NewsAiModelCall.started_at.desc(), NewsAiModelCall.id.desc())
        .limit(50)
    ).all()
    items = [
        {
            "id": call.id,
            "batch_id": call.batch_id,
            "call_type": call.call_type,
            "attempt_depth": int(call.attempt_depth),
            "provider_code": call.provider_code,
            "model_name": call.model_name,
            "news_ids": list(call.news_ids_json or []),
            "request_json": dict(call.request_json or {}),
            "response_text": call.response_text,
            "response_envelope": call.response_envelope,
            "status": call.status,
            "error_category": call.error_category,
            "started_at": _utc_out(call.started_at),
            "completed_at": _utc_out(call.completed_at),
        }
        for call in calls
    ]
    return {
        "items": items,
        "total": len(items),
        "retention_started": True,
        "note": (
            "完整请求提示词与模型原始响应来自机会首次生成时冻结的数据库审计记录。"
            if items
            else "冻结的审计记录已不可用或不属于当前账户，系统不会用后续重跑结果替代。"
        ),
    }


@router.get("/opportunities/{opportunity_id}/fundamentals")
def opportunity_fundamentals(
    opportunity_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.public_id == opportunity_id,
            AiMonitorOpportunity.user_id == user.id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    security = db.scalar(
        select(Security).where(
            Security.symbol == opportunity.symbol,
            Security.is_active.is_(True),
        )
    )
    if security is None:
        return {"symbol": opportunity.symbol, "available": False, "profile": None, "analysis": None}
    profile = db.get(CompanyProfile, security.id)
    analysis = db.scalar(
        select(SecurityFundamentalAnalysis)
        .where(SecurityFundamentalAnalysis.security_id == security.id)
        .order_by(
            SecurityFundamentalAnalysis.as_of_date.desc(),
            SecurityFundamentalAnalysis.generated_at.desc(),
            SecurityFundamentalAnalysis.id.desc(),
        )
    )
    financials = db.scalar(
        select(SecurityFinancialSnapshot)
        .where(SecurityFinancialSnapshot.security_id == security.id)
        .order_by(
            SecurityFinancialSnapshot.snapshot_date.desc(),
            SecurityFinancialSnapshot.retrieved_at.desc(),
            SecurityFinancialSnapshot.id.desc(),
        )
    )
    return {
        "symbol": security.symbol,
        "available": True,
        "security": {
            "id": security.id,
            "exchange": security.exchange,
            "security_type": security.security_type,
            "company_name": security.company_name,
            "company_name_zh": security.company_name_zh,
            "currency": security.currency,
            "country": security.country,
            "cik": security.cik,
            "isin": security.isin,
            "verification_status": security.verification_status,
            "updated_at": _utc_out(security.updated_at),
        },
        "profile": None
        if profile is None
        else {
            "legal_name": profile.legal_name,
            "description": profile.description,
            "industry": profile.industry,
            "industry_zh": profile.industry_zh,
            "sector": profile.sector,
            "sector_zh": profile.sector_zh,
            "website": profile.website,
            "ipo_date": profile.ipo_date,
            "employee_count": profile.employee_count,
            "market_cap": float(profile.market_cap) if profile.market_cap is not None else None,
            "shares_outstanding": (
                float(profile.shares_outstanding)
                if profile.shares_outstanding is not None
                else None
            ),
            "source": profile.source,
            "source_updated_at": _utc_out(profile.source_updated_at),
        },
        "analysis": None
        if analysis is None
        else {
            "analysis_version": analysis.analysis_version,
            "as_of_date": analysis.as_of_date,
            "business_summary": analysis.business_summary,
            "growth_analysis": analysis.growth_analysis,
            "profitability_analysis": analysis.profitability_analysis,
            "valuation_analysis": analysis.valuation_analysis,
            "risk_analysis": analysis.risk_analysis,
            "catalysts": list(analysis.catalysts_json or []),
            "risk_factors": list(analysis.risk_factors_json or []),
            "quality_score": (
                float(analysis.quality_score) if analysis.quality_score is not None else None
            ),
            "growth_score": (
                float(analysis.growth_score) if analysis.growth_score is not None else None
            ),
            "valuation_score": (
                float(analysis.valuation_score) if analysis.valuation_score is not None else None
            ),
            "financial_health_score": (
                float(analysis.financial_health_score)
                if analysis.financial_health_score is not None
                else None
            ),
            "overall_score": (
                float(analysis.overall_score) if analysis.overall_score is not None else None
            ),
            "confidence_score": (
                float(analysis.confidence_score)
                if analysis.confidence_score is not None
                else None
            ),
            "evidence": dict(analysis.evidence_json or {}),
            "generated_at": _utc_out(analysis.generated_at),
        },
        "financials": None
        if financials is None
        else {
            "snapshot_date": financials.snapshot_date,
            "fiscal_period_end": financials.fiscal_period_end,
            "period_type": financials.period_type,
            "currency": financials.currency,
            "data_status": financials.data_status,
            "coverage_pct": (
                float(financials.coverage_pct)
                if financials.coverage_pct is not None
                else None
            ),
            "revenue_ttm": (
                float(financials.revenue_ttm)
                if financials.revenue_ttm is not None
                else None
            ),
            "revenue_growth_yoy_pct": (
                float(financials.revenue_growth_yoy_pct)
                if financials.revenue_growth_yoy_pct is not None
                else None
            ),
            "gross_margin_pct": (
                float(financials.gross_margin_pct)
                if financials.gross_margin_pct is not None
                else None
            ),
            "operating_margin_pct": (
                float(financials.operating_margin_pct)
                if financials.operating_margin_pct is not None
                else None
            ),
            "net_margin_pct": (
                float(financials.net_margin_pct)
                if financials.net_margin_pct is not None
                else None
            ),
            "operating_cash_flow_ttm": (
                float(financials.operating_cash_flow_ttm)
                if financials.operating_cash_flow_ttm is not None
                else None
            ),
            "free_cash_flow_ttm": (
                float(financials.free_cash_flow_ttm)
                if financials.free_cash_flow_ttm is not None
                else None
            ),
            "cash_and_equivalents": (
                float(financials.cash_and_equivalents)
                if financials.cash_and_equivalents is not None
                else None
            ),
            "total_debt": (
                float(financials.total_debt)
                if financials.total_debt is not None
                else None
            ),
            "total_assets": (
                float(financials.total_assets)
                if financials.total_assets is not None
                else None
            ),
            "total_liabilities": (
                float(financials.total_liabilities)
                if financials.total_liabilities is not None
                else None
            ),
            "stockholders_equity": (
                float(financials.stockholders_equity)
                if financials.stockholders_equity is not None
                else None
            ),
            "current_ratio": (
                float(financials.current_ratio)
                if financials.current_ratio is not None
                else None
            ),
            "debt_to_equity": (
                float(financials.debt_to_equity)
                if financials.debt_to_equity is not None
                else None
            ),
            "return_on_equity_pct": (
                float(financials.return_on_equity_pct)
                if financials.return_on_equity_pct is not None
                else None
            ),
            "market_cap": (
                float(financials.market_cap)
                if financials.market_cap is not None
                else None
            ),
            "enterprise_value": (
                float(financials.enterprise_value)
                if financials.enterprise_value is not None
                else None
            ),
            "pe_ratio": (
                float(financials.pe_ratio) if financials.pe_ratio is not None else None
            ),
            "price_to_sales_ratio": (
                float(financials.price_to_sales_ratio)
                if financials.price_to_sales_ratio is not None
                else None
            ),
            "price_to_book_ratio": (
                float(financials.price_to_book_ratio)
                if financials.price_to_book_ratio is not None
                else None
            ),
            "ev_to_ebitda": (
                float(financials.ev_to_ebitda)
                if financials.ev_to_ebitda is not None
                else None
            ),
            "source": financials.source,
            "source_url": financials.source_url,
            "filing_form": financials.filing_form,
            "filing_accession": financials.filing_accession,
            "applicable_metrics": dict(financials.applicable_metrics_json or {}),
            "retrieved_at": _utc_out(financials.retrieved_at),
        },
    }


@router.get("/prediction-records")
def prediction_records(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    items = db.scalars(
        select(AiMonitorPrediction)
        .where(AiMonitorPrediction.user_id == user.id)
        .order_by(AiMonitorPrediction.predicted_at.desc(), AiMonitorPrediction.id.desc())
        .limit(limit)
    ).all()
    return {
        "items": [_prediction_out(item) for item in items],
        "note": "仅记录 AI 机会触发的虚拟预测和到期结果；不会调用模拟盘或实盘下单。",
    }


@router.get("/opportunity-analytics")
def opportunity_analytics(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=500, ge=1, le=500),
    page: int = Query(default=1, ge=1),
    news_score_min: float = Query(default=0, ge=0, le=100),
    indicator_score_min: float = Query(default=0, ge=0, le=100),
    direction: Literal["all", "long", "short"] = Query(default="all"),
) -> dict[str, Any]:
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    result = ai_monitor.historical_opportunity_analytics(
        db,
        repository,
        user.id,
        limit=limit,
        page=page,
        news_score_min=news_score_min,
        indicator_score_min=indicator_score_min,
        direction=direction,
    )
    result["historical_replay_readiness"] = historical_replay.replay_readiness_report(
        db, user.id
    )
    return result


@router.get("/replays")
def list_historical_replays(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    runs = db.scalars(
        select(AiMonitorReplayRun)
        .where(AiMonitorReplayRun.user_id == user.id)
        .order_by(AiMonitorReplayRun.created_at.desc(), AiMonitorReplayRun.id.desc())
        .limit(limit)
    ).all()
    return {
        "items": [historical_replay.replay_run_out(item) for item in runs],
        "readiness": historical_replay.replay_readiness_report(db, user.id),
    }


@router.get("/replays/{replay_id}")
def historical_replay_detail(
    replay_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    run = db.scalar(
        select(AiMonitorReplayRun).where(
            AiMonitorReplayRun.public_id == replay_id,
            AiMonitorReplayRun.user_id == user.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="历史回放任务不存在")
    return {
        **historical_replay.replay_run_out(run),
        "readiness": historical_replay.replay_readiness_report(
            db, user.id, run_id=run.id
        ),
    }


@router.post("/replays", status_code=202)
def create_historical_replay(
    payload: AiMonitorReplayRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_user(request, user)
    active = db.scalar(
        select(AiMonitorReplayRun.id).where(
            AiMonitorReplayRun.user_id == user.id,
            AiMonitorReplayRun.status.in_(("pending", "running")),
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="已有历史回放正在执行")
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    try:
        run = historical_replay.create_replay_run(
            db,
            repository,
            user.id,
            days=payload.days,
            timeframe=payload.timeframe,
            symbols=payload.symbols,
        )
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.replay.create",
            run.public_id,
            {
                "days": payload.days,
                "timeframe": payload.timeframe,
                "symbol_count": run.total_symbols,
            },
        )
        db.commit()
    except historical_replay.HistoricalReplayError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="已有历史回放正在执行") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="历史回放任务暂时无法创建") from None
    background_tasks.add_task(
        historical_replay.execute_replay_run,
        request.app.state.database_engine,
        run.public_id,
        request.app.state.settings.monitor_symbols_config,
    )
    return historical_replay.replay_run_out(run)


@router.post("/news/analyze", status_code=202)
def analyze_single_news(
    payload: AiMonitorNewsAnalyzeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Analyze one explicitly selected news record with the user's default AI model."""

    _require_expected_user(request, user)
    if db.get(News, payload.news_id) is None:
        raise HTTPException(status_code=404, detail="新闻不存在或已被删除")
    try:
        run = ai_monitor.create_single_news_run(db, user.id, payload.news_id)
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.news.analyze",
            payload.news_id,
            {"run_id": run.public_id, "mode": "single"},
        )
        db.commit()
    except ai_monitor.AiMonitorError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="新闻分析任务状态刚刚发生变化，请重试"
        ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="AI 监控数据库暂时不可用，请稍后重试") from None
    background_tasks.add_task(
        ai_monitor.execute_news_run,
        request.app.state.database_engine,
        run.public_id,
        request.app.state.settings.credential_master_key.get_secret_value(),
        [payload.news_id],
        request.app.state.settings.monitor_symbols_config,
        True,
    )
    return _run_out(run)


@router.post("/runs", status_code=202)
def create_run(
    payload: AiMonitorRunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_user(request, user)
    try:
        run = ai_monitor.create_run(db, user.id, payload.run_type)
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.run.create",
            run.public_id,
            {"run_type": payload.run_type},
        )
        db.commit()
    except ai_monitor.AiMonitorError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="任务状态刚刚发生变化，请刷新后重试",
        ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="AI 监控数据库暂时不可用，请稍后重试",
        ) from None
    if payload.run_type == "news":
        background_tasks.add_task(
            ai_monitor.execute_news_run,
            request.app.state.database_engine,
            run.public_id,
            request.app.state.settings.credential_master_key.get_secret_value(),
            None,
            request.app.state.settings.monitor_symbols_config,
            True,
        )
    else:
        background_tasks.add_task(
            ai_monitor.execute_opportunity_run,
            request.app.state.database_engine,
            run.public_id,
            request.app.state.settings.monitor_symbols_config,
        )
    return _run_out(run)
