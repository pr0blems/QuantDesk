"""Application orchestration for deterministic opportunity generation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult
from .news_scoring import news_event_bursts

ScanOpportunities = Callable[
    [Any, Any, Any, Mapping[str, Any], Path],
    dict[str, Any],
]
RefreshProjection = Callable[..., dict[str, Any]]


def filter_monitored_candidates(
    candidates: Sequence[dict[str, Any]], monitor_symbols: Sequence[str]
) -> list[dict[str, Any]]:
    """Apply the user's contract-symbol allowlist; an empty list means all."""

    allowed = {
        str(symbol).strip().upper()
        for symbol in monitor_symbols
        if str(symbol).strip()
    }
    if not allowed:
        return [candidate for candidate in candidates if candidate.get("contract_symbol")]
    return [
        candidate
        for candidate in candidates
        if str(candidate.get("contract_symbol") or "").upper() in allowed
    ]


def annotate_event_cluster_selection(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Allow one strongest candidate per shared news event and direction."""

    claimed: dict[tuple[str, str], str] = {}
    for candidate in sorted(
        candidates,
        key=lambda item: float(item.get("news_score") or 0),
        reverse=True,
    ):
        trigger = dict(candidate.get("news_trigger") or {})
        direction = str(candidate.get("direction") or "long")
        symbol = str(candidate.get("symbol") or "")
        news_ids = sorted(
            str(item)
            for item in trigger.get("actionable_new_news_ids") or []
            if str(item)
        )
        owners = sorted(
            {
                claimed[(direction, news_id)]
                for news_id in news_ids
                if (direction, news_id) in claimed
            }
        )
        selected = not owners
        if selected:
            for news_id in news_ids:
                claimed[(direction, news_id)] = symbol
        cluster_seed = f"{direction}|{','.join(news_ids)}"
        if not news_ids:
            cluster_seed = f"{direction}:{symbol}:no-new-event"
        trigger["event_cluster"] = {
            "version": "shared_news_event_v1",
            "cluster_id": hashlib.sha256(cluster_seed.encode("utf-8")).hexdigest()[:16],
            "selected": selected,
            "selected_symbol": symbol if selected else owners[0],
            "shared_news_ids": news_ids,
            "reason_code": None if selected else "CORRELATED_EVENT_ALREADY_SELECTED",
        }
        candidate["news_trigger"] = trigger
    return list(candidates)


def fresh_candidate_news_ids(
    candidate_news_ids: Sequence[str] | set[str],
    *,
    direction: str,
    consumed_by_direction: Mapping[str, set[str]],
    news_items: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Return fresh event news, suppressing follow-ups from a consumed wire burst."""

    normalized = {
        str(item).strip() for item in candidate_news_ids if str(item).strip()
    }
    consumed = {
        str(item).strip()
        for item in consumed_by_direction.get(str(direction), set())
        if str(item).strip()
    }
    for burst in news_event_bursts(list(news_items or [])):
        burst_ids = {
            str(item.get("id") or "").strip()
            for item in burst
            if str(item.get("id") or "").strip()
        }
        if burst_ids.intersection(consumed):
            consumed.update(burst_ids)
    return sorted(normalized - consumed)


def prepare_candidate_news_triggers(
    candidates: Sequence[dict[str, Any]],
    *,
    consumed_by_direction: Mapping[str, set[str]],
    active_candidate_keys: set[tuple[str, str]],
    require_new_news: bool,
    memory_window_hours: int,
    trigger_window_hours: int,
    now: datetime,
    actionability: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Freeze deterministic fresh-news admission evidence for each candidate."""

    eligible_candidates: list[dict[str, Any]] = []
    reused_news_skipped = 0
    for candidate in candidates:
        news_items = list(candidate.get("news", []))
        candidate_news_ids = {
            str(item.get("id") or "") for item in news_items
        } - {""}
        key = (str(candidate["symbol"]), str(candidate["direction"]))
        new_news_ids = fresh_candidate_news_ids(
            candidate_news_ids,
            direction=str(candidate["direction"]),
            consumed_by_direction=consumed_by_direction,
            news_items=news_items,
        )
        actionability_by_id = {
            str(item.get("id") or ""): dict(actionability(item))
            for item in news_items
            if str(item.get("id") or "") in new_news_ids
        }
        actionable_new_news_ids = sorted(
            news_id
            for news_id, snapshot in actionability_by_id.items()
            if bool(snapshot.get("actionable"))
        )
        non_actionable_news_ids = sorted(
            news_id
            for news_id, snapshot in actionability_by_id.items()
            if not bool(snapshot.get("actionable"))
        )
        newest_news_ts = max(
            (int(item.get("ts") or 0) for item in news_items),
            default=0,
        )
        candidate["news_trigger"] = {
            "version": "fresh_actionable_news_v2",
            "required": require_new_news,
            "memory_window_hours": memory_window_hours,
            "trigger_window_hours": trigger_window_hours,
            "has_new_news": bool(new_news_ids),
            "new_news_ids": new_news_ids,
            "has_actionable_new_news": bool(actionable_new_news_ids),
            "actionable_new_news_ids": actionable_new_news_ids,
            "non_actionable_news_ids": non_actionable_news_ids,
            "non_actionable_reasons": sorted(
                {
                    str(snapshot.get("reason_code"))
                    for snapshot in actionability_by_id.values()
                    if snapshot.get("reason_code")
                }
            ),
            "actionability": actionability_by_id,
            "reused_news_count": len(candidate_news_ids) - len(new_news_ids),
            "newest_news_age_minutes": (
                round(
                    (int(now.replace(tzinfo=UTC).timestamp()) - newest_news_ts) / 60,
                    2,
                )
                if newest_news_ts
                else None
            ),
        }
        if require_new_news and not new_news_ids and key not in active_candidate_keys:
            reused_news_skipped += 1
            continue
        eligible_candidates.append(candidate)
    return eligible_candidates, reused_news_skipped


def strongest_candidate_per_symbol(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one directional signal per instrument, choosing its strongest news side."""

    strongest: dict[str, dict[str, Any]] = {}
    strengths: dict[str, tuple[float, int, int]] = {}
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        key = str(
            candidate.get("contract_symbol") or candidate.get("symbol") or ""
        ).upper()
        if not key:
            continue
        news = list(candidate.get("news") or [])
        latest_news = max((int(item.get("ts") or 0) for item in news), default=0)
        strength = (
            float(candidate.get("news_score") or 0),
            len(news),
            latest_news,
        )
        if key not in strengths or strength > strengths[key]:
            strongest[key] = candidate
            strengths[key] = strength
    return sorted(
        strongest.values(),
        key=lambda item: float(item.get("news_score") or 0),
        reverse=True,
    )


class OpportunityGenerationService:
    """Run the deterministic scanner, then atomically refresh its projection."""

    def __init__(
        self,
        *,
        scan: ScanOpportunities,
        refresh_projection: RefreshProjection,
        version: str,
    ) -> None:
        self._scan = scan
        self._refresh_projection = refresh_projection
        self._version = version

    def execute(
        self,
        db: Any,
        engine: Any,
        run: Any,
        config: Mapping[str, Any],
        symbols_config: Path,
    ) -> AiMonitorStageResult:
        summary = self._scan(db, engine, run, config, symbols_config)
        summary["read_models"] = self._refresh_projection(
            db,
            user_id=run.user_id,
            prediction_limit=1000,
            score_limit=5000,
        )
        return AiMonitorStageResult(
            stage="opportunity_generation",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload=summary,
        )
