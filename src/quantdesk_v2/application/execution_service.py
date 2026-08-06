"""Unified broker write path for backtest, paper, shadow and live adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from ..domain.execution import (
    ExecutionCheckpoint,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    Fill,
    IdempotencyClaimState,
    OrderIntent,
    RecoveryClaimState,
    execution_result_can_advance,
    execution_state_for_order,
)
from ..domain.safety import SafetyDecision
from ..domain.trading import (
    BrokerError,
    MarketOrder,
    OrderReference,
    OrderType,
    PositionSide,
    TriggerOrder,
)
from .execution_control import (
    ExecutionAccountScope,
    ExecutionAlreadyAuthorized,
    ExecutionControlPolicy,
    ExecutionControlStore,
    SafetyCommand,
    SafetyCommandAction,
    SafetyVersionConflict,
)
from .ports import Broker, IdempotencyStore, RiskEvaluator
from .risk import RiskPolicy
from .safety import ExecutionSafetyController, PreflightService


class ExecutionService:
    """Execute an intent exactly once through safety, preflight and risk gates.

    A durable live adapter can implement the same ports without changing this
    orchestration.  The legacy live worker is deliberately not wired here yet.
    """

    __slots__ = (
        "_broker",
        "_clock",
        "_control_policy",
        "_expected_account_scope",
        "_expected_broker_name",
        "_expected_market",
        "_expected_mode",
        "_expected_physical_account_id",
        "_expected_tenant_scope",
        "_expected_user_scope",
        "_execution_control",
        "_idempotency",
        "_preflight",
        "_risk",
        "_safety",
    )

    def __init__(
        self,
        *,
        broker: Broker,
        preflight: PreflightService,
        risk: RiskEvaluator,
        idempotency: IdempotencyStore,
        safety: ExecutionSafetyController | None = None,
        expected_mode: ExecutionMode,
        expected_broker_name: str,
        expected_physical_account_id: str,
        expected_market: str,
        expected_account_scope: str,
        expected_tenant_scope: str,
        expected_user_scope: str,
        execution_control: ExecutionControlStore | None = None,
        risk_policy: RiskPolicy | None = None,
        failure_threshold: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if broker.execution_mode is not expected_mode:
            raise ValueError("broker execution mode differs from service binding")
        normalized_broker_name = str(expected_broker_name).strip().lower()
        normalized_physical_account_id = str(expected_physical_account_id).strip()
        normalized_market = str(expected_market).strip().lower()
        normalized_account_scope = str(expected_account_scope).strip()
        normalized_tenant_scope = str(expected_tenant_scope).strip()
        normalized_user_scope = str(expected_user_scope).strip()
        if not all(
            (
                normalized_broker_name,
                normalized_physical_account_id,
                normalized_market,
                normalized_account_scope,
                normalized_tenant_scope,
                normalized_user_scope,
            )
        ):
            raise ValueError("expected execution bindings must not be empty")
        if broker.name.lower() != normalized_broker_name:
            raise ValueError("broker name differs from service binding")
        if broker.physical_account_id != normalized_physical_account_id:
            raise ValueError("broker physical account differs from service binding")
        if broker.market != normalized_market:
            raise ValueError("broker market differs from service binding")
        if broker.account_scope != normalized_account_scope:
            raise ValueError("broker account differs from service binding")
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or failure_threshold < 1
        ):
            raise ValueError("failure_threshold must be a positive integer")
        if risk_policy is not None and not isinstance(risk_policy, RiskPolicy):
            raise TypeError("risk_policy must be a RiskPolicy")
        if expected_mode is ExecutionMode.LIVE:
            if (
                execution_control is None
                or getattr(execution_control, "durable_for_live", False) is not True
            ):
                raise ValueError("LIVE execution requires a durable execution control store")
            if risk_policy is None:
                raise ValueError("LIVE execution requires an explicit risk policy")
            # A LIVE evaluator must expose the exact policy it applies; assuming
            # an opaque evaluator uses the pinned policy would make drift
            # undetectable at the broker-write boundary.
            exposed_policy = getattr(risk, "policy", None)
            if exposed_policy != risk_policy:
                raise ValueError(
                    "risk evaluator policy differs from durable control policy"
                )
        elif safety is None:
            raise ValueError("non-LIVE execution requires a process-local safety controller")
        self._broker = broker
        self._preflight = preflight
        self._risk = risk
        self._idempotency = idempotency
        self._safety = safety
        self._expected_mode = expected_mode
        self._expected_broker_name = normalized_broker_name
        self._expected_physical_account_id = normalized_physical_account_id
        self._expected_market = normalized_market
        self._expected_account_scope = normalized_account_scope
        self._expected_tenant_scope = normalized_tenant_scope
        self._expected_user_scope = normalized_user_scope
        self._execution_control = execution_control
        self._control_policy = ExecutionControlPolicy(
            risk=risk_policy or RiskPolicy(),
            preflight=preflight.policy,
            failure_threshold=failure_threshold,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        if intent.mode is not self._expected_mode:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_execution_mode_mismatch",
            )
        if intent.market != self._expected_market:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_market_mismatch",
            )
        if intent.account_scope != self._expected_account_scope:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_account_scope_mismatch",
            )
        if intent.tenant_scope != self._expected_tenant_scope:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_tenant_scope_mismatch",
            )
        if intent.user_scope != self._expected_user_scope:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_user_scope_mismatch",
            )
        self._bind_control_policy(intent)
        claim = self._idempotency.claim(intent)
        if claim.state is IdempotencyClaimState.COMPLETED:
            if claim.result is None:  # Defensive guard for third-party port implementations.
                raise RuntimeError("completed idempotency claim has no result")
            return claim.result
        if claim.state is IdempotencyClaimState.IN_PROGRESS:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="idempotency_in_progress",
            )
        claim_token = claim.claim_token
        if claim_token is None:
            raise RuntimeError("acquired idempotency claim has no owner token")

        now = self._clock()
        if now < intent.signal_time:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    error_code="signal_not_yet_valid",
                ),
                claim_token,
            )
        if now > intent.valid_until:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    error_code="intent_expired",
                ),
                claim_token,
            )

        safety = self._safety_decision(intent)
        if not safety.allowed:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    error_code=safety.reason_code,
                ),
                claim_token,
            )

        preflight = self._preflight.run(intent)
        if not preflight.report.ready:
            reason = preflight.report.reason_codes[0]
            self._record_failure(intent, reason)
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.PREFLIGHT_FAILED,
                    preflight=preflight.report,
                    error_code=reason,
                ),
                claim_token,
            )
        if preflight.context is None:
            raise RuntimeError("ready preflight outcome has no execution context")

        risk_decision = self._risk.evaluate(intent, preflight.context)
        if not risk_decision.approved:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.RISK_REJECTED,
                    preflight=preflight.report,
                    risk_decision=risk_decision,
                    error_code=risk_decision.reason_codes[0],
                ),
                claim_token,
            )

        if self._clock() > intent.valid_until:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    preflight=preflight.report,
                    risk_decision=risk_decision,
                    error_code="intent_expired_before_submit",
                ),
                claim_token,
            )

        # A kill switch or operator safe-mode command may arrive while preflight runs.
        final_safety = self._safety_decision(intent)
        if not final_safety.allowed:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    preflight=preflight.report,
                    risk_decision=risk_decision,
                    error_code=final_safety.reason_code,
                ),
                claim_token,
            )

        # Repeat mutable market/account readiness immediately before the durable
        # broker-write checkpoint. The first evaluation remains the slippage basis.
        initial_reference_price = risk_decision.reference_price
        submission_now = self._clock()
        if submission_now > intent.valid_until:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    preflight=preflight.report,
                    risk_decision=risk_decision,
                    error_code="intent_expired_before_submit",
                ),
                claim_token,
            )
        submission_preflight = self._preflight.run(intent)
        if not submission_preflight.report.ready or submission_preflight.context is None:
            reason = (
                submission_preflight.report.reason_codes[0]
                if submission_preflight.report.reason_codes
                else "submission_preflight_failed"
            )
            self._record_failure(intent, reason)
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.PREFLIGHT_FAILED,
                    preflight=submission_preflight.report,
                    error_code=reason,
                ),
                claim_token,
            )
        if intent.max_slippage_bps is not None and initial_reference_price is not None:
            slippage_bps = (
                abs(submission_preflight.context.quote.price - initial_reference_price)
                / initial_reference_price
                * Decimal("10000")
            )
            if slippage_bps > intent.max_slippage_bps:
                return self._complete(
                    ExecutionResult(
                        intent=intent,
                        state=ExecutionState.BLOCKED,
                        preflight=submission_preflight.report,
                        risk_decision=risk_decision,
                        error_code="max_slippage_exceeded",
                    ),
                    claim_token,
                )
        risk_decision = self._risk.evaluate(intent, submission_preflight.context)
        if not risk_decision.approved:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.RISK_REJECTED,
                    preflight=submission_preflight.report,
                    risk_decision=risk_decision,
                    error_code=risk_decision.reason_codes[0],
                ),
                claim_token,
            )

        if self._expected_mode is not ExecutionMode.LIVE:
            checkpoint_safety = self._safety_decision(intent)
            if not checkpoint_safety.allowed:
                return self._complete(
                    ExecutionResult(
                        intent=intent,
                        state=ExecutionState.BLOCKED,
                        preflight=submission_preflight.report,
                        risk_decision=risk_decision,
                        error_code=checkpoint_safety.reason_code,
                    ),
                    claim_token,
                )

        checkpointed_at = self._clock()
        if checkpointed_at > intent.valid_until:
            return self._complete(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.BLOCKED,
                    preflight=submission_preflight.report,
                    risk_decision=risk_decision,
                    error_code="intent_expired_before_checkpoint",
                ),
                claim_token,
            )
        client_order_id = deterministic_client_order_id(intent)
        checkpoint = ExecutionCheckpoint(
            intent=intent,
            preflight=submission_preflight.report,
            risk_decision=risk_decision,
            broker_name=self._expected_broker_name,
            physical_account_id=self._expected_physical_account_id,
            client_order_id=client_order_id,
            checkpointed_at=checkpointed_at,
        )
        control = self._live_control()
        if control is None:
            self._idempotency.save_checkpoint(intent, claim_token, checkpoint)
        else:
            authorization = control.authorize_and_checkpoint(
                intent,
                claim_token=claim_token,
                checkpoint=checkpoint,
                context=submission_preflight.context,
                policy=self._control_policy,
            )
            if not authorization.authorized:
                if authorization.reason_code == "already_authorized":
                    raise ExecutionAlreadyAuthorized(
                        "execution checkpoint is already authorized; recovery is required"
                    )
                return self._complete(
                    ExecutionResult(
                        intent=intent,
                        state=ExecutionState.BLOCKED,
                        preflight=submission_preflight.report,
                        risk_decision=risk_decision,
                        error_code=authorization.reason_code,
                    ),
                    claim_token,
                )
        try:
            order = self._submit(
                intent,
                risk_decision.approved_quantity,
                client_order_id,
            )
        except BrokerError as exc:
            code = f"broker_{exc.category}"[:64]
            self._record_failure(intent, code)
            return self._complete_broker_outcome(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.UNKNOWN,
                    preflight=submission_preflight.report,
                    risk_decision=risk_decision,
                    error_code=code,
                ),
                claim_token,
            )
        except Exception:
            # The port contract requires redacted BrokerError values.  An adapter
            # violation is still an unknown broker outcome and must not be retried.
            self._record_failure(intent, "broker_adapter_error")
            return self._complete_broker_outcome(
                ExecutionResult(
                    intent=intent,
                    state=ExecutionState.UNKNOWN,
                    preflight=submission_preflight.report,
                    risk_decision=risk_decision,
                    error_code="broker_adapter_error",
                ),
                claim_token,
            )

        result = self._result_from_order(
            intent,
            order,
            preflight=submission_preflight.report,
            risk_decision=risk_decision,
        )
        self._record_broker_result(result)
        return self._complete_broker_outcome(result, claim_token)

    def reconcile_unknown(self, intent: OrderIntent) -> ExecutionResult:
        """Advance a recoverable completed order by querying its broker identity."""

        scope_error = self._recovery_scope_error(intent)
        if scope_error is not None:
            return scope_error
        self._bind_control_policy(intent)
        existing = self._idempotency.lookup(intent)
        if existing is None:
            raise LookupError("execution intent has not been recorded")
        if existing.state not in {
            ExecutionState.UNKNOWN,
            ExecutionState.SUBMITTED,
            ExecutionState.PARTIALLY_FILLED,
        }:
            return existing
        checkpoint = self._idempotency.load_checkpoint(intent)
        if checkpoint is None:
            self._engage_safe_mode(intent, "missing_recovery_checkpoint")
            return existing
        binding_error = self._recovery_binding_error(intent, checkpoint)
        if binding_error is not None:
            return binding_error
        recovery = self._idempotency.claim_recovery(intent)
        if recovery.state is RecoveryClaimState.IN_PROGRESS:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                preflight=checkpoint.preflight,
                risk_decision=checkpoint.risk_decision,
                error_code="recovery_in_progress",
            )
        if recovery.state is RecoveryClaimState.NOT_RECOVERABLE:
            return self._idempotency.lookup(intent) or existing
        recovery_token = recovery.recovery_token
        if recovery_token is None:
            raise RuntimeError("acquired recovery claim has no owner token")
        try:
            order = self._broker.get_order(self._recovery_reference(intent, checkpoint))
        except Exception:
            self._record_failure(intent, "broker_reconciliation_failed")
            return existing
        result = self._result_from_order(
            intent,
            order,
            preflight=checkpoint.preflight,
            risk_decision=checkpoint.risk_decision,
        )
        if not execution_result_can_advance(existing, result):
            self._engage_safe_mode(intent, "recovery_state_regression")
            return existing
        self._record_broker_result(result)
        control = self._live_control()
        if control is None:
            self._idempotency.advance_recovered(intent, result, recovery_token)
        else:
            control.record_recovered_outcome(
                intent,
                checkpoint=checkpoint,
                result=result,
                recovery_token=recovery_token,
            )
        return result

    def reconcile_in_progress(self, intent: OrderIntent) -> ExecutionResult:
        """Recover a checkpointed broker write without ever resubmitting it."""

        scope_error = self._recovery_scope_error(intent)
        if scope_error is not None:
            return scope_error
        self._bind_control_policy(intent)
        completed = self._idempotency.lookup(intent)
        if completed is not None:
            if completed.state in {
                ExecutionState.UNKNOWN,
                ExecutionState.SUBMITTED,
                ExecutionState.PARTIALLY_FILLED,
            }:
                return self.reconcile_unknown(intent)
            return completed
        checkpoint = self._idempotency.load_checkpoint(intent)
        if checkpoint is None:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="recovery_checkpoint_unavailable",
            )
        binding_error = self._recovery_binding_error(intent, checkpoint)
        if binding_error is not None:
            return binding_error
        recovery = self._idempotency.claim_recovery(intent)
        if recovery.state is RecoveryClaimState.IN_PROGRESS:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                preflight=checkpoint.preflight,
                risk_decision=checkpoint.risk_decision,
                error_code="recovery_in_progress",
            )
        if recovery.state is RecoveryClaimState.NOT_RECOVERABLE:
            completed = self._idempotency.lookup(intent)
            if completed is not None:
                return completed
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                preflight=checkpoint.preflight,
                risk_decision=checkpoint.risk_decision,
                error_code="recovery_not_available",
            )
        recovery_token = recovery.recovery_token
        if recovery_token is None:
            raise RuntimeError("acquired recovery claim has no owner token")
        try:
            order = self._broker.get_order(self._recovery_reference(intent, checkpoint))
        except Exception:
            # Absence and transient failures are deliberately indistinguishable
            # here: neither authorizes another broker write.
            self._record_failure(intent, "broker_reconciliation_pending")
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                preflight=checkpoint.preflight,
                risk_decision=checkpoint.risk_decision,
                error_code="broker_reconciliation_pending",
            )
        result = self._result_from_order(
            intent,
            order,
            preflight=checkpoint.preflight,
            risk_decision=checkpoint.risk_decision,
        )
        self._record_broker_result(result)
        control = self._live_control()
        if control is None:
            self._idempotency.complete_recovered(
                intent,
                checkpoint,
                result,
                recovery_token,
            )
        else:
            control.record_recovered_outcome(
                intent,
                checkpoint=checkpoint,
                result=result,
                recovery_token=recovery_token,
            )
        return result

    def _recovery_scope_error(self, intent: OrderIntent) -> ExecutionResult | None:
        if intent.mode is not self._expected_mode:
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="broker_execution_mode_mismatch",
            )
        if (
            intent.market != self._expected_market
            or intent.account_scope != self._expected_account_scope
            or intent.tenant_scope != self._expected_tenant_scope
            or intent.user_scope != self._expected_user_scope
        ):
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                error_code="recovery_broker_scope_mismatch",
            )
        return None

    def _recovery_binding_error(
        self,
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint,
    ) -> ExecutionResult | None:
        if (
            checkpoint.intent != intent
            or checkpoint.intent.mode is not self._expected_mode
            or checkpoint.broker_name != self._expected_broker_name
            or checkpoint.physical_account_id != self._expected_physical_account_id
            or checkpoint.intent.market != self._broker.market
            or checkpoint.intent.account_scope != self._broker.account_scope
            or checkpoint.client_order_id != deterministic_client_order_id(intent)
        ):
            self._engage_safe_mode(intent, "recovery_binding_mismatch")
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.BLOCKED,
                preflight=checkpoint.preflight,
                risk_decision=checkpoint.risk_decision,
                error_code="recovery_binding_mismatch",
            )
        return None

    @staticmethod
    def _recovery_reference(
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint,
    ) -> OrderReference:
        return OrderReference(
            client_order_id=checkpoint.client_order_id,
            symbol=intent.symbol,
            conditional=intent.order_type is not OrderType.MARKET,
        )

    def _submit(
        self,
        intent: OrderIntent,
        quantity: Decimal | None,
        client_order_id: str,
    ):
        if quantity is None:
            raise ValueError("approved risk decision has no quantity")
        if intent.order_type is OrderType.MARKET:
            return self._broker.place_market_order(
                MarketOrder(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=quantity,
                    client_order_id=client_order_id,
                    position_side=intent.position_side,
                    reduce_only=intent.reduce_only,
                )
            )
        if intent.trigger_price is None:
            raise ValueError("trigger order intent has no trigger price")
        return self._broker.place_trigger_order(
            TriggerOrder(
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                trigger_price=intent.trigger_price,
                client_order_id=client_order_id,
                position_side=intent.position_side,
                quantity=(
                    None
                    if intent.position_side is PositionSide.BOTH
                    else quantity
                ),
            )
        )

    def _fills(self, intent: OrderIntent, order) -> tuple[Fill, ...]:
        if order.executed_quantity <= 0 or order.average_price is None:
            return ()
        return (
            Fill(
                fill_id=f"fill:{order.reference.client_order_id}",
                client_order_id=order.reference.client_order_id,
                exchange_order_id=order.exchange_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.executed_quantity,
                price=order.average_price,
                occurred_at=self._clock(),
                simulated=self._broker.execution_mode is not ExecutionMode.LIVE,
            ),
        )

    def _result_from_order(self, intent, order, *, preflight, risk_decision):
        if not _order_matches_intent(intent, order, risk_decision):
            return ExecutionResult(
                intent=intent,
                state=ExecutionState.UNKNOWN,
                preflight=preflight,
                risk_decision=risk_decision,
                broker_order=order,
                error_code="broker_order_mismatch",
            )
        state = execution_state_for_order(order)
        fills = self._fills(intent, order)
        error_code = None
        if state is ExecutionState.FILLED and not fills:
            state = ExecutionState.UNKNOWN
            error_code = "filled_order_missing_fill_data"
        elif state is ExecutionState.UNKNOWN:
            error_code = "unknown_broker_status"
        elif state is ExecutionState.BROKER_REJECTED:
            error_code = "broker_rejected"
        elif fills and _slippage_limit_breached(intent, risk_decision.reference_price, fills):
            error_code = "slippage_limit_breached"
        return ExecutionResult(
            intent=intent,
            state=state,
            preflight=preflight,
            risk_decision=risk_decision,
            broker_order=order,
            fills=fills,
            error_code=error_code,
        )

    def _record_broker_result(self, result: ExecutionResult) -> None:
        if result.error_code == "slippage_limit_breached":
            self._engage_safe_mode(result.intent, result.error_code)
        elif result.state in {ExecutionState.UNKNOWN, ExecutionState.BROKER_REJECTED}:
            self._record_failure(result.intent, result.error_code or "broker_failure")
        else:
            self._record_success(result.intent)

    def _live_control(self) -> ExecutionControlStore | None:
        if self._expected_mode is not ExecutionMode.LIVE:
            return None
        control = self._execution_control
        if control is None:  # Constructor invariant; keep the runtime fail closed.
            raise RuntimeError("LIVE execution control store is unavailable")
        return control

    def _bind_control_policy(self, intent: OrderIntent) -> None:
        """Pin the live control policy without mutating failure counters.

        Policy binding is deliberately separate from ``record_success``: a
        retrying deployment must not clear accumulated failures merely by
        starting another execution attempt.
        """

        control = self._live_control()
        if control is not None:
            control.bind_policy(self._control_scope(intent), self._control_policy)

    def _control_scope(self, intent: OrderIntent) -> ExecutionAccountScope:
        return ExecutionAccountScope.from_intent(
            intent,
            broker_name=self._expected_broker_name,
            physical_account_id=self._expected_physical_account_id,
        )

    def _safety_decision(self, intent: OrderIntent) -> SafetyDecision:
        control = self._live_control()
        if control is not None:
            return control.safety_snapshot(self._control_scope(intent)).decision(intent)
        safety = self._safety
        if safety is None:  # Constructor invariant.
            raise RuntimeError("process-local safety controller is unavailable")
        return safety.decision(intent)

    def _record_failure(self, intent: OrderIntent, reason_code: str) -> None:
        control = self._live_control()
        if control is not None:
            control.record_failure(
                self._control_scope(intent),
                reason_code,
                policy=self._control_policy,
            )
            return
        safety = self._safety
        if safety is None:  # Constructor invariant.
            raise RuntimeError("process-local safety controller is unavailable")
        safety.record_failure(reason_code)

    def _record_success(self, intent: OrderIntent) -> None:
        control = self._live_control()
        if control is not None:
            control.record_success(
                self._control_scope(intent),
                policy=self._control_policy,
            )
            return
        safety = self._safety
        if safety is None:  # Constructor invariant.
            raise RuntimeError("process-local safety controller is unavailable")
        safety.record_success()

    def _engage_safe_mode(self, intent: OrderIntent, reason_code: str) -> None:
        control = self._live_control()
        if control is not None:
            scope = self._control_scope(intent)
            last_conflict: SafetyVersionConflict | None = None
            for _ in range(3):
                snapshot = control.safety_snapshot(scope)
                if snapshot.safe_reason is not None:
                    return
                command_digest = hashlib.sha256(
                    "\x1f".join(
                        (*intent.idempotency_scope, reason_code, str(snapshot.version))
                    ).encode("utf-8")
                ).hexdigest()[:48]
                try:
                    control.transition_safety(
                        scope,
                        SafetyCommand(
                            command_id=f"auto:{command_digest}",
                            action=SafetyCommandAction.ENGAGE_SAFE_MODE,
                            actor="system:execution_service",
                            expected_version=snapshot.version,
                            reason_code=reason_code,
                        ),
                    )
                except SafetyVersionConflict as exc:
                    last_conflict = exc
                    continue
                return
            if last_conflict is None:  # pragma: no cover - loop invariant
                raise RuntimeError("durable safe-mode transition did not run")
            raise last_conflict
        safety = self._safety
        if safety is None:  # Constructor invariant.
            raise RuntimeError("process-local safety controller is unavailable")
        safety.engage_safe_mode(reason_code)

    def _complete_broker_outcome(
        self,
        result: ExecutionResult,
        claim_token: str,
    ) -> ExecutionResult:
        control = self._live_control()
        if control is None:
            return self._complete(result, claim_token)
        control.record_broker_outcome(
            result.intent,
            result=result,
            claim_token=claim_token,
        )
        return result

    def _complete(self, result: ExecutionResult, claim_token: str) -> ExecutionResult:
        self._idempotency.complete(result.intent, result, claim_token)
        return result


def deterministic_client_order_id(intent: OrderIntent) -> str:
    """Return the same exchange-safe client id for every retry of an intent."""

    scoped_key = "\x1f".join(intent.idempotency_scope)
    digest = hashlib.sha256(scoped_key.encode("utf-8")).hexdigest()[:32]
    return f"qd-{digest}"


def _order_matches_intent(intent: OrderIntent, order, risk_decision) -> bool:
    conditional = intent.order_type is not OrderType.MARKET
    approved_quantity = risk_decision.approved_quantity
    base_match = (
        order.reference.client_order_id == deterministic_client_order_id(intent)
        and order.reference.conditional is conditional
        and order.symbol == intent.symbol
        and order.side is intent.side
        and order.position_side is intent.position_side
        and order.order_type is intent.order_type
    )
    if not base_match or approved_quantity is None:
        return False
    if not conditional:
        return (
            order.quantity == approved_quantity
            and order.trigger_price is None
            and order.reduce_only is intent.reduce_only
        )
    expected_quantity = (
        Decimal(0) if intent.position_side is PositionSide.BOTH else approved_quantity
    )
    return (
        order.quantity == expected_quantity
        and order.trigger_price == intent.trigger_price
        and not order.reduce_only
    )


def _slippage_limit_breached(
    intent: OrderIntent,
    reference_price: Decimal | None,
    fills: tuple[Fill, ...],
) -> bool:
    if intent.max_slippage_bps is None or reference_price is None:
        return False
    if intent.side.value == "BUY":
        worst_price = max(fill.price for fill in fills)
        unfavorable = worst_price - reference_price
    else:
        worst_price = min(fill.price for fill in fills)
        unfavorable = reference_price - worst_price
    if unfavorable <= 0:
        return False
    actual_bps = unfavorable / reference_price * Decimal("10000")
    return actual_bps > intent.max_slippage_bps
