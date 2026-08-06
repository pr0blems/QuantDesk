from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import Engine, text
from test_execution_journal import (
    execution_checkpoint,
    execution_intent,
    filled_result,
    unknown_result,
)

from quantdesk_v2.application.ports import IdempotencyStore
from quantdesk_v2.domain.execution import (
    ExecutionResult,
    ExecutionState,
    IdempotencyClaimState,
    OrderIntent,
    RecoveryClaimState,
)
from quantdesk_v2.domain.trading import OrderStatus
from quantdesk_v2.infrastructure.persistence.executions import (
    ExecutionJournalIntegrityError,
    ExecutionJournalStateError,
    MySqlExecutionJournal,
    execution_scope_hash,
)


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


def test_mysql_execution_journal_claim_is_atomic_and_scope_isolated(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    intent = execution_intent()

    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(lambda _: journal.claim(intent), range(6)))

    assert sum(claim.state is IdempotencyClaimState.CLAIMED for claim in claims) == 1
    assert sum(claim.state is IdempotencyClaimState.IN_PROGRESS for claim in claims) == 5
    owner = next(claim for claim in claims if claim.state is IdempotencyClaimState.CLAIMED)
    assert owner.claim_token is not None
    assert isinstance(journal, IdempotencyStore)

    other_scope = execution_intent(tenant_scope="tenant-2", account_scope="account-2")
    other = journal.claim(other_scope)
    assert other.state is IdempotencyClaimState.CLAIMED
    assert other.claim_token != owner.claim_token

    with mysql_test_engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM execution_idempotency_records")
        ).scalar_one()
    assert count == 2


def test_mysql_execution_journal_lease_reclaims_only_uncheckpointed_claim(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine, lease_seconds=5)
    intent = execution_intent()
    first = journal.claim(intent)
    assert first.claim_token is not None
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE execution_idempotency_records
                SET lease_expires_at = TIMESTAMPADD(SECOND, -1, UTC_TIMESTAMP(6))
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        )

    reclaimed = journal.claim(intent)

    assert reclaimed.state is IdempotencyClaimState.CLAIMED
    assert reclaimed.claim_token is not None
    assert reclaimed.claim_token != first.claim_token
    with pytest.raises(ExecutionJournalStateError):
        journal.complete(intent, unknown_result(intent), first.claim_token)
    journal.complete(intent, unknown_result(intent), reclaimed.claim_token)


