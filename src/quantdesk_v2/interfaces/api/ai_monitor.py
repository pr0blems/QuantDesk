"""Authenticated AI monitoring workspace routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
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
from starlette.responses import Response, StreamingResponse

from ... import ai_monitor, historical_replay, live_engine, macro_ai, news_ai, ws_depth
from ...ai_model_config import global_ai_model_configured
from ...ai_monitor_read_models import read_models_available
from ...binance_client import BinanceAccountClientError
from ...database import get_db
from ...dependencies import bearer, get_current_user, require_admin_write
from ...finnhub_quotes import FINNHUB_USAGE_SETTING_KEY, FinnhubUsQuoteService
from ...market_config import TRADFI_UNIVERSE_KEY, tradfi_symbols
from ...models import (
    AdminSetting,
    AiMonitorConfig,
    AiMonitorOpportunity,
    AiMonitorOpportunityCurrent,
    AiMonitorPrediction,
    AiMonitorReplayRun,
    AiMonitorRun,
    AuditLog,
    CompanyProfile,
    LiveOrderIntent,
    LiveTradingAccount,
    News,
    NewsAiAnalysisRecord,
    NewsAiBatch,
    NewsAiModelCall,
    OpportunityMarketSnapshot,
    RealtimeMarketFeatureSnapshot,
    Security,
    SecurityFinancialSnapshot,
    SecurityFundamentalAnalysis,
    StrategyDeployment,
    StrategyRevision,
    User,
    UserSession,
    UserStrategy,
    utcnow,
)
from ...monitor import MonitorRepository, MonitorUnavailable
from ...schemas import (
    AdminUnusualWhalesConfigUpdate,
    AiMonitorConfigUpdate,
    AiMonitorCostConfigUpdate,
    AiMonitorFinnhubUsageUpdate,
    AiMonitorLiveCopyConfigUpdate,
    AiMonitorLiveCopyUpdate,
    AiMonitorManualFollowRequest,
    AiMonitorNewsAnalyzeRequest,
    AiMonitorNewsSystemPromptUpdate,
    AiMonitorReplayRequest,
    AiMonitorRunRequest,
    AiMonitorScorePolicyUpdate,
    AiMonitorUnusualWhalesUsageUpdate,
)
from ...security import CredentialCipher, SecurityError, decode_access_token
from ...strategy_artifacts import add_run_manifest, record_revision_artifact

router = APIRouter(prefix="/ai-monitor")

_STREAM_POLL_SECONDS = 2.0
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_RETRY_MILLISECONDS = 3000
_AI_MONITOR_LIVE_SCOPE = "ai_monitor"
_AI_MONITOR_LIVE_ACCOUNT_NAME = "AI发现机会独立跟单"
_AI_MONITOR_LIVE_ADAPTER_NAME = "__AI Monitor Live Execution Adapter__"
_AI_MONITOR_LIVE_RISK_DEFAULTS: dict[str, Any] = {
    "leverage": 10,
    "risk_max_leverage": 10,
    "max_positions": 10,
    "position_size_basis": "account_equity",
    "copy_total_amount": 1_000.0,
    "position_size_pct": 2.0,
    "max_margin_per_trade_pct": 2.0,
    "margin_cap": 0.2,
    "risk_per_trade_pct": 0.5,
    "max_total_risk_pct": 4.0,
    "daily_loss_limit_pct": 2.0,
    "max_drawdown_pct": 6.0,
    "round_trip_cost_bps": 16.0,
    "max_ticker_age_seconds": 120,
    "max_signal_age_seconds": 300,
    "block_high_risk_products": True,
}
_MANUAL_FOLLOW_MESSAGES = {
    "filled_and_protected": "订单已成交，交易所止损与止盈已建立。",
    "already_filled": "本次确认已经执行过，没有重复提交同一笔订单。重新打开确认框可发起新的手动跟买。",
    "order_already_exists": "该信号已有订单正在处理或对账，本次未重复下单。",
    "live_copy_inactive": "独立实盘跟单未处于运行状态，请先开启并确认账户状态。",
    "engine_unavailable": "实盘执行器当前不可用，请稍后重试。",
    "portfolio_margin_unsupported": "当前 Binance 账户类型不受支持，执行器已安全停机。",
    "position_mode_changed": "Binance 持仓模式与跟单配置不一致，执行器已安全停机。",
    "symbol_not_enabled": "该合约不在当前独立跟单品种范围内。",
    "symbol_already_open": "该合约已有持仓，为避免叠加风险，本次未下单。",
    "max_positions_reached": "已达到最大同时持仓数量，本次未下单。",
    "risk_review_required": "账户存在未纳管持仓或保护单异常，需要先完成风险复核。",
    "loss_limit_reached": "账户日内亏损或回撤限制已触发，本次未下单。",
    "filled_audit_pending": "上一笔成交审计尚未完成，本次未下单。",
    "ticker_unavailable": "该合约没有可用的 Binance 实时价格，本次未下单。",
    "symbol_risk_blocked": "该合约未通过当前品种风险准入，本次未下单。",
    "order_not_submitted": "仓位计算或下单前风控未通过，没有提交订单。",
    "order_status_uncertain": "订单状态暂无法确认，执行器已停止重复提交，请先完成对账。",
    "exchange_rejected": "Binance 拒绝或取消了该订单，本次没有重复提交。",
    "execution_failed_closed": "订单提交后的安全校验未通过，执行器已按失败关闭并停止。",
    "direction_invalid": "信号方向无效，本次未下单。",
}


def _utc_out(value: datetime | None) -> datetime | None:
    """Attach the UTC offset stripped by MySQL's timezone-naive DateTime columns."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _finite_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _quote_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc_out(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp <= 0:
            return None
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc_out(parsed)


def _quote_age_seconds(observed_at: datetime | None, now: datetime) -> float | None:
    if observed_at is None:
        return None
    return round(max(0.0, (now - observed_at).total_seconds()), 3)


