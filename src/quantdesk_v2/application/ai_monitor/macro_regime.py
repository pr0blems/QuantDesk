"""Macro regime boundary used by opportunity admission."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


class MacroRegimeService:
    def __init__(
        self,
        *,
        snapshot: Callable[..., dict[str, Any]],
        context: Callable[..., dict[str, Any]],
        version: str = "macro_regime_v1",
    ) -> None:
        self._snapshot = snapshot
        self._context = context
        self._version = version

    def snapshot(self, repository: Any, *, now: datetime) -> AiMonitorStageResult:
        return AiMonitorStageResult(
            stage="macro_regime",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload=self._snapshot(repository, now=now),
        )

    def context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._context(*args, **kwargs)
