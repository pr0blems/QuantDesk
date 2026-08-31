"""Stable contracts shared by AI Monitor domain services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AiMonitorAuthority(StrEnum):
    """Identify whether output may influence deterministic execution."""

    DETERMINISTIC = "deterministic"
    AI_ADVISORY = "ai_advisory"


@dataclass(frozen=True, slots=True)
class AiMonitorStageResult:
    stage: str
    authority: AiMonitorAuthority
    version: str
    payload: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "authority": self.authority.value,
            "version": self.version,
            "payload": self.payload,
        }