def test_mysql_execution_checkpoint_never_reclaims_or_blindly_retries(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine, lease_seconds=5)
    intent = execution_intent()
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    journal.save_checkpoint(intent, claim.claim_token, checkpoint)
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE execution_idempotency_records
                SET lease_expires_at = TIMESTAMPADD(SECOND, -1, UTC_TIMESTAMP(6))
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        )

    duplicate = journal.claim(intent)

    assert duplicate.state is IdempotencyClaimState.IN_PROGRESS
    assert journal.lookup(intent) is None
    assert journal.load_checkpoint(intent) == checkpoint
    assert journal.pending_checkpoints(
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == (checkpoint,)
    assert journal.pending_checkpoints(
        tenant_scope="other-tenant",
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == ()

    recovered = filled_result(intent)
    with ThreadPoolExecutor(max_workers=6) as executor:
        recovery_claims = list(
            executor.map(lambda _: journal.claim_recovery(intent), range(6))
        )
    assert (
        sum(
            item.state is RecoveryClaimState.ACQUIRED
            for item in recovery_claims
        )
        == 1
    )
    assert (
        sum(
            item.state is RecoveryClaimState.IN_PROGRESS
            for item in recovery_claims
        )
        == 5
    )
    recovery = next(
        item
        for item in recovery_claims
        if item.state is RecoveryClaimState.ACQUIRED
    )
    assert recovery.recovery_token is not None
    with pytest.raises(ExecutionJournalStateError):
        journal.complete(intent, unknown_result(intent), claim.claim_token)
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE execution_idempotency_records
                SET recovery_lease_expires_at = TIMESTAMPADD(
                    SECOND, -1, UTC_TIMESTAMP(6)
                )
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        )
    replacement = journal.claim_recovery(intent)
    assert replacement.state is RecoveryClaimState.ACQUIRED
    assert replacement.recovery_token is not None
    assert replacement.recovery_token != recovery.recovery_token
    with pytest.raises(ExecutionJournalStateError):
        journal.complete_recovered(
            intent,
            checkpoint,
            recovered,
            recovery.recovery_token,
        )
    journal.complete_recovered(
        intent,
        checkpoint,
        recovered,
        replacement.recovery_token,
    )
    assert journal.lookup(intent) == recovered
    assert journal.pending_checkpoints(
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == ()
    assert journal.claim(intent).state is IdempotencyClaimState.COMPLETED


def test_mysql_execution_unknown_requires_explicit_resolution(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    intent = execution_intent()
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    journal.save_checkpoint(intent, claim.claim_token, execution_checkpoint(intent))
    unknown = unknown_result(intent)
    journal.complete(intent, unknown, claim.claim_token)

    replay = journal.claim(intent)

    assert replay.state is IdempotencyClaimState.COMPLETED
    assert replay.result == unknown
    resolved = filled_result(intent)
    recovery = journal.claim_recovery(intent)
    assert recovery.recovery_token is not None
    journal.advance_recovered(intent, resolved, recovery.recovery_token)
    assert journal.lookup(intent) == resolved
    assert journal.claim_recovery(intent).state is RecoveryClaimState.NOT_RECOVERABLE


def test_mysql_recovery_advances_order_state_monotonically(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine, recovery_lease_seconds=5)
    intent = execution_intent()
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    journal.save_checkpoint(intent, claim.claim_token, checkpoint)

    recovery = journal.claim_recovery(intent)
    assert recovery.recovery_token is not None
    submitted = _submitted_result(intent)
    journal.complete_recovered(
        intent,
        checkpoint,
        submitted,
        recovery.recovery_token,
    )
    assert journal.pending_checkpoints(
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
    ) == (checkpoint,)

    recovery = journal.claim_recovery(intent)
    assert recovery.recovery_token is not None
    partial = _partially_filled_result(intent)
    journal.advance_recovered(intent, partial, recovery.recovery_token)
    assert journal.lookup(intent) == partial

    recovery = journal.claim_recovery(intent)
    assert recovery.recovery_token is not None
    with pytest.raises(ExecutionJournalStateError):
        journal.advance_recovered(intent, submitted, recovery.recovery_token)
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE execution_idempotency_records
                SET recovery_lease_expires_at = TIMESTAMPADD(
                    SECOND, -1, UTC_TIMESTAMP(6)
                )
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        )

    recovery = journal.claim_recovery(intent)
    assert recovery.recovery_token is not None
    terminal = filled_result(intent)
    journal.advance_recovered(intent, terminal, recovery.recovery_token)
    assert journal.lookup(intent) == terminal
    assert journal.claim_recovery(intent).state is RecoveryClaimState.NOT_RECOVERABLE


def test_mysql_recovery_identical_replay_is_idempotent_without_stealing_lease(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine, recovery_lease_seconds=5)
    intent = execution_intent()
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    checkpoint = execution_checkpoint(intent)
    journal.save_checkpoint(intent, claim.claim_token, checkpoint)
    journal.complete(intent, unknown_result(intent), claim.claim_token)

    first = journal.claim_recovery(intent)
    assert first.recovery_token is not None
    partial = _partially_filled_result(intent)
    journal.advance_recovered(intent, partial, first.recovery_token)

    # A response-lost replay sees the exact committed value and no active lease.
    journal.advance_recovered(intent, partial, first.recovery_token)

    second = journal.claim_recovery(intent)
    assert second.recovery_token is not None
    assert second.recovery_token != first.recovery_token
    with pytest.raises(ExecutionJournalStateError, match="stale"):
        journal.advance_recovered(intent, partial, first.recovery_token)
    with pytest.raises(ExecutionJournalStateError, match="stale"):
        journal.advance_recovered(intent, filled_result(intent), first.recovery_token)

    terminal = filled_result(intent)
    journal.advance_recovered(intent, terminal, second.recovery_token)
    # Terminal identical replay is also a successful no-op after commit.
    journal.advance_recovered(intent, terminal, second.recovery_token)

    assert journal.lookup(intent) == terminal
    assert journal.claim_recovery(intent).state is RecoveryClaimState.NOT_RECOVERABLE


def test_mysql_execution_journal_rejects_scope_conflict_and_tampering(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    intent = execution_intent()
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    journal.complete(intent, unknown_result(intent), claim.claim_token)

    conflicting = replace(intent, intent_id="journal-intent-different")
    with pytest.raises(ExecutionJournalIntegrityError):
        journal.claim(conflicting)

    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE execution_idempotency_records
                SET result_json = CONCAT(result_json, ' ')
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        )
    with pytest.raises(ExecutionJournalIntegrityError):
        journal.lookup(intent)
