"""Application orchestration for deterministic opportunity generation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult

ScanOpportunities = Callable[
    [Any, Any, Any, Mapping[str, Any], Path],
    dict[str, Any],
]
RefreshProjection = Callable[..., dict[str, Any]]


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
