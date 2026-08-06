"""MySQL-backed cross-process execution safety and pending-risk controls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from ...application.execution_control import (
    AuthorizationResult,
    ExecutionAccountScope,
    ExecutionAlreadyAuthorized,
    ExecutionControlIntegrityError,
    ExecutionControlPolicy,
    ExecutionControlSnapshot,
    ExecutionControlStateError,
    ReflectedRiskEvidence,
    RiskReservation,
    RiskReservationState,
    SafetyCommand,
    SafetyCommandAction,
    SafetyVersionConflict,
    account_snapshot_fingerprint,
    execution_account_scope_hash,
    normalize_reason_code,
    reservation_state_for_result,
)
from ...domain.execution import (
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionResult,
    ExecutionState,
    IntentAction,
    OrderIntent,
    RiskDecision,
    execution_result_can_advance,
)
from ...domain.trading import (
    AccountSnapshot,
    OrderSide,
    PositionDirection,
    PositionMode,
    PositionSide,
    Quote,
)
from .executions import (
    deserialize_execution_checkpoint,
    deserialize_execution_result,
    deserialize_order_intent,
    execution_scope_hash,
    serialize_execution_checkpoint,
    serialize_execution_result,
    serialize_order_intent,
)

_ACTIVE_RESERVATION_STATES = (
    RiskReservationState.HELD.value,
    RiskReservationState.COMMITTED_UNREFLECTED.value,
)

_SELECT_CONTROL = """
SELECT control_hash, tenant_scope, user_scope, account_scope, market,
       broker_name, physical_account_id, execution_mode, safe_reason, kill_reason,
       consecutive_failures, version, changed_at, changed_by,
       control_policy_hash, failure_threshold,
       risk_snapshot_high_watermark_at,
       risk_snapshot_source_updated_at,
       risk_snapshot_high_watermark_recorded_at,
       risk_snapshot_high_watermark_hash,
       risk_snapshot_high_watermark_reference
FROM execution_account_controls
WHERE control_hash = :control_hash
"""

_SELECT_EXECUTION_FOR_UPDATE = text(
    """
    SELECT scope_hash, tenant_scope, user_scope, account_scope, deployment_scope,
           idempotency_key, intent_id, intent_hash, intent_json, claim_status,
           claim_token, lease_expires_at,
           (lease_expires_at > UTC_TIMESTAMP(6)) AS lease_valid,
           recovery_token, recovery_lease_expires_at, recovery_started_at,
           (recovery_lease_expires_at > UTC_TIMESTAMP(6)) AS recovery_lease_valid,
           checkpoint_hash, checkpoint_json, broker_name, physical_account_id,
           client_order_id,
           execution_state, result_hash, result_json, updated_at AS execution_updated_at
    FROM execution_idempotency_records
    WHERE scope_hash = :scope_hash
    FOR UPDATE
    """
)

_SELECT_RESERVATION = """
SELECT reservation_id, account_control_hash, execution_scope_hash, intent_id,
       state, reserved_notional, reserved_open_slots, policy_hash,
       risk_decision_hash, snapshot_hash, safety_version, client_order_id,
       position_key_hash,
       symbol, position_mode, target_direction, target_position_side,
       baseline_direction, baseline_position_side, baseline_quantity,
       authorized_quantity, risk_reducing,
       created_at, updated_at, settled_at, release_reason,
       settlement_snapshot_hash, settlement_observed_at, settlement_reference
