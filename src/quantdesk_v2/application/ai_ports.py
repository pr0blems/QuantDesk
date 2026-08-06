"""Application ports for AI decision audit storage."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.ai_decision import DecisionLedgerEvent, DecisionLedgerRecord


@runtime_checkable
class DecisionLedger(Protocol):
    """Append-only persistence boundary for AI decision events."""

    def append(self, event: DecisionLedgerEvent) -> DecisionLedgerRecord: ...

    def records(
        self,
        *,
        decision_run_id: str | None = None,
        actor_scope_id: str | None = None,
    ) -> tuple[DecisionLedgerRecord, ...]: ...

