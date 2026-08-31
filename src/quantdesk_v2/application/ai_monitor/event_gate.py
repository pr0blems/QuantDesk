"""Deterministic event and actionability gate boundary."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


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
