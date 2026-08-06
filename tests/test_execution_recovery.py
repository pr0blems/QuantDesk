from __future__ import annotations

from dataclasses import replace

import pytest
from test_execution_journal import (
    execution_checkpoint,
    execution_intent,
    filled_result,
    unknown_result,
)

from quantdesk_v2.domain.execution import (
    ExecutionResult,
    ExecutionState,
    OrderIntent,
    RecoveryClaimState,
)
from quantdesk_v2.domain.trading import OrderStatus
from quantdesk_v2.infrastructure.memory_execution import InMemoryIdempotencyStore


def _submitted_result(intent: OrderIntent) -> ExecutionResult:
    terminal = filled_result(intent)
    assert terminal.broker_order is not None
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.SUBMITTED,
        preflight=terminal.preflight,
        risk_decision=terminal.risk_decision,
        broker_order=replace(
            terminal.broker_order,
            status=OrderStatus.NEW,
            exchange_status="NEW",
            executed_quantity=terminal.broker_order.executed_quantity * 0,
            average_price=None,
        ),
    )


def _partially_filled_result(intent: OrderIntent) -> ExecutionResult:
    terminal = filled_result(intent)
    assert terminal.broker_order is not None
    half_quantity = terminal.broker_order.executed_quantity / 2
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.PARTIALLY_FILLED,
        preflight=terminal.preflight,
        risk_decision=terminal.risk_decision,
        broker_order=replace(
            terminal.broker_order,
            status=OrderStatus.PARTIALLY_FILLED,
            exchange_status="PARTIALLY_FILLED",
            executed_quantity=half_quantity,
        ),
        fills=(replace(terminal.fills[0], quantity=half_quantity),),
    )


def test_memory_recovery_lease_is_exclusive_and_fences_original_owner() -> None:
    now = [100.0]
    store = InMemoryIdempotencyStore(
        recovery_lease_seconds=5,
        monotonic_clock=lambda: now[0],
    )
    intent = execution_intent()
    claim = store.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    store.save_checkpoint(intent, claim.claim_token, checkpoint)

    first = store.claim_recovery(intent)
    assert first.state is RecoveryClaimState.ACQUIRED
    assert first.recovery_token is not None
    assert store.claim_recovery(intent).state is RecoveryClaimState.IN_PROGRESS
    with pytest.raises(RuntimeError, match="fenced"):
        store.complete(intent, unknown_result(intent), claim.claim_token)

    now[0] += 6
    replacement = store.claim_recovery(intent)
    assert replacement.state is RecoveryClaimState.ACQUIRED
    assert replacement.recovery_token is not None
    assert replacement.recovery_token != first.recovery_token
    with pytest.raises(RuntimeError, match="stale"):
        store.complete_recovered(
            intent,
            checkpoint,
            _submitted_result(intent),
            first.recovery_token,
        )
    store.complete_recovered(
        intent,
        checkpoint,
        _submitted_result(intent),
        replacement.recovery_token,
    )


def test_memory_recovery_only_advances_broker_state_monotonically() -> None:
    now = [100.0]
    store = InMemoryIdempotencyStore(
        recovery_lease_seconds=5,
        monotonic_clock=lambda: now[0],
    )
    intent = execution_intent()
    claim = store.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    store.save_checkpoint(intent, claim.claim_token, checkpoint)
    recovery = store.claim_recovery(intent)
    assert recovery.recovery_token is not None
    submitted = _submitted_result(intent)
    store.complete_recovered(
        intent,
        checkpoint,
        submitted,
        recovery.recovery_token,
    )
    assert store.pending_checkpoints(
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == (checkpoint,)

    recovery = store.claim_recovery(intent)
    assert recovery.recovery_token is not None
    partial = _partially_filled_result(intent)
    store.advance_recovered(intent, partial, recovery.recovery_token)

    recovery = store.claim_recovery(intent)
    assert recovery.recovery_token is not None
    with pytest.raises(RuntimeError, match="backward"):
        store.advance_recovered(intent, submitted, recovery.recovery_token)
    now[0] += 6
    recovery = store.claim_recovery(intent)
    assert recovery.recovery_token is not None
    terminal = filled_result(intent)
    store.advance_recovered(intent, terminal, recovery.recovery_token)

    assert store.lookup(intent) == terminal
    assert store.claim_recovery(intent).state is RecoveryClaimState.NOT_RECOVERABLE
    assert store.pending_checkpoints(
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == ()


def test_memory_recovery_identical_replay_is_idempotent_without_stealing_lease() -> None:
    store = InMemoryIdempotencyStore(recovery_lease_seconds=5)
    intent = execution_intent()
    claim = store.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    store.save_checkpoint(intent, claim.claim_token, checkpoint)
    store.complete(intent, unknown_result(intent), claim.claim_token)

    first = store.claim_recovery(intent)
    assert first.recovery_token is not None
    partial = _partially_filled_result(intent)
    store.advance_recovered(intent, partial, first.recovery_token)

    # A response-lost replay sees the exact committed value and no active lease.
    store.advance_recovered(intent, partial, first.recovery_token)

    second = store.claim_recovery(intent)
    assert second.recovery_token is not None
    assert second.recovery_token != first.recovery_token
    with pytest.raises(RuntimeError, match="stale"):
        store.advance_recovered(intent, partial, first.recovery_token)
    with pytest.raises(RuntimeError, match="stale"):
        store.advance_recovered(intent, filled_result(intent), first.recovery_token)

    terminal = filled_result(intent)
    store.advance_recovered(intent, terminal, second.recovery_token)
    # Terminal identical replay is also a successful no-op after commit.
    store.advance_recovered(intent, terminal, second.recovery_token)

    assert store.lookup(intent) == terminal
    assert store.claim_recovery(intent).state is RecoveryClaimState.NOT_RECOVERABLE
