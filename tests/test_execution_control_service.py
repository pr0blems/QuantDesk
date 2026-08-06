from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

import pytest
from test_execution_service import NOW, MemoryBroker, MemoryFeed, _intent

from quantdesk_v2.application.execution_control import (
    AuthorizationResult,
    ExecutionAccountScope,
    ExecutionAlreadyAuthorized,
    ExecutionControlPolicy,
    ExecutionControlSnapshot,
    ExecutionControlStateError,
    ExecutionControlStore,
    ReflectedRiskEvidence,
    RiskReservation,
    RiskReservationState,
    SafetyCommand,
    SafetyVersionConflict,
    reservation_state_for_result,
)
from quantdesk_v2.application.execution_service import (
    ExecutionService,
    deterministic_client_order_id,
)
from quantdesk_v2.application.risk import DeterministicRiskEvaluator, RiskPolicy
from quantdesk_v2.application.safety import PreflightService
from quantdesk_v2.domain.execution import (
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    IntentAction,
    OrderIntent,
    RiskDecision,
)
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    BrokerError,
    BrokerOrder,
    MarketOrder,
    OrderReference,
    OrderSide,
    OrderType,
    Position,
    PositionDirection,
    PositionMode,
    PositionSide,
)
from quantdesk_v2.infrastructure.memory_execution import InMemoryIdempotencyStore


class LiveMemoryBroker(MemoryBroker):
    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.LIVE


class RecoveringLiveBroker(LiveMemoryBroker):
    def __init__(self) -> None:
        super().__init__()
        self.recovered_order: BrokerOrder | None = None

    def place_market_order(self, order: MarketOrder) -> BrokerOrder:
        self.recovered_order = super().place_market_order(order)
        raise BrokerError("timeout")

    def get_order(self, reference: OrderReference) -> BrokerOrder:
        if self.recovered_order is None:
            raise BrokerError("order_not_found")
        return self.recovered_order


class OpaqueRiskEvaluator:
    def __init__(self, delegate: DeterministicRiskEvaluator) -> None:
        self._delegate = delegate

    def evaluate(
        self,
        intent: OrderIntent,
        context: ExecutionContext,
    ) -> RiskDecision:
        return self._delegate.evaluate(intent, context)


