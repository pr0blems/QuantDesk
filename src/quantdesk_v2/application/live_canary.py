"""Framework-neutral contracts for live Canary acceptance windows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LIVE_WORKER_STALE_SECONDS = 30
LIVE_TICK_STALE_SECONDS = 90
MINIMUM_CANARY_WINDOW_SECONDS = 15 * 60


class LiveCanaryError(RuntimeError):
    """A stable safety or lifecycle validation error."""


@dataclass(frozen=True, slots=True)
class LiveCanaryObservation:
    passed: bool
    failure_codes: tuple[str, ...]
    metrics: dict[str, Any]
