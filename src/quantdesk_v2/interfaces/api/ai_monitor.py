"""Authenticated AI monitoring workspace routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from ... import ai_monitor, historical_replay, news_ai
from ...ai_model_config import global_ai_model_configured
from ...database import get_db
from ...dependencies import bearer, get_current_user, require_admin_write
from ...models import (
    AdminSetting,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorReplayRun,
    AiMonitorRun,
    AuditLog,
    CompanyProfile,
    News,
    NewsAiAnalysisRecord,
    NewsAiBatch,
    NewsAiModelCall,
    OpportunityMarketSnapshot,
    RealtimeMarketFeatureSnapshot,
    Security,
    SecurityFinancialSnapshot,
    SecurityFundamentalAnalysis,
    User,
    UserSession,
    utcnow,
)
from ...monitor import MonitorRepository, MonitorUnavailable
from ...schemas import (
    AdminUnusualWhalesConfigUpdate,
    AiMonitorConfigUpdate,
    AiMonitorCostConfigUpdate,
    AiMonitorNewsAnalyzeRequest,
    AiMonitorNewsSystemPromptUpdate,
    AiMonitorReplayRequest,
    AiMonitorRunRequest,
    AiMonitorScorePolicyUpdate,
)
from ...security import SecurityError, decode_access_token

router = APIRouter(prefix="/ai-monitor")

_STREAM_POLL_SECONDS = 2.0
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_RETRY_MILLISECONDS = 3000


def _utc_out(value: datetime | None) -> datetime | None:
    """Attach the UTC offset stripped by MySQL's timezone-naive DateTime columns."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _local_date_utc_window(
    date_from: date | None,
    date_to: date | None,
    timezone_offset_minutes: int,
) -> tuple[datetime | None, datetime | None]:
    """Translate an inclusive browser-local date range to UTC-naive DB bounds.

    MySQL stores AI-monitor timestamps as UTC-naive ``DATETIME`` values, while the
    date inputs represent calendar days in the browser's timezone.  Applying the
    local dates directly to those columns shifts the effective range whenever the
    browser is not in UTC.  The upper bound remains exclusive so an inclusive
    ``date_to`` never loses records later that day.
    """

    offset = timedelta(minutes=timezone_offset_minutes)
    start = (
        datetime.combine(date_from, datetime_time.min) - offset
        if date_from is not None
        else None
    )
    end = (
        datetime.combine(date_to + timedelta(days=1), datetime_time.min) - offset
        if date_to is not None
        else None
    )
    return start, end


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


def _get_stream_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> int:
    """Authenticate an SSE connection without retaining a dependency session.

    FastAPI keeps generator dependencies alive for the lifetime of a streaming
    response.  Opening the authentication session explicitly here lets it close
    before the stream starts and prevents a long-lived database transaction.
    """

    unauthorized = HTTPException(
        status_code=401,
        detail="invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        claims = decode_access_token(
            credentials.credentials,
            request.app.state.settings.jwt_secret.get_secret_value(),
        )
        user_id = int(claims["sub"])
        session_id = str(claims["sid"])
    except (SecurityError, TypeError, ValueError):
        raise unauthorized from None

    with Session(request.app.state.database_engine) as db:
        authenticated_session = db.scalar(
            select(UserSession.id).where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
        )
        if authenticated_session is None:
            raise unauthorized
        is_active = db.scalar(
            select(User.is_active).where(User.id == user_id)
        )
        if not is_active:
            raise unauthorized
    return user_id


def _revision_value(value: Any) -> str | int | None:
    if isinstance(value, datetime):
        return _utc_out(value).isoformat() if _utc_out(value) is not None else None
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int)):
        return value
    return str(value)