class FakeExecutionControl:
    durable_for_live = True

    def __init__(
        self,
        store: InMemoryIdempotencyStore,
        *,
        denial_reason: str | None = None,
        fail_on_success: bool = False,
        kill_reason: str | None = None,
        checkpoint_before_denial: bool = False,
        safety_conflicts: int = 0,
    ) -> None:
        self.store = store
        self.denial_reason = denial_reason
        self.fail_on_success = fail_on_success
        self.kill_reason = kill_reason
        self.checkpoint_before_denial = checkpoint_before_denial
        self.safety_conflicts = safety_conflicts
        self.control_version = 0
        self.safe_reason: str | None = None
        self.consecutive_failures = 0
        self.authorize_calls = 0
        self.bind_policy_calls = 0
        self.safety_snapshot_calls = 0
        self.broker_outcome_calls = 0
        self.recovered_outcome_calls = 0
        self.failure_calls: list[str] = []
        self.success_calls = 0
        self.transition_commands: list[SafetyCommand] = []
        self._reservation: RiskReservation | None = None
        self._bound_policy_fingerprint: str | None = None
        self._bound_failure_threshold: int | None = None

    def _snapshot(self, scope: ExecutionAccountScope) -> ExecutionControlSnapshot:
        return ExecutionControlSnapshot(
            scope=scope,
            safe_reason=self.safe_reason,
            kill_reason=self.kill_reason,
            consecutive_failures=self.consecutive_failures,
            version=self.control_version,
            changed_at=NOW,
            control_policy_hash=self._bound_policy_fingerprint,
            failure_threshold=self._bound_failure_threshold,
        )

    def bind_policy(
        self,
        scope: ExecutionAccountScope,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        """Mirror the durable pin without treating an attempt as a success."""

        self.bind_policy_calls += 1
        if self._bound_policy_fingerprint is None:
            self._bound_policy_fingerprint = policy.fingerprint
            self._bound_failure_threshold = policy.failure_threshold
            self.control_version += 1
        elif self._bound_policy_fingerprint != policy.fingerprint:
            self.kill_reason = self.kill_reason or "control_policy_mismatch"
            self.control_version += 1
            raise ExecutionControlStateError("execution control policy mismatch")
        return self._snapshot(scope)

    def safety_snapshot(
        self,
        scope: ExecutionAccountScope,
    ) -> ExecutionControlSnapshot:
        self.safety_snapshot_calls += 1
        return self._snapshot(scope)

    def observe_account_snapshot(
        self,
        scope: ExecutionAccountScope,
        snapshot: AccountSnapshot,
        reference: str,
    ) -> ExecutionControlSnapshot:
        del snapshot, reference
        return self._snapshot(scope)

    def transition_safety(
        self,
        scope: ExecutionAccountScope,
        command: SafetyCommand,
    ) -> ExecutionControlSnapshot:
        self.transition_commands.append(command)
        if self.safety_conflicts > 0:
            self.safety_conflicts -= 1
            self.control_version += 1
            raise SafetyVersionConflict("simulated safety CAS conflict")
        if command.expected_version != self.control_version:
            raise SafetyVersionConflict("stale fake safety version")
        self.safe_reason = command.reason_code
        self.control_version += 1
        return self._snapshot(scope)

    def record_failure(
        self,
        scope: ExecutionAccountScope,
        reason_code: str,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        assert self._bound_policy_fingerprint == policy.fingerprint
        self.failure_calls.append(reason_code)
        self.consecutive_failures += 1
        if (
            self.safe_reason is None
            and self.consecutive_failures >= policy.failure_threshold
        ):
            self.safe_reason = f"repeated_{reason_code}"[:64]
        self.control_version += 1
        return self._snapshot(scope)

    def record_success(
        self,
        scope: ExecutionAccountScope,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        assert self._bound_policy_fingerprint == policy.fingerprint
        self.success_calls += 1
        if self.fail_on_success:
            raise RuntimeError("durable safety write failed")
        if (
            self.consecutive_failures > 0
            and self.safe_reason is None
            and self.kill_reason is None
        ):
            self.consecutive_failures = 0
            self.control_version += 1
        return self._snapshot(scope)

    def authorize_and_checkpoint(
        self,
        intent: OrderIntent,
        *,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
        policy: ExecutionControlPolicy,
    ) -> AuthorizationResult:
        del context
        assert self._bound_policy_fingerprint == policy.fingerprint
        self.authorize_calls += 1
        scope = ExecutionAccountScope.from_intent(
            intent,
            broker_name=checkpoint.broker_name,
            physical_account_id=checkpoint.physical_account_id,
        )
        snapshot = self._snapshot(scope)
        if intent.action is IntentAction.TAKE_PROFIT:
            return AuthorizationResult(
                False,
                "protective_pairing_not_supported",
                snapshot,
            )
        if self.denial_reason is not None:
            if self.checkpoint_before_denial:
                self.store.save_checkpoint(intent, claim_token, checkpoint)
            return AuthorizationResult(False, self.denial_reason, snapshot)
        self.store.save_checkpoint(intent, claim_token, checkpoint)
        digest = hashlib.sha256("\x1f".join(intent.idempotency_scope).encode()).hexdigest()
        self._reservation = RiskReservation(
            reservation_id=digest,
            execution_scope_hash=digest,
            account_control_hash="a" * 64,
            state=RiskReservationState.HELD,
            reserved_notional=checkpoint.risk_decision.notional or Decimal(0),
            reserved_open_slots=0 if intent.risk_reducing else 1,
            policy_hash="b" * 64,
            risk_decision_hash="c" * 64,
            snapshot_hash="d" * 64,
            safety_version=0,
            client_order_id=checkpoint.client_order_id,
            position_key_hash="e" * 64,
            symbol=intent.symbol,
            position_mode=PositionMode.ONE_WAY,
            target_direction=PositionDirection.LONG,
            target_position_side=intent.position_side,
            baseline_direction=None,
            baseline_position_side=None,
            baseline_quantity=Decimal(0),
            authorized_quantity=(
                checkpoint.risk_decision.approved_quantity or Decimal(0)
            ),
            risk_reducing=intent.risk_reducing,
            created_at=NOW,
            updated_at=NOW,
        )
        return AuthorizationResult(True, "authorized", snapshot, self._reservation)

    def record_broker_outcome(
        self,
        intent: OrderIntent,
        *,
        result: ExecutionResult,
        claim_token: str,
    ) -> RiskReservation:
        self.broker_outcome_calls += 1
        self.store.complete(intent, result, claim_token)
        return self._advance_reservation(result)

    def record_recovered_outcome(
        self,
        intent: OrderIntent,
        *,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> RiskReservation:
        self.recovered_outcome_calls += 1
        if self.store.lookup(intent) is None:
            self.store.complete_recovered(
                intent,
                checkpoint,
                result,
                recovery_token,
            )
        else:
            self.store.advance_recovered(intent, result, recovery_token)
        return self._advance_reservation(result)

    def reservation(self, intent: OrderIntent) -> RiskReservation | None:
        del intent
        return self._reservation

    def settle_reflected(
        self,
        intent: OrderIntent,
        *,
        evidence: ReflectedRiskEvidence,
    ) -> RiskReservation:
        del intent, evidence
        if self._reservation is None:
            raise RuntimeError("fake authorization reservation is unavailable")
        return self._reservation

    def operator_recover_safe_mode(self) -> None:
        self.safe_reason = None
        self.control_version += 1

    def _advance_reservation(self, result: ExecutionResult) -> RiskReservation:
        if self._reservation is None:
            raise RuntimeError("fake authorization reservation is unavailable")
        self._reservation = replace(
            self._reservation,
            state=reservation_state_for_result(result),
            updated_at=NOW,
        )
        return self._reservation


def _live_service(
    broker: LiveMemoryBroker,
    store: InMemoryIdempotencyStore,
    control: FakeExecutionControl | None,
    *,
    policy: RiskPolicy,
    evaluator_policy: RiskPolicy | None = None,
    failure_threshold: int = 3,
) -> ExecutionService:
    return ExecutionService(
        broker=broker,
        preflight=PreflightService(broker, MemoryFeed(), clock=lambda: NOW),
        risk=DeterministicRiskEvaluator(
            evaluator_policy or policy,
            clock=lambda: NOW,
        ),
        idempotency=store,
        expected_mode=ExecutionMode.LIVE,
        expected_broker_name="memory",
        expected_physical_account_id="memory-wallet-1",
        expected_market="binance_usdm",
        expected_account_scope="account-1",
        expected_tenant_scope="tenant-1",
        expected_user_scope="user-1",
        execution_control=control,
        risk_policy=policy,
        failure_threshold=failure_threshold,
        clock=lambda: NOW,
    )


def _binding_mismatch_recovery(
    *,
    kill_reason: str | None = None,
    safety_conflicts: int = 0,
) -> tuple[ExecutionService, OrderIntent, FakeExecutionControl]:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(
        store,
        kill_reason=kill_reason,
        safety_conflicts=safety_conflicts,
    )
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)
    preflight = PreflightService(broker, MemoryFeed(), clock=lambda: NOW).run(intent)
    assert preflight.context is not None
    risk = DeterministicRiskEvaluator(policy, clock=lambda: NOW).evaluate(
        intent,
        preflight.context,
    )
    checkpoint = ExecutionCheckpoint(
        intent=intent,
        preflight=preflight.report,
        risk_decision=risk,
        broker_name="wrong-broker",
        physical_account_id=broker.physical_account_id,
        client_order_id=deterministic_client_order_id(intent),
        checkpointed_at=NOW,
    )
    claim = store.claim(intent)
    assert claim.claim_token is not None
    store.save_checkpoint(intent, claim.claim_token, checkpoint)
    return _live_service(broker, store, control, policy=policy), intent, control


def test_live_execution_requires_durable_control_and_explicit_policy() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    policy = RiskPolicy()

    with pytest.raises(ValueError, match="durable execution control"):
        _live_service(broker, store, None, policy=policy)

    non_durable = FakeExecutionControl(store)
    non_durable.durable_for_live = False
    with pytest.raises(ValueError, match="durable execution control"):
        _live_service(broker, store, non_durable, policy=policy)

    with pytest.raises(ValueError, match="explicit risk policy"):
        ExecutionService(
            broker=broker,
            preflight=PreflightService(broker, MemoryFeed(), clock=lambda: NOW),
            risk=DeterministicRiskEvaluator(policy, clock=lambda: NOW),
            idempotency=store,
            expected_mode=ExecutionMode.LIVE,
            expected_broker_name="memory",
            expected_physical_account_id="memory-wallet-1",
            expected_market="binance_usdm",
            expected_account_scope="account-1",
            expected_tenant_scope="tenant-1",
            expected_user_scope="user-1",
            execution_control=FakeExecutionControl(store),
            clock=lambda: NOW,
        )


def test_live_control_denial_completes_blocked_without_broker_write() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store, denial_reason="account_budget_exhausted")
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)

    assert isinstance(control, ExecutionControlStore)

    result = _live_service(broker, store, control, policy=policy).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "account_budget_exhausted"
    assert broker.market_orders == []
    assert control.authorize_calls == 1
    assert control.broker_outcome_calls == 0
    assert store.load_checkpoint(intent) is None
    assert store.lookup(intent) is result


def test_live_take_profit_fails_closed_until_protective_pairing_is_durable() -> None:
    broker = LiveMemoryBroker()
    broker._account = replace(
        broker._account,
        positions=(
            Position(
                symbol="BTCUSDT",
                direction=PositionDirection.LONG,
                position_side=PositionSide.BOTH,
                quantity=Decimal("0.1"),
                notional=Decimal("6000"),
            ),
        ),
    )
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    policy = RiskPolicy()
    intent = replace(
        _intent("take-profit"),
        mode=ExecutionMode.LIVE,
        action=IntentAction.TAKE_PROFIT,
        order_type=OrderType.TAKE_PROFIT_MARKET,
        trigger_price=Decimal("61000"),
        side=OrderSide.SELL,
    )

    result = _live_service(broker, store, control, policy=policy).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "protective_pairing_not_supported"
    assert broker.market_orders == []
    assert broker.trigger_orders == []
    assert control.authorize_calls == 1
    assert control.broker_outcome_calls == 0
    assert store.load_checkpoint(intent) is None


def test_live_forged_owner_scope_never_reaches_control_or_broker() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    policy = RiskPolicy()
    service = _live_service(broker, store, control, policy=policy)
    forged_tenant = replace(
        _intent(),
        mode=ExecutionMode.LIVE,
        tenant_scope="tenant-forged",
    )
    forged_user = replace(
        _intent("forged-user"),
        mode=ExecutionMode.LIVE,
        user_scope="user-forged",
    )

    tenant_result = service.execute(forged_tenant)
    user_recovery = service.reconcile_in_progress(forged_user)
    tenant_recovery = service.reconcile_unknown(forged_tenant)

    assert tenant_result.error_code == "broker_tenant_scope_mismatch"
    assert user_recovery.error_code == "recovery_broker_scope_mismatch"
    assert tenant_recovery.error_code == "recovery_broker_scope_mismatch"
    assert broker.market_orders == []
    assert control.safety_snapshot_calls == 0
    assert control.authorize_calls == 0


def test_live_already_authorized_response_is_left_for_recovery() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(
        store,
        denial_reason="already_authorized",
        checkpoint_before_denial=True,
    )
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)

    with pytest.raises(ExecutionAlreadyAuthorized):
        _live_service(broker, store, control, policy=policy).execute(intent)

    assert broker.market_orders == []
    assert store.lookup(intent) is None
    assert store.load_checkpoint(intent) is not None


