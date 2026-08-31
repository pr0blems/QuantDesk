"""Deterministic news scoring boundary; model text is evidence, not authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


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
