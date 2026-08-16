"""Normalize vendor calendars into auditable signal-risk windows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MarketRiskEvent, utcnow

_CRITICAL_MARKERS = (
    "fomc",
    "fed interest rate",
    "interest rate decision",
    "consumer price index",
    " cpi",
    "nonfarm",
    "non-farm",
    "payroll",
    "personal consumption expenditure",
    " pce",
)
_HIGH_MARKERS = (
    "gdp",
    "jobless claims",
    "unemployment",
    "retail sales",
    "producer price",
    "ppi",
    "powell",
)


def economic_event_risk_level(event_name: str, event_type: str = "") -> str:
    """Classify only well-known market-moving events as entry blockers."""

    normalized = f" {event_type} {event_name}".casefold()
    if any(marker in normalized for marker in _CRITICAL_MARKERS):
        return "critical"
    if any(marker in normalized for marker in _HIGH_MARKERS):
        return "high"
    return "medium"


def _event_identity(event: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "event": str(event.get("event") or "").strip(),
            "event_time_ms": int(event.get("event_time_ms") or 0),
            "type": str(event.get("type") or "").strip().lower(),
            "reported_period": str(event.get("reported_period") or "").strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _status_for(
    scheduled_at: datetime,
    now: datetime,
    *,
    before_seconds: int,
    after_seconds: int,
) -> str:
    if scheduled_at - timedelta(seconds=before_seconds) <= now <= scheduled_at + timedelta(
        seconds=after_seconds
    ):
        return "active"
    if now > scheduled_at + timedelta(seconds=after_seconds):
        return "completed"
    return "scheduled"


def sync_economic_calendar(
    db: Session,
    payload: Mapping[str, Any],
    *,
    provider: str = "unusual_whales",
    block_before_minutes: int = 30,
    block_after_minutes: int = 15,
    now: datetime | None = None,
) -> dict[str, int]:
    """Idempotently upsert an official economic calendar response.

    Medium-impact events remain visible for audit but are not hard blockers; the
    signal gate only consumes high/critical rows. The caller owns the transaction.
    """

    evaluated_at = (now or utcnow()).replace(tzinfo=None)
    before_seconds = max(0, int(block_before_minutes)) * 60
    after_seconds = max(0, int(block_after_minutes)) * 60
    rows = payload.get("events")
    events = rows if isinstance(rows, list) else []
    created = 0
    updated = 0
    skipped = 0
    for raw in events:
        if not isinstance(raw, Mapping):
            skipped += 1
            continue
        name = str(raw.get("event") or "").strip()
        try:
            event_time_ms = int(raw.get("event_time_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            event_time_ms = 0
        if not name or event_time_ms <= 0:
            skipped += 1
            continue
        scheduled_at = datetime.fromtimestamp(event_time_ms / 1_000, UTC).replace(
            tzinfo=None
        )
        event_type = str(raw.get("type") or "economic").strip().lower()[:32]
        dedup_key = _event_identity(raw)
        row = db.scalar(
            select(MarketRiskEvent).where(
                MarketRiskEvent.provider == provider,
                MarketRiskEvent.dedup_key == dedup_key,
            )
        )
        values = {
            "provider_event_id": dedup_key[:32],
            "event_type": event_type or "economic",
            "event_name": name[:191],
            "scheduled_at": scheduled_at,
            "risk_level": economic_event_risk_level(name, event_type),
            "blocking_before_seconds": before_seconds,
            "blocking_after_seconds": after_seconds,
            "status": _status_for(
                scheduled_at,
                evaluated_at,
                before_seconds=before_seconds,
                after_seconds=after_seconds,
            ),
            "source_payload_json": {
                key: raw.get(key)
                for key in (
                    "event",
                    "event_time_ms",
                    "time",
                    "type",
                    "forecast",
                    "previous",
                    "reported_period",
                )
            },
            "source_updated_at": evaluated_at,
        }
        if row is None:
            db.add(MarketRiskEvent(provider=provider, dedup_key=dedup_key, **values))
            created += 1
        else:
            for key, value in values.items():
                setattr(row, key, value)
            updated += 1

    # Move expired rows forward even if the vendor response no longer includes
    # yesterday's event. Never delete historical event evidence.
    pending = db.scalars(
        select(MarketRiskEvent).where(
            MarketRiskEvent.provider == provider,
            MarketRiskEvent.status.in_(("scheduled", "active")),
        )
    ).all()
    for row in pending:
        next_status = _status_for(
            row.scheduled_at,
            evaluated_at,
            before_seconds=row.blocking_before_seconds,
            after_seconds=row.blocking_after_seconds,
        )
        if row.status != next_status:
            row.status = next_status
            row.source_updated_at = evaluated_at
    return {"created": created, "updated": updated, "skipped": skipped}