def test_live_authorization_and_broker_outcome_are_written_once() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)
    service = _live_service(broker, store, control, policy=policy)

    result = service.execute(intent)
    replay = service.execute(intent)

    assert result.state is ExecutionState.FILLED
    assert replay is result
    assert len(broker.market_orders) == 1
    assert control.authorize_calls == 1
    assert control.broker_outcome_calls == 1
    assert control.success_calls == 1
    assert store.lookup(intent) is result


def test_live_recovery_uses_control_outcome_without_resubmission() -> None:
    broker = RecoveringLiveBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)
    service = _live_service(broker, store, control, policy=policy)

    unknown = service.execute(intent)
    recovered = service.reconcile_unknown(intent)

    assert unknown.state is ExecutionState.UNKNOWN
    assert recovered.state is ExecutionState.FILLED
    assert len(broker.market_orders) == 1
    assert control.broker_outcome_calls == 1
    assert control.recovered_outcome_calls == 1
    assert store.lookup(intent) is recovered


def test_live_recovery_keeps_result_recoverable_if_safety_write_fails() -> None:
    broker = RecoveringLiveBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store, fail_on_success=True)
    policy = RiskPolicy()
    intent = replace(_intent(), mode=ExecutionMode.LIVE)
    service = _live_service(broker, store, control, policy=policy)
    unknown = service.execute(intent)

    with pytest.raises(RuntimeError, match="durable safety write failed"):
        service.reconcile_unknown(intent)

    assert unknown.state is ExecutionState.UNKNOWN
    assert store.lookup(intent) is unknown
    assert control.success_calls == 1
    assert control.recovered_outcome_calls == 0
    assert len(broker.market_orders) == 1


