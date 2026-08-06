from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from test_execution_journal import (
    execution_checkpoint,
    filled_result,
    unknown_result,
)
from test_execution_journal import execution_intent as journal_execution_intent

from quantdesk_v2.application.execution_control import (
    ExecutionAccountScope,
    ExecutionAlreadyAuthorized,
    ExecutionControlIntegrityError,
    ExecutionControlPolicy,
    ExecutionControlStateError,
    ExecutionControlStore,
    ReflectedRiskEvidence,
    RiskReservationState,
    SafetyCommand,
    SafetyCommandAction,
    SafetyVersionConflict,
)
from quantdesk_v2.application.risk import RiskPolicy
from quantdesk_v2.application.safety import PreflightPolicy
from quantdesk_v2.domain.execution import (
    ExecutionContext,
    ExecutionResult,
    ExecutionState,
    IntentAction,
    RecoveryClaimState,
)
from quantdesk_v2.domain.safety import SafetyMode
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    BrokerOrder,
    InstrumentRules,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionDirection,
    PositionMode,
    PositionSide,
    Quote,
)
from quantdesk_v2.infrastructure.persistence.execution_controls import (
    MySqlExecutionControlStore,
)
from quantdesk_v2.infrastructure.persistence.executions import (
    MySqlExecutionJournal,
    execution_scope_hash,
)


def execution_intent(*args, **kwargs):
    intent = journal_execution_intent(*args, **kwargs)
    now = datetime.now(UTC)
    return replace(
        intent,
        signal_time=now - timedelta(minutes=1),
        valid_until=now + timedelta(minutes=5),
        created_at=now,
    )


def _context(
    *,
    wallet_balance: Decimal = Decimal("1000"),
    positions: tuple[Position, ...] = (),
) -> ExecutionContext:
    observed_at = datetime.now(UTC)
    return ExecutionContext(
        account=AccountSnapshot(
            account_type=AccountType.USD_M_FUTURES,
            can_trade=True,
            wallet_balance=wallet_balance,
            available_balance=wallet_balance,
            unrealized_pnl=Decimal(0),
            currency="USDT",
            updated_at=observed_at,
            positions=positions,
        ),
        quote=Quote("BTCUSDT", Decimal("60000"), observed_at),
        rules=InstrumentRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            price_tick=Decimal("0.1"),
            minimum_notional=Decimal("5"),
        ),
        position_mode=PositionMode.ONE_WAY,
    )


def _reflection_snapshot(
    observed_at,
    *,
    quantity: Decimal | None = Decimal("0.1"),
) -> AccountSnapshot:
    return AccountSnapshot(
        account_type=AccountType.USD_M_FUTURES,
        can_trade=True,
        wallet_balance=Decimal("1000"),
        available_balance=Decimal("1000"),
        unrealized_pnl=Decimal(0),
        currency="USDT",
        updated_at=observed_at,
        positions=(
            ()
            if quantity is None
            else (
                Position(
                    symbol="BTCUSDT",
                    direction=PositionDirection.LONG,
                    position_side=PositionSide.BOTH,
                    quantity=quantity,
                    mark_price=Decimal("60000"),
                ),
            )
        ),
    )


def _cancel_result(intent, *, executed_quantity: Decimal) -> ExecutionResult:
    checkpoint = execution_checkpoint(intent)
    order = BrokerOrder(
        reference=OrderReference(checkpoint.client_order_id, intent.symbol),
        exchange_order_id="exchange-cancel-1",
        symbol=intent.symbol,
        side=intent.side,
        position_side=intent.position_side,
        order_type=OrderType.MARKET,
        status=OrderStatus.CANCELED,
        exchange_status="CANCELED",
        quantity=intent.quantity,
        executed_quantity=executed_quantity,
        average_price=Decimal("60000") if executed_quantity else None,
    )
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.CANCELED,
        preflight=checkpoint.preflight,
        risk_decision=checkpoint.risk_decision,
        broker_order=order,
    )


def _submitted_result(intent) -> ExecutionResult:
    checkpoint = execution_checkpoint(intent)
    order = BrokerOrder(
        reference=OrderReference(checkpoint.client_order_id, intent.symbol),
        exchange_order_id="exchange-new-1",
        symbol=intent.symbol,
        side=intent.side,
        position_side=intent.position_side,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        exchange_status="NEW",
        quantity=intent.quantity,
    )
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.SUBMITTED,
        preflight=checkpoint.preflight,
        risk_decision=checkpoint.risk_decision,
        broker_order=order,
    )


