"""Application orchestration for deterministic opportunity generation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
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