def test_live_recovery_latches_safe_mode_even_while_kill_switch_is_active() -> None:
    service, intent, control = _binding_mismatch_recovery(
        kill_reason="operator_emergency_stop"
    )

    result = service.reconcile_in_progress(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "recovery_binding_mismatch"
    assert control.kill_reason == "operator_emergency_stop"
    assert control.safe_reason == "recovery_binding_mismatch"
    assert len(control.transition_commands) == 1


def test_live_auto_safe_latch_can_repeat_after_operator_recovery() -> None:
    service, intent, control = _binding_mismatch_recovery()

    first = service.reconcile_in_progress(intent)
    first_command_id = control.transition_commands[-1].command_id
    control.operator_recover_safe_mode()
    second = service.reconcile_in_progress(intent)
    second_command_id = control.transition_commands[-1].command_id

    assert first.error_code == "recovery_binding_mismatch"
    assert second.error_code == "recovery_binding_mismatch"
    assert control.safe_reason == "recovery_binding_mismatch"
    assert len(control.transition_commands) == 2
    assert first_command_id != second_command_id


def test_live_auto_safe_latch_retries_after_snapshot_cas_race() -> None:
    service, intent, control = _binding_mismatch_recovery(safety_conflicts=1)

    result = service.reconcile_in_progress(intent)

    assert result.error_code == "recovery_binding_mismatch"
    assert control.safe_reason == "recovery_binding_mismatch"
    assert len(control.transition_commands) == 2
    assert control.transition_commands[0].expected_version == 1
    assert control.transition_commands[1].expected_version == 2
    assert (
        control.transition_commands[0].command_id
        != control.transition_commands[1].command_id
    )


def test_live_auto_safe_latch_fails_closed_after_bounded_cas_conflicts() -> None:
    service, intent, control = _binding_mismatch_recovery(safety_conflicts=3)

    with pytest.raises(SafetyVersionConflict):
        service.reconcile_in_progress(intent)

    assert control.safe_reason is None
    assert len(control.transition_commands) == 3


def test_live_rejects_risk_policy_drift() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)

    with pytest.raises(ValueError, match="policy differs"):
        _live_service(
            broker,
            store,
            control,
            policy=RiskPolicy(max_open_positions=5),
            evaluator_policy=RiskPolicy(max_open_positions=6),
        )