def _authorize(
    journal: MySqlExecutionJournal,
    controls: MySqlExecutionControlStore,
    intent,
    *,
    policy: RiskPolicy | None = None,
):
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    result = controls.authorize_and_checkpoint(
        intent,
        claim_token=claim.claim_token,
        checkpoint=execution_checkpoint(intent),
        context=_context(),
        policy=_control_policy(policy),
    )
    return claim.claim_token, result


def _control_policy(risk: RiskPolicy | None = None) -> ExecutionControlPolicy:
    return ExecutionControlPolicy(
        risk=risk or RiskPolicy(),
        preflight=PreflightPolicy(
            max_quote_age_seconds=60 * 60 * 24 * 365,
            max_account_age_seconds=60 * 60 * 24 * 365,
            max_future_skew_seconds=60 * 60 * 24 * 365,
        ),
        failure_threshold=3,
    )


def _scope(intent=None) -> ExecutionAccountScope:
    selected_intent = intent or execution_intent()
    checkpoint = execution_checkpoint(selected_intent)
    return ExecutionAccountScope.from_intent(
        selected_intent,
        broker_name=checkpoint.broker_name,
        physical_account_id=checkpoint.physical_account_id,
    )


def test_mysql_safety_commands_keep_dual_latches_and_are_cas_idempotent(
    mysql_test_engine: Engine,
) -> None:
    controls = MySqlExecutionControlStore(mysql_test_engine)
    assert isinstance(controls, ExecutionControlStore)
    scope = _scope()

    initial = controls.safety_snapshot(scope)
    safe_command = SafetyCommand(
        "safe-1",
        SafetyCommandAction.ENGAGE_SAFE_MODE,
        "operator-1",
        initial.version,
        "manual_risk_review",
    )
    safe = controls.transition_safety(scope, safe_command)
    kill_command = SafetyCommand(
        "kill-1",
        SafetyCommandAction.ENGAGE_KILL_SWITCH,
        "operator-1",
        safe.version,
        "operator_emergency_stop",
    )
    killed = controls.transition_safety(scope, kill_command)

    assert killed.mode is SafetyMode.KILL_SWITCH
    assert killed.safe_reason == "manual_risk_review"
    assert killed.kill_reason == "operator_emergency_stop"
    assert controls.transition_safety(scope, kill_command) == killed
    with pytest.raises(SafetyVersionConflict):
        controls.transition_safety(
            scope,
            SafetyCommand(
                "stale-release",
                SafetyCommandAction.RELEASE_KILL_SWITCH,
                "operator-2",
                safe.version,
            ),
        )

    released = controls.transition_safety(
        scope,
        SafetyCommand(
            "release-1",
            SafetyCommandAction.RELEASE_KILL_SWITCH,
            "operator-2",
            killed.version,
        ),
    )
    assert released.mode is SafetyMode.SAFE_MODE
    assert released.safe_reason == "manual_risk_review"
    recovered = controls.transition_safety(
        scope,
        SafetyCommand(
            "recover-1",
            SafetyCommandAction.RECOVER_SAFE_MODE,
            "operator-2",
            released.version,
        ),
    )
    assert recovered.mode is SafetyMode.NORMAL
    # A replay returns the original command result but never rewinds current state.
    assert controls.transition_safety(scope, kill_command) == killed
    assert controls.safety_snapshot(scope) == recovered


def test_mysql_failure_counter_is_atomic_across_workers(
    mysql_test_engine: Engine,
) -> None:
    scope = _scope()
    first_worker = MySqlExecutionControlStore(mysql_test_engine)
    second_worker = MySqlExecutionControlStore(mysql_test_engine)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                first_worker.record_failure if index % 2 else second_worker.record_failure,
                scope,
                "broker_timeout",
                policy=_control_policy(),
            )
            for index in range(6)
        ]
        for future in futures:
            future.result()

    final = first_worker.safety_snapshot(scope)
    assert final.consecutive_failures == 6
    assert final.version == 7  # policy bind plus six failure transitions
    assert final.mode is SafetyMode.SAFE_MODE
    assert final.safe_reason == "repeated_broker_timeout"
    # Success cannot clear a cross-process latch.
    assert second_worker.record_success(scope, policy=_control_policy()) == final