def _price_comparison_out(
    live_market: Mapping[str, Any] | None,
    spot_quote: Mapping[str, Any] | None,
    enhanced_market: Mapping[str, Any] | None,
    *,
    direction: str | None = None,
    news_score: float | None = None,
    news_count: int = 0,
    new_news_count: int = 0,
    reused_news_count: int = 0,
    memory_window_hours: int = 168,
) -> dict[str, Any]:
    """Build a current three-provider quote view without treating it as risk-free arbitrage.

    Binance is the execution/valuation source for the mapped contract. Finnhub and
    Unusual Whales describe the US cash-market reference and may be delayed or
    closed.  A spread is therefore a basis observation until both legs are fresh,
    synchronized and executable.
    """

    now = datetime.now(UTC)
    market = dict(live_market or {})
    finnhub = dict(spot_quote or {})
    enhanced = dict(enhanced_market or {})
    uw_quote = dict(enhanced.get("quote") or {})

    binance_price = _finite_price(market.get("price"))
    binance_at = _quote_time(market.get("ts"))
    binance_age = _quote_age_seconds(binance_at, now)
    binance = {
        "source": "binance",
        "label": "BN",
        "venue": "mapped_contract",
        "role": "execution",
        "available": binance_price is not None,
        "price": binance_price,
        "observed_at": binance_at,
        "age_seconds": binance_age,
        "fresh": bool(binance_price is not None and binance_age is not None and binance_age <= 120),
    }

    finnhub_price = _finite_price(finnhub.get("price"))
    finnhub_previous_close = _finite_price(finnhub.get("previous_close"))
    finnhub_at = _quote_time(finnhub.get("source_timestamp")) or _quote_time(
        finnhub.get("fetched_at")
    )
    finnhub_age = _quote_age_seconds(finnhub_at, now)
    finnhub_fresh = bool(
        finnhub_price is not None
        and finnhub.get("stale") is not True
        and finnhub.get("live") is True
        and finnhub_age is not None
        and finnhub_age <= 600
    )
    finnhub_source = {
        "source": "finnhub",
        "label": "FH",
        "venue": "us_cash_last_trade",
        "role": "reference",
        "available": finnhub_price is not None,
        "price": finnhub_price,
        "previous_close": finnhub_previous_close,
        "observed_at": finnhub_at,
        "age_seconds": finnhub_age,
        "fresh": finnhub_fresh,
        "live": bool(finnhub.get("live")),
        "storage": finnhub.get("storage"),
    }

    uw_bid = _finite_price(uw_quote.get("bid"))
    uw_ask = _finite_price(uw_quote.get("ask"))
    uw_last = _finite_price(uw_quote.get("last_price"))
    uw_previous_close = _finite_price(uw_quote.get("previous_close"))
    uw_midpoint = (
        (uw_bid + uw_ask) / 2
        if uw_bid is not None and uw_ask is not None and uw_ask >= uw_bid
        else None
    )
    uw_price = uw_midpoint or uw_last
    uw_at = (
        _quote_time(uw_quote.get("quote_received_at_ms"))
        or _quote_time(uw_quote.get("received_at_ms"))
        or _quote_time(enhanced.get("captured_at"))
        or _quote_time(enhanced.get("bucket_at"))
    )
    uw_age = _quote_age_seconds(uw_at, now)
    uw_fresh = bool(uw_price is not None and uw_age is not None and uw_age <= 360)
    unusual_whales = {
        "source": "unusual_whales",
        "label": "UW",
        "venue": "us_cash_nbbo" if uw_midpoint is not None else "us_cash_last_trade",
        "role": "reference",
        "available": uw_price is not None,
        "price": uw_price,
        "previous_close": uw_previous_close,
        "bid": uw_bid,
        "ask": uw_ask,
        "spread_bps": uw_quote.get("spread_bps"),
        "observed_at": uw_at,
        "age_seconds": uw_age,
        "fresh": uw_fresh,
        "market_session": uw_quote.get("market_session"),
    }

    fresh_references = [
        source
        for source in (finnhub_source, unusual_whales)
        if source["fresh"] and source["price"] is not None
    ]
    reference_price = (
        sum(float(source["price"]) for source in fresh_references)
        / len(fresh_references)
        if fresh_references
        else None
    )
    available_references = [
        source
        for source in (finnhub_source, unusual_whales)
        if source["available"] and source["price"] is not None
    ]
    latest_reference = max(
        available_references,
        key=lambda source: (
            source["observed_at"].timestamp()
            if isinstance(source.get("observed_at"), datetime)
            else 0.0
        ),
        default=None,
    )
    snapshot_reference_price = (
        float(latest_reference["price"]) if latest_reference is not None else None
    )
    snapshot_reference_at = (
        latest_reference.get("observed_at") if latest_reference is not None else None
    )
    snapshot_reference_source = (
        latest_reference.get("source") if latest_reference is not None else None
    )
    previous_close_price = finnhub_previous_close or uw_previous_close
    basis_bps = (
        round((binance_price / reference_price - 1) * 10_000, 4)
        if binance_price is not None and reference_price is not None
        else None
    )
    snapshot_gap_bps = (
        round((binance_price / snapshot_reference_price - 1) * 10_000, 4)
        if binance_price is not None and snapshot_reference_price is not None
        else None
    )
    previous_close_gap_bps = (
        round((binance_price / previous_close_price - 1) * 10_000, 4)
        if binance_price is not None and previous_close_price is not None
        else None
    )
    provider_divergence_bps = (
        round(abs(finnhub_price / uw_price - 1) * 10_000, 4)
        if finnhub_price is not None and uw_price is not None
        else None
    )
    comparable = bool(binance["fresh"] and fresh_references)
    threshold_bps = 30.0
    if not binance["available"]:
        state = "execution_unavailable"
    elif not comparable and (snapshot_reference_price is not None or previous_close_price is not None):
        state = "opening_gap_watch"
    elif not comparable:
        state = "reference_unavailable"
    elif basis_bps is not None and abs(basis_bps) >= threshold_bps:
        state = "spread_watch"
    else:
        state = "aligned"
    pair_direction = None
    if comparable and basis_bps is not None and abs(basis_bps) >= threshold_bps:
        pair_direction = (
            "short_binance_long_spot"
            if basis_bps > 0
            else "long_binance_short_spot"
        )

    normalized_direction = str(direction or "").strip().lower()
    normalized_news_score = (
        max(0.0, min(100.0, float(news_score)))
        if news_score is not None
        else None
    )
    normalized_news_count = max(0, int(news_count or 0))
    normalized_new_news_count = max(0, int(new_news_count or 0))
    normalized_reused_news_count = max(0, int(reused_news_count or 0))
    normalized_memory_window_hours = max(1, int(memory_window_hours or 168))
    forecast_gap_bps = snapshot_gap_bps
    if forecast_gap_bps is None:
        forecast_gap_bps = previous_close_gap_bps
    forecast_available = bool(
        binance_price is not None
        and forecast_gap_bps is not None
        and normalized_direction in {"long", "short"}
        and normalized_news_score is not None
    )
    forecast_direction = (
        "down"
        if normalized_direction == "short"
        else "up"
        if normalized_direction == "long"
        else "neutral"
    )
    gap_aligned = bool(
        forecast_available
        and (
            (normalized_direction == "long" and float(forecast_gap_bps) >= 0)
            or (normalized_direction == "short" and float(forecast_gap_bps) <= 0)
        )
    )
    forecast_confidence = normalized_news_score
    if forecast_available and forecast_confidence is not None:
        if abs(float(forecast_gap_bps)) >= threshold_bps:
            forecast_confidence += 8.0 if gap_aligned else -8.0
        forecast_confidence += min(5.0, max(0, normalized_news_count - 1))
        if provider_divergence_bps is not None and provider_divergence_bps >= 100:
            forecast_confidence -= 5.0
        forecast_confidence = round(max(0.0, min(99.0, forecast_confidence)), 1)
    if not forecast_available or (normalized_news_score or 0) < 60:
        forecast_label = "neutral_watch"
    elif forecast_direction == "down":
        forecast_label = "bearish_open"
    else:
        forecast_label = "bullish_open"
    return {
        "version": "cross_venue_basis_v2",
        "execution_source": "binance",
        "sources": {
            "binance": binance,
            "finnhub": finnhub_source,
            "unusual_whales": unusual_whales,
        },
        "reference_price": reference_price,
        "fresh_reference_count": len(fresh_references),
        "snapshot_reference_price": snapshot_reference_price,
        "snapshot_reference_at": snapshot_reference_at,
        "snapshot_reference_source": snapshot_reference_source,
        "previous_close_price": previous_close_price,
        "basis_bps": basis_bps,
        "snapshot_gap_bps": snapshot_gap_bps,
        "previous_close_gap_bps": previous_close_gap_bps,
        "provider_divergence_bps": provider_divergence_bps,
        "provider_divergence_mode": (
            "live" if finnhub_fresh and uw_fresh else "snapshot"
        ) if provider_divergence_bps is not None else None,
        "minimum_watch_bps": threshold_bps,
        "state": state,
        "pair_direction": pair_direction,
        "comparable": comparable,
        "actionable": False,
        "research_only_reason": "cash_contract_basis_requires_synchronized_executable_two_leg_quotes",
        "opening_forecast": {
            "available": forecast_available,
            "label": forecast_label,
            "direction": forecast_direction,
            "confidence": forecast_confidence,
            "news_score": normalized_news_score,
            "news_count": normalized_news_count,
            "related_news_count": normalized_news_count,
            "new_news_count": normalized_new_news_count,
            "reused_news_count": normalized_reused_news_count,
            "memory_window_hours": normalized_memory_window_hours,
            "gap_bps": forecast_gap_bps,
            "gap_aligned": gap_aligned if forecast_available else None,
            "reference_mode": (
                "latest_cash_snapshot"
                if snapshot_reference_price is not None
                else "previous_close"
                if previous_close_price is not None
                else "unavailable"
            ),
            "research_only": True,
        },
        "calculated_at": now,
    }


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


def _strategy_live_readiness(db: Session, user_id: int) -> dict[str, Any]:
    """Evaluate the current settlement cohort before any new real-fund entry."""

    current_config = ai_monitor.config_data(db.get(AiMonitorConfig, user_id))
    return ai_monitor.strategy_readiness_report(db, user_id, current_config)


def _require_strategy_live_readiness(db: Session, user_id: int) -> dict[str, Any]:
    """Fail closed when the cost-adjusted strategy has not passed research gates."""

    readiness = _strategy_live_readiness(db, user_id)
    if bool(readiness.get("quantitative_ready")):
        return readiness
    passed = int(readiness.get("passed_count") or 0)
    total = int(readiness.get("total_count") or 0)
    raise HTTPException(
        status_code=409,
        detail=(
            "当前止盈止损策略尚未通过成本后量化准入，已禁止新增真实资金订单"
            f"（通过 {passed}/{total} 项；策略版本 "
            f"{ai_monitor.PREDICTION_SETTLEMENT_VERSION}）。"
        ),
    )


def _score_policy_out(db: Session, *, can_edit: bool) -> dict[str, Any]:
    policy = ai_monitor.unusual_whales_signal_policy(db)
    setting = db.get(AdminSetting, ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY)
    weights = {
        key: round(float(value), 8)
        for key, value in dict(policy["weights"]).items()
    }
    finnhub_setting = db.get(AdminSetting, FINNHUB_USAGE_SETTING_KEY)
    finnhub_value = (
        finnhub_setting.value_json
        if finnhub_setting is not None
        and isinstance(finnhub_setting.value_json, Mapping)
        else {}
    )
    finnhub_enabled = bool(finnhub_value.get("enabled", True))
    return {
        "scope": "platform",
        "enabled": bool(policy["enabled"]),
        "mode": str(policy["mode"]),
        "effective_mode": str(policy["effective_mode"]),
        "refresh_interval_seconds": 5 * 60,
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
        "unusual_whales_enabled": bool(policy["enabled"]),
        "finnhub_enabled": finnhub_enabled,
        "finnhub": {
            "enabled": finnhub_enabled,
            "market_open_only": True,
            "storage": "finnhub_quote_snapshots",
        },
        "updated_at": _utc_out(setting.updated_at) if setting is not None else None,
        "effective_usage": {
            "scoring": "六域可用证据按数据质量降权后重新归一化",
            "missing_data": "缺失域不计中性分，也不占有效权重",
            "hard_gate": "仅平台 mode=gate 时参与硬门控；当前保存操作启用评分但不启用硬门控",
            "history": "历史机会继续使用生成时冻结的权重版本，不追溯改写",
        },
    }


def _binance_trade_permission_requested(user: User) -> bool:
    permissions = user.binance_permissions or {}
    requested = permissions.get("requested") if isinstance(permissions, Mapping) else None
    return isinstance(requested, list) and "TRADE" in requested


def _is_ai_monitor_live_account(account: LiveTradingAccount) -> bool:
    config = account.config_json or {}
    return (
        isinstance(config, Mapping)
        and str(config.get("execution_scope") or "") == _AI_MONITOR_LIVE_SCOPE
    )


