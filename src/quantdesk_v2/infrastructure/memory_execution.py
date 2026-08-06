"""Thread-safe in-memory idempotency adapter for non-live execution modes."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from ..domain.execution import (
    ExecutionCheckpoint,
    ExecutionResult,
    ExecutionState,
    IdempotencyClaim,
    IdempotencyClaimState,
    OrderIntent,
    RecoveryClaim,
    RecoveryClaimState,
    execution_result_can_advance,
)
from ..domain.trading import OrderType

_RECOVERABLE_STATES = frozenset(
    {
        ExecutionState.UNKNOWN,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
    }
)
_RECOVERY_RESULT_STATES = _RECOVERABLE_STATES | frozenset(
    {
        ExecutionState.FILLED,
        ExecutionState.CANCELED,
        ExecutionState.BROKER_REJECTED,
    }
)


class InMemoryIdempotencyStore:
    """Atomic first-writer-wins reservations used by backtest/paper/shadow."""

    __slots__ = (
        "_checkpoints",
        "_clock",
        "_intents",
        "_lock",
        "_recovery_lease_seconds",
        "_recovery_leases",
        "_recovery_started",
        "_results",
        "_tokens",
    )

    def __init__(
        self,
        *,
        recovery_lease_seconds: float = 30,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(recovery_lease_seconds, bool)
            or not 0 < recovery_lease_seconds <= 3600
        ):
            raise ValueError("recovery_lease_seconds must be between 0 and 3600")
        self._lock = threading.RLock()
        self._clock = monotonic_clock or time.monotonic
        self._recovery_lease_seconds = float(recovery_lease_seconds)
        self._intents: dict[tuple[str, str, str, str, str], OrderIntent] = {}
        self._results: dict[tuple[str, str, str, str, str], ExecutionResult] = {}
        self._tokens: dict[tuple[str, str, str, str, str], str] = {}
        self._checkpoints: dict[
            tuple[str, str, str, str, str], ExecutionCheckpoint
        ] = {}
        self._recovery_leases: dict[
            tuple[str, str, str, str, str], tuple[str, float]
        ] = {}
        self._recovery_started: set[tuple[str, str, str, str, str]] = set()

    def claim(self, intent: OrderIntent) -> IdempotencyClaim:
        key = intent.idempotency_scope
        with self._lock:
            existing_intent = self._intents.get(key)
            if existing_intent is not None and existing_intent != intent:
                raise ValueError("idempotency key is already bound to a different intent")
            result = self._results.get(key)
            if result is not None:
                return IdempotencyClaim(IdempotencyClaimState.COMPLETED, result)
            if existing_intent is not None:
                return IdempotencyClaim(IdempotencyClaimState.IN_PROGRESS)
            self._intents[key] = intent
            token = uuid.uuid4().hex
            self._tokens[key] = token
            return IdempotencyClaim(
                IdempotencyClaimState.CLAIMED,
                claim_token=token,
            )

    def save_checkpoint(
        self,
        intent: OrderIntent,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        key = intent.idempotency_scope
        with self._lock:
            if self._intents.get(key) != intent or self._tokens.get(key) != claim_token:
                raise RuntimeError("execution claim owner token is stale")
            if checkpoint.intent != intent:
                raise ValueError("checkpoint does not match execution intent")
            existing = self._checkpoints.get(key)
            if existing is not None and existing != checkpoint:
                raise RuntimeError("execution checkpoint already exists")
            self._checkpoints[key] = checkpoint

    def load_checkpoint(self, intent: OrderIntent) -> ExecutionCheckpoint | None:
        with self._lock:
            return self._checkpoints.get(intent.idempotency_scope)

    def pending_checkpoints(
        self,
        *,
        tenant_scope: str,
        user_scope: str,
        account_scope: str,
        limit: int = 100,
    ) -> tuple[ExecutionCheckpoint, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            return tuple(
                checkpoint
                for key, checkpoint in self._checkpoints.items()
                if (
                    key not in self._results
                    or self._results[key].state in _RECOVERABLE_STATES
                )
                and checkpoint.intent.tenant_scope == tenant_scope
                and checkpoint.intent.user_scope == user_scope
                and checkpoint.intent.account_scope == account_scope
            )[:limit]

    def complete(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        claim_token: str,
    ) -> None:
        key = intent.idempotency_scope
        with self._lock:
            reserved = self._intents.get(key)
            if reserved is None:
                raise RuntimeError("cannot complete an unclaimed idempotency key")
            if reserved != intent or result.intent != intent:
                raise ValueError("execution result does not match its idempotency reservation")
            if self._tokens.get(key) != claim_token:
                raise RuntimeError("execution claim owner token is stale")
            if key in self._recovery_started:
                raise RuntimeError("execution claim owner was fenced by recovery")
            existing = self._results.get(key)
            if existing is not None and existing != result:
                raise RuntimeError("idempotency result is already complete")
            self._results[key] = result

    def claim_recovery(self, intent: OrderIntent) -> RecoveryClaim:
        key = intent.idempotency_scope
        with self._lock:
            if self._intents.get(key) != intent:
                return RecoveryClaim(RecoveryClaimState.NOT_RECOVERABLE)
            if key not in self._checkpoints:
                return RecoveryClaim(RecoveryClaimState.NOT_RECOVERABLE)
            existing = self._results.get(key)
            if existing is not None and existing.state not in _RECOVERABLE_STATES:
                return RecoveryClaim(RecoveryClaimState.NOT_RECOVERABLE)
            now = self._clock()
            active = self._recovery_leases.get(key)
            if active is not None and active[1] > now:
                return RecoveryClaim(RecoveryClaimState.IN_PROGRESS)
            recovery_token = uuid.uuid4().hex
            self._recovery_leases[key] = (
                recovery_token,
                now + self._recovery_lease_seconds,
            )
            self._recovery_started.add(key)
            return RecoveryClaim(
                RecoveryClaimState.ACQUIRED,
                recovery_token=recovery_token,
            )

    def complete_recovered(
        self,
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        key = intent.idempotency_scope
        with self._lock:
            if self._intents.get(key) != intent:
                raise RuntimeError("execution claim does not exist")
            if self._checkpoints.get(key) != checkpoint:
                raise RuntimeError("execution recovery checkpoint differs")
            _require_recovered_result(intent, checkpoint, result)
            self._require_recovery_owner(key, recovery_token)
            existing = self._results.get(key)
            if existing is not None:
                raise RuntimeError("execution claim is already completed")
            self._results[key] = result
            self._recovery_leases.pop(key, None)

    def lookup(self, intent: OrderIntent) -> ExecutionResult | None:
        with self._lock:
            return self._results.get(intent.idempotency_scope)

    def advance_recovered(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        key = intent.idempotency_scope
        with self._lock:
            existing = self._results.get(key)
            if existing is None:
                raise RuntimeError("execution result is not recoverable")
            if existing.intent != intent or result.intent != intent:
                raise ValueError("reconciled execution does not match its reservation")
            checkpoint = self._checkpoints.get(key)
            if checkpoint is None:
                raise RuntimeError("execution recovery checkpoint is unavailable")
            _require_recovered_result(intent, checkpoint, result)
            identical = existing == result
            active = self._recovery_leases.get(key)
            now = self._clock()
            if identical:
                if active is None or active[1] <= now:
                    return
                if active[0] != recovery_token:
                    raise RuntimeError("execution recovery owner token is stale")
                self._recovery_leases.pop(key, None)
                return
            if existing.state not in _RECOVERABLE_STATES:
                raise RuntimeError("execution result is not recoverable")
            self._require_recovery_owner(key, recovery_token)
            if not execution_result_can_advance(existing, result):
                raise RuntimeError("recovered execution result cannot move backward")
            self._results[key] = result
            self._recovery_leases.pop(key, None)

    def resolve_unknown(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        """Compatibility alias for token-fenced recovery callers."""

        self.advance_recovered(intent, result, recovery_token)

    def _require_recovery_owner(
        self,
        key: tuple[str, str, str, str, str],
        recovery_token: str,
    ) -> None:
        active = self._recovery_leases.get(key)
        if (
            active is None
            or active[0] != recovery_token
            or active[1] <= self._clock()
        ):
            raise RuntimeError("execution recovery owner token is stale")


def _require_recovered_result(
    intent: OrderIntent,
    checkpoint: ExecutionCheckpoint,
    result: ExecutionResult,
) -> None:
    if result.intent != intent:
        raise ValueError("recovered result does not match execution intent")
    if (
        result.state not in _RECOVERY_RESULT_STATES
        or result.preflight != checkpoint.preflight
        or result.risk_decision != checkpoint.risk_decision
    ):
        raise ValueError("recovered result does not preserve its execution checkpoint")
    order = result.broker_order
    if order is None:
        if result.state is not ExecutionState.UNKNOWN:
            raise ValueError("known recovered result requires a broker order")
        return
    expected_conditional = intent.order_type is not OrderType.MARKET
    if (
        order.reference.client_order_id != checkpoint.client_order_id
        or order.reference.symbol != intent.symbol
        or order.reference.conditional is not expected_conditional
    ):
        raise ValueError("recovered broker order differs from its checkpoint")