def test_mysql_authorization_serializes_pending_account_budget(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intents = (execution_intent("budget-1"), execution_intent("budget-2"))
    claims = tuple(journal.claim(intent) for intent in intents)
    assert all(claim.claim_token is not None for claim in claims)
    policy = _control_policy(
        RiskPolicy(max_notional_to_equity=Decimal("10"))
    )
    authorization_context = _context()

    def authorize(index: int):
        token = claims[index].claim_token
        assert token is not None
        return controls.authorize_and_checkpoint(
            intents[index],
            claim_token=token,
            checkpoint=execution_checkpoint(intents[index]),
            context=authorization_context,
            policy=policy,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(authorize, range(2)))

    assert sum(result.authorized for result in results) == 1
    rejected = next(result for result in results if not result.authorized)
    assert rejected.reason_code == "position_reconciliation_pending"
    with mysql_test_engine.connect() as connection:
        reservation_count = connection.execute(
            text("SELECT COUNT(*) FROM execution_risk_reservations")
        ).scalar_one()
        checkpoint_count = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM execution_idempotency_records
                WHERE checkpoint_json IS NOT NULL
                """
            )
        ).scalar_one()
    assert reservation_count == 1
    assert checkpoint_count == 1


def test_mysql_existing_checkpoint_is_recovery_only_not_reauthorized(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent("already-authorized")
    claim_token, authorization = _authorize(journal, controls, intent)
    assert authorization.authorized

    with pytest.raises(ExecutionAlreadyAuthorized):
        controls.authorize_and_checkpoint(
            intent,
            claim_token=claim_token,
            checkpoint=execution_checkpoint(intent),
            context=_context(),
            policy=_control_policy(),
        )

    assert journal.lookup(intent) is None
    assert journal.load_checkpoint(intent) == execution_checkpoint(intent)
    assert controls.reservation(intent).state is RiskReservationState.HELD


def test_mysql_reduce_only_requires_matching_sufficient_position(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = replace(
        execution_intent("reduce"),
        intent_id="journal-intent-reduce-close",
        idempotency_key="journal:strategy-v1:signal-reduce-close",
        action=IntentAction.CLOSE,
        side=OrderSide.SELL,
        reduce_only=True,
    )
    claim = journal.claim(intent)
    assert claim.claim_token is not None

    rejected = controls.authorize_and_checkpoint(
        intent,
        claim_token=claim.claim_token,
        checkpoint=execution_checkpoint(intent),
        context=_context(),
        policy=_control_policy(),
    )
    assert not rejected.authorized
    assert rejected.reason_code == "position_to_reduce_not_found"
    assert journal.load_checkpoint(intent) is None

    insufficient_position = Position(
        symbol=intent.symbol,
        direction=PositionDirection.LONG,
        position_side=PositionSide.BOTH,
        quantity=Decimal("0.05"),
        mark_price=Decimal("60000"),
    )
    insufficient = controls.authorize_and_checkpoint(
        intent,
        claim_token=claim.claim_token,
        checkpoint=execution_checkpoint(intent),
        context=_context(positions=(insufficient_position,)),
        policy=_control_policy(),
    )
    assert not insufficient.authorized
    assert insufficient.reason_code == "reduce_quantity_exceeds_position"

    matching_position = replace(insufficient_position, quantity=Decimal("0.2"))
    authorized = controls.authorize_and_checkpoint(
        intent,
        claim_token=claim.claim_token,
        checkpoint=execution_checkpoint(intent),
        context=_context(positions=(matching_position,)),
        policy=_control_policy(),
    )
    assert authorized.authorized
    assert authorized.reservation is not None
    assert authorized.reservation.reserved_notional == 0
    assert authorized.reservation.reserved_open_slots == 0


def test_mysql_expired_claim_fails_before_reservation_or_checkpoint(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine, lease_seconds=5)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent("expired")
    claim = journal.claim(intent)
    assert claim.claim_token is not None
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

    with pytest.raises(ExecutionControlStateError, match="lease expired"):
        controls.authorize_and_checkpoint(
            intent,
            claim_token=claim.claim_token,
            checkpoint=execution_checkpoint(intent),
            context=_context(),
            policy=_control_policy(),
        )

    with mysql_test_engine.connect() as connection:
        reservation_count = connection.execute(
            text("SELECT COUNT(*) FROM execution_risk_reservations")
        ).scalar_one()
        checkpoint = connection.execute(
            text(
                """
                SELECT checkpoint_json FROM execution_idempotency_records
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        ).scalar_one()
    assert reservation_count == 0
    assert checkpoint is None