def _ai_monitor_live_account(
    db: Session,
    user_id: int,
    *,
    for_update: bool = False,
) -> LiveTradingAccount | None:
    query = (
        select(LiveTradingAccount)
        .where(
            LiveTradingAccount.user_id == user_id,
            LiveTradingAccount.status != "archived",
        )
        .order_by(LiveTradingAccount.created_at, LiveTradingAccount.id)
    )
    if for_update:
        query = query.with_for_update()
    return next(
        (
            account
            for account in db.scalars(query).all()
            if _is_ai_monitor_live_account(account)
        ),
        None,
    )


def _ai_monitor_live_config(*, enabled: bool) -> dict[str, Any]:
    return {
        **_AI_MONITOR_LIVE_RISK_DEFAULTS,
        "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        "signal_source": _AI_MONITOR_LIVE_SCOPE,
        "symbols": tradfi_symbols(),
        "universe_key": TRADFI_UNIVERSE_KEY,
        "ai_monitor_live_copy_enabled": enabled,
        "ai_monitor_live_signal_max_age_seconds": 300,
        "ai_monitor_live_min_combined_score": 70.0,
        "ai_monitor_live_require_entry_ready": True,
        "ai_monitor_live_regular_session_only": True,
        "ai_monitor_live_allow_long": True,
        "ai_monitor_live_allow_short": True,
        "position_mode": "one_way",
    }


def _ai_monitor_live_adapter(
    db: Session,
    user: User,
) -> tuple[UserStrategy, StrategyRevision]:
    strategy = db.scalar(
        select(UserStrategy)
        .where(
            UserStrategy.user_id == user.id,
            UserStrategy.name == _AI_MONITOR_LIVE_ADAPTER_NAME,
        )
        .order_by(UserStrategy.id)
        .with_for_update()
    )
    if strategy is None:
        strategy = UserStrategy(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            source_template_id=None,
            name=_AI_MONITOR_LIVE_ADAPTER_NAME,
            category="execution_adapter",
            description="AI 发现机会独立实盘执行适配器；不参与普通策略求值。",
            status="archived",
            version=1,
            engine_key="multi_factor",
            strategy_kind="legacy_signal",
            lifecycle_status="retired",
            spec_schema_version=None,
            spec_json=None,
            spec_hash=None,
            risk_level="high",
            parameter_schema_json=[],
            parameters_json={},
            risk_defaults_json=dict(_AI_MONITOR_LIVE_RISK_DEFAULTS),
            created_via="manual",
        )
        db.add(strategy)
        db.flush()
    revision = db.scalar(
        select(StrategyRevision)
        .where(
            StrategyRevision.user_id == user.id,
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.version == strategy.version,
        )
        .with_for_update()
    )
    if revision is None:
        snapshot = {
            "public_id": strategy.public_id,
            "name": "AI 发现机会独立信号",
            "version": strategy.version,
            "engine_key": strategy.engine_key,
            "strategy_kind": strategy.strategy_kind,
            "parameters": {},
            "risk_defaults": dict(_AI_MONITOR_LIVE_RISK_DEFAULTS),
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        }
        revision = StrategyRevision(
            user_strategy_id=strategy.id,
            user_id=user.id,
            version=strategy.version,
            change_source="manual",
            change_summary="创建 AI 机会独立执行适配器",
            snapshot_json=snapshot,
            validation_json={"execution_adapter": True, "ordinary_strategy": False},
            lifecycle_status="retired",
            published_at=utcnow(),
        )
        db.add(revision)
        db.flush()
        record_revision_artifact(db, strategy, revision)
    return strategy, revision


def _ensure_ai_monitor_live_account(
    db: Session,
    user: User,
) -> tuple[LiveTradingAccount, StrategyDeployment]:
    strategy, revision = _ai_monitor_live_adapter(db, user)
    account = _ai_monitor_live_account(db, user.id, for_update=True)
    if account is None:
        account = LiveTradingAccount(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            strategy_id=strategy.id,
            name=_AI_MONITOR_LIVE_ACCOUNT_NAME,
            status="paused",
            config_json=_ai_monitor_live_config(enabled=False),
            strategy_snapshot_json=dict(revision.snapshot_json or {}),
            credential_version=user.binance_key_version,
        )
        db.add(account)
        db.flush()
    deployment = db.scalar(
        select(StrategyDeployment)
        .where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "live",
            StrategyDeployment.target_account_id == account.id,
            StrategyDeployment.status != "stopped",
        )
        .order_by(StrategyDeployment.id.desc())
        .with_for_update()
    )
    if deployment is None:
        deployment = StrategyDeployment(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            strategy_id=strategy.id,
            strategy_revision_id=revision.id,
            mode="live",
            target_account_id=account.id,
            name=_AI_MONITOR_LIVE_ACCOUNT_NAME,
            status="paused",
            universe_override_json={
                "universe_key": TRADFI_UNIVERSE_KEY,
                "symbols": tradfi_symbols(),
            },
            risk_override_json=dict(_AI_MONITOR_LIVE_RISK_DEFAULTS),
            runtime_state_json={"execution_scope": _AI_MONITOR_LIVE_SCOPE},
        )
        db.add(deployment)
        add_run_manifest(
            db,
            deployment,
            revision,
            data_set_id=f"live-account:{account.public_id}",
            extra={"execution_scope": _AI_MONITOR_LIVE_SCOPE},
        )
    return account, deployment


def _live_copy_account_out(
    account: LiveTradingAccount,
    *,
    unresolved_order_count: int,
) -> dict[str, Any]:
    config = dict(account.config_json or {})
    return {
        "id": account.public_id,
        "name": account.name,
        "status": account.status,
        "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        "independent": True,
        "provisioned": True,
        "configured": bool(config.get("ai_monitor_live_copy_enabled")),
        "enabled_at": config.get("ai_monitor_live_copy_enabled_at"),
        "last_tick_at": _utc_out(account.last_tick_at),
        "last_error_code": account.last_error_code,
        "unresolved_order_count": int(unresolved_order_count),
        "risk": {
            "position_mode": str(
                config.get("position_mode")
                or ("hedge" if account.last_error_code == "position_mode_changed" else "one_way")
            ),
            "leverage": int(config.get("leverage", 1)),
            "max_positions": int(config.get("max_positions", 1)),
            "position_size_basis": (
                str(config.get("position_size_basis"))
                if config.get("position_size_basis")
                in {"account_equity", "copy_total_amount"}
                else "account_equity"
            ),
            "copy_total_amount": float(config.get("copy_total_amount", 1_000)),
            "position_size_pct": float(config.get("position_size_pct", 2)),
            "risk_per_trade_pct": float(config.get("risk_per_trade_pct", 0.5)),
            "max_total_risk_pct": float(config.get("max_total_risk_pct", 4)),
            "margin_cap_pct": round(float(config.get("margin_cap", 0.2)) * 100, 4),
            "daily_loss_limit_pct": float(config.get("daily_loss_limit_pct", 2)),
            "max_drawdown_pct": float(config.get("max_drawdown_pct", 6)),
            "round_trip_cost_bps": float(config.get("round_trip_cost_bps", 16)),
            "signal_max_age_seconds": int(
                config.get("ai_monitor_live_signal_max_age_seconds", 300)
            ),
            "minimum_combined_score": float(
                config.get("ai_monitor_live_min_combined_score", 70)
            ),
            "regular_session_only": bool(
                config.get("ai_monitor_live_regular_session_only", True)
            ),
            "allow_long": bool(config.get("ai_monitor_live_allow_long", True)),
            "allow_short": bool(config.get("ai_monitor_live_allow_short", True)),
        },
    }


def _manual_follow_evidence(value: Any) -> dict[str, Any]:
    basis = dict(value) if isinstance(value, Mapping) else {}
    signal = basis.get("signal")
    signal = dict(signal) if isinstance(signal, Mapping) else {}
    evidence = signal.get("evidence")
    return dict(evidence) if isinstance(evidence, Mapping) else {}


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _manual_close_reason(intent: Mapping[str, Any] | None) -> str | None:
    if intent is None:
        return None
    request_json = intent.get("request_json")
    if isinstance(request_json, Mapping) and request_json.get("reason"):
        return str(request_json["reason"])
    signal_key = str(intent.get("signal_key") or "")
    marker = ":close:"
    if marker in signal_key:
        suffix = signal_key.split(marker, 1)[1]
        reason, separator, minute = suffix.rpartition(":")
        if separator and minute.isdigit() and reason:
            return reason
    if "reconciled-close" in signal_key:
        return "exchange_position_absent"
    return None