def _ai_monitor_revisions(db: Session, user_id: int) -> dict[str, dict[str, Any]]:
    """Read lightweight revision cursors; callers own a short-lived session."""

    opportunity_row = db.execute(
        select(
            func.max(AiMonitorOpportunity.updated_at),
            func.max(AiMonitorOpportunity.id),
        ).where(AiMonitorOpportunity.user_id == user_id)
    ).one()
    run_row = db.execute(
        select(func.max(AiMonitorRun.updated_at), func.max(AiMonitorRun.id)).where(
            AiMonitorRun.user_id == user_id
        )
    ).one()
    news_row = db.execute(
        select(func.max(News.ts), func.max(News.ai_analyzed_at))
    ).one()
    market_row = db.execute(
        select(
            func.max(RealtimeMarketFeatureSnapshot.captured_at),
            func.max(RealtimeMarketFeatureSnapshot.id),
        )
    ).one()
    return {
        "opportunities": {
            "updated_at": _revision_value(opportunity_row[0]),
            "cursor": _revision_value(opportunity_row[1]),
        },
        "runs": {
            "updated_at": _revision_value(run_row[0]),
            "cursor": _revision_value(run_row[1]),
        },
        "news": {
            "latest_ts": _revision_value(news_row[0]),
            "analyzed_at": _revision_value(news_row[1]),
        },
        "market": {
            "captured_at": _revision_value(market_row[0]),
            "cursor": _revision_value(market_row[1]),
        },
    }


def _read_ai_monitor_revisions(database_engine: Any, user_id: int) -> dict[str, dict[str, Any]]:
    with Session(database_engine) as db:
        return _ai_monitor_revisions(db, user_id)


