"""Deterministic event and actionability gate boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


def utc_event_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def market_risk_event_snapshot(
    row: Any,
    *,
    now: datetime,
    blocking_before_minutes: int | None = None,
    blocking_after_minutes: int | None = None,
) -> dict[str, Any]:
    """Freeze one event's visibility and block timing at a signal timestamp."""

    normalized_now = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
    anchor = row.actual_at or row.scheduled_at
    before_seconds = (
        max(0, int(blocking_before_minutes)) * 60
        if blocking_before_minutes is not None
        else max(0, row.blocking_before_seconds)
    )
    after_seconds = (
        max(0, int(blocking_after_minutes)) * 60
        if blocking_after_minutes is not None
        else max(0, row.blocking_after_seconds)
    )
    starts_at = anchor - timedelta(seconds=before_seconds)
    ends_at = anchor + timedelta(seconds=after_seconds)
    blocking_active = bool(
        row.risk_level in {"high", "critical"}
        and (row.status == "active" or starts_at <= normalized_now <= ends_at)
    )
    minutes_until_event = round(
        (anchor - normalized_now).total_seconds() / 60,
        2,
    )
    return {
        "id": row.public_id,
        "event_type": row.event_type,
        "event_name": row.event_name,
        "title": row.event_name,
        "symbol": row.symbol,
        "risk_level": row.risk_level,
        "status": row.status,
        "scheduled_at": utc_event_iso(row.scheduled_at),
        "actual_at": utc_event_iso(row.actual_at),
        "blocking_starts_at": utc_event_iso(starts_at),
        "blocking_ends_at": utc_event_iso(ends_at),
        "minutes_until_event": minutes_until_event,
        "blocking_active": blocking_active,
        "proximity": (
            "blocking"
            if blocking_active
            else "upcoming"
            if minutes_until_event >= 0
            else "recent"
        ),
        "provider": row.provider,
    }


def market_risk_event_gate_snapshot(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize nearby-event visibility without promoting it to a hard gate."""

    normalized = [dict(item) for item in events if isinstance(item, Mapping)]
    normalized.sort(
        key=lambda item: (
            abs(float(item.get("minutes_until_event") or 0.0)),
            str(item.get("scheduled_at") or ""),
        )
    )
    blocking = [item for item in normalized if bool(item.get("blocking_active"))]
    nearest = (blocking or normalized or [{}])[0]
    if blocking:
        status = "blocked"
        risk_level = str(nearest.get("risk_level") or "high")
    elif normalized:
        status = "warning"
        risk_level = "warning"
    else:
        status = "clear"
        risk_level = "normal"
    return {
        "version": "event_visibility_v2",
        "status": status,
        "risk_level": risk_level,
        "blocking": bool(blocking),
        "event_count": len(normalized),
        "blocking_event_count": len(blocking),
        "nearest_event": nearest or None,
        "event_name": nearest.get("event_name") if nearest else None,
        "scheduled_at": nearest.get("scheduled_at") if nearest else None,
        "minutes_until_event": nearest.get("minutes_until_event") if nearest else None,
    }


class EventGateService:
    def __init__(
        self,
        *,
        event_gate: Callable[[Sequence[dict[str, Any]]], dict[str, Any]],
        stable_gate: Callable[..., dict[str, Any]],
        version: str,
    ) -> None:
        self._event_gate = event_gate
        self._stable_gate = stable_gate
        self._version = version

    def event_gate(self, events: Sequence[dict[str, Any]]) -> AiMonitorStageResult:
        return AiMonitorStageResult(
            stage="event_gate",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload=self._event_gate(events),
        )

    def stable_gate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._stable_gate(*args, **kwargs)
