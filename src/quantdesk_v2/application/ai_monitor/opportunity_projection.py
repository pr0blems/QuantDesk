"""Projection-only application boundary for the active opportunity list."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class OpportunityProjectionError(RuntimeError):
    """Base error for a read model that cannot safely answer a query."""


class OpportunityProjectionUnavailable(OpportunityProjectionError):
    """The projection schema is not available on this deployment."""


class OpportunityProjectionLagging(OpportunityProjectionError):
    """Source facts exist but the active projection has not caught up."""


class OpportunityProjectionReader(Protocol):
    """Framework-neutral port implemented by a persistence adapter."""

    def current_page(
        self,
        *,
        user_id: int,
        limit: int,
        page: int,
        now: datetime,
    ) -> dict[str, Any]: ...


class OpportunityProjectionService:
    """Serve current opportunities exclusively through the projection port."""

    def __init__(self, reader: OpportunityProjectionReader) -> None:
        self._reader = reader

    def current_page(
        self,
        *,
        user_id: int,
        limit: int,
        page: int,
        now: datetime,
    ) -> dict[str, Any]:
        return self._reader.current_page(
            user_id=user_id,
            limit=limit,
            page=page,
            now=now,
        )
