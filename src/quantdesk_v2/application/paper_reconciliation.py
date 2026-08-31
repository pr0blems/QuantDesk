"""Application orchestration for restart-safe paper execution projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PaperProjectionOutcome:
    execution_id: int
    state: str

    def __post_init__(self) -> None:
        if self.execution_id <= 0:
            raise ValueError("execution_id must be positive")
        if self.state not in {"applied", "already_applied"}:
            raise ValueError("projection state is invalid")


@dataclass(frozen=True, slots=True)
class PaperReconciliationResult:
    user_id: int
    paper_account_id: int
    discovered: int
    applied: int
    already_applied: int
    failed: int
    remaining: int
    drift_codes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.failed == 0 and self.remaining == 0 and not self.drift_codes


class PaperProjectionStore(Protocol):
    def pending_execution_ids(
        self, *, user_id: int, paper_account_id: int, limit: int
    ) -> tuple[int, ...]: ...

    def project_execution(
        self, *, user_id: int, paper_account_id: int, execution_id: int
    ) -> PaperProjectionOutcome: ...

    def record_failure(
        self,
        *,
        user_id: int,
        paper_account_id: int,
        execution_id: int,
        error: str,
    ) -> None: ...

    def audit_account(
        self, *, user_id: int, paper_account_id: int
    ) -> tuple[int, tuple[str, ...], tuple[str, ...]]: ...

    def record_reconciliation(
        self,
        *,
        user_id: int,
        paper_account_id: int,
        pending_count: int,
        drift_codes: tuple[str, ...],
        warning_codes: tuple[str, ...],
        errors: tuple[str, ...],
        ready: bool,
    ) -> None: ...


class PaperExecutionReconciliationService:
    """Replay pending fills and refuse new work while projections are unhealthy."""

    __slots__ = ("_store",)

    def __init__(self, store: PaperProjectionStore) -> None:
        self._store = store

    def reconcile_account(
        self, *, user_id: int, paper_account_id: int, limit: int = 100
    ) -> PaperReconciliationResult:
        if user_id <= 0 or paper_account_id <= 0:
            raise ValueError("paper reconciliation scope must be positive")
        if not 1 <= limit <= 1_000:
            raise ValueError("paper reconciliation limit must be between 1 and 1000")
        pending = self._store.pending_execution_ids(
            user_id=user_id,
            paper_account_id=paper_account_id,
            limit=limit,
        )
        applied = 0
        already_applied = 0
        errors: list[str] = []
        for execution_id in pending:
            try:
                outcome = self._store.project_execution(
                    user_id=user_id,
                    paper_account_id=paper_account_id,
                    execution_id=execution_id,
                )
            except Exception as exc:
                message = f"execution:{execution_id}:{type(exc).__name__}:{exc}"
                errors.append(message[:500])
                self._store.record_failure(
                    user_id=user_id,
                    paper_account_id=paper_account_id,
                    execution_id=execution_id,
                    error=message,
                )
                continue
            if outcome.state == "applied":
                applied += 1
            else:
                already_applied += 1
        remaining, drift_codes, warning_codes = self._store.audit_account(
            user_id=user_id,
            paper_account_id=paper_account_id,
        )
        result = PaperReconciliationResult(
            user_id=user_id,
            paper_account_id=paper_account_id,
            discovered=len(pending),
            applied=applied,
            already_applied=already_applied,
            failed=len(errors),
            remaining=remaining,
            drift_codes=drift_codes,
            warning_codes=warning_codes,
            errors=tuple(errors),
        )
        self._store.record_reconciliation(
            user_id=user_id,
            paper_account_id=paper_account_id,
            pending_count=remaining,
            drift_codes=drift_codes,
            warning_codes=warning_codes,
            errors=result.errors,
            ready=result.ready,
        )
        return result
