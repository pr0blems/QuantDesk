"""Persistence adapter for AI Monitor event visibility and legacy backfill."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ...application.ai_monitor.event_gate import (
    market_risk_event_gate_snapshot,
    market_risk_event_snapshot,
    utc_event_iso,
)
from ...models import AiMonitorPrediction, MarketRiskEvent, utcnow

MARKET_RISK_EVENT_LOOKAHEAD_HOURS = 48


def market_risk_event_contexts(
    db: Session,
    *,
    now: datetime,
    symbols: Sequence[str],
    blocking_before_minutes: int | None = None,
    blocking_after_minutes: int | None = None,
    lookahead_hours: int = MARKET_RISK_EVENT_LOOKAHEAD_HOURS,
) -> dict[str, list[dict[str, Any]]]:
    """Load visible event context while preserving the smaller block window."""

    normalized = {
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    }
    normalized_now = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
    maximum_after_seconds = max(
        max(0, int(blocking_after_minutes or 0)) * 60,
        2 * 60 * 60,
    )
    rows = db.scalars(
        select(MarketRiskEvent)
        .where(
            MarketRiskEvent.status.in_(("scheduled", "active")),
            or_(
                MarketRiskEvent.symbol.is_(None),
                MarketRiskEvent.symbol.in_(normalized),
            ),
            or_(
                MarketRiskEvent.status == "active",
                and_(
                    MarketRiskEvent.scheduled_at
                    >= normalized_now - timedelta(seconds=maximum_after_seconds),
                    MarketRiskEvent.scheduled_at
                    <= normalized_now
                    + timedelta(hours=max(1, int(lookahead_hours))),
                ),
            ),
        )
        .order_by(MarketRiskEvent.scheduled_at, MarketRiskEvent.id)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {"*": []}
    for row in rows:
        item = market_risk_event_snapshot(
            row,
            now=normalized_now,
            blocking_before_minutes=blocking_before_minutes,
            blocking_after_minutes=blocking_after_minutes,
        )
        result.setdefault((row.symbol or "*").strip().upper(), []).append(item)
    return result


def active_market_risk_events(
    db: Session,
    *,
    now: datetime,
    symbols: Sequence[str],
    blocking_before_minutes: int | None = None,
    blocking_after_minutes: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return only high-impact events whose blocking window is active now."""

    contexts = market_risk_event_contexts(
        db,
        now=now,
        symbols=symbols,
        blocking_before_minutes=blocking_before_minutes,
        blocking_after_minutes=blocking_after_minutes,
    )
    return {
        key: [item for item in rows if bool(item.get("blocking_active"))]
        for key, rows in contexts.items()
    }


def backfill_prediction_event_contexts(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
    lookback_days: int = 14,
    limit: int = 1000,
) -> dict[str, int]:
    """Repair legacy prediction snapshots that omitted scheduled-event context."""

    updated_at = now or utcnow()
    predictions = list(
        db.scalars(
            select(AiMonitorPrediction)
            .where(
                AiMonitorPrediction.user_id == user_id,
                AiMonitorPrediction.predicted_at
                >= updated_at - timedelta(days=max(1, int(lookback_days))),
            )
            .order_by(
                AiMonitorPrediction.predicted_at.desc(),
                AiMonitorPrediction.id.desc(),
            )
            .limit(max(1, int(limit)))
        ).all()
    )
    missing = [
        prediction
        for prediction in predictions
        if "event_gate" not in dict(prediction.evidence_json or {})
    ]
    if not missing:
        return {"scanned": len(predictions), "updated": 0}
    first_signal = min(item.predicted_at for item in missing)
    last_signal = max(item.predicted_at for item in missing)
    events = list(
        db.scalars(
            select(MarketRiskEvent)
            .where(
                MarketRiskEvent.status != "cancelled",
                MarketRiskEvent.scheduled_at >= first_signal - timedelta(hours=2),
                MarketRiskEvent.scheduled_at
                <= last_signal
                + timedelta(hours=MARKET_RISK_EVENT_LOOKAHEAD_HOURS),
            )
            .order_by(MarketRiskEvent.scheduled_at, MarketRiskEvent.id)
        ).all()
    )
    updated = 0
    for prediction in missing:
        signal_at = prediction.predicted_at
        symbol = str(prediction.symbol or "").strip().upper()
        nearby: list[dict[str, Any]] = []
        for event in events:
            event_symbol = str(event.symbol or "").strip().upper()
            if event_symbol and event_symbol != symbol:
                continue
            anchor = event.actual_at or event.scheduled_at
            delta = anchor - signal_at
            if not (
                -timedelta(hours=2)
                <= delta
                <= timedelta(hours=MARKET_RISK_EVENT_LOOKAHEAD_HOURS)
            ):
                continue
            nearby.append(market_risk_event_snapshot(event, now=signal_at))
        nearby.sort(
            key=lambda item: (
                abs(float(item.get("minutes_until_event") or 0.0)),
                str(item.get("scheduled_at") or ""),
            )
        )
        evidence = dict(prediction.evidence_json or {})
        evidence["risk_events"] = nearby
        evidence["blocking_risk_events"] = [
            item for item in nearby if bool(item.get("blocking_active"))
        ]
        evidence["event_gate"] = market_risk_event_gate_snapshot(nearby)
        evidence["event_context_backfill"] = {
            "version": "event_visibility_v2",
            "backfilled_at": utc_event_iso(updated_at),
        }
        prediction.evidence_json = evidence
        prediction.updated_at = updated_at
        updated += 1
    if updated:
        db.flush()
    return {"scanned": len(predictions), "updated": updated}