FROM execution_risk_reservations
WHERE execution_scope_hash = :execution_scope_hash
"""

_SELECT_EXECUTION_IDENTITY = text(
    """
    SELECT scope_hash, tenant_scope, user_scope, account_scope, deployment_scope,
           idempotency_key, intent_id, intent_hash, intent_json,
           checkpoint_hash, checkpoint_json, broker_name, physical_account_id,
           client_order_id
    FROM execution_idempotency_records
    WHERE scope_hash = :scope_hash
    """
)


@dataclass(frozen=True, slots=True)
class _PositionBaseline:
    position_mode: PositionMode
    target_direction: PositionDirection
    target_position_side: PositionSide
    baseline_direction: PositionDirection | None
    baseline_position_side: PositionSide | None
    baseline_quantity: Decimal
    authorized_quantity: Decimal


class MySqlExecutionControlStore:
    """One account mutex for durable safety, risk reservation, and checkpointing."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RuntimeError("execution controls require MySQL or MariaDB")
        self._engine = engine

    @property
    def durable_for_live(self) -> bool:
        return True

    def _authorized_scope(
        self,
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint | None = None,
    ) -> ExecutionAccountScope:
        execution_hash = execution_scope_hash(intent)
        with self._engine.connect() as connection:
            selected = connection.execute(
                _SELECT_EXECUTION_IDENTITY,
                {"scope_hash": execution_hash},
            ).mappings().one_or_none()
        if selected is None:
            raise ExecutionControlStateError("execution claim does not exist")
        row = dict(selected)
        _verify_execution_identity(row, intent)
        stored_checkpoint = _verified_checkpoint(row, intent)
        if checkpoint is not None and stored_checkpoint != checkpoint:
            raise ExecutionControlIntegrityError(
                "recovery checkpoint differs from the authorized checkpoint"
            )
        return ExecutionAccountScope.from_intent(
            intent,
            broker_name=stored_checkpoint.broker_name,
            physical_account_id=stored_checkpoint.physical_account_id,
        )

    def safety_snapshot(self, scope: ExecutionAccountScope) -> ExecutionControlSnapshot:
        _require_scope(scope)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            return _control_snapshot(row, scope)

    def bind_policy(
        self,
        scope: ExecutionAccountScope,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        _require_scope(scope)
        _require_control_policy(policy)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            row, matches = _bind_or_match_control_policy(connection, row, policy)
            snapshot = _control_snapshot(row, scope)
        if not matches:
            raise ExecutionControlStateError("execution control policy mismatch")
        return snapshot

    def observe_account_snapshot(
        self,
        scope: ExecutionAccountScope,
        snapshot: AccountSnapshot,
        reference: str,
    ) -> ExecutionControlSnapshot:
        _require_scope(scope)
        if not isinstance(snapshot, AccountSnapshot):
            raise TypeError("snapshot must be an AccountSnapshot")
        normalized_reference = _snapshot_reference(reference)
        snapshot_hash = account_snapshot_fingerprint(snapshot)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            now = _database_now(connection)
            if (snapshot.observed_at.astimezone(UTC) - now).total_seconds() > 5:
                raise ExecutionControlStateError("account snapshot is in the future")
            rejection = _risk_watermark_rejection(
                row,
                snapshot.updated_at,
                snapshot.observed_at,
                snapshot_hash,
            )
            if rejection is not None:
                raise ExecutionControlStateError(rejection)
            row = _advance_risk_watermark(
                connection,
                row,
                source_updated_at=snapshot.updated_at,
                observed_at=snapshot.observed_at,
                snapshot_hash=snapshot_hash,
                reference=normalized_reference,
            )
            return _control_snapshot(row, scope)

    def transition_safety(
        self,
        scope: ExecutionAccountScope,
        command: SafetyCommand,
    ) -> ExecutionControlSnapshot:
        _require_scope(scope)
        if not isinstance(command, SafetyCommand):
            raise TypeError("command must be a SafetyCommand")
        control_hash = execution_account_scope_hash(scope)
        command_hash = _safety_command_hash(command)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            replay = connection.execute(
                text(
                    """
                    SELECT command_hash, result_safe_reason, result_kill_reason,
                           result_consecutive_failures, resulting_version,
                           result_control_policy_hash,
                           result_failure_threshold,
                           result_risk_snapshot_high_watermark_at,
                           result_risk_snapshot_high_watermark_hash,
                           result_risk_snapshot_high_watermark_reference,
                           result_changed_at, actor
                    FROM execution_safety_events
                    WHERE account_control_hash = :control_hash
                      AND command_id = :command_id
                    """
                ),
                {"control_hash": control_hash, "command_id": command.command_id},
            ).mappings().one_or_none()
            if replay is not None:
                replay_row = dict(replay)
                if str(replay_row["command_hash"]) != command_hash:
                    raise ExecutionControlIntegrityError(
                        "safety command_id is bound to different command data"
                    )
                return _control_snapshot_from_event(replay_row, scope)

            current_version = int(row["version"])
            if current_version != command.expected_version:
                raise SafetyVersionConflict(
                    f"expected safety version {command.expected_version}, "
                    f"found {current_version}"
                )

            safe_reason = _optional_string(row.get("safe_reason"))
            kill_reason = _optional_string(row.get("kill_reason"))
            failures = int(row["consecutive_failures"])
            if command.action is SafetyCommandAction.ENGAGE_SAFE_MODE:
                safe_reason = command.reason_code
            elif command.action is SafetyCommandAction.RECOVER_SAFE_MODE:
                safe_reason = None
                failures = 0
            elif command.action is SafetyCommandAction.ENGAGE_KILL_SWITCH:
                kill_reason = command.reason_code
            elif command.action is SafetyCommandAction.RELEASE_KILL_SWITCH:
                kill_reason = None
            else:  # pragma: no cover - enum validation makes this unreachable
                raise ExecutionControlStateError("unsupported safety command")

            next_version = current_version + 1
            updated = connection.execute(
                text(
                    """
                    UPDATE execution_account_controls
                    SET safe_reason = :safe_reason,
                        kill_reason = :kill_reason,
                        consecutive_failures = :failures,
                        version = :next_version,
                        changed_at = UTC_TIMESTAMP(6),
                        changed_by = :actor,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE control_hash = :control_hash
                      AND version = :expected_version
                    """
                ),
                {
                    "safe_reason": safe_reason,
                    "kill_reason": kill_reason,
                    "failures": failures,
                    "next_version": next_version,
                    "actor": command.actor,
                    "control_hash": control_hash,
                    "expected_version": current_version,
                },
            )
            if updated.rowcount != 1:
                raise SafetyVersionConflict("safety state changed concurrently")
            result_row = _locked_control(connection, control_hash)
            result = _control_snapshot(result_row, scope)
            connection.execute(
                text(
                    """
                    INSERT INTO execution_safety_events (
                        event_id, account_control_hash, command_id, command_hash,
                        action, actor, reason_code, expected_version,
                        resulting_version, result_safe_reason, result_kill_reason,
                        result_consecutive_failures,
                        result_control_policy_hash, result_failure_threshold,
                        result_risk_snapshot_high_watermark_at,
                        result_risk_snapshot_high_watermark_hash,
                        result_risk_snapshot_high_watermark_reference,
                        result_changed_at, created_at
                    ) VALUES (
                        :event_id, :control_hash, :command_id, :command_hash,
                        :action, :actor, :reason_code, :expected_version,
                        :resulting_version, :safe_reason, :kill_reason,
                        :failures, :control_policy_hash, :failure_threshold,
                        :risk_watermark_at, :risk_watermark_hash,
                        :risk_watermark_reference, :changed_at, UTC_TIMESTAMP(6)
                    )
                    """
                ),
                {
                    "event_id": _sha256(
                        _canonical_json([control_hash, command.command_id])
                    ),
                    "control_hash": control_hash,
                    "command_id": command.command_id,
                    "command_hash": command_hash,
                    "action": command.action.value,
                    "actor": command.actor,
                    "reason_code": command.reason_code,
                    "expected_version": command.expected_version,
                    "resulting_version": result.version,
                    "safe_reason": result.safe_reason,
                    "kill_reason": result.kill_reason,
                    "failures": result.consecutive_failures,
                    "control_policy_hash": result.control_policy_hash,
                    "failure_threshold": result.failure_threshold,
                    "risk_watermark_at": (
                        None
                        if result.risk_snapshot_high_watermark_at is None
                        else _naive_utc(result.risk_snapshot_high_watermark_at)
                    ),
                    "risk_watermark_hash": result.risk_snapshot_high_watermark_hash,
                    "risk_watermark_reference": (
                        result.risk_snapshot_high_watermark_reference
                    ),
                    "changed_at": _naive_utc(result.changed_at),
                },
            )
            return result

    def record_failure(
        self,
        scope: ExecutionAccountScope,
        reason_code: str,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        _require_scope(scope)
        _require_control_policy(policy)
        reason = normalize_reason_code(reason_code)
        control_hash = execution_account_scope_hash(scope)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            row, policy_matches = _bind_or_match_control_policy(
                connection,
                row,
                policy,
            )
            if not policy_matches:
                snapshot = _control_snapshot(row, scope)
            else:
                failures = int(row["consecutive_failures"]) + 1
                safe_reason = _optional_string(row.get("safe_reason"))
                if safe_reason is None and failures >= int(row["failure_threshold"]):
                    safe_reason = f"repeated_{reason}"[:64]
                connection.execute(
                    text(
                        """
                        UPDATE execution_account_controls
                        SET safe_reason = :safe_reason,
                            consecutive_failures = :failures,
                            version = version + 1,
                            changed_at = UTC_TIMESTAMP(6),
                            changed_by = 'system:failure',
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE control_hash = :control_hash
                        """
                    ),
                    {
                        "safe_reason": safe_reason,
                        "failures": failures,
                        "control_hash": control_hash,
                    },
                )
                snapshot = _control_snapshot(
                    _locked_control(connection, control_hash), scope
                )
        if not policy_matches:
            raise ExecutionControlStateError("execution control policy mismatch")
        return snapshot

    def record_success(
        self,
        scope: ExecutionAccountScope,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot:
        _require_scope(scope)
        _require_control_policy(policy)
        control_hash = execution_account_scope_hash(scope)
        with self._engine.begin() as connection:
            row = _ensure_and_lock_control(connection, scope)
            row, policy_matches = _bind_or_match_control_policy(
                connection,
                row,
                policy,
            )
            if not policy_matches:
                snapshot = _control_snapshot(row, scope)
            elif (
                int(row["consecutive_failures"]) == 0
                or row.get("safe_reason") is not None
                or row.get("kill_reason") is not None
            ):
                snapshot = _control_snapshot(row, scope)
            else:
                connection.execute(
                    text(
                        """
                        UPDATE execution_account_controls
                        SET consecutive_failures = 0,
                            version = version + 1,
                            changed_at = UTC_TIMESTAMP(6),
                            changed_by = 'system:success',
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE control_hash = :control_hash
                        """
                    ),
                    {"control_hash": control_hash},
                )
                snapshot = _control_snapshot(
                    _locked_control(connection, control_hash), scope
                )
        if not policy_matches:
            raise ExecutionControlStateError("execution control policy mismatch")
        return snapshot

    def authorize_and_checkpoint(
        self,
        intent: OrderIntent,
        *,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
        policy: ExecutionControlPolicy,
    ) -> AuthorizationResult:
        _require_authorization_inputs(
            intent,
            claim_token,
            checkpoint,
            context,
            policy,
        )
        scope = ExecutionAccountScope.from_intent(
            intent,
            broker_name=checkpoint.broker_name,
            physical_account_id=checkpoint.physical_account_id,
        )
        control_hash = execution_account_scope_hash(scope)
        execution_hash = execution_scope_hash(intent)
        checkpoint_json = serialize_execution_checkpoint(checkpoint)
        checkpoint_hash = _sha256(checkpoint_json)
        decision_hash = _risk_decision_hash(checkpoint.risk_decision)
        policy_hash = policy.fingerprint
        snapshot_hash = _context_hash(context)
        account_snapshot_hash = account_snapshot_fingerprint(context.account)

        self.bind_policy(scope, policy)

        with self._engine.begin() as connection:
            control_row = _ensure_and_lock_control(connection, scope)
            _require_matching_control_policy(control_row, policy)
            execution_row = _locked_execution(connection, execution_hash)
            _verify_execution_identity(execution_row, intent)
            existing = _locked_reservation(connection, execution_hash)
            if existing is not None:
                stored_checkpoint = _verified_checkpoint(execution_row, intent)
                if stored_checkpoint != checkpoint:
                    raise ExecutionControlIntegrityError(
                        "existing checkpoint differs from authorization data"
                    )
                _verify_existing_authorization(
                    execution_row,
                    existing,
                    checkpoint_json=checkpoint_json,
                    checkpoint_hash=checkpoint_hash,
                    policy_hash=policy_hash,
                    decision_hash=decision_hash,
                    snapshot_hash=snapshot_hash,
                    control_hash=control_hash,
                )
                raise ExecutionAlreadyAuthorized(
                    "execution checkpoint is already authorized; reconcile it instead"
                )

            if execution_row.get("checkpoint_json") is not None:
                raise ExecutionControlIntegrityError(
                    "checkpoint exists without its atomic risk reservation"
                )
            _require_live_claim(execution_row, claim_token)
            if (
                context.quote.symbol != intent.symbol
                or context.rules.symbol != intent.symbol
            ):
                raise ExecutionControlIntegrityError(
                    "execution context symbol differs from intent"
                )
            now = _database_now(connection)
            account_rejection = _account_snapshot_time_rejection(
                context.account,
                policy,
                now,
            )
            quote_rejection = _freshness_rejection(
                "quote_freshness",
                context.quote.observed_at,
                now,
                policy.preflight.max_quote_age_seconds,
                policy.preflight.max_future_skew_seconds,
            )
            if account_rejection is None:
                account_rejection = _risk_watermark_rejection(
                    control_row,
                    context.account.updated_at,
                    context.account.observed_at,
                    account_snapshot_hash,
                )
                if account_rejection is None:
                    control_row = _advance_risk_watermark(
                        connection,
                        control_row,
                        source_updated_at=context.account.updated_at,
                        observed_at=context.account.observed_at,
                        snapshot_hash=account_snapshot_hash,
                        reference=f"authorization:{execution_hash}",
                    )
            if quote_rejection is None:
                quote_rejection = _observe_quote_watermark(
                    connection,
                    control_hash=control_hash,
                    quote=context.quote,
                    reference=f"authorization:{execution_hash}",
                )
            safety = _control_snapshot(control_row, scope)
            fact_rejection = account_rejection or quote_rejection
            if fact_rejection is not None:
                return AuthorizationResult(False, fact_rejection, safety)

            temporal_rejection = _authorization_business_time_rejection(
                intent,
                checkpoint,
                policy,
                now,
            )
            if temporal_rejection is not None:
                return AuthorizationResult(False, temporal_rejection, safety)
            safety_decision = safety.decision(intent)
            if not safety_decision.allowed:
                return AuthorizationResult(False, safety_decision.reason_code, safety)

            rejection, reserved_notional, reserved_slots, baseline = _budget_requirements(
                connection,
                control_hash=control_hash,
                intent=intent,
                checkpoint=checkpoint,
                context=context,
                policy=policy,
            )
            if rejection is not None:
                return AuthorizationResult(False, rejection, safety)
            if baseline is None:  # pragma: no cover - approved budget invariant
                raise ExecutionControlIntegrityError("position baseline is unavailable")

            connection.execute(
                text(
                    """
                    INSERT INTO execution_risk_reservations (
                        reservation_id, account_control_hash, execution_scope_hash,
                        intent_id, state, reserved_notional, reserved_open_slots,
                        policy_hash, risk_decision_hash, snapshot_hash,
                        safety_version, client_order_id, position_key_hash,
                        symbol, position_mode,
                        target_direction, target_position_side,
                        baseline_direction, baseline_position_side,
                        baseline_quantity, authorized_quantity, risk_reducing,
                        created_at, updated_at
                    ) VALUES (
                        :reservation_id, :control_hash, :execution_scope_hash,
                        :intent_id, 'held', :reserved_notional, :reserved_slots,
                        :policy_hash, :risk_decision_hash, :snapshot_hash,
                        :safety_version, :client_order_id, :position_key_hash,
                        :symbol, :position_mode,
                        :target_direction, :target_position_side,
                        :baseline_direction, :baseline_position_side,
                        :baseline_quantity, :authorized_quantity, :risk_reducing,
                        UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                    )
                    """
                ),
                {
                    "reservation_id": execution_hash,
                    "control_hash": control_hash,
                    "execution_scope_hash": execution_hash,
                    "intent_id": intent.intent_id,
                    "reserved_notional": reserved_notional,
                    "reserved_slots": reserved_slots,
                    "policy_hash": policy_hash,
                    "risk_decision_hash": decision_hash,
                    "snapshot_hash": snapshot_hash,
                    "safety_version": safety.version,
                    "client_order_id": checkpoint.client_order_id,
                    "position_key_hash": _position_key_hash(
                        control_hash,
                        baseline.position_mode,
                        intent.symbol,
                        baseline.target_position_side,
                    ),
                    "symbol": intent.symbol,
                    "position_mode": baseline.position_mode.value,
                    "target_direction": baseline.target_direction.value,
                    "target_position_side": baseline.target_position_side.value,
                    "baseline_direction": (
                        None
                        if baseline.baseline_direction is None
                        else baseline.baseline_direction.value
                    ),
                    "baseline_position_side": (
                        None
                        if baseline.baseline_position_side is None
                        else baseline.baseline_position_side.value
                    ),
                    "baseline_quantity": baseline.baseline_quantity,
                    "authorized_quantity": baseline.authorized_quantity,
                    "risk_reducing": intent.risk_reducing,
                },
            )
            updated = connection.execute(
                text(
                    """
                    UPDATE execution_idempotency_records
                    SET checkpoint_hash = :checkpoint_hash,
                        checkpoint_json = :checkpoint_json,
                        broker_name = :broker_name,
                        physical_account_id = :physical_account_id,
                        client_order_id = :client_order_id,
                        checkpointed_at = UTC_TIMESTAMP(6),
                        lease_expires_at = UTC_TIMESTAMP(6),
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE scope_hash = :scope_hash
                      AND claim_status = 'in_progress'
                      AND claim_token = :claim_token
                      AND lease_expires_at > UTC_TIMESTAMP(6)
                      AND recovery_started_at IS NULL
                      AND checkpoint_json IS NULL
                    """
                ),
                {
                    "checkpoint_hash": checkpoint_hash,
                    "checkpoint_json": checkpoint_json,
                    "broker_name": checkpoint.broker_name,
                    "physical_account_id": checkpoint.physical_account_id,
                    "client_order_id": checkpoint.client_order_id,
                    "scope_hash": execution_hash,
                    "claim_token": claim_token,
                },
            )
            if updated.rowcount != 1:
                raise ExecutionControlStateError(
                    "execution claim changed before atomic checkpoint authorization"
                )
            reservation_row = _locked_reservation(connection, execution_hash)
            if reservation_row is None:  # pragma: no cover - insert invariant
                raise ExecutionControlIntegrityError("risk reservation disappeared")
            return AuthorizationResult(
                True,
                "authorized",
                safety,
                _reservation_from_row(reservation_row),
            )

    def record_broker_outcome(
        self,
        intent: OrderIntent,
        *,
        result: ExecutionResult,
        claim_token: str,
    ) -> RiskReservation:
        return self._record_outcome(
            intent,
            result=result,
            checkpoint=None,
            owner_token=claim_token,
            recovered=False,
        )

    def record_recovered_outcome(
        self,
        intent: OrderIntent,
        *,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> RiskReservation:
        if not isinstance(checkpoint, ExecutionCheckpoint) or checkpoint.intent != intent:
            raise ValueError("execution checkpoint does not match intent")
        return self._record_outcome(
            intent,
            result=result,
            checkpoint=checkpoint,
            owner_token=recovery_token,
            recovered=True,
        )

    def _record_outcome(
        self,
        intent: OrderIntent,
        *,
        result: ExecutionResult,
        checkpoint: ExecutionCheckpoint | None,
        owner_token: str,
        recovered: bool,
    ) -> RiskReservation:
        _require_result(intent, result)
        if not owner_token or len(owner_token) > 64:
            raise ValueError("execution owner token is invalid")
        desired_state = reservation_state_for_result(result)
        scope = (
            ExecutionAccountScope.from_intent(
                intent,
                broker_name=checkpoint.broker_name,
                physical_account_id=checkpoint.physical_account_id,
            )
            if checkpoint is not None
            else self._authorized_scope(intent)
        )
        execution_hash = execution_scope_hash(intent)
        result_json = serialize_execution_result(result)
        result_hash = _sha256(result_json)

        with self._engine.begin() as connection:
            _ensure_and_lock_control(connection, scope)
            execution_row = _locked_execution(connection, execution_hash)
            _verify_execution_identity(execution_row, intent)
            stored_checkpoint = _verified_checkpoint(execution_row, intent)
            if checkpoint is not None and stored_checkpoint != checkpoint:
                raise ExecutionControlIntegrityError(
                    "recovery checkpoint differs from the authorized checkpoint"
                )
            reservation_row = _locked_reservation(connection, execution_hash)
            if reservation_row is None:
                raise ExecutionControlIntegrityError(
                    "checkpointed execution has no risk reservation"
                )
            _verify_reservation_binding(reservation_row, intent, scope, stored_checkpoint)

            status = str(execution_row["claim_status"])
            identical = (
                status == "completed"
                and str(execution_row.get("result_hash") or "") == result_hash
                and str(execution_row.get("result_json") or "") == result_json
            )
            if status == "in_progress":
                if recovered:
                    _require_recovery_owner(execution_row, owner_token)
                else:
                    _require_original_owner(execution_row, owner_token)
            elif status == "completed":
                previous = _verified_result(execution_row, intent)
                if recovered:
                    _require_recovery_owner(execution_row, owner_token)
                if not identical:
                    if (
                        RiskReservationState(str(reservation_row["state"]))
                        is RiskReservationState.SETTLED
                    ):
                        raise ExecutionControlStateError(
                            "settled reservation cannot accept a newer broker outcome"
                        )
                    if not recovered:
                        raise ExecutionControlStateError(
                            "only a recovery owner can advance a completed result"
                        )
                    if not execution_result_can_advance(previous, result):
                        raise ExecutionControlStateError(
                            "recovered execution result cannot move backward"
                        )
            else:
                raise ExecutionControlIntegrityError("execution claim status is invalid")

            reservation = _advance_reservation(
                connection,
                reservation_row,
                desired_state,
            )
            if not identical:
                parameters = {
                    "scope_hash": execution_hash,
                    "execution_state": result.state.value,
                    "result_hash": result_hash,
                    "result_json": result_json,
                }
                if recovered:
                    connection.execute(
                        text(
                            """
                            UPDATE execution_idempotency_records
                            SET claim_status = 'completed',
                                execution_state = :execution_state,
                                result_hash = :result_hash,
                                result_json = :result_json,
                                completed_at = UTC_TIMESTAMP(6),
                                recovery_token = NULL,
                                recovery_lease_expires_at = NULL,
                                updated_at = UTC_TIMESTAMP(6)
                            WHERE scope_hash = :scope_hash
                            """
                        ),
                        parameters,
                    )
                else:
                    connection.execute(
                        text(
                            """
                            UPDATE execution_idempotency_records
                            SET claim_status = 'completed',
                                execution_state = :execution_state,
                                result_hash = :result_hash,
                                result_json = :result_json,
                                completed_at = UTC_TIMESTAMP(6),
                                updated_at = UTC_TIMESTAMP(6)
                            WHERE scope_hash = :scope_hash
                            """
                        ),
                        parameters,
                    )
            elif recovered:
                connection.execute(
                    text(
                        """
                        UPDATE execution_idempotency_records
                        SET recovery_token = NULL,
                            recovery_lease_expires_at = NULL,
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE scope_hash = :scope_hash
                          AND recovery_token = :recovery_token
                        """
                    ),
                    {
                        "scope_hash": execution_hash,
                        "recovery_token": owner_token,
                    },
                )
            return reservation

    def settle_reflected(
        self,
        intent: OrderIntent,
        *,
        evidence: ReflectedRiskEvidence,
    ) -> RiskReservation:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        if not isinstance(evidence, ReflectedRiskEvidence):
            raise TypeError("evidence must be ReflectedRiskEvidence")
        scope = self._authorized_scope(intent)
        execution_hash = execution_scope_hash(intent)
        existing = self.reservation(intent)
        if existing is None:
            raise ExecutionControlIntegrityError(
                "checkpointed execution has no risk reservation"
            )
        if existing.state is RiskReservationState.SETTLED:
            if (
                existing.settlement_snapshot_hash != evidence.snapshot_hash
                or existing.settlement_observed_at
                != evidence.observed_at.astimezone(UTC)
                or existing.settlement_reference != evidence.reference
            ):
                self.observe_account_snapshot(
                    scope,
                    evidence.snapshot,
                    evidence.reference,
                )
            return existing

        # Observation is deliberately committed before the position proof.  A valid
        # newer account snapshot must fence older authorizations even when it does
        # not yet prove this particular execution was reflected.
        self.observe_account_snapshot(scope, evidence.snapshot, evidence.reference)
        with self._engine.begin() as connection:
            control_row = _ensure_and_lock_control(connection, scope)
            if not _watermark_matches_evidence(control_row, evidence):
                raise ExecutionControlStateError(
                    "reflection evidence is no longer the current account snapshot"
                )
            execution_row = _locked_execution(connection, execution_hash)
            _verify_execution_identity(execution_row, intent)
            checkpoint = _verified_checkpoint(execution_row, intent)
            reservation_row = _locked_reservation(connection, execution_hash)
            if reservation_row is None:
                raise ExecutionControlStateError("risk reservation does not exist")
            _verify_reservation_binding(reservation_row, intent, scope, checkpoint)
            current = RiskReservationState(str(reservation_row["state"]))
            if current is RiskReservationState.SETTLED:
                return _reservation_from_row(reservation_row)
            if current is not RiskReservationState.COMMITTED_UNREFLECTED:
                raise ExecutionControlStateError(
                    "only a committed reservation can be settled as reflected"
                )
            if str(execution_row.get("claim_status")) != "completed":
                raise ExecutionControlStateError(
                    "reflection requires a completed broker outcome"
                )
            result = _verified_result(execution_row, intent)
            executed_quantity = _settleable_executed_quantity(result)
            if executed_quantity > Decimal(reservation_row["authorized_quantity"]):
                raise ExecutionControlIntegrityError(
                    "broker executed quantity exceeds the authorized reservation"
                )
            latest_outcome_at = max(
                _aware_utc(execution_row["execution_updated_at"]),
                _aware_utc(reservation_row["updated_at"]),
            )
            watermark_recorded_at = control_row.get(
                "risk_snapshot_high_watermark_recorded_at"
            )
            if watermark_recorded_at is None:
                raise ExecutionControlIntegrityError(
                    "account snapshot watermark recording time is missing"
                )
            if _aware_utc(watermark_recorded_at) <= latest_outcome_at:
                raise ExecutionControlStateError(
                    "reflection evidence must be observed after the broker outcome"
                )
            if not _reservation_position_is_reflected(
                reservation_row,
                intent,
                evidence.snapshot,
                executed_quantity,
            ):
                raise ExecutionControlStateError(
                    "account snapshot does not reflect the executed position delta"
                )
            reused = connection.execute(
                text(
                    """
                    SELECT execution_scope_hash
                    FROM execution_risk_reservations
                    WHERE account_control_hash = :control_hash
                      AND position_key_hash = :position_key_hash
                      AND state = 'settled'
                      AND settlement_snapshot_hash = :snapshot_hash
                      AND settlement_observed_at = :observed_at
                      AND execution_scope_hash <> :execution_scope_hash
                    LIMIT 1
                    FOR UPDATE
                    """
                ),
                {
                    "control_hash": execution_account_scope_hash(scope),
                    "position_key_hash": str(reservation_row["position_key_hash"]),
                    "snapshot_hash": evidence.snapshot_hash,
                    "observed_at": _naive_utc(evidence.observed_at),
                    "execution_scope_hash": execution_hash,
                },
            ).scalar_one_or_none()
            if reused is not None:
                raise ExecutionControlStateError(
                    "account snapshot evidence was already consumed"
                )
            connection.execute(
                text(
                    """
                    UPDATE execution_risk_reservations
                    SET state = 'settled',
                        settled_at = UTC_TIMESTAMP(6),
                        release_reason = 'account_snapshot_reflected',
                        settlement_snapshot_hash = :snapshot_hash,
                        settlement_observed_at = :observed_at,
                        settlement_reference = :reference,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE execution_scope_hash = :execution_scope_hash
                      AND state = 'committed_unreflected'
                    """
                ),
                {
                    "snapshot_hash": evidence.snapshot_hash,
                    "observed_at": _naive_utc(evidence.observed_at),
                    "reference": evidence.reference,
                    "execution_scope_hash": execution_hash,
                },
            )
            updated_row = _locked_reservation(connection, execution_hash)
            if updated_row is None:  # pragma: no cover - update invariant
                raise ExecutionControlIntegrityError("risk reservation disappeared")
            return _reservation_from_row(updated_row)

    def reservation(self, intent: OrderIntent) -> RiskReservation | None:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        execution_hash = execution_scope_hash(intent)
        with self._engine.connect() as connection:
            selected = connection.execute(
                text(_SELECT_RESERVATION),
                {"execution_scope_hash": execution_hash},
            ).mappings().one_or_none()
        if selected is None:
            return None
        row = dict(selected)
        expected_control = execution_account_scope_hash(self._authorized_scope(intent))
        if (
            str(row["execution_scope_hash"]) != execution_hash
            or str(row["account_control_hash"]) != expected_control
            or str(row["intent_id"]) != intent.intent_id
        ):
            raise ExecutionControlIntegrityError(
                "risk reservation is bound to different execution data"
            )
        return _reservation_from_row(row)


def _ensure_and_lock_control(
    connection: Any,
    scope: ExecutionAccountScope,
) -> dict[str, Any]:
    control_hash = execution_account_scope_hash(scope)
    connection.execute(
        text(
            """
            INSERT INTO execution_account_controls (
                control_hash, tenant_scope, user_scope, account_scope,
                broker_name, physical_account_id, market, execution_mode,
                consecutive_failures, version,
                changed_at, created_at, updated_at
            ) VALUES (
                :control_hash, :tenant_scope, :user_scope, :account_scope,
                :broker_name, :physical_account_id, :market, :execution_mode, 0, 0,
                UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            )
            ON DUPLICATE KEY UPDATE control_hash = execution_account_controls.control_hash
            """
        ),
        {
            "control_hash": control_hash,
            "tenant_scope": scope.tenant_scope,
            "user_scope": scope.user_scope,
            "account_scope": scope.account_scope,
            "broker_name": scope.broker_name,
            "physical_account_id": scope.physical_account_id,
            "market": scope.market,
            "execution_mode": scope.execution_mode.value,
        },
    )
    row = _locked_control(connection, control_hash)
    _verify_control_scope(row, scope)
    return row


def _require_control_policy(policy: ExecutionControlPolicy) -> None:
    if not isinstance(policy, ExecutionControlPolicy):
        raise TypeError("policy must be an ExecutionControlPolicy")


def _bind_or_match_control_policy(
    connection: Any,
    row: Mapping[str, Any],
    policy: ExecutionControlPolicy,
) -> tuple[dict[str, Any], bool]:
    _require_control_policy(policy)
    control_hash = str(row["control_hash"])
    stored_hash = _optional_string(row.get("control_policy_hash"))
    stored_threshold = row.get("failure_threshold")
    if stored_hash is None and stored_threshold is None:
        connection.execute(
            text(
                """
                UPDATE execution_account_controls
                SET control_policy_hash = :policy_hash,
                    failure_threshold = :failure_threshold,
                    version = version + 1,
                    changed_at = UTC_TIMESTAMP(6),
                    changed_by = 'system:policy_bind',
                    updated_at = UTC_TIMESTAMP(6)
                WHERE control_hash = :control_hash
                  AND control_policy_hash IS NULL
                  AND failure_threshold IS NULL
                """
            ),
            {
                "policy_hash": policy.fingerprint,
                "failure_threshold": policy.failure_threshold,
                "control_hash": control_hash,
            },
        )
        return _locked_control(connection, control_hash), True
    if (stored_hash is None) != (stored_threshold is None):
        raise ExecutionControlIntegrityError("stored control policy binding is incomplete")
    if (
        stored_hash == policy.fingerprint
        and int(stored_threshold) == policy.failure_threshold
    ):
        return dict(row), True
    if row.get("kill_reason") is None:
        connection.execute(
            text(
                """
                UPDATE execution_account_controls
                SET kill_reason = 'control_policy_mismatch',
                    version = version + 1,
                    changed_at = UTC_TIMESTAMP(6),
                    changed_by = 'system:policy_mismatch',
                    updated_at = UTC_TIMESTAMP(6)
                WHERE control_hash = :control_hash
                """
            ),
            {"control_hash": control_hash},
        )
        row = _locked_control(connection, control_hash)
    return dict(row), False


def _require_matching_control_policy(
    row: Mapping[str, Any],
    policy: ExecutionControlPolicy,
) -> None:
    if (
        str(row.get("control_policy_hash") or "") != policy.fingerprint
        or int(row.get("failure_threshold") or 0) != policy.failure_threshold
    ):
        raise ExecutionControlIntegrityError(
            "execution control policy changed after binding"
        )


def _database_now(connection: Any) -> datetime:
    value = connection.execute(
        text("SELECT UTC_TIMESTAMP(6) AS observed_now")
    ).scalar_one()
    return _aware_utc(value)


def _snapshot_reference(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 191 or "\x00" in normalized:
        raise ValueError("snapshot reference must contain 1-191 characters")
    return normalized


def _risk_watermark_rejection(
    row: Mapping[str, Any],
    source_updated_at: datetime,
    observed_at: datetime,
    snapshot_hash: str,
) -> str | None:
    watermark_at = row.get("risk_snapshot_high_watermark_at")
    if watermark_at is None:
        return None
    stored_source = row.get("risk_snapshot_source_updated_at")
    if stored_source is None:
        raise ExecutionControlIntegrityError(
            "account snapshot source watermark is missing"
        )
    current_observed_at = _aware_utc(watermark_at)
    current_source_updated_at = _aware_utc(stored_source)
    candidate_observed_at = observed_at.astimezone(UTC)
    candidate_source_updated_at = source_updated_at.astimezone(UTC)
    if (
        candidate_observed_at < current_observed_at
        or candidate_source_updated_at < current_source_updated_at
    ):
        return "risk_snapshot_stale"
    if (
        candidate_observed_at == current_observed_at
        and str(row.get("risk_snapshot_high_watermark_hash") or "")
        != snapshot_hash
    ):
        return "risk_snapshot_conflict"
    return None


def _advance_risk_watermark(
    connection: Any,
    row: Mapping[str, Any],
    *,
    source_updated_at: datetime,
    observed_at: datetime,
    snapshot_hash: str,
    reference: str,
) -> dict[str, Any]:
    rejection = _risk_watermark_rejection(
        row,
        source_updated_at,
        observed_at,
        snapshot_hash,
    )
    if rejection is not None:
        raise ExecutionControlStateError(rejection)
    watermark_at = row.get("risk_snapshot_high_watermark_at")
    if (
        watermark_at is not None
        and _aware_utc(watermark_at) == observed_at.astimezone(UTC)
        and _aware_utc(row["risk_snapshot_source_updated_at"])
        == source_updated_at.astimezone(UTC)
    ):
        return dict(row)
    connection.execute(
        text(
            """
            UPDATE execution_account_controls
            SET risk_snapshot_high_watermark_at = :observed_at,
                risk_snapshot_source_updated_at = :source_updated_at,
                risk_snapshot_high_watermark_recorded_at = UTC_TIMESTAMP(6),
                risk_snapshot_high_watermark_hash = :snapshot_hash,
                risk_snapshot_high_watermark_reference = :reference,
                updated_at = UTC_TIMESTAMP(6)
            WHERE control_hash = :control_hash
            """
        ),
        {
            "observed_at": _naive_utc(observed_at),
            "source_updated_at": _naive_utc(source_updated_at),
            "snapshot_hash": snapshot_hash,
            "reference": _snapshot_reference(reference),
            "control_hash": str(row["control_hash"]),
        },
    )
    return _locked_control(connection, str(row["control_hash"]))


def _watermark_matches_evidence(
    row: Mapping[str, Any],
    evidence: ReflectedRiskEvidence,
) -> bool:
    observed_at = row.get("risk_snapshot_high_watermark_at")
    return (
        observed_at is not None
        and _aware_utc(observed_at) == evidence.observed_at.astimezone(UTC)
        and row.get("risk_snapshot_source_updated_at") is not None
        and _aware_utc(row["risk_snapshot_source_updated_at"])
        == evidence.snapshot.updated_at.astimezone(UTC)
        and str(row.get("risk_snapshot_high_watermark_hash") or "")
        == evidence.snapshot_hash
    )


def _quote_fingerprint(quote: Quote) -> str:
    return _sha256(
        _canonical_json(
            {
                "observed_at": _timestamp(quote.observed_at),
                "price": str(quote.price),
                "symbol": quote.symbol,
            }
        )
    )


def _observe_quote_watermark(
    connection: Any,
    *,
    control_hash: str,
    quote: Quote,
    reference: str,
) -> str | None:
    selected = connection.execute(
        text(
            """
            SELECT symbol, observed_at, quote_hash
            FROM execution_quote_watermarks
            WHERE account_control_hash = :control_hash
              AND symbol = :symbol
            FOR UPDATE
            """
        ),
        {"control_hash": control_hash, "symbol": quote.symbol},
    ).mappings().one_or_none()
    quote_hash = _quote_fingerprint(quote)
    observed_at = quote.observed_at.astimezone(UTC)
    if selected is not None:
        row = dict(selected)
        rejection = _quote_watermark_rejection(row, quote)
        if rejection is not None:
            return rejection
        current_at = _aware_utc(row["observed_at"])
        if observed_at == current_at:
            return None
        connection.execute(
            text(
                """
                UPDATE execution_quote_watermarks
                SET observed_at = :observed_at,
                    quote_hash = :quote_hash,
                    reference = :reference,
                    updated_at = UTC_TIMESTAMP(6)
                WHERE account_control_hash = :control_hash
                  AND symbol = :symbol
                """
            ),
            {
                "observed_at": _naive_utc(observed_at),
                "quote_hash": quote_hash,
                "reference": _snapshot_reference(reference),
                "control_hash": control_hash,
                "symbol": quote.symbol,
            },
        )
        return None
    connection.execute(
        text(
            """
            INSERT INTO execution_quote_watermarks (
                account_control_hash, symbol, observed_at, quote_hash,
                reference, created_at, updated_at
            ) VALUES (
                :control_hash, :symbol, :observed_at, :quote_hash,
                :reference, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            )
            """
        ),
        {
            "control_hash": control_hash,
            "symbol": quote.symbol,
            "observed_at": _naive_utc(observed_at),
            "quote_hash": quote_hash,
            "reference": _snapshot_reference(reference),
        },
    )
    return None


def _quote_watermark_rejection(
    row: Mapping[str, Any],
    quote: Quote,
) -> str | None:
    if str(row.get("symbol") or "") != quote.symbol:
        raise ExecutionControlIntegrityError(
            "quote watermark is bound to a different symbol"
        )
    current_at = _aware_utc(row["observed_at"])
    observed_at = quote.observed_at.astimezone(UTC)
    if observed_at < current_at:
        return "quote_snapshot_stale"
    if observed_at == current_at and str(row["quote_hash"]) != _quote_fingerprint(quote):
        return "quote_snapshot_conflict"
    return None


def _account_snapshot_time_rejection(
    snapshot: AccountSnapshot,
    policy: ExecutionControlPolicy,
    now: datetime,
) -> str | None:
    return _freshness_rejection(
        "account_freshness",
        snapshot.observed_at,
        now,
        policy.preflight.max_account_age_seconds,
        policy.preflight.max_future_skew_seconds,
    )


def _authorization_business_time_rejection(
    intent: OrderIntent,
    checkpoint: ExecutionCheckpoint,
    policy: ExecutionControlPolicy,
    now: datetime,
) -> str | None:
    future_skew = policy.preflight.max_future_skew_seconds
    if (intent.signal_time.astimezone(UTC) - now).total_seconds() > future_skew:
        return "intent_signal_in_future"
    if now > intent.valid_until.astimezone(UTC):
        return "intent_expired_before_checkpoint"
    if checkpoint.checkpointed_at > intent.valid_until:
        return "checkpoint_after_intent_expiry"
    if (checkpoint.checkpointed_at.astimezone(UTC) - now).total_seconds() > future_skew:
        return "checkpoint_in_future"
    return None


def _freshness_rejection(
    prefix: str,
    observed_at: datetime,
    now: datetime,
    max_age_seconds: float,
    max_future_skew_seconds: float,
) -> str | None:
    age = (now - observed_at.astimezone(UTC)).total_seconds()
    if age < -max_future_skew_seconds:
        return f"{prefix}_in_future"
    if age > max_age_seconds:
        return f"{prefix}_stale"
    return None


def _position_key_hash(
    control_hash: str,
    position_mode: PositionMode,
    symbol: str,
    position_side: PositionSide,
) -> str:
    return _sha256(
        _canonical_json(
            [control_hash, symbol, position_mode.value, position_side.value]
        )
    )


def _locked_control(connection: Any, control_hash: str) -> dict[str, Any]:
    selected = connection.execute(
        text(_SELECT_CONTROL + " FOR UPDATE"),
        {"control_hash": control_hash},
    ).mappings().one_or_none()
    if selected is None:
        raise ExecutionControlStateError("execution account control does not exist")
    return dict(selected)


def _locked_execution(connection: Any, scope_hash: str) -> dict[str, Any]:
    selected = connection.execute(
        _SELECT_EXECUTION_FOR_UPDATE,
        {"scope_hash": scope_hash},
    ).mappings().one_or_none()
    if selected is None:
        raise ExecutionControlStateError("execution claim does not exist")
    return dict(selected)


def _locked_reservation(connection: Any, execution_hash: str) -> dict[str, Any] | None:
    selected = connection.execute(
        text(_SELECT_RESERVATION + " FOR UPDATE"),
        {"execution_scope_hash": execution_hash},
    ).mappings().one_or_none()
    return None if selected is None else dict(selected)


def _verify_control_scope(
    row: Mapping[str, Any],
    scope: ExecutionAccountScope,
) -> None:
    expected = {
        "control_hash": execution_account_scope_hash(scope),
        "tenant_scope": scope.tenant_scope,
        "user_scope": scope.user_scope,
        "account_scope": scope.account_scope,
        "broker_name": scope.broker_name,
        "physical_account_id": scope.physical_account_id,
        "market": scope.market,
        "execution_mode": scope.execution_mode.value,
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise ExecutionControlIntegrityError(
            "execution account hash is bound to different scope data"
        )


def _verify_execution_identity(row: Mapping[str, Any], intent: OrderIntent) -> None:
    intent_json = serialize_order_intent(intent)
    intent_hash = _sha256(intent_json)
    expected = {
        "scope_hash": execution_scope_hash(intent),
        "tenant_scope": intent.tenant_scope,
        "user_scope": intent.user_scope,
        "account_scope": intent.account_scope,
        "deployment_scope": intent.deployment_scope,
        "idempotency_key": intent.idempotency_key,
        "intent_id": intent.intent_id,
        "intent_hash": intent_hash,
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise ExecutionControlIntegrityError(
            "execution claim is bound to different intent data"
        )
    stored_json = str(row.get("intent_json") or "")
    if stored_json != intent_json or _sha256(stored_json) != intent_hash:
        raise ExecutionControlIntegrityError("execution intent hash verification failed")
    try:
        stored_intent = deserialize_order_intent(stored_json)
    except Exception as exc:
        raise ExecutionControlIntegrityError("execution intent payload is invalid") from exc
    if stored_intent != intent:
        raise ExecutionControlIntegrityError("execution intent did not round-trip")


def _require_live_claim(row: Mapping[str, Any], claim_token: str) -> None:
    if str(row.get("claim_status")) != "in_progress":
        raise ExecutionControlStateError("execution claim is not in progress")
    if str(row.get("claim_token") or "") != claim_token:
        raise ExecutionControlStateError("execution claim owner token is stale")
    if not bool(row.get("lease_valid")):
        raise ExecutionControlStateError("execution claim lease expired before authorization")
    if row.get("recovery_started_at") is not None:
        raise ExecutionControlStateError("execution claim owner was fenced by recovery")


def _require_original_owner(row: Mapping[str, Any], claim_token: str) -> None:
    if str(row.get("claim_token") or "") != claim_token:
        raise ExecutionControlStateError("execution claim owner token is stale")
    if row.get("recovery_started_at") is not None:
        raise ExecutionControlStateError("execution claim owner was fenced by recovery")


def _require_recovery_owner(row: Mapping[str, Any], recovery_token: str) -> None:
    if str(row.get("recovery_token") or "") != recovery_token:
        raise ExecutionControlStateError("execution recovery owner token is stale")
    if not bool(row.get("recovery_lease_valid")):
        raise ExecutionControlStateError("execution recovery owner lease expired")


def _verified_checkpoint(
    row: Mapping[str, Any],
    intent: OrderIntent,
) -> ExecutionCheckpoint:
    checkpoint_json = str(row.get("checkpoint_json") or "")
    checkpoint_hash = str(row.get("checkpoint_hash") or "")
    if not checkpoint_json or _sha256(checkpoint_json) != checkpoint_hash:
        raise ExecutionControlIntegrityError("execution checkpoint hash verification failed")
    try:
        checkpoint = deserialize_execution_checkpoint(checkpoint_json)
    except Exception as exc:
        raise ExecutionControlIntegrityError("execution checkpoint payload is invalid") from exc
    if checkpoint.intent != intent:
        raise ExecutionControlIntegrityError("execution checkpoint belongs to another intent")
    if (
        str(row.get("broker_name") or "") != checkpoint.broker_name
        or str(row.get("physical_account_id") or "")
        != checkpoint.physical_account_id
        or str(row.get("client_order_id") or "") != checkpoint.client_order_id
    ):
        raise ExecutionControlIntegrityError(
            "execution checkpoint broker binding is inconsistent"
        )
    return checkpoint


def _verified_result(row: Mapping[str, Any], intent: OrderIntent) -> ExecutionResult:
    result_json = str(row.get("result_json") or "")
    result_hash = str(row.get("result_hash") or "")
    if not result_json or _sha256(result_json) != result_hash:
        raise ExecutionControlIntegrityError("execution result hash verification failed")
    try:
        result = deserialize_execution_result(result_json)
    except Exception as exc:
        raise ExecutionControlIntegrityError("execution result payload is invalid") from exc
    if result.intent != intent or str(row.get("execution_state")) != result.state.value:
        raise ExecutionControlIntegrityError("execution result binding is inconsistent")
    return result


def _verify_existing_authorization(
    execution_row: Mapping[str, Any],
    reservation_row: Mapping[str, Any],
    *,
    checkpoint_json: str,
    checkpoint_hash: str,
    policy_hash: str,
    decision_hash: str,
    snapshot_hash: str,
    control_hash: str,
) -> None:
    if (
        str(execution_row.get("checkpoint_json") or "") != checkpoint_json
        or str(execution_row.get("checkpoint_hash") or "") != checkpoint_hash
    ):
        raise ExecutionControlIntegrityError(
            "risk reservation is not paired with the requested checkpoint"
        )
    expected = {
        "reservation_id": str(execution_row["scope_hash"]),
        "execution_scope_hash": str(execution_row["scope_hash"]),
        "account_control_hash": control_hash,
        "intent_id": str(execution_row["intent_id"]),
        "policy_hash": policy_hash,
        "risk_decision_hash": decision_hash,
        "snapshot_hash": snapshot_hash,
        "client_order_id": str(execution_row["client_order_id"]),
    }
    if any(str(reservation_row.get(name)) != value for name, value in expected.items()):
        raise ExecutionControlIntegrityError(
            "existing risk reservation differs from authorization data"
        )


def _verify_reservation_binding(
    row: Mapping[str, Any],
    intent: OrderIntent,
    scope: ExecutionAccountScope,
    checkpoint: ExecutionCheckpoint,
) -> None:
    execution_hash = execution_scope_hash(intent)
    expected = {
        "reservation_id": execution_hash,
        "execution_scope_hash": execution_hash,
        "account_control_hash": execution_account_scope_hash(scope),
        "intent_id": intent.intent_id,
        "client_order_id": checkpoint.client_order_id,
        "risk_decision_hash": _risk_decision_hash(checkpoint.risk_decision),
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise ExecutionControlIntegrityError(
            "risk reservation is bound to different execution data"
        )


def _budget_requirements(
    connection: Any,
    *,
    control_hash: str,
    intent: OrderIntent,
    checkpoint: ExecutionCheckpoint,
    context: ExecutionContext,
    policy: ExecutionControlPolicy,
) -> tuple[str | None, Decimal, int, _PositionBaseline | None]:
    risk_policy = policy.risk
    decision = checkpoint.risk_decision
    if not decision.approved:
        raise ExecutionControlIntegrityError("checkpoint risk decision is not approved")
    if context.quote.symbol != intent.symbol or context.rules.symbol != intent.symbol:
        raise ExecutionControlIntegrityError("execution context symbol differs from intent")
    trade_permission = context.account.can_trade is True or (
        context.account.can_trade is None
        and not policy.preflight.require_explicit_trade_permission
    )
    if not trade_permission:
        return "account_cannot_trade", Decimal(0), 0, None
    try:
        approved_quantity = context.rules.quantity(intent.quantity)
    except ValueError:
        return "quantity_outside_instrument_rules", Decimal(0), 0, None
    expected_notional = approved_quantity * context.quote.price
    if (
        decision.intent_id != intent.intent_id
        or decision.approved_quantity != approved_quantity
        or decision.reference_price != context.quote.price
        or decision.notional != expected_notional
    ):
        raise ExecutionControlIntegrityError(
            "approved risk decision differs from the authorization context"
        )
    if expected_notional < context.rules.minimum_notional:
        return "minimum_notional_not_met", Decimal(0), 0, None
    if (
        risk_policy.allowed_symbols is not None
        and intent.symbol not in risk_policy.allowed_symbols
    ):
        return "symbol_not_allowed", Decimal(0), 0, None
    if (
        risk_policy.max_order_quantity is not None
        and approved_quantity > risk_policy.max_order_quantity
    ):
        return "order_quantity_limit", Decimal(0), 0, None
    if (
        risk_policy.max_order_notional is not None
        and expected_notional > risk_policy.max_order_notional
    ):
        return "order_notional_limit", Decimal(0), 0, None
    if (
        context.position_mode is PositionMode.ONE_WAY
        and intent.position_side is not PositionSide.BOTH
    ) or (
        context.position_mode is PositionMode.HEDGE
        and intent.position_side is PositionSide.BOTH
    ):
        return "position_mode_mismatch", Decimal(0), 0, None
    if intent.action is IntentAction.TAKE_PROFIT:
        return "protective_pairing_not_supported", Decimal(0), 0, None

    baseline_rejection, baseline = _position_baseline(
        intent,
        context,
        approved_quantity,
    )
    if baseline_rejection is not None:
        return baseline_rejection, Decimal(0), 0, None
    if baseline is None:  # pragma: no cover - helper invariant
        raise ExecutionControlIntegrityError("position baseline is unavailable")
    position_key_hash = _position_key_hash(
        control_hash,
        baseline.position_mode,
        intent.symbol,
        baseline.target_position_side,
    )
    active_execution = connection.execute(
        text(
            """
            SELECT execution_scope_hash
            FROM execution_risk_reservations
            WHERE account_control_hash = :control_hash
              AND position_key_hash = :position_key_hash
              AND state IN ('held', 'committed_unreflected')
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "control_hash": control_hash,
            "position_key_hash": position_key_hash,
        },
    ).scalar_one_or_none()
    if active_execution is not None:
        return "position_reconciliation_pending", Decimal(0), 0, None

    if intent.risk_reducing:
        if baseline.baseline_quantity == 0:
            return "position_to_reduce_not_found", Decimal(0), 0, None
        if approved_quantity > baseline.baseline_quantity:
            return "reduce_quantity_exceeds_position", Decimal(0), 0, None
        return None, Decimal(0), 0, baseline
    if context.account.wallet_balance <= 0:
        return "account_equity_unavailable", Decimal(0), 0, None
    current_exposure = _portfolio_notional(context)
    if current_exposure is None:
        return "account_exposure_snapshot_incomplete", Decimal(0), 0, None
    pending = connection.execute(
        text(
            """
            SELECT COALESCE(SUM(reserved_notional), 0) AS pending_notional,
                   COALESCE(SUM(reserved_open_slots), 0) AS pending_slots
            FROM execution_risk_reservations
            WHERE account_control_hash = :control_hash
              AND state IN ('held', 'committed_unreflected')
            """
        ),
        {"control_hash": control_hash},
    ).mappings().one()
    pending_notional = Decimal(pending["pending_notional"])
    pending_slots = int(pending["pending_slots"])
    exposure_limit = (
        context.account.wallet_balance * risk_policy.max_notional_to_equity
    )
    if current_exposure + pending_notional + expected_notional > exposure_limit:
        return "account_exposure_limit", Decimal(0), 0, None
    open_positions = sum(position.quantity > 0 for position in context.account.positions)
    if open_positions + pending_slots + 1 > risk_policy.max_open_positions:
        return "position_count_limit", Decimal(0), 0, None
    return None, expected_notional, 1, baseline


def _position_baseline(
    intent: OrderIntent,
    context: ExecutionContext,
    authorized_quantity: Decimal,
) -> tuple[str | None, _PositionBaseline | None]:
    target_direction = (
        PositionDirection.LONG
        if (
            (not intent.risk_reducing and intent.side is OrderSide.BUY)
            or (intent.risk_reducing and intent.side is OrderSide.SELL)
        )
        else PositionDirection.SHORT
    )
    target_side = (
        PositionSide.BOTH
        if context.position_mode is PositionMode.ONE_WAY
        else intent.position_side
    )
    symbol_positions = tuple(
        position
        for position in context.account.positions
        if position.symbol == intent.symbol
    )
    if context.position_mode is PositionMode.ONE_WAY:
        if any(
            position.position_side is not PositionSide.BOTH
            for position in symbol_positions
        ):
            raise ExecutionControlIntegrityError(
                "one-way account snapshot contains hedge-side positions"
            )
        if len({position.direction for position in symbol_positions}) > 1:
            raise ExecutionControlIntegrityError(
                "one-way account snapshot contains opposing positions"
            )
        if not intent.risk_reducing and any(
            position.direction is not target_direction
            for position in symbol_positions
        ):
            return "position_direction_conflict", None
    elif any(
        position.position_side is PositionSide.BOTH
        for position in symbol_positions
    ):
        raise ExecutionControlIntegrityError(
            "hedge account snapshot contains one-way positions"
        )
    matching = tuple(
        position
        for position in symbol_positions
        if position.direction is target_direction
        and position.position_side is target_side
    )
    quantity = sum(
        (position.quantity for position in matching),
        start=Decimal(0),
    )
    return None, _PositionBaseline(
        position_mode=context.position_mode,
        target_direction=target_direction,
        target_position_side=target_side,
        baseline_direction=target_direction if quantity > 0 else None,
        baseline_position_side=target_side if quantity > 0 else None,
        baseline_quantity=quantity,
        authorized_quantity=authorized_quantity,
    )


def _settleable_executed_quantity(result: ExecutionResult) -> Decimal:
    executed = sum((fill.quantity for fill in result.fills), start=Decimal(0))
    if result.broker_order is not None:
        executed = max(executed, result.broker_order.executed_quantity)
    if result.state is ExecutionState.FILLED and executed > 0:
        return executed
    if (
        result.state in {ExecutionState.CANCELED, ExecutionState.BROKER_REJECTED}
        and executed > 0
    ):
        return executed
    raise ExecutionControlStateError(
        "only a terminal broker outcome with executed quantity can be settled"
    )


def _reservation_position_is_reflected(
    reservation: Mapping[str, Any],
    intent: OrderIntent,
    snapshot: AccountSnapshot,
    executed_quantity: Decimal,
) -> bool:
    symbol = str(reservation["symbol"])
    mode = PositionMode(str(reservation["position_mode"]))
    target_direction = PositionDirection(str(reservation["target_direction"]))
    target_side = PositionSide(str(reservation["target_position_side"]))
    baseline_quantity = Decimal(reservation["baseline_quantity"])
    positions = tuple(position for position in snapshot.positions if position.symbol == symbol)
    if mode is PositionMode.ONE_WAY and len(
        {position.direction for position in positions}
    ) > 1:
        raise ExecutionControlIntegrityError(
            "one-way reflection snapshot contains opposing positions"
        )
    if _database_bool(reservation["risk_reducing"]):
        expected_remaining = max(
            baseline_quantity - executed_quantity,
            Decimal(0),
        )
        if mode is PositionMode.ONE_WAY:
            if any(
                position.position_side is not PositionSide.BOTH
                or position.direction is not target_direction
                for position in positions
            ):
                return False
        current_quantity = sum(
            (
                position.quantity
                for position in positions
                if position.direction is target_direction
                and position.position_side is target_side
            ),
            start=Decimal(0),
        )
        return current_quantity <= expected_remaining
    if mode is PositionMode.HEDGE:
        current_quantity = sum(
            (
                position.quantity
                for position in positions
                if position.direction is target_direction
                and position.position_side is target_side
            ),
            start=Decimal(0),
        )
        return current_quantity >= baseline_quantity + executed_quantity
    if any(position.position_side is not PositionSide.BOTH for position in positions):
        raise ExecutionControlIntegrityError(
            "one-way reflection snapshot contains hedge-side positions"
        )
    current_signed = sum(
        (
            position.quantity
            if position.direction is PositionDirection.LONG
            else -position.quantity
            for position in positions
        ),
        start=Decimal(0),
    )
    baseline_signed = (
        baseline_quantity
        if reservation.get("baseline_direction") in {
            PositionDirection.LONG,
            PositionDirection.LONG.value,
        }
        else -baseline_quantity
    )
    if baseline_quantity == 0:
        baseline_signed = Decimal(0)
    if intent.side is OrderSide.BUY:
        return current_signed >= baseline_signed + executed_quantity
    return current_signed <= baseline_signed - executed_quantity


def _advance_reservation(
    connection: Any,
    row: Mapping[str, Any],
    desired: RiskReservationState,
) -> RiskReservation:
    current = RiskReservationState(str(row["state"]))
    execution_hash = str(row["execution_scope_hash"])
    if current is RiskReservationState.SETTLED:
        return _reservation_from_row(row)
    if current is RiskReservationState.RELEASED:
        if desired is not RiskReservationState.RELEASED:
            raise ExecutionControlStateError("released reservation cannot become active")
        return _reservation_from_row(row)
    allowed = {
        RiskReservationState.HELD: {
            RiskReservationState.HELD,
            RiskReservationState.COMMITTED_UNREFLECTED,
            RiskReservationState.RELEASED,
        },
        RiskReservationState.COMMITTED_UNREFLECTED: {
            RiskReservationState.COMMITTED_UNREFLECTED,
            RiskReservationState.RELEASED,
        },
    }
    if desired not in allowed.get(current, set()):
        raise ExecutionControlStateError("risk reservation cannot move backward")
    if desired is current:
        return _reservation_from_row(row)
    terminal = desired is RiskReservationState.RELEASED
    connection.execute(
        text(
            """
            UPDATE execution_risk_reservations
            SET state = :state,
                settled_at = CASE WHEN :terminal = 1 THEN UTC_TIMESTAMP(6) ELSE NULL END,
                release_reason = CASE
                    WHEN :terminal = 1 THEN 'zero_fill_terminal'
                    ELSE NULL
                END,
                settlement_snapshot_hash = NULL,
                settlement_observed_at = NULL,
                settlement_reference = NULL,
                updated_at = UTC_TIMESTAMP(6)
            WHERE execution_scope_hash = :execution_scope_hash
            """
        ),
        {
            "state": desired.value,
            "terminal": 1 if terminal else 0,
            "execution_scope_hash": execution_hash,
        },
    )
    updated = _locked_reservation(connection, execution_hash)
    if updated is None:  # pragma: no cover - update invariant
        raise ExecutionControlIntegrityError("risk reservation disappeared")
    return _reservation_from_row(updated)


def _control_snapshot(
    row: Mapping[str, Any],
    scope: ExecutionAccountScope,
) -> ExecutionControlSnapshot:
    return ExecutionControlSnapshot(
        scope=scope,
        safe_reason=_optional_string(row.get("safe_reason")),
        kill_reason=_optional_string(row.get("kill_reason")),
        consecutive_failures=int(row["consecutive_failures"]),
        version=int(row["version"]),
        changed_at=_aware_utc(row["changed_at"]),
        changed_by=_optional_string(row.get("changed_by")),
        control_policy_hash=_optional_string(row.get("control_policy_hash")),
        failure_threshold=(
            None
            if row.get("failure_threshold") is None
            else int(row["failure_threshold"])
        ),
        risk_snapshot_high_watermark_at=_optional_aware_utc(
            row.get("risk_snapshot_high_watermark_at")
        ),
        risk_snapshot_high_watermark_hash=_optional_string(
            row.get("risk_snapshot_high_watermark_hash")
        ),
        risk_snapshot_high_watermark_reference=_optional_string(
            row.get("risk_snapshot_high_watermark_reference")
        ),
    )


def _control_snapshot_from_event(
    row: Mapping[str, Any],
    scope: ExecutionAccountScope,
) -> ExecutionControlSnapshot:
    return ExecutionControlSnapshot(
        scope=scope,
        safe_reason=_optional_string(row.get("result_safe_reason")),
        kill_reason=_optional_string(row.get("result_kill_reason")),
        consecutive_failures=int(row["result_consecutive_failures"]),
        version=int(row["resulting_version"]),
        changed_at=_aware_utc(row["result_changed_at"]),
        changed_by=str(row["actor"]),
        control_policy_hash=_optional_string(row.get("result_control_policy_hash")),
        failure_threshold=(
            None
            if row.get("result_failure_threshold") is None
            else int(row["result_failure_threshold"])
        ),
        risk_snapshot_high_watermark_at=_optional_aware_utc(
            row.get("result_risk_snapshot_high_watermark_at")
        ),
        risk_snapshot_high_watermark_hash=_optional_string(
            row.get("result_risk_snapshot_high_watermark_hash")
        ),
        risk_snapshot_high_watermark_reference=_optional_string(
            row.get("result_risk_snapshot_high_watermark_reference")
        ),
    )


def _reservation_from_row(row: Mapping[str, Any]) -> RiskReservation:
    settled_at = row.get("settled_at")
    settlement_observed_at = row.get("settlement_observed_at")
    return RiskReservation(
        reservation_id=str(row["reservation_id"]),
        execution_scope_hash=str(row["execution_scope_hash"]),
        account_control_hash=str(row["account_control_hash"]),
        state=RiskReservationState(str(row["state"])),
        reserved_notional=Decimal(row["reserved_notional"]),
        reserved_open_slots=int(row["reserved_open_slots"]),
        policy_hash=str(row["policy_hash"]),
        risk_decision_hash=str(row["risk_decision_hash"]),
        snapshot_hash=str(row["snapshot_hash"]),
        safety_version=int(row["safety_version"]),
        client_order_id=str(row["client_order_id"]),
        position_key_hash=str(row["position_key_hash"]),
        symbol=str(row["symbol"]),
        position_mode=PositionMode(str(row["position_mode"])),
        target_direction=PositionDirection(str(row["target_direction"])),
        target_position_side=PositionSide(str(row["target_position_side"])),
        baseline_direction=(
            None
            if row.get("baseline_direction") is None
            else PositionDirection(str(row["baseline_direction"]))
        ),
        baseline_position_side=(
            None
            if row.get("baseline_position_side") is None
            else PositionSide(str(row["baseline_position_side"]))
        ),
        baseline_quantity=Decimal(row["baseline_quantity"]),
        authorized_quantity=Decimal(row["authorized_quantity"]),
        risk_reducing=_database_bool(row["risk_reducing"]),
        created_at=_aware_utc(row["created_at"]),
        updated_at=_aware_utc(row["updated_at"]),
        settled_at=None if settled_at is None else _aware_utc(settled_at),
        release_reason=_optional_string(row.get("release_reason")),
        settlement_snapshot_hash=_optional_string(
            row.get("settlement_snapshot_hash")
        ),
        settlement_observed_at=(
            None
            if settlement_observed_at is None
            else _aware_utc(settlement_observed_at)
        ),
        settlement_reference=_optional_string(row.get("settlement_reference")),
    )


def _safety_command_hash(command: SafetyCommand) -> str:
    return _sha256(
        _canonical_json(
            {
                "action": command.action.value,
                "actor": command.actor,
                "command_id": command.command_id,
                "expected_version": command.expected_version,
                "reason_code": command.reason_code,
            }
        )
    )


def _risk_decision_hash(decision: RiskDecision) -> str:
    return _sha256(
        _canonical_json(
            {
                "approved_quantity": _decimal_string(decision.approved_quantity),
                "decided_at": _timestamp(decision.decided_at),
                "intent_id": decision.intent_id,
                "notional": _decimal_string(decision.notional),
                "outcome": decision.outcome.value,
                "reason_codes": list(decision.reason_codes),
                "reference_price": _decimal_string(decision.reference_price),
                "requested_quantity": str(decision.requested_quantity),
            }
        )
    )


def _context_hash(context: ExecutionContext) -> str:
    positions = sorted(
        (
            {
                "direction": position.direction.value,
                "entry_price": _decimal_string(position.entry_price),
                "mark_price": _decimal_string(position.mark_price),
                "notional": _decimal_string(position.notional),
                "position_side": position.position_side.value,
                "quantity": str(position.quantity),
                "symbol": position.symbol,
                "updated_at_ms": position.updated_at_ms,
            }
            for position in context.account.positions
        ),
        key=lambda item: (
            str(item["symbol"]),
            str(item["direction"]),
            str(item["position_side"]),
        ),
    )
    return _sha256(
        _canonical_json(
            {
                "account": {
                    "account_type": context.account.account_type.value,
                    "can_trade": context.account.can_trade,
                    "currency": context.account.currency,
                    "positions": positions,
                    "updated_at": _timestamp(context.account.updated_at),
                    "observed_at": _timestamp(context.account.observed_at),
                    "wallet_balance": str(context.account.wallet_balance),
                },
                "position_mode": context.position_mode.value,
                "quote": {
                    "observed_at": _timestamp(context.quote.observed_at),
                    "price": str(context.quote.price),
                    "symbol": context.quote.symbol,
                },
                "rules": {
                    "maximum_quantity": str(context.rules.maximum_quantity),
                    "minimum_notional": str(context.rules.minimum_notional),
                    "minimum_quantity": str(context.rules.minimum_quantity),
                    "price_tick": str(context.rules.price_tick),
                    "quantity_step": str(context.rules.quantity_step),
                    "symbol": context.rules.symbol,
                },
            }
        )
    )


def _portfolio_notional(context: ExecutionContext) -> Decimal | None:
    total = Decimal(0)
    for position in context.account.positions:
        if position.notional is not None:
            total += abs(position.notional)
            continue
        reference_price = position.mark_price or position.entry_price
        if reference_price is None:
            return None
        total += position.quantity * abs(reference_price)
    return total


def _require_scope(scope: ExecutionAccountScope) -> None:
    if not isinstance(scope, ExecutionAccountScope):
        raise TypeError("scope must be an ExecutionAccountScope")


def _require_authorization_inputs(
    intent: OrderIntent,
    claim_token: str,
    checkpoint: ExecutionCheckpoint,
    context: ExecutionContext,
    policy: ExecutionControlPolicy,
) -> None:
    if not isinstance(intent, OrderIntent):
        raise TypeError("intent must be an OrderIntent")
    if not claim_token or len(claim_token) > 64:
        raise ValueError("claim_token is invalid")
    if not isinstance(checkpoint, ExecutionCheckpoint) or checkpoint.intent != intent:
        raise ValueError("execution checkpoint does not match intent")
    if not isinstance(context, ExecutionContext):
        raise TypeError("context must be an ExecutionContext")
    _require_control_policy(policy)


def _require_result(intent: OrderIntent, result: ExecutionResult) -> None:
    if not isinstance(intent, OrderIntent):
        raise TypeError("intent must be an OrderIntent")
    if not isinstance(result, ExecutionResult) or result.intent != intent:
        raise ValueError("execution result does not match intent")
    if result.state in {
        ExecutionState.BLOCKED,
        ExecutionState.PREFLIGHT_FAILED,
        ExecutionState.RISK_REJECTED,
    }:
        raise ValueError("result is not a post-checkpoint broker outcome")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_aware_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _aware_utc(value)


def _naive_utc(value: datetime) -> datetime:
    return _aware_utc(value).replace(tzinfo=None)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _database_bool(value: Any) -> bool:
    if value == 0:
        return False
    if value == 1:
        return True
    raise ExecutionControlIntegrityError("stored boolean value is invalid")