def test_live_rejects_evaluator_that_cannot_prove_its_policy() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    policy = RiskPolicy()

    with pytest.raises(ValueError, match="policy differs"):
        ExecutionService(
            broker=broker,
            preflight=PreflightService(broker, MemoryFeed(), clock=lambda: NOW),
            risk=OpaqueRiskEvaluator(
                DeterministicRiskEvaluator(policy, clock=lambda: NOW)
            ),
            idempotency=store,
            expected_mode=ExecutionMode.LIVE,
            expected_broker_name="memory",
            expected_physical_account_id="memory-wallet-1",
            expected_market="binance_usdm",
            expected_account_scope="account-1",
            expected_tenant_scope="tenant-1",
            expected_user_scope="user-1",
            execution_control=FakeExecutionControl(store),
            risk_policy=policy,
            clock=lambda: NOW,
        )


def test_live_policy_bind_does_not_reset_failures_between_attempts() -> None:
    broker = LiveMemoryBroker(error=BrokerError("timeout"))
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    policy = RiskPolicy()
    service = _live_service(
        broker,
        store,
        control,
        policy=policy,
        failure_threshold=3,
    )

    results = [
        service.execute(replace(_intent(str(index)), mode=ExecutionMode.LIVE))
        for index in range(1, 4)
    ]
    blocked = service.execute(
        replace(_intent("after-threshold"), mode=ExecutionMode.LIVE)
    )

    assert [result.state for result in results] == [ExecutionState.UNKNOWN] * 3
    assert control.failure_calls == ["broker_timeout"] * 3
    assert control.consecutive_failures == 3
    assert control.safe_reason == "repeated_broker_timeout"
    assert control.bind_policy_calls == 4
    assert control.authorize_calls == 3
    assert blocked.state is ExecutionState.BLOCKED
    assert blocked.error_code == "repeated_broker_timeout"
    assert len(broker.market_orders) == 3


def test_live_policy_pin_mismatch_latches_and_fails_before_claim() -> None:
    broker = LiveMemoryBroker()
    store = InMemoryIdempotencyStore()
    control = FakeExecutionControl(store)
    first_policy = RiskPolicy(max_open_positions=5)
    second_policy = RiskPolicy(max_open_positions=6)
    first = replace(_intent("policy-1"), mode=ExecutionMode.LIVE)
    second = replace(_intent("policy-2"), mode=ExecutionMode.LIVE)

    first_result = _live_service(
        broker,
        store,
        control,
        policy=first_policy,
    ).execute(first)
    with pytest.raises(ExecutionControlStateError, match="policy mismatch"):
        _live_service(
            broker,
            store,
            control,
            policy=second_policy,
        ).execute(second)

    assert first_result.state is ExecutionState.FILLED
    assert control.kill_reason == "control_policy_mismatch"
    assert store.lookup(second) is None
    assert len(broker.market_orders) == 1