def test_mysql_checkpoint_failure_rolls_back_inserted_reservation(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent("rollback")
    claim = journal.claim(intent)
    assert claim.claim_token is not None
    with mysql_test_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER `test_execution_checkpoint_failure`
            BEFORE UPDATE ON execution_idempotency_records
            FOR EACH ROW
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'forced checkpoint failure'
            """
        )
    try:
        with pytest.raises(DBAPIError):
            controls.authorize_and_checkpoint(
                intent,
                claim_token=claim.claim_token,
                checkpoint=execution_checkpoint(intent),
                context=_context(),
                policy=_control_policy(),
            )
    finally:
        with mysql_test_engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER IF EXISTS `test_execution_checkpoint_failure`"
            )

    with mysql_test_engine.connect() as connection:
        reservation_count = connection.execute(
            text("SELECT COUNT(*) FROM execution_risk_reservations")
        ).scalar_one()
        checkpoint = connection.execute(
            text(
                """
                SELECT checkpoint_json FROM execution_idempotency_records
                WHERE scope_hash = :scope_hash
                """
            ),
            {"scope_hash": execution_scope_hash(intent)},
        ).scalar_one()
    assert reservation_count == 0
    assert checkpoint is None


def test_mysql_unknown_and_partial_outcomes_keep_budget_until_reflected(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent("recover")
    claim_token, authorization = _authorize(journal, controls, intent)
    assert authorization.authorized

    held = controls.record_broker_outcome(
        intent,
        result=unknown_result(intent),
        claim_token=claim_token,
    )
    assert held.state is RiskReservationState.HELD

    recovery = journal.claim_recovery(intent)
    assert recovery.state is RecoveryClaimState.ACQUIRED
    assert recovery.recovery_token is not None
    committed = controls.record_recovered_outcome(
        intent,
        checkpoint=execution_checkpoint(intent),
        result=filled_result(intent),
        recovery_token=recovery.recovery_token,
    )
    assert committed.state is RiskReservationState.COMMITTED_UNREFLECTED
    assert journal.lookup(intent) == filled_result(intent)

    stale_evidence = ReflectedRiskEvidence(
        snapshot=_reflection_snapshot(committed.updated_at, quantity=None),
        reference="account-snapshot:recover:stale",
    )
    with pytest.raises(ExecutionControlStateError, match="position delta"):
        controls.settle_reflected(intent, evidence=stale_evidence)
    evidence = ReflectedRiskEvidence(
        snapshot=_reflection_snapshot(committed.updated_at + timedelta(seconds=1)),
        reference="account-snapshot:recover:1",
    )
    settled = controls.settle_reflected(intent, evidence=evidence)
    assert settled.state is RiskReservationState.SETTLED
    assert settled.release_reason == "account_snapshot_reflected"
    assert settled.settlement_snapshot_hash == evidence.snapshot_hash
    assert controls.settle_reflected(intent, evidence=evidence) == settled


def test_mysql_submitted_reservation_cannot_settle_without_terminal_outcome(
    mysql_test_engine: Engine,
) -> None:
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent("settled-fence")
    claim_token, authorization = _authorize(journal, controls, intent)
    assert authorization.authorized
    committed = controls.record_broker_outcome(
        intent,
        result=_submitted_result(intent),
        claim_token=claim_token,
    )
    evidence = ReflectedRiskEvidence(
        snapshot=_reflection_snapshot(committed.updated_at + timedelta(seconds=1)),
        reference="account-snapshot:settled-fence:1",
    )
    with pytest.raises(ExecutionControlStateError, match="terminal broker outcome"):
        controls.settle_reflected(intent, evidence=evidence)
    assert journal.lookup(intent) == _submitted_result(intent)
    assert (
        controls.reservation(intent).state
        is RiskReservationState.COMMITTED_UNREFLECTED
    )


@pytest.mark.parametrize(
    ("executed_quantity", "expected_state"),
    [
        (Decimal("0"), RiskReservationState.RELEASED),
        (Decimal("0.01"), RiskReservationState.COMMITTED_UNREFLECTED),
    ],
)
def test_mysql_cancel_releases_only_zero_fill_reservation(
    mysql_test_engine: Engine,
    executed_quantity: Decimal,
    expected_state: RiskReservationState,
) -> None:
    suffix = "cancel-zero" if executed_quantity == 0 else "cancel-partial"
    journal = MySqlExecutionJournal(mysql_test_engine)
    controls = MySqlExecutionControlStore(mysql_test_engine)
    intent = execution_intent(suffix)
    claim_token, authorization = _authorize(journal, controls, intent)
    assert authorization.authorized

    reservation = controls.record_broker_outcome(
        intent,
        result=_cancel_result(intent, executed_quantity=executed_quantity),
        claim_token=claim_token,
    )

    assert reservation.state is expected_state
    if expected_state is RiskReservationState.RELEASED:
        assert reservation.release_reason == "zero_fill_terminal"
    else:
        assert reservation.release_reason is None


def test_mysql_command_id_cannot_be_reused_with_different_payload(
    mysql_test_engine: Engine,
) -> None:
    controls = MySqlExecutionControlStore(mysql_test_engine)
    scope = _scope()
    first = SafetyCommand(
        "same-command",
        SafetyCommandAction.ENGAGE_SAFE_MODE,
        "operator-1",
        0,
        "reason_one",
    )
    controls.transition_safety(scope, first)

    with pytest.raises(ExecutionControlIntegrityError):
        controls.transition_safety(
            scope,
            replace(first, reason_code="reason_two"),
        )
