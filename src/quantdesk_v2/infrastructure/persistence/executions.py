"""MySQL execution journal with atomic, tenant-scoped idempotency claims."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine, text

from ...domain.execution import (
    ExecutionCheckpoint,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    Fill,
    IdempotencyClaim,
    IdempotencyClaimState,
    IntentAction,
    OrderIntent,
    RecoveryClaim,
    RecoveryClaimState,
    RiskDecision,
    RiskOutcome,
    execution_result_can_advance,
)
from ...domain.safety import PreflightCheck, PreflightReport
from ...domain.trading import (
    BrokerOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)

_SCHEMA_VERSION = 1
_RECOVERABLE_EXECUTION_STATES = frozenset(
    {
        ExecutionState.UNKNOWN,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
    }
)
_RECOVERY_RESULT_STATES = _RECOVERABLE_EXECUTION_STATES | frozenset(
    {
        ExecutionState.FILLED,
        ExecutionState.CANCELED,
        ExecutionState.BROKER_REJECTED,
    }
)
_SELECT_RECORD = text(
    """
    SELECT scope_hash, tenant_scope, user_scope, account_scope, deployment_scope,
           idempotency_key, intent_id, intent_hash, intent_json, claim_status,
           claim_token, lease_expires_at, recovery_token,
           recovery_lease_expires_at, recovery_started_at, recovery_epoch,
           checkpoint_hash, checkpoint_json,
           broker_name, physical_account_id, client_order_id,
           execution_state, result_hash, result_json
    FROM execution_idempotency_records
    WHERE scope_hash = :scope_hash
    """
)


class ExecutionJournalIntegrityError(RuntimeError):
    """Persisted execution content failed identity or payload verification."""


class ExecutionJournalStateError(RuntimeError):
    """An execution journal lifecycle transition was not allowed."""


class MySqlExecutionJournal:
    """Durable implementation of the application ``IdempotencyStore`` port.

    The complete tenant/user/account/deployment/key scope is hashed into the
    primary key. The original fields remain stored and are verified on every
    read, so a hash collision cannot cross an execution boundary. An existing
    in-progress claim always remains in progress until an explicit reconciler
    resolves it; ``claim`` never performs a blind retry.
    """

    __slots__ = ("_engine", "_lease_seconds", "_recovery_lease_seconds")

    def __init__(
        self,
        engine: Engine,
        *,
        lease_seconds: int = 60,
        recovery_lease_seconds: int = 60,
    ) -> None:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RuntimeError("execution journal requires MySQL or MariaDB")
        if isinstance(lease_seconds, bool) or not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        if (
            isinstance(recovery_lease_seconds, bool)
            or not 5 <= recovery_lease_seconds <= 3600
        ):
            raise ValueError("recovery_lease_seconds must be between 5 and 3600")
        self._engine = engine
        self._lease_seconds = int(lease_seconds)
        self._recovery_lease_seconds = int(recovery_lease_seconds)

    def claim(self, intent: OrderIntent) -> IdempotencyClaim:
        _require_intent(intent)
        scope_hash = execution_scope_hash(intent)
        intent_json = serialize_order_intent(intent)
        intent_hash = _sha256(intent_json)
        claim_token = secrets.token_hex(16)
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO execution_idempotency_records (
                        scope_hash, tenant_scope, user_scope, account_scope,
                        deployment_scope, idempotency_key, intent_id, intent_hash,
                        intent_json, claim_status, claim_token, lease_expires_at,
                        claimed_at, updated_at
                    ) VALUES (
                        :scope_hash, :tenant_scope, :user_scope, :account_scope,
                        :deployment_scope, :idempotency_key, :intent_id, :intent_hash,
                        :intent_json, 'in_progress', :claim_token,
                        TIMESTAMPADD(SECOND, :lease_seconds, UTC_TIMESTAMP(6)),
                        UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                    )
                    ON DUPLICATE KEY UPDATE
                        scope_hash = VALUES(scope_hash)
                    """
                ),
                {
                    **_intent_parameters(intent, scope_hash, intent_hash, intent_json),
                    "claim_token": claim_token,
                    "lease_seconds": self._lease_seconds,
                },
            )
            row = _locked_record(connection, scope_hash)
            _verify_record_identity(row, intent, intent_json, intent_hash)
            if str(row.get("claim_token")) == claim_token:
                return IdempotencyClaim(
                    IdempotencyClaimState.CLAIMED,
                    claim_token=claim_token,
                )
            status = str(row["claim_status"])
            if status == "completed":
                return IdempotencyClaim(
                    IdempotencyClaimState.COMPLETED,
                    _result_from_record(row, intent),
                )
            if status != "in_progress":
                raise ExecutionJournalIntegrityError("execution claim status is invalid")
            if row.get("checkpoint_json") is None:
                reclaimed = connection.execute(
                    text(
                        """
                        UPDATE execution_idempotency_records
                        SET claim_token = :claim_token,
                            lease_expires_at = TIMESTAMPADD(
                                SECOND, :lease_seconds, UTC_TIMESTAMP(6)
                            ),
                            claimed_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE scope_hash = :scope_hash
                          AND claim_status = 'in_progress'
                          AND checkpoint_json IS NULL
                          AND lease_expires_at <= UTC_TIMESTAMP(6)
                        """
                    ),
                    {
                        "scope_hash": scope_hash,
                        "claim_token": claim_token,
                        "lease_seconds": self._lease_seconds,
                    },
                )
                if reclaimed.rowcount == 1:
                    return IdempotencyClaim(
                        IdempotencyClaimState.CLAIMED,
                        claim_token=claim_token,
                    )
            return IdempotencyClaim(IdempotencyClaimState.IN_PROGRESS)

    def save_checkpoint(
        self,
        intent: OrderIntent,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        _require_checkpoint(intent, checkpoint)
        checkpoint_json = serialize_execution_checkpoint(checkpoint)
        checkpoint_hash = _sha256(checkpoint_json)
        scope_hash = execution_scope_hash(intent)
        with self._engine.begin() as connection:
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
                      AND intent_hash = :intent_hash
                      AND claim_status = 'in_progress'
                      AND claim_token = :claim_token
                      AND checkpoint_json IS NULL
                    """
                ),
                {
                    "scope_hash": scope_hash,
                    "intent_hash": _sha256(serialize_order_intent(intent)),
                    "claim_token": claim_token,
                    "checkpoint_hash": checkpoint_hash,
                    "checkpoint_json": checkpoint_json,
                    "broker_name": checkpoint.broker_name,
                    "physical_account_id": checkpoint.physical_account_id,
                    "client_order_id": checkpoint.client_order_id,
                },
            )
            if updated.rowcount == 1:
                return
            row = _locked_record(connection, scope_hash)
            _verify_record_identity(
                row,
                intent,
                serialize_order_intent(intent),
                _sha256(serialize_order_intent(intent)),
            )
            if (
                str(row.get("claim_token")) == claim_token
                and str(row.get("checkpoint_hash")) == checkpoint_hash
                and str(row.get("checkpoint_json")) == checkpoint_json
                and str(row.get("physical_account_id"))
                == checkpoint.physical_account_id
            ):
                return
            raise ExecutionJournalStateError("execution checkpoint cannot be saved")

    def load_checkpoint(self, intent: OrderIntent) -> ExecutionCheckpoint | None:
        _require_intent(intent)
        scope_hash = execution_scope_hash(intent)
        with self._engine.connect() as connection:
            selected = connection.execute(
                _SELECT_RECORD,
                {"scope_hash": scope_hash},
            ).mappings().one_or_none()
        if selected is None:
            return None
        row = dict(selected)
        intent_json = serialize_order_intent(intent)
        _verify_record_identity(row, intent, intent_json, _sha256(intent_json))
        if row.get("checkpoint_json") is None:
            return None
        return _checkpoint_from_record(row, intent)

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
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT scope_hash, tenant_scope, user_scope, account_scope,
                           deployment_scope, idempotency_key, intent_id, intent_hash,
                           intent_json, claim_status, claim_token, lease_expires_at,
                           recovery_token, recovery_lease_expires_at,
                           recovery_started_at, recovery_epoch,
                           checkpoint_hash, checkpoint_json, broker_name,
                           physical_account_id, client_order_id,
                           execution_state, result_hash, result_json
                    FROM execution_idempotency_records
                    WHERE tenant_scope = :tenant_scope
                      AND user_scope = :user_scope
                      AND account_scope = :account_scope
                      AND checkpoint_json IS NOT NULL
                      AND (
                          claim_status = 'in_progress'
                          OR (
                              claim_status = 'completed'
                              AND execution_state IN (
                                  'unknown', 'submitted', 'partially_filled'
                              )
                          )
                      )
                    ORDER BY checkpointed_at, scope_hash
                    LIMIT :limit
                    """
                ),
                {
                    "tenant_scope": tenant_scope,
                    "user_scope": user_scope,
                    "account_scope": account_scope,
                    "limit": limit,
                },
            ).mappings().all()
        checkpoints: list[ExecutionCheckpoint] = []
        for selected in rows:
            row = dict(selected)
            intent = deserialize_order_intent(str(row["intent_json"]))
            intent_json = serialize_order_intent(intent)
            _verify_record_identity(row, intent, intent_json, _sha256(intent_json))
            checkpoints.append(_checkpoint_from_record(row, intent))
        return tuple(checkpoints)

    def complete(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        claim_token: str,
    ) -> None:
        _require_matching_result(intent, result)
        result_json = serialize_execution_result(result)
        result_hash = _sha256(result_json)
        scope_hash = execution_scope_hash(intent)
        with self._engine.begin() as connection:
            updated = connection.execute(
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
                      AND intent_hash = :intent_hash
                      AND claim_status = 'in_progress'
                      AND claim_token = :claim_token
                      AND recovery_started_at IS NULL
                    """
                ),
                {
                    "scope_hash": scope_hash,
                    "intent_hash": _sha256(serialize_order_intent(intent)),
                    "claim_token": claim_token,
                    "execution_state": result.state.value,
                    "result_hash": result_hash,
                    "result_json": result_json,
                },
            )
            if updated.rowcount == 1:
                return
            row = _locked_record(connection, scope_hash)
            _verify_record_identity(
                row,
                intent,
                serialize_order_intent(intent),
                _sha256(serialize_order_intent(intent)),
            )
            if row.get("recovery_started_at") is not None:
                raise ExecutionJournalStateError(
                    "execution claim owner was fenced by recovery"
                )
            if (
                str(row["claim_status"]) == "completed"
                and str(row["result_hash"]) == result_hash
                and str(row["result_json"]) == result_json
            ):
                return
            raise ExecutionJournalStateError("execution claim cannot be completed")

    def claim_recovery(self, intent: OrderIntent) -> RecoveryClaim:
        """Acquire one expiring recovery owner lease and fence the live owner."""

        _require_intent(intent)
        scope_hash = execution_scope_hash(intent)
        intent_json = serialize_order_intent(intent)
        intent_hash = _sha256(intent_json)
        recovery_token = secrets.token_hex(16)
        with self._engine.begin() as connection:
            selected = connection.execute(
                text(str(_SELECT_RECORD) + " FOR UPDATE"),
                {"scope_hash": scope_hash},
            ).mappings().one_or_none()
            if selected is None:
                return RecoveryClaim(RecoveryClaimState.NOT_RECOVERABLE)
            row = dict(selected)
            _verify_record_identity(row, intent, intent_json, intent_hash)
            if not _record_is_recoverable(row):
                return RecoveryClaim(RecoveryClaimState.NOT_RECOVERABLE)
            updated = connection.execute(
                text(
                    """
                    UPDATE execution_idempotency_records
                    SET recovery_token = :recovery_token,
                        recovery_lease_expires_at = TIMESTAMPADD(
                            SECOND, :recovery_lease_seconds, UTC_TIMESTAMP(6)
                        ),
                        recovery_started_at = COALESCE(
                            recovery_started_at, UTC_TIMESTAMP(6)
                        ),
                        recovery_epoch = recovery_epoch + 1,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE scope_hash = :scope_hash
                      AND intent_hash = :intent_hash
                      AND checkpoint_json IS NOT NULL
                      AND (
                          recovery_token IS NULL
                          OR recovery_lease_expires_at <= UTC_TIMESTAMP(6)
                      )
                      AND (
                          claim_status = 'in_progress'
                          OR (
                              claim_status = 'completed'
                              AND execution_state IN (
                                  'unknown', 'submitted', 'partially_filled'
                              )
                          )
                      )
                    """
                ),
                {
                    "scope_hash": scope_hash,
                    "intent_hash": intent_hash,
                    "recovery_token": recovery_token,
                    "recovery_lease_seconds": self._recovery_lease_seconds,
                },
            )
            if updated.rowcount == 1:
                return RecoveryClaim(
                    RecoveryClaimState.ACQUIRED,
                    recovery_token=recovery_token,
                )
            return RecoveryClaim(RecoveryClaimState.IN_PROGRESS)

    def complete_recovered(
        self,
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        _require_checkpoint(intent, checkpoint)
        _require_recovered_result(intent, checkpoint, result)
        checkpoint_json = serialize_execution_checkpoint(checkpoint)
        checkpoint_hash = _sha256(checkpoint_json)
        result_json = serialize_execution_result(result)
        result_hash = _sha256(result_json)
        scope_hash = execution_scope_hash(intent)
        with self._engine.begin() as connection:
            updated = connection.execute(
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
                      AND claim_status = 'in_progress'
                      AND checkpoint_hash = :checkpoint_hash
                      AND checkpoint_json = :checkpoint_json
                      AND recovery_token = :recovery_token
                      AND recovery_lease_expires_at > UTC_TIMESTAMP(6)
                    """
                ),
                {
                    "scope_hash": scope_hash,
                    "checkpoint_hash": checkpoint_hash,
                    "checkpoint_json": checkpoint_json,
                    "recovery_token": recovery_token,
                    "execution_state": result.state.value,
                    "result_hash": result_hash,
                    "result_json": result_json,
                },
            )
            if updated.rowcount == 1:
                return
            row = _locked_record(connection, scope_hash)
            _verify_record_identity(
                row,
                intent,
                serialize_order_intent(intent),
                _sha256(serialize_order_intent(intent)),
            )
            raise ExecutionJournalStateError("checkpointed execution cannot be recovered")

    def lookup(self, intent: OrderIntent) -> ExecutionResult | None:
        _require_intent(intent)
        scope_hash = execution_scope_hash(intent)
        with self._engine.connect() as connection:
            selected = connection.execute(
                _SELECT_RECORD,
                {"scope_hash": scope_hash},
            ).mappings().one_or_none()
        if selected is None:
            return None
        row = dict(selected)
        intent_json = serialize_order_intent(intent)
        _verify_record_identity(row, intent, intent_json, _sha256(intent_json))
        status = str(row["claim_status"])
        if status == "in_progress":
            return None
        if status != "completed":
            raise ExecutionJournalIntegrityError("execution claim status is invalid")
        return _result_from_record(row, intent)

    def advance_recovered(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        _require_matching_result(intent, result)
        result_json = serialize_execution_result(result)
        result_hash = _sha256(result_json)
        scope_hash = execution_scope_hash(intent)
        intent_json = serialize_order_intent(intent)
        intent_hash = _sha256(intent_json)
        with self._engine.begin() as connection:
            row = _locked_record(connection, scope_hash)
            _verify_record_identity(row, intent, intent_json, intent_hash)
            if str(row["claim_status"]) != "completed":
                raise ExecutionJournalStateError("execution result is not recoverable")
            previous = _result_from_record(row, intent)
            checkpoint = _checkpoint_from_record(row, intent)
            _require_recovered_result(intent, checkpoint, result)
            identical = (
                str(row.get("result_hash") or "") == result_hash
                and str(row.get("result_json") or "") == result_json
            )
            if identical and not _record_is_recoverable(row):
                # The prior transaction may have committed a terminal result
                # even though its acknowledgement never reached the caller.
                return
            if not _record_is_recoverable(row):
                raise ExecutionJournalStateError("execution result is not recoverable")
            if not identical and not execution_result_can_advance(previous, result):
                raise ExecutionJournalStateError(
                    "recovered execution result cannot move backward"
                )
            updated = connection.execute(
                text(
                    """
                    UPDATE execution_idempotency_records
                    SET execution_state = :execution_state,
                        result_hash = :result_hash,
                        result_json = :result_json,
                        completed_at = UTC_TIMESTAMP(6),
                        recovery_token = NULL,
                        recovery_lease_expires_at = NULL,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE scope_hash = :scope_hash
                      AND intent_hash = :intent_hash
                      AND claim_status = 'completed'
                      AND execution_state IN (
                          'unknown', 'submitted', 'partially_filled'
                      )
                      AND recovery_token = :recovery_token
                      AND recovery_lease_expires_at > UTC_TIMESTAMP(6)
                    """
                ),
                {
                    "scope_hash": scope_hash,
                    "intent_hash": intent_hash,
                    "recovery_token": recovery_token,
                    "execution_state": result.state.value,
                    "result_hash": result_hash,
                    "result_json": result_json,
                },
            )
            if updated.rowcount == 1:
                return
            if identical:
                active_lease = connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM execution_idempotency_records
                        WHERE scope_hash = :scope_hash
                          AND recovery_token IS NOT NULL
                          AND recovery_lease_expires_at > UTC_TIMESTAMP(6)
                        """
                    ),
                    {"scope_hash": scope_hash},
                ).scalar_one()
                if int(active_lease) == 0:
                    return
            raise ExecutionJournalStateError("execution recovery owner token is stale")

    def resolve_unknown(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None:
        """Compatibility alias for token-fenced recovery callers."""

        self.advance_recovered(intent, result, recovery_token)


def execution_scope_hash(intent: OrderIntent) -> str:
    _require_intent(intent)
    return _sha256(_canonical_json(list(intent.idempotency_scope)))


def serialize_order_intent(intent: OrderIntent) -> str:
    _require_intent(intent)
    return _canonical_json(_intent_payload(intent))


def deserialize_order_intent(payload_json: str) -> OrderIntent:
    try:
        return _intent_from_payload(_json_object(payload_json))
    except ExecutionJournalIntegrityError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ExecutionJournalIntegrityError("execution intent payload is invalid") from exc


def serialize_execution_checkpoint(checkpoint: ExecutionCheckpoint) -> str:
    if not isinstance(checkpoint, ExecutionCheckpoint):
        raise TypeError("checkpoint must be an ExecutionCheckpoint")
    return _canonical_json(_checkpoint_payload(checkpoint))


def deserialize_execution_checkpoint(payload_json: str) -> ExecutionCheckpoint:
    try:
        return _checkpoint_from_payload(_json_object(payload_json))
    except ExecutionJournalIntegrityError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ExecutionJournalIntegrityError("execution checkpoint payload is invalid") from exc


def serialize_execution_result(result: ExecutionResult) -> str:
    if not isinstance(result, ExecutionResult):
        raise TypeError("result must be an ExecutionResult")
    return _canonical_json(_result_payload(result))


def deserialize_execution_result(payload_json: str) -> ExecutionResult:
    try:
        return _result_from_payload(_json_object(payload_json))
    except ExecutionJournalIntegrityError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ExecutionJournalIntegrityError("execution result payload is invalid") from exc


def _intent_parameters(
    intent: OrderIntent,
    scope_hash: str,
    intent_hash: str,
    intent_json: str,
) -> dict[str, str]:
    return {
        "scope_hash": scope_hash,
        "tenant_scope": intent.tenant_scope,
        "user_scope": intent.user_scope,
        "account_scope": intent.account_scope,
        "deployment_scope": intent.deployment_scope,
        "idempotency_key": intent.idempotency_key,
        "intent_id": intent.intent_id,
        "intent_hash": intent_hash,
        "intent_json": intent_json,
    }


def _locked_record(connection: Any, scope_hash: str) -> dict[str, Any]:
    selected = connection.execute(
        text(str(_SELECT_RECORD) + " FOR UPDATE"),
        {"scope_hash": scope_hash},
    ).mappings().one_or_none()
    if selected is None:
        raise ExecutionJournalStateError("execution claim does not exist")
    return dict(selected)


def _record_is_recoverable(row: Mapping[str, Any]) -> bool:
    if row.get("checkpoint_json") is None:
        return False
    status = str(row.get("claim_status"))
    if status == "in_progress":
        return True
    return status == "completed" and str(row.get("execution_state")) in {
        state.value for state in _RECOVERABLE_EXECUTION_STATES
    }


def _verify_record_identity(
    row: Mapping[str, Any],
    intent: OrderIntent,
    expected_json: str,
    expected_hash: str,
) -> None:
    expected = {
        "scope_hash": execution_scope_hash(intent),
        "tenant_scope": intent.tenant_scope,
        "user_scope": intent.user_scope,
        "account_scope": intent.account_scope,
        "deployment_scope": intent.deployment_scope,
        "idempotency_key": intent.idempotency_key,
        "intent_id": intent.intent_id,
        "intent_hash": expected_hash,
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise ExecutionJournalIntegrityError(
            "execution idempotency scope is bound to different intent data"
        )
    stored_json = str(row.get("intent_json") or "")
    if stored_json != expected_json or _sha256(stored_json) != expected_hash:
        raise ExecutionJournalIntegrityError("execution intent hash verification failed")
    if deserialize_order_intent(stored_json) != intent:
        raise ExecutionJournalIntegrityError("execution intent did not round-trip")


def _result_from_record(row: Mapping[str, Any], intent: OrderIntent) -> ExecutionResult:
    result_json = str(row.get("result_json") or "")
    result_hash = str(row.get("result_hash") or "")
    if not result_json or _sha256(result_json) != result_hash:
        raise ExecutionJournalIntegrityError("execution result hash verification failed")
    result = deserialize_execution_result(result_json)
    if result.intent != intent:
        raise ExecutionJournalIntegrityError("execution result belongs to a different intent")
    if str(row.get("execution_state")) != result.state.value:
        raise ExecutionJournalIntegrityError("execution state differs from result payload")
    return result


def _checkpoint_from_record(
    row: Mapping[str, Any], intent: OrderIntent
) -> ExecutionCheckpoint:
    checkpoint_json = str(row.get("checkpoint_json") or "")
    checkpoint_hash = str(row.get("checkpoint_hash") or "")
    if not checkpoint_json or _sha256(checkpoint_json) != checkpoint_hash:
        raise ExecutionJournalIntegrityError("execution checkpoint hash verification failed")
    checkpoint = deserialize_execution_checkpoint(checkpoint_json)
    if checkpoint.intent != intent:
        raise ExecutionJournalIntegrityError("execution checkpoint belongs to another intent")
    if str(row.get("broker_name")) != checkpoint.broker_name:
        raise ExecutionJournalIntegrityError("checkpoint broker binding differs from record")
    if str(row.get("physical_account_id")) != checkpoint.physical_account_id:
        raise ExecutionJournalIntegrityError(
            "checkpoint physical account binding differs from record"
        )
    if str(row.get("client_order_id")) != checkpoint.client_order_id:
        raise ExecutionJournalIntegrityError("checkpoint client order id differs from record")
    return checkpoint


def _require_intent(intent: OrderIntent) -> None:
    if not isinstance(intent, OrderIntent):
        raise TypeError("intent must be an OrderIntent")


def _require_matching_result(intent: OrderIntent, result: ExecutionResult) -> None:
    _require_intent(intent)
    if not isinstance(result, ExecutionResult):
        raise TypeError("result must be an ExecutionResult")
    if result.intent != intent:
        raise ValueError("execution result does not match intent")


def _require_checkpoint(intent: OrderIntent, checkpoint: ExecutionCheckpoint) -> None:
    _require_intent(intent)
    if not isinstance(checkpoint, ExecutionCheckpoint):
        raise TypeError("checkpoint must be an ExecutionCheckpoint")
    if checkpoint.intent != intent:
        raise ValueError("execution checkpoint does not match intent")


def _require_recovered_result(
    intent: OrderIntent,
    checkpoint: ExecutionCheckpoint,
    result: ExecutionResult,
) -> None:
    _require_matching_result(intent, result)
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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_object(value: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutionJournalIntegrityError("execution payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ExecutionJournalIntegrityError("execution payload must be a JSON object")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ExecutionJournalIntegrityError("unsupported execution payload schema")
    return payload


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ExecutionJournalIntegrityError("execution datetime must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionJournalIntegrityError("execution datetime must include timezone")
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ExecutionJournalIntegrityError("execution decimal must be a string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ExecutionJournalIntegrityError("execution decimal must be finite")
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionJournalIntegrityError("optional execution value must be a string")
    return value


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ExecutionJournalIntegrityError("execution boolean has an invalid type")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionJournalIntegrityError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExecutionJournalIntegrityError(f"{name} must be an array")
    return value


def _intent_payload(intent: OrderIntent) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "type": "order_intent",
        "intent_id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "strategy_version_id": intent.strategy_version_id,
        "tenant_scope": intent.tenant_scope,
        "user_scope": intent.user_scope,
        "account_scope": intent.account_scope,
        "deployment_scope": intent.deployment_scope,
        "mode": intent.mode.value,
        "market": intent.market,
        "symbol": intent.symbol,
        "timeframe": intent.timeframe,
        "action": intent.action.value,
        "side": intent.side.value,
        "quantity": str(intent.quantity),
        "signal_time": _timestamp(intent.signal_time),
        "valid_until": _timestamp(intent.valid_until),
        "created_at": _timestamp(intent.created_at),
        "position_side": intent.position_side.value,
        "order_type": intent.order_type.value,
        "trigger_price": None if intent.trigger_price is None else str(intent.trigger_price),
        "reduce_only": intent.reduce_only,
        "max_slippage_bps": (
            None if intent.max_slippage_bps is None else str(intent.max_slippage_bps)
        ),
    }


def _intent_from_payload(payload: Mapping[str, Any]) -> OrderIntent:
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("type") != "order_intent"
    ):
        raise ExecutionJournalIntegrityError("execution intent type is invalid")
    return OrderIntent(
        intent_id=str(payload["intent_id"]),
        idempotency_key=str(payload["idempotency_key"]),
        strategy_version_id=str(payload["strategy_version_id"]),
        tenant_scope=str(payload["tenant_scope"]),
        user_scope=str(payload["user_scope"]),
        account_scope=str(payload["account_scope"]),
        deployment_scope=str(payload["deployment_scope"]),
        mode=ExecutionMode(str(payload["mode"])),
        market=str(payload["market"]),
        symbol=str(payload["symbol"]),
        timeframe=str(payload["timeframe"]),
        action=IntentAction(str(payload["action"])),
        side=OrderSide(str(payload["side"])),
        quantity=_decimal(payload["quantity"]),
        signal_time=_parse_timestamp(payload["signal_time"]),
        valid_until=_parse_timestamp(payload["valid_until"]),
        created_at=_parse_timestamp(payload["created_at"]),
        position_side=PositionSide(str(payload["position_side"])),
        order_type=OrderType(str(payload["order_type"])),
        trigger_price=_optional_decimal(payload.get("trigger_price")),
        reduce_only=_boolean(payload["reduce_only"]),
        max_slippage_bps=_optional_decimal(payload.get("max_slippage_bps")),
    )


def _result_payload(result: ExecutionResult) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "type": "execution_result",
        "intent": _intent_payload(result.intent),
        "state": result.state.value,
        "preflight": _preflight_payload(result.preflight),
        "risk_decision": _risk_payload(result.risk_decision),
        "broker_order": _broker_order_payload(result.broker_order),
        "fills": [_fill_payload(fill) for fill in result.fills],
        "error_code": result.error_code,
    }


def _checkpoint_payload(checkpoint: ExecutionCheckpoint) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "type": "execution_checkpoint",
        "intent": _intent_payload(checkpoint.intent),
        "preflight": _preflight_payload(checkpoint.preflight),
        "risk_decision": _risk_payload(checkpoint.risk_decision),
        "broker_name": checkpoint.broker_name,
        "physical_account_id": checkpoint.physical_account_id,
        "client_order_id": checkpoint.client_order_id,
        "checkpointed_at": _timestamp(checkpoint.checkpointed_at),
    }


def _checkpoint_from_payload(payload: Mapping[str, Any]) -> ExecutionCheckpoint:
    if payload.get("type") != "execution_checkpoint":
        raise ExecutionJournalIntegrityError("execution checkpoint type is invalid")
    preflight = _preflight_from_payload(payload.get("preflight"))
    risk = _risk_from_payload(payload.get("risk_decision"))
    if preflight is None or risk is None:
        raise ExecutionJournalIntegrityError("execution checkpoint is incomplete")
    return ExecutionCheckpoint(
        intent=_intent_from_payload(_mapping(payload["intent"], "intent")),
        preflight=preflight,
        risk_decision=risk,
        broker_name=str(payload["broker_name"]),
        physical_account_id=str(payload["physical_account_id"]),
        client_order_id=str(payload["client_order_id"]),
        checkpointed_at=_parse_timestamp(payload["checkpointed_at"]),
    )


def _result_from_payload(payload: Mapping[str, Any]) -> ExecutionResult:
    if payload.get("type") != "execution_result":
        raise ExecutionJournalIntegrityError("execution result type is invalid")
    return ExecutionResult(
        intent=_intent_from_payload(_mapping(payload["intent"], "intent")),
        state=ExecutionState(str(payload["state"])),
        preflight=_preflight_from_payload(payload.get("preflight")),
        risk_decision=_risk_from_payload(payload.get("risk_decision")),
        broker_order=_broker_order_from_payload(payload.get("broker_order")),
        fills=tuple(
            _fill_from_payload(_mapping(item, "fill"))
            for item in _sequence(payload["fills"], "fills")
        ),
        error_code=_optional_string(payload.get("error_code")),
    )


def _preflight_payload(value: PreflightReport | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "checked_at": _timestamp(value.checked_at),
        "checks": [
            {"name": check.name, "passed": check.passed, "code": check.code}
            for check in value.checks
        ],
    }


def _preflight_from_payload(value: Any) -> PreflightReport | None:
    if value is None:
        return None
    payload = _mapping(value, "preflight")
    return PreflightReport(
        checked_at=_parse_timestamp(payload["checked_at"]),
        checks=tuple(
            PreflightCheck(
                name=str(check["name"]),
                passed=_boolean(check["passed"]),
                code=str(check["code"]),
            )
            for item in _sequence(payload["checks"], "preflight checks")
            for check in (_mapping(item, "preflight check"),)
        ),
    )


def _risk_payload(value: RiskDecision | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "intent_id": value.intent_id,
        "outcome": value.outcome.value,
        "reason_codes": list(value.reason_codes),
        "decided_at": _timestamp(value.decided_at),
        "requested_quantity": str(value.requested_quantity),
        "approved_quantity": (
            None if value.approved_quantity is None else str(value.approved_quantity)
        ),
        "reference_price": None if value.reference_price is None else str(value.reference_price),
        "notional": None if value.notional is None else str(value.notional),
    }


def _risk_from_payload(value: Any) -> RiskDecision | None:
    if value is None:
        return None
    payload = _mapping(value, "risk decision")
    reasons = _sequence(payload["reason_codes"], "risk reason codes")
    if not all(isinstance(reason, str) for reason in reasons):
        raise ExecutionJournalIntegrityError("risk reason codes must be strings")
    return RiskDecision(
        intent_id=str(payload["intent_id"]),
        outcome=RiskOutcome(str(payload["outcome"])),
        reason_codes=tuple(reasons),
        decided_at=_parse_timestamp(payload["decided_at"]),
        requested_quantity=_decimal(payload["requested_quantity"]),
        approved_quantity=_optional_decimal(payload.get("approved_quantity")),
        reference_price=_optional_decimal(payload.get("reference_price")),
        notional=_optional_decimal(payload.get("notional")),
    )


def _broker_order_payload(value: BrokerOrder | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "reference": {
            "client_order_id": value.reference.client_order_id,
            "symbol": value.reference.symbol,
            "conditional": value.reference.conditional,
        },
        "exchange_order_id": value.exchange_order_id,
        "symbol": value.symbol,
        "side": value.side.value,
        "position_side": value.position_side.value,
        "order_type": value.order_type.value,
        "status": value.status.value,
        "exchange_status": value.exchange_status,
        "quantity": str(value.quantity),
        "executed_quantity": str(value.executed_quantity),
        "average_price": None if value.average_price is None else str(value.average_price),
        "trigger_price": None if value.trigger_price is None else str(value.trigger_price),
        "reduce_only": value.reduce_only,
    }


def _broker_order_from_payload(value: Any) -> BrokerOrder | None:
    if value is None:
        return None
    payload = _mapping(value, "broker order")
    reference = _mapping(payload["reference"], "order reference")
    return BrokerOrder(
        reference=OrderReference(
            client_order_id=str(reference["client_order_id"]),
            symbol=_optional_string(reference.get("symbol")),
            conditional=_boolean(reference["conditional"]),
        ),
        exchange_order_id=_optional_string(payload.get("exchange_order_id")),
        symbol=str(payload["symbol"]),
        side=OrderSide(str(payload["side"])),
        position_side=PositionSide(str(payload["position_side"])),
        order_type=OrderType(str(payload["order_type"])),
        status=OrderStatus(str(payload["status"])),
        exchange_status=str(payload["exchange_status"]),
        quantity=_decimal(payload["quantity"]),
        executed_quantity=_decimal(payload["executed_quantity"]),
        average_price=_optional_decimal(payload.get("average_price")),
        trigger_price=_optional_decimal(payload.get("trigger_price")),
        reduce_only=_boolean(payload["reduce_only"]),
    )


def _fill_payload(value: Fill) -> dict[str, Any]:
    return {
        "fill_id": value.fill_id,
        "client_order_id": value.client_order_id,
        "exchange_order_id": value.exchange_order_id,
        "symbol": value.symbol,
        "side": value.side.value,
        "quantity": str(value.quantity),
        "price": str(value.price),
        "occurred_at": _timestamp(value.occurred_at),
        "simulated": value.simulated,
        "fee": str(value.fee),
        "fee_asset": value.fee_asset,
    }


def _fill_from_payload(payload: Mapping[str, Any]) -> Fill:
    return Fill(
        fill_id=str(payload["fill_id"]),
        client_order_id=str(payload["client_order_id"]),
        exchange_order_id=_optional_string(payload.get("exchange_order_id")),
        symbol=str(payload["symbol"]),
        side=OrderSide(str(payload["side"])),
        quantity=_decimal(payload["quantity"]),
        price=_decimal(payload["price"]),
        occurred_at=_parse_timestamp(payload["occurred_at"]),
        simulated=_boolean(payload["simulated"]),
        fee=_decimal(payload["fee"]),
        fee_asset=_optional_string(payload.get("fee_asset")),
    )
