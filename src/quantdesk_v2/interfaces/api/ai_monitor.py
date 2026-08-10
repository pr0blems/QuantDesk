"""Authenticated AI monitoring workspace routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ... import ai_monitor
from ...database import get_db
from ...dependencies import get_current_user
from ...models import (
    AiModelConfig,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorRun,
    AuditLog,
    CompanyProfile,
    News,
    NewsAiBatch,
    Security,
    SecurityFinancialSnapshot,
    SecurityFundamentalAnalysis,
    User,
    utcnow,
)
from ...monitor import MonitorRepository
from ...schemas import (
    AiMonitorConfigUpdate,
    AiMonitorNewsAnalyzeRequest,
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


def _opportunity_out(item: AiMonitorOpportunity) -> dict[str, Any]:
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
        "discovered_at": _utc_out(item.discovered_at),
        "expires_at": _utc_out(item.expires_at),
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
    conflicts = ai_monitor.indicator_conflicts(payload.indicator_keys)
    if conflicts:
        conflict = conflicts[0]
        raise HTTPException(
            status_code=422,
            detail=(
                f"指标条件互相冲突：{conflict['left_name']} 与 "
                f"{conflict['right_name']} 无法在同一根 K 线上同时满足"
            ),
        )
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
            "match_policy": "all",
        },
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
        "match_policy": "all",
        "templates": ai_monitor.indicator_templates(),
        "conflict_pairs": [list(pair) for pair in ai_monitor.INDICATOR_CONFLICT_PAIRS],
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
    if include_expired:
        statement = statement.order_by(
            AiMonitorOpportunity.combined_score.desc(),
            AiMonitorOpportunity.discovered_at.desc(),
            AiMonitorOpportunity.id.desc(),
        )
    else:
        statement = statement.order_by(
            AiMonitorOpportunity.updated_at.desc(),
            AiMonitorOpportunity.id.desc(),
        )
    items = db.scalars(statement.limit(300 if not include_expired else limit)).all()
    if not include_expired:
        unique: dict[str, AiMonitorOpportunity] = {}
        for item in items:
            instrument = (item.contract_symbol or item.symbol).strip().upper()
            unique.setdefault(instrument, item)
        items = sorted(
            unique.values(),
            key=lambda item: (float(item.combined_score), item.updated_at, item.id),
            reverse=True,
        )[:limit]
    return {"items": [_opportunity_out(item) for item in items]}


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
    limit: int = Query(default=300, ge=1, le=500),
) -> dict[str, Any]:
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    return ai_monitor.historical_opportunity_analytics(
        db,
        repository,
        user.id,
        limit=limit,
    )


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
