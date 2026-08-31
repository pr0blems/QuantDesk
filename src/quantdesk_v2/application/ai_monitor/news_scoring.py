"""Deterministic news scoring boundary; model text is evidence, not authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult

NEWS_EVENT_BURST_SECONDS = 2 * 60
NEWS_EVENT_BURST_MAX_SPAN_SECONDS = 10 * 60


def news_event_bursts(
    news_items: Sequence[Mapping[str, Any]],
    *,
    maximum_gap_seconds: int = NEWS_EVENT_BURST_SECONDS,
    maximum_span_seconds: int = NEWS_EVENT_BURST_MAX_SPAN_SECONDS,
) -> list[list[Mapping[str, Any]]]:
    """Cluster same-source/category wire updates without mutating source records."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, item in enumerate(news_items):
        source = str(item.get("source") or "").strip().casefold()
        category = str(item.get("category") or "").strip().casefold()
        item_id = str(item.get("id") or index).strip()
        group_key = f"{source}|{category}" if source else f"id:{item_id}"
        grouped.setdefault(group_key, []).append(item)

    bursts: list[list[Mapping[str, Any]]] = []
    for items in grouped.values():
        ordered = sorted(
            items,
            key=lambda item: (int(item.get("ts") or 0), str(item.get("id") or "")),
        )
        current: list[Mapping[str, Any]] = []
        previous_ts = 0
        first_ts = 0
        for item in ordered:
            item_ts = int(item.get("ts") or 0)
            if current and (
                previous_ts <= 0
                or item_ts <= 0
                or item_ts - previous_ts > max(0, int(maximum_gap_seconds))
                or item_ts - first_ts > max(0, int(maximum_span_seconds))
            ):
                bursts.append(current)
                current = []
                first_ts = 0
            if not current:
                first_ts = item_ts
            current.append(item)
            previous_ts = item_ts
        if current:
            bursts.append(current)
    return bursts


class NewsScoringService:
    def __init__(
        self,
        *,
        aggregate: Callable[..., list[dict[str, Any]]],
        select_directional: Callable[..., list[dict[str, Any]]],
        version: str,
    ) -> None:
        self._aggregate = aggregate
        self._select_directional = select_directional
        self._version = version

    def aggregate(
        self,
        news_rows: Sequence[Any],
        symbol_map: Mapping[str, str],
        *,
        minimum_confidence: float,
        minimum_mentions: int,
    ) -> AiMonitorStageResult:
        candidates = self._aggregate(
            news_rows,
            symbol_map,
            minimum_confidence=minimum_confidence,
            minimum_mentions=minimum_mentions,
        )
        return AiMonitorStageResult(
            stage="news_scoring",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={"candidates": candidates},
        )

    def select_directional(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._select_directional(*args, **kwargs)
