"""Versioned market-feature access boundary for AI Monitor."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


class MarketFeatureService:
    def __init__(
        self,
        *,
        latest: Callable[..., dict[str, Any]],
        normalize: Callable[[Any], dict[str, Any]],
        version: str,
    ) -> None:
        self._latest = latest
        self._normalize = normalize
        self._version = version

    def latest(self, db: Any, symbols: Sequence[str]) -> AiMonitorStageResult:
        rows = self._latest(db, symbols)
        return AiMonitorStageResult(
            stage="market_features",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={"items": rows},
        )

    def normalize(self, value: Any) -> dict[str, Any]:
        return self._normalize(value)