def _revision_event_id(revisions: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        revisions,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _changed_revision_scopes(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[str]:
    if previous is None:
        return list(current)
    return [scope for scope, revision in current.items() if previous.get(scope) != revision]


def _sse_message(
    *,
    event: str,
    event_id: str,
    data: Mapping[str, Any],
    retry_milliseconds: int = _STREAM_RETRY_MILLISECONDS,
) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"id: {event_id}\n"
        f"event: {event}\n"
        f"retry: {retry_milliseconds}\n"
        f"data: {payload}\n\n"
    )


def _epoch_ms_out(value: Any) -> str | None:
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1_000, UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _safe_market_data_health(request: Request) -> dict[str, Any]:
    """Expose operational state without credentials or provider payloads."""

    runtime = getattr(request.app.state, "unusual_whales_runtime", None)
    try:
        snapshot = runtime.health_snapshot() if runtime is not None else {}
    except (RuntimeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    try:
        channel_health = (
            runtime.channel_health_snapshot() if runtime is not None else {}
        )
    except (RuntimeError, ValueError):
        channel_health = {}
    if not isinstance(channel_health, Mapping):
        channel_health = {}
    websocket_connected = bool(snapshot.get("connected"))
    rest = dict(snapshot.get("rest") or {})
    writer = dict(snapshot.get("writer") or {})
    leadership = dict(snapshot.get("leadership") or {})
    retention = dict(snapshot.get("retention") or {})
    price_health = dict(channel_health.get("price") or {})
    return {
        "websocket_connected": websocket_connected,
        "stream_connected": websocket_connected,
        "rest_healthy": str(rest.get("status") or "disabled")
        in {"ready", "degraded"},
        "last_event_at": _epoch_ms_out(snapshot.get("last_event_at_ms")),
        "quote": {
            "age_ms": price_health.get("age_ms"),
            "status": price_health.get("status") or "unavailable",
        },
        "sources": {
            "websocket": {
                "connected": websocket_connected,
                "status": snapshot.get("status") or "disabled",
                "last_message_at": _epoch_ms_out(snapshot.get("last_event_at_ms")),
            },
            "rest": {
                "healthy": str(rest.get("status") or "disabled")
                in {"ready", "degraded"},
                "status": rest.get("status") or "disabled",
                "last_poll_at": _epoch_ms_out(rest.get("last_poll_at_ms")),
            },
        },
        "leadership": {
            "status": leadership.get("status") or "unknown",
            "is_leader": bool(leadership.get("is_leader")),
        },
        "writer": {
            "queue_utilization": writer.get("queue_utilization"),
            "events_per_minute": writer.get("events_per_minute"),
            "write_latency_ms": dict(writer.get("write_latency_ms") or {}),
        },
        "retention": {
            "status": retention.get("status") or "unknown",
            "last_run_at": _epoch_ms_out(retention.get("last_run_at_ms")),
        },
        "versions": {
            "feature": ai_monitor.MARKET_FEATURE_VERSION,
            "weights": ai_monitor.OPPORTUNITY_WEIGHTS_VERSION,
            "decision": ai_monitor.OPPORTUNITY_DECISION_VERSION,
        },
    }


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


def _score_policy_out(db: Session, *, can_edit: bool) -> dict[str, Any]:
    policy = ai_monitor.unusual_whales_signal_policy(db)
    setting = db.get(AdminSetting, ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY)
    weights = {
        key: round(float(value), 8)
        for key, value in dict(policy["weights"]).items()
    }
    return {
        "scope": "platform",
        "mode": str(policy["mode"]),
        "weights": weights,
        "weight_unit": "fraction",
        "weight_total": round(sum(weights.values()), 8),
        "published_version": int(policy["published_version"]),
        "policy_version": str(policy["policy_version"]),
        "weights_version": str(policy["weights_version"]),
        "decision_version": str(policy["decision_version"]),
        "score_enabled": bool(policy["score_enabled"]),
        "hard_gate_enabled": bool(policy["hard_gate_enabled"]),
        "can_edit": can_edit,
        "updated_at": _utc_out(setting.updated_at) if setting is not None else None,
        "effective_usage": {
            "scoring": "六域可用证据按数据质量降权后重新归一化",
            "missing_data": "缺失域不计中性分，也不占有效权重",
            "hard_gate": "仅平台 mode=gate 时参与硬门控；当前保存操作启用评分但不启用硬门控",
            "history": "历史机会继续使用生成时冻结的权重版本，不追溯改写",
        },
    }


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
        phase = "monitoring_exit"
    elif now <= grace_deadline:
        phase = "awaiting_market_data"
    else:
        phase = "overdue"

    next_retry_at: datetime | None = None
    if item.status == "pending":
        last_updated_at = item.updated_at or item.predicted_at
        eligible_at = last_updated_at + timedelta(
            minutes=ai_monitor.PREDICTION_SETTLEMENT_RETRY_MINUTES
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
        "exit_at": _utc_out(getattr(item, "exit_at", None)),
        "exit_reason": getattr(item, "exit_reason", None),
        "grace_deadline": _utc_out(grace_deadline),
        "last_attempt_at": _utc_out(item.updated_at),
        "next_retry_at": _utc_out(next_retry_at),
        "poll_interval_seconds": ai_monitor.PREDICTION_SETTLEMENT_POLL_SECONDS,
        "retry_interval_minutes": ai_monitor.PREDICTION_SETTLEMENT_RETRY_MINUTES,
        "grace_hours": ai_monitor.PREDICTION_SETTLEMENT_GRACE_HOURS,
        "price_timeframe": "15m",
    }


def _stable_gate_checks(value: Any) -> dict[str, bool]:
    """Normalize current and legacy gate check payloads to the stable API shape."""
    if isinstance(value, Mapping):
        return {str(key): bool(passed) for key, passed in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        checks: dict[str, bool] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "").strip()
            if key:
                checks[key] = bool(item.get("passed"))
        return checks
    return {}


def _stable_quote_available(quote: Mapping[str, Any]) -> bool:
    """Return whether a historical/current payload contains a sane NBBO."""
    try:
        bid = Decimal(str(quote.get("bid")))
        ask = Decimal(str(quote.get("ask")))
    except (ArithmeticError, TypeError, ValueError):
        return False
    return bool(
        bid.is_finite()
        and ask.is_finite()
        and bid > 0
        and ask > 0
        and ask >= bid
    )


def _stable_opportunity_contract(
    evidence: dict[str, Any],
    snapshot: OpportunityMarketSnapshot | None,
) -> dict[str, Any]:
    market_flow = dict(evidence.get("market_flow") or {})
    if snapshot is not None:
        quote = dict(snapshot.quote_snapshot_json or {})
        option_flow = dict(snapshot.option_flow_snapshot_json or {})
        gex = dict(snapshot.gex_snapshot_json or {})
        institutional_flow = dict(snapshot.institutional_flow_snapshot_json or {})
        gate_summary = dict(snapshot.risk_gate_snapshot_json or {})
        score_components = dict(snapshot.score_components_json or {})
        data_quality = dict(snapshot.data_quality_json or {})
        version = {
            "api": ai_monitor.OPPORTUNITY_API_VERSION,
            "feature": snapshot.feature_version,
            "weights": snapshot.weights_version,
            "decision": snapshot.decision_version,
        }
    else:
        quote = dict(evidence.get("quote") or {})
        option_flow = dict(
            evidence.get("option_flow") or market_flow.get("option_flow") or {}
        )
        gex = dict(evidence.get("gex") or market_flow.get("gex") or {})
        institutional_flow = dict(
            evidence.get("institutional_flow")
            or market_flow.get("institutional_flow")
            or {}
        )
        gate_summary = dict(evidence.get("gate_summary") or {})
        score_components = dict(evidence.get("score_components") or {})
        data_quality = dict(evidence.get("data_quality") or {})
        version = dict(evidence.get("version") or {})
        version.setdefault("api", ai_monitor.OPPORTUNITY_API_VERSION)
        version.setdefault("feature", ai_monitor.MARKET_FEATURE_VERSION)
        version.setdefault("weights", ai_monitor.OPPORTUNITY_WEIGHTS_VERSION)
        version.setdefault("decision", ai_monitor.OPPORTUNITY_DECISION_VERSION)

    if not gate_summary:
        market_quality = dict(evidence.get("market_quality") or {})
        virtual_gate = dict(evidence.get("virtual_entry_gate") or {})
        passed = bool(
            virtual_gate.get(
                "entry_ready",
                virtual_gate.get("passed", market_quality.get("passed", False)),
            )
        )
        gate_summary = {
            "status": "passed" if passed else "not_evaluated",
            "passed": passed,
            "checks": _stable_gate_checks(
                virtual_gate.get("checks") or market_quality.get("checks") or {}
            ),
            "blocking_reasons": [],
            "warnings": ["LEGACY_GATE_SNAPSHOT"],
            "coverage": None,
            "evaluated_at": virtual_gate.get("checked_at"),
            "decision_version": "legacy",
        }
    gate_summary.setdefault(
        "status", "passed" if gate_summary.get("passed") else "blocked"
    )
    gate_summary.setdefault("blocking_reasons", [])
    gate_summary.setdefault("warnings", [])
    gate_summary["checks"] = _stable_gate_checks(gate_summary.get("checks"))
    quote_available = _stable_quote_available(quote)
    data_quality.setdefault("quote_available", quote_available)
    blocking_reasons = [
        str(item)
        for item in gate_summary.get("blocking_reasons") or []
        if str(item).strip()
    ]
    quote_rejection = next(
        (
            reason
            for reason in blocking_reasons
            if reason.startswith("REFERENCE_")
            or reason == "SYMBOL_HALTED_OR_COOLDOWN"
        ),
        None,
    )
    if quote_rejection:
        data_quality.setdefault("quote_status", "rejected")
        data_quality.setdefault("reject_reason", quote_rejection)
    elif not quote_available:
        # Historical records predate the execution-quote gate.  Keep their
        # original lifecycle result, but never present their missing NBBO as a
        # valid reference for a new decision.
        data_quality.setdefault("quote_status", "unavailable")
        data_quality.setdefault("reject_reason", "REFERENCE_QUOTE_UNAVAILABLE")
        warnings = [str(item) for item in gate_summary.get("warnings") or []]
        if "REFERENCE_QUOTE_UNAVAILABLE" not in warnings:
            warnings.append("REFERENCE_QUOTE_UNAVAILABLE")
        gate_summary["warnings"] = warnings
    else:
        data_quality.setdefault("quote_status", "available")

    if not score_components:
        score_snapshot = dict(evidence.get("score_snapshot") or {})
        signal_scores = dict(evidence.get("signal_scores") or {})
        score_components = {
            "news": signal_scores.get("news", score_snapshot.get("news")),
            "technical": signal_scores.get(
                "indicator", score_snapshot.get("technical")
            ),
            "market_flow": signal_scores.get(
                "market_flow", score_snapshot.get("market_flow")
            ),
            "option_flow": None,
            "gex": None,
            "institutional_flow": None,
            "base_combined": signal_scores.get(
                "base_combined", score_snapshot.get("base_combined")
            ),
            "macro_adjustment": signal_scores.get(
                "macro_adjustment", score_snapshot.get("macro_adjustment")
            ),
            "combined": signal_scores.get("combined", score_snapshot.get("combined")),
            "configured_weights": dict(evidence.get("score_weights") or {}),
            "effective_weights": dict(evidence.get("effective_score_weights") or {}),
            "weights_version": version["weights"],
        }
    flow = {
        **market_flow,
        "option_flow": option_flow,
        "institutional_flow": institutional_flow,
    }
    return {
        "api_version": ai_monitor.OPPORTUNITY_API_VERSION,
        "gate_summary": gate_summary,
        "score_components": score_components,
        "quote": quote,
        "flow": flow,
        "gex": gex,
        "data_quality": data_quality,
        "version": version,
        "signal_snapshot": (
            {
                "id": snapshot.id,
                "captured_at": _utc_out(snapshot.captured_at),
                "immutable": True,
            }
            if snapshot is not None
            else None
        ),
    }


def _opportunity_out(
    item: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None = None,
    live_market: dict[str, Any] | None = None,
    market_snapshot: OpportunityMarketSnapshot | None = None,
    *,
    use_frozen: bool = False,
) -> dict[str, Any]:
    prediction_evidence = dict(prediction.evidence_json or {}) if prediction else {}
    current_evidence = dict(item.evidence_json or {})
    contract_evidence = (
        prediction_evidence if use_frozen and prediction_evidence else current_evidence
    )
    stable_contract = _stable_opportunity_contract(
        contract_evidence,
        market_snapshot if use_frozen else None,
    )
    if market_snapshot is not None and not use_frozen:
        stable_contract["signal_snapshot"] = {
            "id": market_snapshot.id,
            "captured_at": _utc_out(market_snapshot.captured_at),
            "immutable": True,
        }
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
    frozen_scores = dict(stable_contract["score_components"])
    news_score = (
        float(frozen_scores["news"])
        if use_frozen and frozen_scores.get("news") is not None
        else float(item.news_score)
    )
    indicator_score = (
        float(frozen_scores["technical"])
        if use_frozen and frozen_scores.get("technical") is not None
        else float(item.indicator_score)
    )
    combined_score = (
        float(frozen_scores["combined"])
        if use_frozen and frozen_scores.get("combined") is not None
        else float(item.combined_score)
    )
    lifecycle_status = {
        "candidate": "candidate",
        "discovered": "confirmed",
        "expired": "expired",
        "dismissed": "dismissed",
    }.get(item.status, "candidate")
    return {
        **stable_contract,
        "id": item.public_id,
        "symbol": item.symbol,
        "contract_symbol": item.contract_symbol,
        "direction": item.direction,
        "status": item.status,
        "lifecycle_status": lifecycle_status,
        "timeframe": item.timeframe,
        "news_score": news_score,
        "indicator_score": indicator_score,
        "combined_score": combined_score,
        "matched_indicator_keys": list(item.matched_indicator_keys_json or []),
        "news_ids": list(item.news_ids_json or []),
        "evidence": contract_evidence,
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
    stable_contract = _stable_opportunity_contract(
        dict(item.evidence_json or {}),
        None,
    )
    return {
        **stable_contract,
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
        "exit_at": _utc_out(item.exit_at),
        "exit_reason": item.exit_reason,
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
    request: Request,
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
    model_configured = global_ai_model_configured(db, legacy_fallback_user_id=user.id)
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
        "data_health": _safe_market_data_health(request),
        "latest_run": _run_out(latest_run) if latest_run is not None else None,
        "updated_at": _utc_out(now),
    }


@router.get("/events")
def ai_monitor_events(
    request: Request,
    user_id: Annotated[int, Depends(_get_stream_user_id)],
) -> StreamingResponse:
    """Stream tenant-scoped revision changes for incremental UI refreshes."""

    database_engine = request.app.state.database_engine
    resume_event_id = request.headers.get("Last-Event-ID", "").strip()[:128]

    async def event_stream():
        previous: dict[str, dict[str, Any]] | None = None
        current_event_id = "unavailable"
        heartbeat_at = asyncio.get_running_loop().time()
        while True:
            if await request.is_disconnected():
                return
            try:
                revisions = await run_in_threadpool(
                    _read_ai_monitor_revisions,
                    database_engine,
                    user_id,
                )
                next_event_id = _revision_event_id(revisions)
                if previous is None:
                    scopes = (
                        []
                        if resume_event_id and resume_event_id == next_event_id
                        else list(revisions)
                    )
                    yield _sse_message(
                        event="ready",
                        event_id=next_event_id,
                        data={
                            "scopes": scopes,
                            "revisions": revisions,
                            "resumed": bool(resume_event_id),
                        },
                    )
                else:
                    scopes = _changed_revision_scopes(previous, revisions)
                    if scopes:
                        yield _sse_message(
                            event="update",
                            event_id=next_event_id,
                            data={
                                "scopes": scopes,
                                "revisions": {
                                    scope: revisions[scope] for scope in scopes
                                },
                            },
                        )
                previous = revisions
                current_event_id = next_event_id
            except SQLAlchemyError:
                # Keep database or driver details out of the stream.  The retry
                # field instructs clients to reconnect if the transport closes.
                yield _sse_message(
                    event="degraded",
                    event_id=current_event_id,
                    data={"code": "REVISION_CHECK_FAILED", "scopes": []},
                )

            now = asyncio.get_running_loop().time()
            if now - heartbeat_at >= _STREAM_HEARTBEAT_SECONDS:
                yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                heartbeat_at = now
            await asyncio.sleep(_STREAM_POLL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/market-context")
def market_context(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the cached US macro regime used by opportunity scoring."""

    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    return request.app.state.macro_market_service.snapshot(repository)


@router.get("/config")
def get_config(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _config_out(db.get(AiMonitorConfig, user.id))


@router.get("/score-policy")
def get_score_policy(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Expose the platform six-domain score policy without market-data secrets."""

    return _score_policy_out(db, can_edit=bool(user.is_admin))


@router.put("/score-policy")
def update_score_policy(
    payload: AiMonitorScorePolicyUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    """Update only six-domain weights while preserving the complete feed config."""

    _require_expected_user(request, admin)
    setting = db.scalar(
        select(AdminSetting)
        .where(AdminSetting.key == ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY)
        .with_for_update()
    )
    if setting is None:
        complete_config = AdminUnusualWhalesConfigUpdate().model_dump(
            exclude={"api_key"}
        )
        setting = AdminSetting(
            key=ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY,
            value_json=complete_config,
            version=1,
            updated_by=admin.id,
        )
        db.add(setting)
    else:
        raw = setting.value_json
        if not isinstance(raw, Mapping):
            raise HTTPException(
                status_code=409,
                detail="当前 Unusual Whales 平台配置损坏，请先在管理后台修复",
            )
        try:
            complete_config = AdminUnusualWhalesConfigUpdate.model_validate(
                dict(raw)
            ).model_dump(exclude={"api_key"})
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="当前 Unusual Whales 平台配置不兼容，请先在管理后台修复",
            ) from None
        setting.version = max(0, int(setting.version or 0)) + 1
        setting.updated_by = admin.id
        setting.updated_at = utcnow()

    previous_mode = str(complete_config.get("mode") or "record")
    previous_weights = dict(complete_config.get("weights") or {})
    complete_config["weights"] = payload.weights.model_dump()
    complete_config["mode"] = "score"
    setting.value_json = complete_config
    _audit(
        db,
        request,
        admin.id,
        "ai_monitor.score_policy.update",
        ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY,
        {
            "previous_mode": previous_mode,
            "mode": "score",
            "previous_weights": previous_weights,
            "weights": dict(complete_config["weights"]),
            "weights_version": f"uw_weights_v{int(setting.version)}",
            "preserved_sections": [
                "channels",
                "thresholds",
                "retention",
                "rest_enabled",
                "websocket_enabled",
            ],
        },
    )
    db.commit()
    db.refresh(setting)
    return _score_policy_out(db, can_edit=True)


@router.get("/news-system-prompt")
def get_news_system_prompt(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    config = db.get(AiMonitorConfig, user.id)
    data = ai_monitor.config_data(config)
    return {
        "system_prompt": data["news_system_prompt"],
        "default_system_prompt": ai_monitor.DEFAULT_NEWS_ANALYSIS_SYSTEM_PROMPT,
        "is_custom": bool(data["news_system_prompt_is_custom"]),
        "max_length": 8000,
        "updated_at": _utc_out(config.updated_at) if config is not None else None,
    }


@router.put("/news-system-prompt")
def update_news_system_prompt(
    payload: AiMonitorNewsSystemPromptUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Persist the user-level system prompt used by subsequent news model calls."""

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
    config.news_system_prompt = payload.system_prompt
    _audit(
        db,
        request,
        user.id,
        "ai_monitor.news_system_prompt.update",
        str(user.id),
        {
            "is_custom": payload.system_prompt is not None,
            "character_count": len(payload.system_prompt or ""),
        },
    )
    db.commit()
    db.refresh(config)
    data = ai_monitor.config_data(config)
    return {
        "system_prompt": data["news_system_prompt"],
        "default_system_prompt": ai_monitor.DEFAULT_NEWS_ANALYSIS_SYSTEM_PROMPT,
        "is_custom": bool(data["news_system_prompt_is_custom"]),
        "max_length": 8000,
        "updated_at": _utc_out(config.updated_at),
    }


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
    config.prediction_max_holding_bars = payload.prediction_max_holding_bars
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
            "news_lookback_hours": payload.news_lookback_hours,
            "prediction_max_holding_bars": payload.prediction_max_holding_bars,
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
    snapshot_by_opportunity_id = (
        {
            snapshot.opportunity_id: snapshot
            for snapshot in db.scalars(
                select(OpportunityMarketSnapshot).where(
                    OpportunityMarketSnapshot.user_id == user.id,
                    OpportunityMarketSnapshot.opportunity_id.in_(
                        [item.id for item in items]
                    ),
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
                snapshot_by_opportunity_id.get(item.id),
                use_frozen=include_expired,
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


@router.get("/opportunities/{opportunity_id}/news-analysis-records")
def opportunity_news_analysis_records(
    opportunity_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the symbol's auditable seven-day rolling AI news memory."""

    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.public_id == opportunity_id,
            AiMonitorOpportunity.user_id == user.id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    symbol = str(opportunity.symbol or "").strip().upper()
    symbol_aliases = news_ai._load_security_memory_aliases(db).get(symbol)
    cutoff = utcnow() - timedelta(days=7)
    opportunity_news_ids = {
        str(item) for item in (opportunity.news_ids_json or []) if str(item)
    }
    raw_rows = db.execute(
        select(NewsAiAnalysisRecord, News)
        .join(News, News.id == NewsAiAnalysisRecord.news_id)
        .where(
            NewsAiAnalysisRecord.user_id == user.id,
            NewsAiAnalysisRecord.symbol == symbol,
            NewsAiAnalysisRecord.analyzed_at >= cutoff,
        )
        .order_by(
            NewsAiAnalysisRecord.analyzed_at.desc(),
            NewsAiAnalysisRecord.id.desc(),
        )
        .limit(500)
    ).all()
    eligible_rows = [
        (record, news)
        for record, news in raw_rows
        if news_ai.news_stock_relation_supported(
            news,
            record.symbol,
            float(record.relevance),
            aliases=symbol_aliases,
        )
    ]
    rows = eligible_rows[:200]
    context_ids = {
        int(context_id)
        for record, _news in rows
        for context_id in (record.context_record_ids_json or [])
        if str(context_id).isdigit() and int(context_id) > 0
    }
    context_symbols = (
        {
            int(record_id): str(context_symbol or "").strip().upper()
            for record_id, context_symbol in db.execute(
                select(NewsAiAnalysisRecord.id, NewsAiAnalysisRecord.symbol).where(
                    NewsAiAnalysisRecord.id.in_(context_ids)
                )
            ).all()
        }
        if context_ids
        else {}
    )
    items: list[dict[str, Any]] = []
    for index, (record, news) in enumerate(rows):
        record_context_ids = [
            int(context_id)
            for context_id in (record.context_record_ids_json or [])
            if str(context_id).isdigit() and int(context_id) > 0
        ]
        symbol_context_ids = [
            context_id
            for context_id in record_context_ids
            if context_symbols.get(context_id) == symbol
        ]
        has_earlier_symbol_record = any(
            older_record.analyzed_at < record.analyzed_at
            or (
                older_record.analyzed_at == record.analyzed_at
                and int(older_record.id) < int(record.id)
            )
            for older_record, _older_news in rows[index + 1 :]
        )
        if record.prior_record_id:
            memory_link_status = "linked"
        elif has_earlier_symbol_record:
            memory_link_status = "context_missing"
        else:
            memory_link_status = "initial"
        items.append({
            "id": int(record.id),
            "symbol": record.symbol,
            "news_id": record.news_id,
            "news_title": news.title_zh or news.title,
            "news_original_title": news.title,
            "news_summary": news.summary,
            "news_source": news.source,
            "news_link": news.link,
            "news_published_at": int(record.news_published_at),
            "direction": record.direction,
            "confidence": float(record.confidence),
            "relevance": float(record.relevance),
            "impact_strength": record.impact_strength,
            "time_horizon": record.time_horizon,
            "category": record.category,
            "analysis_reason": record.analysis_reason,
            "memory_effect": record.memory_effect,
            "memory_reason": record.memory_reason,
            "judgment_basis": dict(record.judgment_basis_json or {}),
            "position_effect": record.position_effect,
            "position_reason": record.position_reason,
            "previous_direction": record.previous_direction,
            "previous_confidence": (
                float(record.previous_confidence)
                if record.previous_confidence is not None
                else None
            ),
            "prior_record_id": record.prior_record_id,
            "context_record_ids": record_context_ids,
            "symbol_context_record_ids": symbol_context_ids,
            "symbol_context_count": len(symbol_context_ids),
            "shared_context_count": len(record_context_ids) - len(symbol_context_ids),
            "memory_link_status": memory_link_status,
            "model_name": record.model_name,
            "analyzed_at": _utc_out(record.analyzed_at),
            "belongs_to_opportunity": record.news_id in opportunity_news_ids,
        })
    current_opportunity_total = sum(
        1 for item in items if item["belongs_to_opportunity"]
    )
    return {
        "symbol": symbol,
        "window_days": 7,
        "cutoff_at": _utc_out(cutoff),
        "items": items,
        "total": len(items),
        "current_opportunity_total": current_opportunity_total,
        "historical_total": len(items) - current_opportunity_total,
        "excluded_total": len(raw_rows) - len(eligible_rows),
        "truncated": len(eligible_rows) > len(rows),
    }


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
        "note": "仅记录 AI 机会触发的预测和到期结果；不会调用模拟盘或实盘下单。",
    }


@router.get("/opportunity-analytics")
def opportunity_analytics(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=500, ge=1, le=500),
    page: int = Query(default=1, ge=1),
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    timezone_offset_minutes: int = Query(default=0, ge=-840, le=840),
    symbol: str = Query(default="", max_length=32),
    news_score_min: float = Query(default=0, ge=0, le=100),
    indicator_score_min: float = Query(default=0, ge=0, le=100),
    combined_score_min: float = Query(default=0, ge=0, le=100),
    option_flow_score_min: float = Query(default=0, ge=0, le=100),
    gex_score_min: float = Query(default=0, ge=0, le=100),
    min_data_coverage: float = Query(default=0, ge=0, le=100),
    feature_version: str = Query(default="", max_length=32),
    decision_version: str = Query(default="", max_length=32),
    direction: Literal["all", "long", "short"] = Query(default="all"),
    market_session: Literal[
        "all", "premarket", "regular", "postmarket", "closed", "unknown"
    ] = Query(default="all"),
    quote_quality: Literal["all", "passed", "blocked", "missing"] = Query(
        default="all"
    ),
    event_risk: Literal["all", "clear", "warning", "blocked"] = Query(default="all"),
    exit_reason: str = Query(default="all", max_length=64),
    include_readiness: bool = Query(default=False),
) -> dict[str, Any]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )
    date_from_utc, date_to_utc = _local_date_utc_window(
        date_from,
        date_to,
        timezone_offset_minutes,
    )
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
        date_from=date_from_utc,
        date_to=date_to_utc,
        timezone_offset_minutes=timezone_offset_minutes,
        symbol=symbol,
        news_score_min=news_score_min,
        indicator_score_min=indicator_score_min,
        combined_score_min=combined_score_min,
        option_flow_score_min=option_flow_score_min,
        gex_score_min=gex_score_min,
        min_data_coverage=min_data_coverage,
        feature_version=feature_version,
        decision_version=decision_version,
        direction=direction,
        market_session=market_session,
        quote_quality=quote_quality,
        event_risk=event_risk,
        exit_reason=exit_reason,
        include_readiness=include_readiness,
    )
    return result


@router.get("/opportunity-readiness")
def opportunity_readiness(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the global readiness gate separately from filtered list queries."""

    current_config = ai_monitor.config_data(db.get(AiMonitorConfig, user.id))
    return ai_monitor.strategy_readiness_report(db, user.id, current_config)


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
