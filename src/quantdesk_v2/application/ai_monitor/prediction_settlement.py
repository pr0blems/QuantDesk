"""Application orchestration for deterministic prediction settlement."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


class PredictionSettlementService:
    """Settle due facts and refresh projections without AI authority."""

    def __init__(
        self,
        *,
        settle: Callable[..., dict[str, Any]],
        reopen_legacy: Callable[[Any], int],
        refresh_projection: Callable[..., dict[str, Any]],
        version: str,
    ) -> None:
        self._settle = settle
        self._reopen_legacy = reopen_legacy
        self._refresh_projection = refresh_projection
        self._version = version

    def execute_cycle(self, db: Any, repository: Any) -> AiMonitorStageResult:
        reopened = self._reopen_legacy(db)
        settlement = self._settle(db, repository)
        projection: dict[str, Any] | None = None
        if settlement.get("completed") or settlement.get("unavailable") or reopened:
            projection = self._refresh_projection(
                db,
                prediction_limit=1000,
                score_limit=5000,
            )
        return AiMonitorStageResult(
            stage="prediction_settlement",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={
                "reopened_legacy": reopened,
                "settlement": settlement,
                "read_models": projection,
            },
        )