def _manual_follow_history_out(
    intents: Sequence[Mapping[str, Any]],
    *,
    positions: Sequence[Mapping[str, Any]] = (),
    income_records: Sequence[Any] = (),
    generated_at: datetime,
    history_status: str,
    history_error: str | None = None,
    history_start_at: datetime | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Combine the local execution ledger with Binance's authoritative PnL."""

    ordered = sorted(intents, key=lambda item: int(item.get("id") or 0))
    manual_opens: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for intent in ordered:
        evidence = _manual_follow_evidence(intent.get("entry_basis_json"))
        if intent.get("action") == "open" and evidence.get("manual_follow") is True:
            manual_opens.append((intent, evidence))
    manual_opens = manual_opens[-limit:]
    current_positions = {
        (
            str(item.get("symbol") or "").upper(),
            str(item.get("position_side") or "BOTH").upper(),
        ): item
        for item in positions
    }

    records: list[dict[str, Any]] = []
    for opened, evidence in manual_opens:
        open_id = int(opened.get("id") or 0)
        symbol = str(opened.get("symbol") or "").upper()
        position_side = str(opened.get("position_side") or "BOTH").upper()
        attempt_id = str(evidence.get("manual_attempt_id") or "")
        closed: Mapping[str, Any] | None = None
        for candidate in ordered:
            if (
                int(candidate.get("id") or 0) <= open_id
                or candidate.get("action") != "close"
                or str(candidate.get("symbol") or "").upper() != symbol
                or str(candidate.get("position_side") or "BOTH").upper()
                != position_side
            ):
                continue
            candidate_evidence = _manual_follow_evidence(
                candidate.get("entry_basis_json")
            )
            candidate_request = candidate.get("request_json")
            same_open = bool(
                isinstance(candidate_request, Mapping)
                and int(candidate_request.get("open_intent_id") or 0) == open_id
            )
            same_attempt = bool(
                attempt_id
                and str(candidate_evidence.get("manual_attempt_id") or "")
                == attempt_id
            )
            if same_open or same_attempt:
                closed = candidate
                break

        opened_at = _utc_out(opened.get("submitted_at") or opened.get("created_at"))
        closed_at = (
            _utc_out(closed.get("submitted_at") or closed.get("created_at"))
            if closed is not None
            else None
        )
        position = current_positions.get((symbol, position_side))
        status = "closed" if closed is not None else "open" if position else "reconciling"
        basis = opened.get("entry_basis_json")
        basis = dict(basis) if isinstance(basis, Mapping) else {}
        execution = basis.get("execution")
        execution = dict(execution) if isinstance(execution, Mapping) else {}
        response_json = opened.get("response_json")
        response_json = (
            dict(response_json) if isinstance(response_json, Mapping) else {}
        )
        close_response = closed.get("response_json") if closed is not None else {}
        close_response = (
            dict(close_response) if isinstance(close_response, Mapping) else {}
        )

        realized = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")
        income_available = history_status in {"available", "partial"} and bool(
            opened_at is not None
            and (history_start_at is None or opened_at >= history_start_at)
        )
        if opened_at is not None and income_available:
            start_ms = int(opened_at.timestamp() * 1_000) - 5_000
            end_ms = int((closed_at or generated_at).timestamp() * 1_000) + 5_000
            for income in income_records:
                if str(getattr(income, "symbol", "") or "").upper() != symbol:
                    continue
                income_time = int(getattr(income, "time_ms", 0) or 0)
                if not start_ms <= income_time <= end_ms:
                    continue
                amount = Decimal(str(getattr(income, "income", 0) or 0))
                income_type = str(getattr(income, "income_type", "") or "").upper()
                if income_type == "REALIZED_PNL":
                    realized += amount
                elif income_type == "COMMISSION":
                    commission += amount
                elif income_type == "FUNDING_FEE":
                    funding += amount

        unrealized_value = (
            _number_or_none(position.get("upnl")) if position is not None else 0.0
        )
        unrealized = Decimal(str(unrealized_value or 0))
        net = realized + commission + funding + unrealized
        entry_price = _number_or_none(execution.get("entry_price"))
        if entry_price is None:
            entry_price = _number_or_none(
                response_json.get("avgPrice") or response_json.get("price")
            )
        exit_price = _number_or_none(
            close_response.get("avgPrice") or close_response.get("price")
        )
        records.append(
            {
                "id": str(opened.get("public_id") or open_id),
                "manual_attempt_id": attempt_id or None,
                "symbol": symbol,
                "direction": "short" if position_side == "SHORT" else "long",
                "position_side": position_side,
                "status": status,
                "quantity": _number_or_none(opened.get("quantity")),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "mark_price": (
                    _number_or_none(position.get("mark_price"))
                    if position is not None
                    else None
                ),
                "opened_at": opened_at,
                "closed_at": closed_at,
                "close_reason": _manual_close_reason(closed),
                "pnl_status": history_status if income_available else "unavailable",
                "realized_pnl": float(realized) if income_available else None,
                "commission": float(commission) if income_available else None,
                "funding": float(funding) if income_available else None,
                "unrealized_pnl": float(unrealized) if position is not None else 0.0,
                "net_pnl": float(net) if income_available else None,
            }
        )

    records.reverse()
    available_records = [item for item in records if item["net_pnl"] is not None]
    closed_records = [item for item in records if item["status"] == "closed"]
    closed_available = [item for item in closed_records if item["net_pnl"] is not None]
    wins = sum(1 for item in closed_available if float(item["net_pnl"]) > 0)
    losses = sum(1 for item in closed_available if float(item["net_pnl"]) < 0)
    realized_pnl = sum(float(item["realized_pnl"] or 0) for item in available_records)
    commission = sum(float(item["commission"] or 0) for item in available_records)
    funding = sum(float(item["funding"] or 0) for item in available_records)
    unrealized_pnl = sum(
        float(item["unrealized_pnl"] or 0)
        for item in records
        if item["status"] == "open"
    )
    net_pnl = sum(float(item["net_pnl"] or 0) for item in available_records)
    return {
        "generated_at": generated_at,
        "history_status": history_status,
        "history_error": history_error,
        "summary": {
            "total": len(records),
            "open_count": sum(1 for item in records if item["status"] == "open"),
            "closed_count": len(closed_records),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (
                round(wins / len(closed_available) * 100, 2)
                if closed_available
                else None
            ),
            "realized_pnl": round(realized_pnl, 8),
            "commission": round(commission, 8),
            "funding": round(funding, 8),
            "unrealized_pnl": round(unrealized_pnl, 8),
            "net_pnl": round(net_pnl, 8),
        },
        "records": records,
    }


def _live_copy_out(
    db: Session,
    request: Request,
    user: User,
) -> dict[str, Any]:
    account = _ai_monitor_live_account(db, user.id)
    unresolved_order_count = 0
    selected: dict[str, Any]
    if account is not None:
        unresolved_order_count = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(
                    LiveOrderIntent.user_id == user.id,
                    LiveOrderIntent.live_account_id == account.id,
                    LiveOrderIntent.status == "unknown",
                )
            )
            or 0
        )
        selected = _live_copy_account_out(
            account,
            unresolved_order_count=unresolved_order_count,
        )
    else:
        preview = _ai_monitor_live_config(enabled=False)
        selected = {
            "id": None,
            "name": _AI_MONITOR_LIVE_ACCOUNT_NAME,
            "status": "ready",
            "configured": False,
            "enabled_at": None,
            "last_tick_at": None,
            "last_error_code": None,
            "unresolved_order_count": 0,
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
            "independent": True,
            "provisioned": False,
            "risk": {
                "position_mode": str(preview["position_mode"]),
                "leverage": int(preview["leverage"]),
                "max_positions": int(preview["max_positions"]),
                "position_size_basis": str(preview["position_size_basis"]),
                "copy_total_amount": float(preview["copy_total_amount"]),
                "position_size_pct": float(preview["position_size_pct"]),
                "risk_per_trade_pct": float(preview["risk_per_trade_pct"]),
                "max_total_risk_pct": float(preview["max_total_risk_pct"]),
                "margin_cap_pct": round(float(preview["margin_cap"]) * 100, 4),
                "daily_loss_limit_pct": float(preview["daily_loss_limit_pct"]),
                "max_drawdown_pct": float(preview["max_drawdown_pct"]),
                "round_trip_cost_bps": float(preview["round_trip_cost_bps"]),
                "signal_max_age_seconds": int(
                    preview["ai_monitor_live_signal_max_age_seconds"]
                ),
                "minimum_combined_score": float(
                    preview["ai_monitor_live_min_combined_score"]
                ),
                "regular_session_only": bool(
                    preview["ai_monitor_live_regular_session_only"]
                ),
                "allow_long": bool(preview["ai_monitor_live_allow_long"]),
                "allow_short": bool(preview["ai_monitor_live_allow_short"]),
            },
        }
    strategy_readiness = _strategy_live_readiness(db, user.id)
    credentials_configured = bool(user.binance_credentials_configured)
    trade_permission_requested = _binance_trade_permission_requested(user)
    blockers: list[str] = []
    if not credentials_configured:
        blockers.append("尚未配置 Binance API 凭据")
    if not trade_permission_requested:
        blockers.append("Binance API 未申请 TRADE 权限")
    if not tradfi_symbols():
        blockers.append("Binance TradFi 交易品种池为空")
    if unresolved_order_count > 0:
        blockers.append("存在状态未知的 Binance 订单，需先完成对账")
    if not bool(strategy_readiness.get("quantitative_ready")):
        blockers.append(
            "当前止盈止损策略尚未通过成本后量化准入，禁止新增真实资金订单"
        )
    requested_enabled = bool(selected["configured"])
    enabled = bool(
        requested_enabled
        and selected["status"] == "active"
    )
    return {
        "enabled": enabled,
        "requested_enabled": requested_enabled,
        "server_enabled": True,
        "ordinary_live_server_enabled": bool(
            request.app.state.settings.binance_live_trading_enabled
        ),
        "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        "independent_execution": True,
        "confirmation_name": _AI_MONITOR_LIVE_ACCOUNT_NAME,
        "credentials_configured": credentials_configured,
        "trade_permission_requested": trade_permission_requested,
        "ready_to_enable": not blockers,
        "blockers": blockers,
        "strategy_readiness": {
            "settlement_policy_version": ai_monitor.PREDICTION_SETTLEMENT_VERSION,
            "quantitative_ready": bool(
                strategy_readiness.get("quantitative_ready")
            ),
            "passed_count": int(strategy_readiness.get("passed_count") or 0),
            "total_count": int(strategy_readiness.get("total_count") or 0),
        },
        "account": selected,
        "accounts": [selected],
        "signal_policy": {
            "source": "ai_monitor",
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
            "ordinary_strategy_switch_independent": True,
            "new_signals_only": True,
            "require_entry_ready": True,
            "minimum_combined_score": selected["risk"]["minimum_combined_score"],
            "maximum_signal_age_seconds": selected["risk"]["signal_max_age_seconds"],
            "allow_long": selected["risk"]["allow_long"],
            "allow_short": selected["risk"]["allow_short"],
            "existing_positions_on_disable": "keep_protected_and_manage_exits",
            "idempotent_orders": True,
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
    *,
    recompute_current_gate: bool = False,
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

    if recompute_current_gate:
        # Current opportunities may contain a gate snapshot written before
        # Binance became the primary execution venue.  Re-apply the active
        # policy to the latest stored market inputs so delayed/closed US cash
        # references remain observations in record/score mode instead of
        # incorrectly blocking a fresh Binance contract quote.  Frozen
        # prediction/history responses deliberately skip this branch.
        market_quality = dict(evidence.get("market_quality") or {})
        signal_policy = dict(evidence.get("unusual_whales_policy") or {})
        policy_mode = str(
            signal_policy.get("effective_mode")
            or market_quality.get("policy_mode")
            or gate_summary.get("policy_mode")
            or "record"
        ).strip().lower()
        if market_quality:
            gate_summary = ai_monitor.stable_gate_summary(
                market_quality,
                market_flow,
                evaluated_at=datetime.now(UTC),
                policy_mode=policy_mode,
            )
            version["decision"] = gate_summary.get(
                "decision_version", ai_monitor.OPPORTUNITY_DECISION_VERSION
            )

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


def _compact_opportunity_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return the card payload without repeating heavyweight scan internals.

    Opportunity evidence keeps up to 96 score snapshots.  Older snapshots may
    contain the complete quote/order-book feature document, which made the
    20-card endpoint return tens of megabytes.  Cards and their trend dialog
    only consume the scalar score and depth series below; detail endpoints keep
    the original database evidence unchanged.
    """

    compact = dict(evidence)
    raw_history = evidence.get("score_history")
    if not isinstance(raw_history, list):
        compact["score_history"] = []
        return compact

    scalar_keys = (
        "calculated_at",
        "news",
        "technical",
        "market_flow",
        "option_flow",
        "gex",
        "institutional",
        "institutional_flow",
        "macro",
        "macro_context",
        "combined",
        "score",
        "main_force_ratio",
        "active_buy_ratio",
        "book_imbalance",
        "book_imbalance_5",
        "bid_depth_notional",
        "ask_depth_notional",
        "bid_depth_notional_5",
        "ask_depth_notional_5",
        "bid_depth_change_30s_pct",
        "ask_depth_change_30s_pct",
        "data_quality",
    )
    component_keys = (
        "news",
        "technical",
        "market_flow",
        "option_flow",
        "gex",
        "institutional",
        "macro",
    )
    flow_keys = (
        "score",
        "main_force_ratio",
        "active_buy_ratio",
        "book_imbalance",
        "book_imbalance_5",
        "bid_depth_notional",
        "ask_depth_notional",
        "bid_depth_notional_5",
        "ask_depth_notional_5",
        "bid_depth_change_30s_pct",
        "ask_depth_change_30s_pct",
        "data_quality",
    )
    history: list[dict[str, Any]] = []
    for raw_point in raw_history[-96:]:
        if not isinstance(raw_point, Mapping):
            continue
        point = {key: raw_point[key] for key in scalar_keys if key in raw_point}
        components = raw_point.get("components")
        if isinstance(components, Mapping):
            point["components"] = {
                key: components[key] for key in component_keys if key in components
            }
        flow = raw_point.get("market_flow_snapshot")
        if isinstance(flow, Mapping):
            point["market_flow_snapshot"] = {
                key: flow[key] for key in flow_keys if key in flow
            }
        history.append(point)
    compact["score_history"] = history
    return compact


def _opportunity_out(
    item: AiMonitorOpportunity,
    prediction: AiMonitorPrediction | None = None,
    live_market: dict[str, Any] | None = None,
    current_market_flow: Mapping[str, Any] | None = None,
    market_snapshot: OpportunityMarketSnapshot | None = None,
    spot_quote: dict[str, Any] | None = None,
    enhanced_market: Mapping[str, Any] | None = None,
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
        recompute_current_gate=not use_frozen,
    )
    live_flow = dict(current_market_flow or {})
    if live_flow and not use_frozen:
        captured_at = live_flow.get("captured_at")
        try:
            captured_at = int(captured_at) if captured_at is not None else 0
        except (TypeError, ValueError):
            captured_at = 0
        live_flow["observed_at"] = (
            _utc_out(datetime.fromtimestamp(captured_at, tz=UTC))
            if captured_at > 0
            else None
        )
        # ``flow`` is the display contract for current opportunities.  Keep
        # signal-time enhanced domains, but let live Binance depth and taker
        # inputs replace stale/null scalar fields.  The immutable evidence and
        # historical market snapshot are never rewritten.
        stable_contract["flow"] = {
            **dict(stable_contract.get("flow") or {}),
            **live_flow,
        }
        stable_contract["current_market_flow"] = live_flow
        stable_contract["flow_display_source"] = (
            "binance_live"
            if live_flow.get("fresh")
            else "signal_snapshot"
        )
    else:
        stable_contract["current_market_flow"] = None
        stable_contract["flow_display_source"] = "signal_snapshot"
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
    news_trigger = (
        dict(contract_evidence.get("news_trigger") or {})
        if isinstance(contract_evidence.get("news_trigger"), Mapping)
        else {}
    )
    evidence_news = (
        list(contract_evidence.get("news") or [])
        if isinstance(contract_evidence.get("news"), list)
        else []
    )
    related_news_ids = {
        str(news_id)
        for news_id in (item.news_ids_json or [])
        if str(news_id)
    }
    related_news_ids.update(
        str(news_item.get("id"))
        for news_item in evidence_news
        if isinstance(news_item, Mapping) and str(news_item.get("id") or "")
    )
    new_news_ids = {
        str(news_id)
        for news_id in (news_trigger.get("new_news_ids") or [])
        if str(news_id)
    }
    price_comparison = _price_comparison_out(
        live_market,
        spot_quote,
        enhanced_market,
        direction=item.direction,
        news_score=news_score,
        news_count=len(related_news_ids),
        new_news_count=len(new_news_ids),
        reused_news_count=int(news_trigger.get("reused_news_count") or 0),
        memory_window_hours=int(news_trigger.get("memory_window_hours") or 168),
    )
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
        "evidence": _compact_opportunity_evidence(contract_evidence),
        "prediction_id": prediction.public_id if prediction is not None else None,
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
        "finnhub_spot_quote": dict(spot_quote or {}),
        "binance_contract_quote": dict(price_comparison["sources"]["binance"]),
        "price_comparison": price_comparison,
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
        "exit_subreason": item.exit_subreason,
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
        "peak_favorable_bps_at_exit": (
            float(item.peak_favorable_bps_at_exit)
            if item.peak_favorable_bps_at_exit is not None
            else None
        ),
        "protected_bps_at_exit": (
            float(item.protected_bps_at_exit)
            if item.protected_bps_at_exit is not None
            else None
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
            ).where(
                AiMonitorPrediction.user_id == user.id,
                AiMonitorPrediction.settlement_version
                == ai_monitor.PREDICTION_SETTLEMENT_VERSION,
            )
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
        # Flush response headers immediately so browsers can complete the SSE
        # handshake before the first database revision snapshot is calculated.
        yield ": connected\n\n"
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
    background_tasks: BackgroundTasks,
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the cached US macro regime used by opportunity scoring."""

    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    snapshot = request.app.state.macro_market_service.snapshot(repository)
    fingerprint = macro_ai.macro_analysis_fingerprint(snapshot)
    previous = getattr(request.app.state, "macro_ai_scheduled_fingerprint", None)
    previous_at = float(getattr(request.app.state, "macro_ai_scheduled_at", 0.0) or 0.0)
    monotonic_now = time.monotonic()
    if fingerprint != previous or monotonic_now - previous_at >= 6 * 60 * 60:
        request.app.state.macro_ai_scheduled_fingerprint = fingerprint
        request.app.state.macro_ai_scheduled_at = monotonic_now
        background_tasks.add_task(
            macro_ai.refresh_macro_analysis,
            request.app.state.database_engine,
            request.app.state.settings.credential_master_key.get_secret_value(),
            snapshot,
        )
    return snapshot


@router.get("/market-context/ai-analysis")
def market_context_ai_analysis(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    snapshot = request.app.state.macro_market_service.snapshot(repository)
    return macro_ai.macro_analysis_state(db, snapshot)


@router.post("/market-context/ai-analysis/refresh")
async def refresh_market_context_ai_analysis(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    force: bool = Query(default=False),
) -> dict[str, Any]:
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    snapshot = request.app.state.macro_market_service.snapshot(repository)
    return await run_in_threadpool(
        macro_ai.refresh_macro_analysis,
        request.app.state.database_engine,
        request.app.state.settings.credential_master_key.get_secret_value(),
        snapshot,
        force=bool(force and user.is_admin),
    )


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


@router.get("/live-copy")
def get_live_copy_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return a secret-free, fail-closed view of AI-monitor live following."""

    return _live_copy_out(db, request, user)


@router.get("/live-copy/history")
def get_live_copy_history(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Return manual-follow executions with PnL from Binance income history."""

    response.headers["Cache-Control"] = "private, no-store"
    generated_at = datetime.now(UTC)
    account = _ai_monitor_live_account(db, user.id)
    if account is None:
        return _manual_follow_history_out(
            (),
            generated_at=generated_at,
            history_status="not_configured",
            limit=limit,
        )
    rows = list(
        db.scalars(
            select(LiveOrderIntent)
            .where(
                LiveOrderIntent.user_id == user.id,
                LiveOrderIntent.live_account_id == account.id,
                LiveOrderIntent.action.in_(("open", "close")),
                LiveOrderIntent.status == "filled",
            )
            .order_by(LiveOrderIntent.id.desc())
            .limit(2_000)
        )
    )
    intents = [
        {
            "id": item.id,
            "public_id": item.public_id,
            "symbol": item.symbol,
            "action": item.action,
            "side": item.side,
            "position_side": item.position_side,
            "quantity": item.quantity,
            "signal_key": item.signal_key,
            "request_json": dict(item.request_json or {}),
            "entry_basis_json": dict(item.entry_basis_json or {}),
            "response_json": dict(item.response_json or {}),
            "submitted_at": item.submitted_at,
            "created_at": item.created_at,
        }
        for item in rows
    ]
    manual_open_times = [
        _utc_out(item["submitted_at"] or item["created_at"])
        for item in intents
        if item["action"] == "open"
        and _manual_follow_evidence(item["entry_basis_json"]).get("manual_follow") is True
    ]
    manual_open_times = [item for item in manual_open_times if item is not None]
    if not manual_open_times:
        return _manual_follow_history_out(
            intents,
            generated_at=generated_at,
            history_status="available",
            limit=limit,
        )

    encrypted_key = user.binance_api_key_encrypted
    encrypted_secret = user.binance_api_secret_encrypted
    credentials_configured = bool(user.binance_credentials_configured)
    # Do not retain a MySQL transaction while waiting for Binance.
    db.rollback()
    if not credentials_configured:
        return _manual_follow_history_out(
            intents,
            generated_at=generated_at,
            history_status="not_configured",
            limit=limit,
        )

    cipher = CredentialCipher(
        request.app.state.settings.credential_master_key.get_secret_value()
    )
    try:
        api_key = cipher.decrypt(encrypted_key or "")
        api_secret = cipher.decrypt(encrypted_secret or "")
    except SecurityError:
        return _manual_follow_history_out(
            intents,
            generated_at=generated_at,
            history_status="request_failed",
            history_error="credential_error",
            limit=limit,
        )

    service = request.app.state.binance_service
    try:
        snapshot = service.account(api_key, api_secret)
        earliest = min(manual_open_times)
        retention_start = generated_at - timedelta(days=30)
        start_at = max(earliest - timedelta(minutes=1), retention_start)
        history = service.income_history(
            api_key,
            api_secret,
            account_type=snapshot.account_type,
            start_time_ms=int(start_at.timestamp() * 1_000),
            end_time_ms=int(generated_at.timestamp() * 1_000),
        )
    except BinanceAccountClientError as exc:
        return _manual_follow_history_out(
            intents,
            generated_at=generated_at,
            history_status="request_failed",
            history_error=exc.category,
            limit=limit,
        )

    history_status = (
        "available"
        if history.complete and min(manual_open_times) >= retention_start
        else "partial"
    )
    return _manual_follow_history_out(
        intents,
        positions=snapshot.positions,
        income_records=history.records,
        generated_at=generated_at,
        history_status=history_status,
        history_start_at=start_at,
        limit=limit,
    )


@router.post("/live-copy/manual-follow")
def manual_follow_live_copy(
    payload: AiMonitorManualFollowRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Execute one explicitly confirmed opportunity through the live risk engine."""

    _require_expected_user(request, user)
    if not payload.acknowledge_real_funds:
        raise HTTPException(status_code=409, detail="请确认本操作会使用真实资金")
    account = _ai_monitor_live_account(db, user.id)
    if account is None or account.public_id != payload.account_id:
        raise HTTPException(status_code=409, detail="实盘跟单账户已变化，请刷新后重试")
    account_config = dict(account.config_json or {})
    if (
        account.status != "active"
        or not bool(account_config.get("ai_monitor_live_copy_enabled"))
    ):
        raise HTTPException(status_code=409, detail="请先开启独立实盘跟单")
    _require_strategy_live_readiness(db, user.id)

    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.user_id == user.id,
            AiMonitorOpportunity.public_id == payload.opportunity_id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="机会已更新，请刷新后重试")
    prediction = None
    if payload.prediction_id is not None:
        prediction = db.scalar(
            select(AiMonitorPrediction).where(
                AiMonitorPrediction.user_id == user.id,
                AiMonitorPrediction.opportunity_id == opportunity.id,
                AiMonitorPrediction.public_id == payload.prediction_id,
            )
        )
        if prediction is None:
            raise HTTPException(status_code=404, detail="预测已更新，请刷新后重试")
    actual_symbol = str(
        prediction.contract_symbol if prediction is not None else opportunity.contract_symbol
    ).upper()
    actual_direction = str(
        prediction.direction if prediction is not None else opportunity.direction
    ).lower()
    if (
        actual_symbol != payload.expected_contract_symbol
        or actual_direction != payload.expected_direction
    ):
        raise HTTPException(status_code=409, detail="信号方向或合约已变化，请刷新后重试")

    request_id = payload.manual_attempt_id
    _audit(
        db,
        request,
        user.id,
        "ai_monitor.live_copy.manual_follow_requested",
        request_id,
        {
            "account_id": account.public_id,
            "opportunity_id": opportunity.public_id,
            "prediction_id": prediction.public_id if prediction is not None else None,
            "manual_attempt_id": payload.manual_attempt_id,
            "contract_symbol": actual_symbol,
            "direction": actual_direction,
            "acknowledge_real_funds": True,
        },
    )
    # The request audit must be durable before any exchange-side write begins.
    db.commit()

    try:
        result = live_engine.execute_ai_monitor_manual_follow(
            user_id=user.id,
            live_account_id=account.id,
            opportunity_public_id=opportunity.public_id,
            prediction_public_id=(prediction.public_id if prediction is not None else None),
            manual_attempt_id=payload.manual_attempt_id,
            expected_symbol=actual_symbol,
            expected_direction=actual_direction,
            selected_at=(
                prediction.predicted_at if prediction is not None else opportunity.discovered_at
            ),
            selected_evidence=(
                dict(prediction.evidence_json or {})
                if prediction is not None
                else dict(opportunity.evidence_json or {})
            ),
            selected_score=float(
                prediction.confidence_score
                if prediction is not None
                else opportunity.combined_score
            ),
        )
    except (BinanceAccountClientError, SecurityError, RuntimeError) as exc:
        result = {
            "status": "blocked",
            "reason": "engine_unavailable",
            "symbol": actual_symbol,
            "direction": actual_direction,
            "intent": None,
            "error_category": type(exc).__name__,
        }

    reason = str(result.get("reason") or "execution_failed_closed")
    message = _MANUAL_FOLLOW_MESSAGES.get(
        reason,
        "手动跟单未完成，执行器没有继续重复提交。",
    )
    audit_warning = False
    try:
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.live_copy.manual_follow_result",
            request_id,
            {
                "account_id": account.public_id,
                "opportunity_id": opportunity.public_id,
                "prediction_id": prediction.public_id if prediction is not None else None,
                "manual_attempt_id": payload.manual_attempt_id,
                "contract_symbol": actual_symbol,
                "direction": actual_direction,
                "status": result.get("status"),
                "reason": reason,
                "intent_id": (result.get("intent") or {}).get("id"),
            },
        )
        db.commit()
    except SQLAlchemyError:
        # Never turn a confirmed exchange result into an HTTP failure merely
        # because this secondary API audit row could not be persisted.  The
        # live_order_intent remains the authoritative execution ledger.
        db.rollback()
        audit_warning = True
        message += " 操作审计暂未写入，请立即核对订单记录。"
    return {
        "request_id": request_id,
        "message": message,
        "audit_warning": audit_warning,
        **result,
    }


@router.put("/live-copy/config")
def update_live_copy_config(
    payload: AiMonitorLiveCopyConfigUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Persist risk settings for the isolated opportunity execution account.

    Saving does not enable a disabled account.  If an already-enabled account was
    stopped solely because its Binance position mode did not match, explicitly
    saving the corrected mode re-arms that same isolated execution domain.
    """

    _require_expected_user(request, user)
    locked_user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    account, deployment = _ensure_ai_monitor_live_account(db, locked_user)
    if payload.account_id and payload.account_id != account.public_id:
        raise HTTPException(status_code=409, detail="实盘跟单账户已变化，请刷新后重试")

    previous_config = dict(account.config_json or {})
    requested_enabled = bool(previous_config.get("ai_monitor_live_copy_enabled"))
    account_config = {
        **_ai_monitor_live_config(enabled=requested_enabled),
        **previous_config,
        "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        "signal_source": _AI_MONITOR_LIVE_SCOPE,
        "position_mode": payload.position_mode,
        "leverage": payload.leverage,
        "risk_max_leverage": payload.leverage,
        "max_positions": payload.max_positions,
        "position_size_basis": payload.position_size_basis,
        "copy_total_amount": payload.copy_total_amount,
        "position_size_pct": payload.position_size_pct,
        "max_margin_per_trade_pct": payload.position_size_pct,
        "risk_per_trade_pct": payload.risk_per_trade_pct,
        "max_total_risk_pct": payload.max_total_risk_pct,
        "margin_cap": payload.margin_cap_pct / 100.0,
        "daily_loss_limit_pct": payload.daily_loss_limit_pct,
        "max_drawdown_pct": payload.max_drawdown_pct,
        "round_trip_cost_bps": payload.round_trip_cost_bps,
        "max_signal_age_seconds": payload.signal_max_age_seconds,
        "ai_monitor_live_signal_max_age_seconds": payload.signal_max_age_seconds,
        "ai_monitor_live_min_combined_score": payload.minimum_combined_score,
        "ai_monitor_live_allow_long": payload.allow_long,
        "ai_monitor_live_allow_short": payload.allow_short,
    }
    account.config_json = account_config
    account.credential_version = locked_user.binance_key_version
    risk_keys = (
        "leverage",
        "risk_max_leverage",
        "max_positions",
        "position_size_basis",
        "copy_total_amount",
        "position_size_pct",
        "max_margin_per_trade_pct",
        "margin_cap",
        "risk_per_trade_pct",
        "max_total_risk_pct",
        "daily_loss_limit_pct",
        "max_drawdown_pct",
        "round_trip_cost_bps",
        "max_ticker_age_seconds",
        "max_signal_age_seconds",
        "block_high_risk_products",
    )
    deployment.risk_override_json = {
        key: account_config[key] for key in risk_keys if key in account_config
    }
    deployment.runtime_state_json = {
        **dict(deployment.runtime_state_json or {}),
        "execution_scope": _AI_MONITOR_LIVE_SCOPE,
        "position_mode": payload.position_mode,
        "position_size_basis": payload.position_size_basis,
        "copy_total_amount": payload.copy_total_amount,
    }

    resumed_after_mode_fix = bool(
        requested_enabled and account.last_error_code == "position_mode_changed"
    )
    if resumed_after_mode_fix:
        _require_strategy_live_readiness(db, user.id)
        account.status = "active"
        account.last_error_code = None
        account.armed_at = utcnow()
        deployment.status = "running"
        deployment.last_error_code = None
        deployment.started_at = deployment.started_at or utcnow()

    _audit(
        db,
        request,
        user.id,
        "ai_monitor.live_copy.config_update",
        account.public_id,
        {
            "account_id": account.public_id,
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
            "position_mode": payload.position_mode,
            "leverage": payload.leverage,
            "max_positions": payload.max_positions,
            "position_size_basis": payload.position_size_basis,
            "copy_total_amount": payload.copy_total_amount,
            "minimum_combined_score": payload.minimum_combined_score,
            "allow_long": payload.allow_long,
            "allow_short": payload.allow_short,
            "resumed_after_position_mode_fix": resumed_after_mode_fix,
            "enabled_state_changed": False,
        },
    )
    db.commit()
    db.refresh(locked_user)
    live_engine.start()
    return _live_copy_out(db, request, locked_user)


@router.put("/live-copy")
def update_live_copy_status(
    payload: AiMonitorLiveCopyUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Control the dedicated AI-monitor live execution domain.

    The dedicated account and deployment are infrastructure adapters only; they
    never evaluate or inherit the user's ordinary live strategies.  The endpoint
    itself never sends an exchange order.  It only arms the isolated AI signal
    source consumed by the live executor, whose loss limits, idempotency, fill
    verification and exchange-native stop/target protections remain authoritative.
    """

    _require_expected_user(request, user)
    locked_user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked_user is None:
        raise HTTPException(status_code=404, detail="user not found")

    if payload.enabled:
        if not locked_user.binance_credentials_configured:
            raise HTTPException(status_code=409, detail="请先配置 Binance API 凭据")
        if not _binance_trade_permission_requested(locked_user):
            raise HTTPException(status_code=409, detail="Binance API 未申请 TRADE 权限")
        if not tradfi_symbols():
            raise HTTPException(status_code=503, detail="Binance TradFi 交易品种池为空")
        _require_strategy_live_readiness(db, user.id)
        account, deployment = _ensure_ai_monitor_live_account(db, locked_user)
        unresolved = db.scalar(
            select(func.count(LiveOrderIntent.id)).where(
                LiveOrderIntent.user_id == user.id,
                LiveOrderIntent.live_account_id == account.id,
                LiveOrderIntent.status == "unknown",
            )
        )
        if int(unresolved or 0):
            raise HTTPException(
                status_code=409,
                detail="存在状态未知的 Binance 订单，请先完成对账",
            )
        legacy_accounts = db.scalars(
            select(LiveTradingAccount)
            .where(
                LiveTradingAccount.user_id == user.id,
                LiveTradingAccount.status != "archived",
                LiveTradingAccount.id != account.id,
            )
            .with_for_update()
        ).all()
        for candidate in legacy_accounts:
            candidate_config = dict(candidate.config_json or {})
            candidate_config["ai_monitor_live_copy_enabled"] = False
            if candidate_config.get("signal_source") == _AI_MONITOR_LIVE_SCOPE:
                candidate_config["signal_source"] = "strategy"
            candidate.config_json = candidate_config
        account_config = {
            **_ai_monitor_live_config(enabled=True),
            **dict(account.config_json or {}),
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
            "signal_source": _AI_MONITOR_LIVE_SCOPE,
            "ai_monitor_live_copy_enabled": True,
            "ai_monitor_live_copy_enabled_at": utcnow().isoformat(),
        }
        account.config_json = account_config
        account.status = "active"
        account.credential_version = locked_user.binance_key_version
        account.armed_at = utcnow()
        account.last_error_code = None
        deployment.status = "running"
        deployment.started_at = deployment.started_at or utcnow()
        deployment.last_error_code = None
        action = "ai_monitor.live_copy.enable"
        resource_id = account.public_id
        metadata = {
            "account_id": account.public_id,
            "execution_scope": _AI_MONITOR_LIVE_SCOPE,
            "ordinary_strategy_switch_independent": True,
            "new_signals_only": True,
            "maximum_signal_age_seconds": 300,
            "minimum_combined_score": 70.0,
            "existing_risk_controls_preserved": True,
        }
    else:
        account = _ai_monitor_live_account(db, user.id, for_update=True)
        if account is not None:
            account_config = dict(account.config_json or {})
            account_config.update(
                {
                    "execution_scope": _AI_MONITOR_LIVE_SCOPE,
                    "signal_source": _AI_MONITOR_LIVE_SCOPE,
                    "ai_monitor_live_copy_enabled": False,
                    "ai_monitor_live_copy_disabled_at": utcnow().isoformat(),
                }
            )
            account.config_json = account_config
        action = "ai_monitor.live_copy.disable"
        resource_id = account.public_id if account is not None else "none"
        metadata = {
            "account_id": account.public_id if account is not None else None,
            "new_entries_stopped": True,
            "existing_positions_closed": False,
            "existing_protections_preserved": True,
        }

    _audit(db, request, user.id, action, resource_id, metadata)
    db.commit()
    db.refresh(locked_user)
    live_engine.start()
    return _live_copy_out(db, request, locked_user)


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
                "enabled",
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


@router.put("/unusual-whales-enabled")
def update_unusual_whales_usage(
    payload: AiMonitorUnusualWhalesUsageUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    """Enable or bypass Unusual Whales collection, scoring and hard gates."""

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

    previous_enabled = bool(complete_config.get("enabled", True))
    complete_config["enabled"] = bool(payload.enabled)
    setting.value_json = complete_config
    _audit(
        db,
        request,
        admin.id,
        "ai_monitor.unusual_whales.toggle",
        ai_monitor.UNUSUAL_WHALES_SIGNAL_SETTING_KEY,
        {
            "previous_enabled": previous_enabled,
            "enabled": bool(payload.enabled),
            "refresh_interval_seconds": 5 * 60,
            "published_version": int(setting.version),
        },
    )
    db.commit()
    db.refresh(setting)

    runtime_warning = None
    apply_runtime_config = getattr(
        request.app.state,
        "apply_unusual_whales_runtime_config",
        None,
    )
    if callable(apply_runtime_config):
        try:
            apply_runtime_config(complete_config)
        except (RuntimeError, ValueError) as exc:
            runtime_warning = type(exc).__name__
    result = _score_policy_out(db, can_edit=True)
    if runtime_warning:
        result["runtime_warning"] = runtime_warning
    return result


@router.put("/finnhub-enabled")
def update_finnhub_usage(
    payload: AiMonitorFinnhubUsageUpdate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    """Enable or pause platform-wide Finnhub US cash-equity collection."""

    _require_expected_user(request, admin)
    setting = db.scalar(
        select(AdminSetting)
        .where(AdminSetting.key == FINNHUB_USAGE_SETTING_KEY)
        .with_for_update()
    )
    if setting is None:
        previous_enabled = True
        setting = AdminSetting(
            key=FINNHUB_USAGE_SETTING_KEY,
            value_json={"enabled": bool(payload.enabled), "market_open_only": True},
            version=1,
            updated_by=admin.id,
        )
        db.add(setting)
    else:
        raw = setting.value_json
        if raw is not None and not isinstance(raw, Mapping):
            raise HTTPException(status_code=409, detail="当前 Finnhub 平台配置损坏")
        config = dict(raw or {})
        previous_enabled = bool(config.get("enabled", True))
        config.update({"enabled": bool(payload.enabled), "market_open_only": True})
        setting.value_json = config
        setting.version = max(0, int(setting.version or 0)) + 1
        setting.updated_by = admin.id
        setting.updated_at = utcnow()
    _audit(
        db,
        request,
        admin.id,
        "ai_monitor.finnhub.toggle",
        FINNHUB_USAGE_SETTING_KEY,
        {
            "previous_enabled": previous_enabled,
            "enabled": bool(payload.enabled),
            "market_open_only": True,
            "published_version": int(setting.version),
        },
    )
    db.commit()
    db.refresh(setting)

    runtime_warning = None
    apply_runtime_config = getattr(request.app.state, "apply_finnhub_runtime_config", None)
    if callable(apply_runtime_config):
        try:
            apply_runtime_config(dict(setting.value_json or {}))
        except (RuntimeError, ValueError) as exc:
            runtime_warning = type(exc).__name__
    result = _score_policy_out(db, can_edit=True)
    if runtime_warning:
        result["runtime_warning"] = runtime_warning
    return result


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


def _current_opportunity_projection_page(
    db: Session,
    *,
    user_id: int,
    limit: int,
    page: int,
    now: datetime,
) -> dict[str, Any] | None:
    """Return a compact, pre-paged current view when migration 0059 is active.

    An empty projection falls back to the source query while active source rows
    still exist. This makes the API safe during the migration/backfill window.
    """

    if not read_models_available(db):
        return None
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
            return None

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
        item.prediction_id for item in projections if item.prediction_id is not None
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


@router.get("/opportunities")
def opportunities(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=300),
    page: int = Query(default=1, ge=1),
    scope: Literal["legacy", "current", "history"] = Query(default="legacy"),
    include_expired: bool = False,
) -> dict[str, Any]:
    now = utcnow()
    projection_page = (
        _current_opportunity_projection_page(
            db,
            user_id=user.id,
            limit=limit,
            page=page,
            now=now,
        )
        if scope == "current"
        else None
    )
    if projection_page is not None:
        rows = list(projection_page["rows"])
    elif scope == "history":
        statement = (
            select(AiMonitorOpportunity, AiMonitorPrediction)
            .join(
                AiMonitorPrediction,
                AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
            )
            .where(
                AiMonitorOpportunity.user_id == user.id,
                AiMonitorPrediction.user_id == user.id,
                AiMonitorPrediction.status.in_(("pending", "unavailable")),
                AiMonitorPrediction.settlement_version
                == ai_monitor.PREDICTION_SETTLEMENT_VERSION,
            )
        )
    elif projection_page is None:
        statement = (
            select(AiMonitorOpportunity, AiMonitorPrediction)
            .outerjoin(
                AiMonitorPrediction,
                AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id,
            )
            .where(AiMonitorOpportunity.user_id == user.id)
        )
        if scope == "current" or not include_expired:
            statement = statement.where(
                AiMonitorOpportunity.status.in_(("candidate", "discovered")),
                AiMonitorOpportunity.expires_at > now,
            )
    if projection_page is None:
        statement = statement.order_by(
            AiMonitorOpportunity.discovered_at.desc(),
            AiMonitorOpportunity.id.desc(),
        )
        if scope == "legacy":
            rows = list(
                db.execute(
                    statement.limit(300 if not include_expired else limit)
                ).all()
            )
        else:
            rows = list(db.execute(statement).all())

    if scope == "current" and projection_page is None:
        unique_rows: dict[str, tuple[AiMonitorOpportunity, AiMonitorPrediction | None]] = {}
        for opportunity, prediction in rows:
            if prediction is not None and prediction.status == "completed":
                continue
            instrument = (opportunity.contract_symbol or opportunity.symbol).strip().upper()
            unique_rows.setdefault(instrument, (opportunity, prediction))
        rows = list(unique_rows.values())
    elif scope == "legacy" and not include_expired:
        unique_rows = {}
        for opportunity, prediction in rows:
            instrument = (opportunity.contract_symbol or opportunity.symbol).strip().upper()
            unique_rows.setdefault(instrument, (opportunity, prediction))
        rows = list(unique_rows.values())[:limit]

    direction_counts = (
        dict(projection_page["direction_counts"])
        if projection_page is not None
        else {
            "long": sum(opportunity.direction == "long" for opportunity, _ in rows),
            "short": sum(opportunity.direction == "short" for opportunity, _ in rows),
        }
    )
    settlement_counts = (
        dict(projection_page["settlement_counts"])
        if projection_page is not None
        else {
            "total": len(rows),
            "pending": sum(
                prediction is not None and prediction.status == "pending"
                for _opportunity, prediction in rows
            ),
            "unavailable": sum(
                prediction is not None and prediction.status == "unavailable"
                for _opportunity, prediction in rows
            ),
        }
    )
    if scope == "legacy":
        page_rows = rows
        current_page = 1
        total_pages = 1
    elif projection_page is not None:
        page_rows = rows
        current_page = int(projection_page["pagination"]["page"])
        total_pages = int(projection_page["pagination"]["total_pages"])
    else:
        total_pages = max(1, (len(rows) + limit - 1) // limit)
        current_page = min(page, total_pages)
        page_start = (current_page - 1) * limit
        page_rows = rows[page_start : page_start + limit]
    items = [opportunity for opportunity, _prediction in page_rows]
    prediction_by_opportunity_id = {
        prediction.opportunity_id: prediction
        for _opportunity, prediction in page_rows
        if prediction is not None
    }
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
    current_market_flows: dict[int, dict[str, Any]] = {}
    finnhub_spot_quotes: dict[str, dict[str, Any]] = {}
    enhanced_market_features: dict[str, RealtimeMarketFeatureSnapshot] = {}
    if items:
        try:
            repository = MonitorRepository(
                request.app.state.database_engine,
                request.app.state.settings.monitor_symbols_config,
            )
            live_tickers = repository.latest_tickers(
                [item.contract_symbol for item in items]
            )
            market_flow_inputs = ai_monitor._market_flow_input_maps(db, repository)
            market_flow_now = datetime.now(UTC)
            current_market_flows = {
                item.id: ai_monitor.market_flow_snapshot(
                    market_flow_inputs,
                    symbol=item.symbol,
                    contract_symbol=item.contract_symbol,
                    direction=item.direction,
                    now=market_flow_now,
                )
                for item in items
            }
        except MonitorUnavailable:
            live_tickers = {}
            current_market_flows = {}
        quote_service = getattr(request.app.state, "finnhub_us_quote_service", None)
        if isinstance(quote_service, FinnhubUsQuoteService) and quote_service.enabled:
            finnhub_spot_quotes = quote_service.latest_many(item.symbol for item in items)
        enhanced_market_features = ai_monitor.latest_realtime_feature_snapshots(
            db,
            [item.symbol for item in items],
        )
    response_now = now
    response = {
        "items": [
            _opportunity_out(
                item,
                prediction_by_opportunity_id.get(item.id),
                live_tickers.get((item.contract_symbol or "").upper()),
                current_market_flows.get(item.id),
                snapshot_by_opportunity_id.get(item.id),
                finnhub_spot_quotes.get(
                    FinnhubUsQuoteService.normalize_symbol(item.symbol)
                ),
                ai_monitor.realtime_feature_payload(
                    enhanced_market_features.get(item.symbol.strip().upper())
                ),
                # The frontend requests active and historical rows together so
                # it can switch tabs without a second round-trip.  The query's
                # ``include_expired`` flag must not freeze active candidates;
                # only genuinely inactive/expired rows use their immutable
                # signal-time market snapshot.
                use_frozen=bool(
                    (include_expired or scope == "history")
                    and (
                        item.status not in {"candidate", "discovered"}
                        or item.expires_at <= response_now
                    )
                ),
            )
            for item in items
        ],
        "direction_counts": direction_counts,
        "settlement_counts": settlement_counts,
    }
    if scope != "legacy":
        response["pagination"] = (
            dict(projection_page["pagination"])
            if projection_page is not None
            else {
                "page": current_page,
                "page_size": limit,
                "total": len(rows),
                "total_pages": total_pages,
                "has_previous": current_page > 1,
                "has_next": current_page < total_pages,
            }
        )
    response["query_mode"] = (
        "current_read_model" if projection_page is not None else "source_fallback"
    )
    return response


@router.get("/opportunities/{opportunity_id}/order-book")
def opportunity_order_book(
    opportunity_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100),
) -> dict[str, Any]:
    """Expose the synchronized Binance Futures book already held in memory."""

    if limit not in {20, 50, 100}:
        raise HTTPException(status_code=422, detail="limit must be 20, 50, or 100")
    opportunity = db.scalar(
        select(AiMonitorOpportunity).where(
            AiMonitorOpportunity.public_id == opportunity_id,
            AiMonitorOpportunity.user_id == user.id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    contract_symbol = str(opportunity.contract_symbol or "").strip().upper()
    if not contract_symbol:
        raise HTTPException(status_code=409, detail="opportunity has no Binance contract mapping")
    snapshot = ws_depth.live_order_book_snapshot(contract_symbol, limit)
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="Binance live order book is synchronizing; retry shortly",
        )
    return {
        **snapshot,
        "opportunity_id": str(opportunity.public_id),
        "equity_symbol": str(opportunity.symbol or "").strip().upper(),
        "contract_symbol": contract_symbol,
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
        .where(
            AiMonitorPrediction.user_id == user.id,
            AiMonitorPrediction.settlement_version
            == ai_monitor.PREDICTION_SETTLEMENT_VERSION,
        )
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
    settlement_version: str = Query(default="current", max_length=32),
    direction: Literal["all", "long", "short"] = Query(default="all"),
    market_session: Literal[
        "all", "premarket", "regular", "postmarket", "closed", "unknown"
    ] = Query(default="all"),
    quote_quality: Literal[
        "all", "passed", "partial", "blocked", "missing"
    ] = Query(
        default="all"
    ),
    event_risk: Literal["all", "clear", "warning", "blocked"] = Query(default="all"),
    exit_reason: str = Query(default="all", max_length=64),
    include_readiness: bool = Query(default=False),
    include_ablation: bool = Query(default=False),
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
    if not include_ablation:
        projected = ai_monitor.historical_opportunity_fact_analytics(
            db,
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
            settlement_version=settlement_version,
            direction=direction,
            market_session=market_session,
            quote_quality=quote_quality,
            event_risk=event_risk,
            exit_reason=exit_reason,
            include_readiness=include_readiness,
        )
        if projected is not None:
            return projected
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
        settlement_version=settlement_version,
        direction=direction,
        market_session=market_session,
        quote_quality=quote_quality,
        event_risk=event_risk,
        exit_reason=exit_reason,
        include_readiness=include_readiness,
        include_ablation=include_ablation,
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
