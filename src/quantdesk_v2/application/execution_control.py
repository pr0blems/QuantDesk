"""Durable execution safety and account-risk control contracts.

The types in this module deliberately do not depend on a concrete journal or
database adapter.  A live execution path can therefore require this protocol
without weakening the existing in-memory shadow path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..domain.execution import (
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    OrderIntent,
)
from ..domain.safety import SafetyDecision, SafetyMode, SafetySnapshot
from ..domain.trading import (
    AccountSnapshot,
    PositionDirection,
    PositionMode,
    PositionSide,
)
from .risk import RiskPolicy
from .safety import PreflightPolicy

_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9._:@/-]{1,191}")
_MARKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
_BROKER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,63}")
_PHYSICAL_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,190}")
_COMMAND_PATTERN = re.compile(r"[A-Za-z0-9._:/-]{1,64}")


class SafetyCommandAction(StrEnum):
    ENGAGE_SAFE_MODE = "engage_safe_mode"
    RECOVER_SAFE_MODE = "recover_safe_mode"
    ENGAGE_KILL_SWITCH = "engage_kill_switch"
    RELEASE_KILL_SWITCH = "release_kill_switch"


class RiskReservationState(StrEnum):
    HELD = "held"
    COMMITTED_UNREFLECTED = "committed_unreflected"
    RELEASED = "released"
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class ExecutionControlPolicy:
    risk: RiskPolicy
    preflight: PreflightPolicy
    failure_threshold: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.risk, RiskPolicy):
            raise TypeError("risk must be a RiskPolicy")
        if not isinstance(self.preflight, PreflightPolicy):
            raise TypeError("preflight must be a PreflightPolicy")
        if (
            isinstance(self.failure_threshold, bool)
            or not isinstance(self.failure_threshold, int)
            or self.failure_threshold < 1
        ):
            raise ValueError("failure_threshold must be a positive integer")

    @property
    def fingerprint(self) -> str:
        return execution_control_policy_fingerprint(self)


@dataclass(frozen=True, slots=True)
class ExecutionAccountScope:
    """The account boundary shared by deployments for durable controls."""

    tenant_scope: str
    user_scope: str
    account_scope: str
    broker_name: str
    physical_account_id: str
    market: str
    execution_mode: ExecutionMode

    def __post_init__(self) -> None:
        for name in ("tenant_scope", "user_scope", "account_scope"):
            value = str(getattr(self, name)).strip()
            if not _SCOPE_PATTERN.fullmatch(value):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, value)
        market = str(self.market).strip().lower()
        if not _MARKET_PATTERN.fullmatch(market):
            raise ValueError("invalid market")
        object.__setattr__(self, "market", market)
        broker_name = str(self.broker_name).strip().lower()
        if not _BROKER_PATTERN.fullmatch(broker_name):
            raise ValueError("invalid broker_name")
        object.__setattr__(self, "broker_name", broker_name)
        physical_account_id = str(self.physical_account_id).strip()
        if not _PHYSICAL_ACCOUNT_PATTERN.fullmatch(physical_account_id):
            raise ValueError("invalid physical_account_id")
        object.__setattr__(self, "physical_account_id", physical_account_id)
        if not isinstance(self.execution_mode, ExecutionMode):
            object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))

    @classmethod
    def from_intent(
        cls,
        intent: OrderIntent,
        *,
        broker_name: str,
        physical_account_id: str,
    ) -> ExecutionAccountScope:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        return cls(
            tenant_scope=intent.tenant_scope,
            user_scope=intent.user_scope,
            account_scope=intent.account_scope,
            broker_name=broker_name,
            physical_account_id=physical_account_id,
            market=intent.market,
            execution_mode=intent.mode,
        )

    @property
    def physical_identity(self) -> tuple[str, str, str, str]:
        return (
            self.broker_name,
            self.market,
            self.physical_account_id,
            self.execution_mode.value,
        )

    def owns(self, intent: OrderIntent) -> bool:
        return (
            isinstance(intent, OrderIntent)
            and self.tenant_scope == intent.tenant_scope
            and self.user_scope == intent.user_scope
            and self.account_scope == intent.account_scope
            and self.market == intent.market
            and self.execution_mode is intent.mode
        )


def execution_account_scope_hash(scope: ExecutionAccountScope) -> str:
    if not isinstance(scope, ExecutionAccountScope):
        raise TypeError("scope must be an ExecutionAccountScope")
    payload = json.dumps(
        list(scope.physical_identity),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SafetyCommand:
    """One idempotent, compare-and-swap operator transition."""

    command_id: str
    action: SafetyCommandAction
    actor: str
    expected_version: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        command_id = str(self.command_id).strip()
        if not _COMMAND_PATTERN.fullmatch(command_id):
            raise ValueError("invalid safety command_id")
        object.__setattr__(self, "command_id", command_id)
        if not isinstance(self.action, SafetyCommandAction):
            object.__setattr__(self, "action", SafetyCommandAction(self.action))
        actor = str(self.actor).strip()
        if not actor or len(actor) > 191 or "\x00" in actor:
            raise ValueError("safety command actor must contain 1-191 characters")
        object.__setattr__(self, "actor", actor)
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative integer")
        if self.reason_code is None:
            if self.action is SafetyCommandAction.RECOVER_SAFE_MODE:
                reason = "operator_safe_mode_recovery"
            elif self.action is SafetyCommandAction.RELEASE_KILL_SWITCH:
                reason = "operator_kill_switch_release"
            else:
                raise ValueError("engaging a safety latch requires a reason_code")
        else:
            reason = normalize_reason_code(self.reason_code)
        object.__setattr__(self, "reason_code", reason)


@dataclass(frozen=True, slots=True)
class ExecutionControlSnapshot:
    scope: ExecutionAccountScope
    safe_reason: str | None
    kill_reason: str | None
    consecutive_failures: int
    version: int
    changed_at: datetime
    changed_by: str | None = None
    control_policy_hash: str | None = None
    failure_threshold: int | None = None
    risk_snapshot_high_watermark_at: datetime | None = None
    risk_snapshot_high_watermark_hash: str | None = None
    risk_snapshot_high_watermark_reference: str | None = None

    def __post_init__(self) -> None:
        if self.safe_reason is not None:
            object.__setattr__(self, "safe_reason", normalize_reason_code(self.safe_reason))
        if self.kill_reason is not None:
            object.__setattr__(self, "kill_reason", normalize_reason_code(self.kill_reason))
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must not be negative")
        if self.version < 0:
            raise ValueError("version must not be negative")
        _require_aware(self.changed_at, "changed_at")
        if (self.control_policy_hash is None) != (self.failure_threshold is None):
            raise ValueError("control policy binding is incomplete")
        if self.control_policy_hash is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", self.control_policy_hash):
                raise ValueError("invalid control_policy_hash")
            if self.failure_threshold is None or self.failure_threshold < 1:
                raise ValueError("invalid stored failure_threshold")
        watermark = (
            self.risk_snapshot_high_watermark_at,
            self.risk_snapshot_high_watermark_hash,
            self.risk_snapshot_high_watermark_reference,
        )
        if any(value is not None for value in watermark) and any(
            value is None for value in watermark
        ):
            raise ValueError("risk snapshot high-watermark metadata is incomplete")
        if self.risk_snapshot_high_watermark_at is not None:
            _require_aware(
                self.risk_snapshot_high_watermark_at,
                "risk_snapshot_high_watermark_at",
            )
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                str(self.risk_snapshot_high_watermark_hash),
            ):
                raise ValueError("invalid risk_snapshot_high_watermark_hash")
            reference = str(self.risk_snapshot_high_watermark_reference)
            if not reference or len(reference) > 191 or "\x00" in reference:
                raise ValueError("invalid risk_snapshot_high_watermark_reference")

    @property
    def mode(self) -> SafetyMode:
        if self.kill_reason is not None:
            return SafetyMode.KILL_SWITCH
        if self.safe_reason is not None:
            return SafetyMode.SAFE_MODE
        return SafetyMode.NORMAL

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.kill_reason is not None:
            reasons.append(self.kill_reason)
        if self.safe_reason is not None:
            reasons.append(self.safe_reason)
        return tuple(reasons)

    def safety_snapshot(self) -> SafetySnapshot:
        return SafetySnapshot(
            mode=self.mode,
            consecutive_failures=self.consecutive_failures,
            reason_codes=self.reason_codes,
            changed_at=self.changed_at,
        )

    def decision(self, intent: OrderIntent) -> SafetyDecision:
        if not self.scope.owns(intent):
            raise ValueError("intent belongs to a different execution account scope")
        if self.kill_reason is not None:
            return SafetyDecision(False, SafetyMode.KILL_SWITCH, self.kill_reason)
        if self.safe_reason is not None and not intent.risk_reducing:
            return SafetyDecision(False, SafetyMode.SAFE_MODE, self.safe_reason)
        return SafetyDecision(True, self.mode, "ok")


@dataclass(frozen=True, slots=True)
class RiskReservation:
    reservation_id: str
    execution_scope_hash: str
    account_control_hash: str
    state: RiskReservationState
    reserved_notional: Decimal
    reserved_open_slots: int
    policy_hash: str
    risk_decision_hash: str
    snapshot_hash: str
    safety_version: int
    client_order_id: str
    position_key_hash: str
    symbol: str
    position_mode: PositionMode
    target_direction: PositionDirection
    target_position_side: PositionSide
    baseline_direction: PositionDirection | None
    baseline_position_side: PositionSide | None
    baseline_quantity: Decimal
    authorized_quantity: Decimal
    risk_reducing: bool
    created_at: datetime
    updated_at: datetime
    settled_at: datetime | None = None
    release_reason: str | None = None
    settlement_snapshot_hash: str | None = None
    settlement_observed_at: datetime | None = None
    settlement_reference: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "execution_scope_hash",
            "account_control_hash",
            "policy_hash",
            "risk_decision_hash",
            "snapshot_hash",
            "position_key_hash",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", str(getattr(self, name))):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.state, RiskReservationState):
            object.__setattr__(self, "state", RiskReservationState(self.state))
        if (
            not isinstance(self.reserved_notional, Decimal)
            or not self.reserved_notional.is_finite()
            or self.reserved_notional < 0
        ):
            raise ValueError("reserved_notional must be a non-negative finite Decimal")
        if self.reserved_open_slots < 0:
            raise ValueError("reserved_open_slots must not be negative")
        if self.safety_version < 0:
            raise ValueError("safety_version must not be negative")
        symbol = str(self.symbol).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{1,31}", symbol):
            raise ValueError("invalid reservation symbol")
        object.__setattr__(self, "symbol", symbol)
        for name, enum_type in (
            ("position_mode", PositionMode),
            ("target_direction", PositionDirection),
            ("target_position_side", PositionSide),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                object.__setattr__(self, name, enum_type(value))
        if self.baseline_direction is not None and not isinstance(
            self.baseline_direction, PositionDirection
        ):
            object.__setattr__(
                self,
                "baseline_direction",
                PositionDirection(self.baseline_direction),
            )
        if self.baseline_position_side is not None and not isinstance(
            self.baseline_position_side, PositionSide
        ):
            object.__setattr__(
                self,
                "baseline_position_side",
                PositionSide(self.baseline_position_side),
            )
        for name in ("baseline_quantity", "authorized_quantity"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative finite Decimal")
        if self.authorized_quantity <= 0:
            raise ValueError("authorized_quantity must be positive")
        baseline_binding = (
            self.baseline_direction,
            self.baseline_position_side,
        )
        if self.baseline_quantity == 0 and any(
            value is not None for value in baseline_binding
        ):
            raise ValueError("zero baseline cannot carry position identity")
        if self.baseline_quantity > 0 and any(
            value is None for value in baseline_binding
        ):
            raise ValueError("positive baseline requires position identity")
        if (
            self.position_mode is PositionMode.ONE_WAY
            and self.target_position_side is not PositionSide.BOTH
        ):
            raise ValueError("one-way reservation must target BOTH position side")
        if (
            self.position_mode is PositionMode.HEDGE
            and self.target_position_side is PositionSide.BOTH
        ):
            raise ValueError("hedge reservation requires LONG or SHORT position side")
        if not isinstance(self.risk_reducing, bool):
            raise ValueError("risk_reducing must be a bool")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.settled_at is not None:
            _require_aware(self.settled_at, "settled_at")
        if self.settlement_observed_at is not None:
            _require_aware(self.settlement_observed_at, "settlement_observed_at")
        if self.settlement_snapshot_hash is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.settlement_snapshot_hash
        ):
            raise ValueError("invalid settlement_snapshot_hash")
        if self.settlement_reference is not None and (
            not self.settlement_reference
            or len(self.settlement_reference) > 191
            or "\x00" in self.settlement_reference
        ):
            raise ValueError("invalid settlement_reference")
        evidence = (
            self.settlement_snapshot_hash,
            self.settlement_observed_at,
            self.settlement_reference,
        )
        if self.state in {
            RiskReservationState.HELD,
            RiskReservationState.COMMITTED_UNREFLECTED,
        }:
            if self.settled_at is not None or self.release_reason is not None or any(evidence):
                raise ValueError("active reservation cannot carry terminal metadata")
        elif self.state is RiskReservationState.RELEASED:
            if self.settled_at is None or self.release_reason is None or any(evidence):
                raise ValueError("released reservation has invalid terminal metadata")
        elif (
            self.settled_at is None
            or self.release_reason is None
            or any(value is None for value in evidence)
        ):
            raise ValueError("settled reservation requires account snapshot evidence")


@dataclass(frozen=True, slots=True)
class ReflectedRiskEvidence:
    """Proof that committed risk appears in a newer account snapshot."""

    snapshot: AccountSnapshot
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AccountSnapshot):
            raise TypeError("reflection snapshot must be an AccountSnapshot")
        reference = str(self.reference).strip()
        if not reference or len(reference) > 191 or "\x00" in reference:
            raise ValueError("reflection reference must contain 1-191 characters")
        object.__setattr__(self, "reference", reference)

    @property
    def snapshot_hash(self) -> str:
        return account_snapshot_fingerprint(self.snapshot)

    @property
    def observed_at(self) -> datetime:
        return self.snapshot.observed_at


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    authorized: bool
    reason_code: str
    safety: ExecutionControlSnapshot
    reservation: RiskReservation | None = None

    def __post_init__(self) -> None:
        reason = normalize_reason_code(self.reason_code)
        object.__setattr__(self, "reason_code", reason)
        if self.authorized:
            if reason != "authorized" or self.reservation is None:
                raise ValueError("authorized result requires its reservation")
        elif self.reservation is not None:
            raise ValueError("rejected authorization cannot include a reservation")


class ExecutionControlError(RuntimeError):
    """Base error for fail-closed durable execution controls."""


class ExecutionControlIntegrityError(ExecutionControlError):
    """Stored scope or payload data failed integrity verification."""


class ExecutionControlStateError(ExecutionControlError):
    """A requested execution-control lifecycle transition is not allowed."""


class ExecutionAlreadyAuthorized(ExecutionControlStateError):
    """The checkpoint fence exists and only recovery may inspect its outcome."""


class SafetyVersionConflict(ExecutionControlStateError):
    """A safety command was based on a stale control version."""


@runtime_checkable
class ExecutionControlStore(Protocol):
    @property
    def durable_for_live(self) -> bool: ...

    def safety_snapshot(self, scope: ExecutionAccountScope) -> ExecutionControlSnapshot: ...

    def bind_policy(
        self,
        scope: ExecutionAccountScope,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot: ...

    def observe_account_snapshot(
        self,
        scope: ExecutionAccountScope,
        snapshot: AccountSnapshot,
        reference: str,
    ) -> ExecutionControlSnapshot: ...

    def transition_safety(
        self,
        scope: ExecutionAccountScope,
        command: SafetyCommand,
    ) -> ExecutionControlSnapshot: ...

    def record_failure(
        self,
        scope: ExecutionAccountScope,
        reason_code: str,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot: ...

    def record_success(
        self,
        scope: ExecutionAccountScope,
        *,
        policy: ExecutionControlPolicy,
    ) -> ExecutionControlSnapshot: ...

    def authorize_and_checkpoint(
        self,
        intent: OrderIntent,
        *,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
        policy: ExecutionControlPolicy,
    ) -> AuthorizationResult: ...

    def record_broker_outcome(
        self,
        intent: OrderIntent,
        *,
        result: ExecutionResult,
        claim_token: str,
    ) -> RiskReservation: ...

    def record_recovered_outcome(
        self,
        intent: OrderIntent,
        *,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> RiskReservation: ...

    def settle_reflected(
        self,
        intent: OrderIntent,
        *,
        evidence: ReflectedRiskEvidence,
    ) -> RiskReservation: ...

    def reservation(self, intent: OrderIntent) -> RiskReservation | None: ...


def normalize_reason_code(value: str) -> str:
    normalized = "".join(
        character
        for character in str(value).strip().lower()
        if character.isalnum() or character == "_"
    )[:64]
    if not normalized:
        raise ValueError("reason code cannot be empty")
    return normalized


def policy_fingerprint(policy: RiskPolicy) -> str:
    if not isinstance(policy, RiskPolicy):
        raise TypeError("policy must be a RiskPolicy")
    payload = {
        "allowed_symbols": (
            None if policy.allowed_symbols is None else sorted(policy.allowed_symbols)
        ),
        "max_notional_to_equity": str(policy.max_notional_to_equity),
        "max_open_positions": policy.max_open_positions,
        "max_order_notional": (
            None if policy.max_order_notional is None else str(policy.max_order_notional)
        ),
        "max_order_quantity": (
            None if policy.max_order_quantity is None else str(policy.max_order_quantity)
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def preflight_policy_fingerprint(policy: PreflightPolicy) -> str:
    if not isinstance(policy, PreflightPolicy):
        raise TypeError("policy must be a PreflightPolicy")
    payload = {
        "max_account_age_seconds": format(policy.max_account_age_seconds, ".17g"),
        "max_future_skew_seconds": format(policy.max_future_skew_seconds, ".17g"),
        "max_quote_age_seconds": format(policy.max_quote_age_seconds, ".17g"),
        "require_explicit_trade_permission": policy.require_explicit_trade_permission,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execution_control_policy_fingerprint(policy: ExecutionControlPolicy) -> str:
    if not isinstance(policy, ExecutionControlPolicy):
        raise TypeError("policy must be an ExecutionControlPolicy")
    canonical = json.dumps(
        {
            "failure_threshold": policy.failure_threshold,
            "preflight_policy_hash": preflight_policy_fingerprint(policy.preflight),
            "risk_policy_hash": policy_fingerprint(policy.risk),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def account_snapshot_fingerprint(snapshot: AccountSnapshot) -> str:
    if not isinstance(snapshot, AccountSnapshot):
        raise TypeError("snapshot must be an AccountSnapshot")
    positions = sorted(
        (
            {
                "break_even_price": _optional_decimal(position.break_even_price),
                "direction": position.direction.value,
                "entry_price": _optional_decimal(position.entry_price),
                "initial_margin": _optional_decimal(position.initial_margin),
                "leverage": position.leverage,
                "liquidation_price": _optional_decimal(position.liquidation_price),
                "maintenance_margin": _optional_decimal(position.maintenance_margin),
                "mark_price": _optional_decimal(position.mark_price),
                "notional": _optional_decimal(position.notional),
                "position_side": position.position_side.value,
                "quantity": str(position.quantity),
                "symbol": position.symbol,
                "unrealized_pnl": _optional_decimal(position.unrealized_pnl),
                "updated_at_ms": position.updated_at_ms,
            }
            for position in snapshot.positions
        ),
        key=lambda item: (
            str(item["symbol"]),
            str(item["direction"]),
            str(item["position_side"]),
        ),
    )
    payload = {
        "account_type": snapshot.account_type.value,
        "available_balance": str(snapshot.available_balance),
        "can_trade": snapshot.can_trade,
        "currency": snapshot.currency,
        "positions": positions,
        "unrealized_pnl": str(snapshot.unrealized_pnl),
        "updated_at": snapshot.updated_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
        "observed_at": snapshot.observed_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
        "wallet_balance": str(snapshot.wallet_balance),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reservation_state_for_result(result: ExecutionResult) -> RiskReservationState:
    """Map one broker result to the conservative reservation state it requires."""

    if not isinstance(result, ExecutionResult):
        raise TypeError("result must be an ExecutionResult")
    if result.state is ExecutionState.UNKNOWN:
        return RiskReservationState.HELD
    if result.state in {
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
    }:
        return RiskReservationState.COMMITTED_UNREFLECTED
    if result.state in {ExecutionState.CANCELED, ExecutionState.BROKER_REJECTED}:
        executed = sum((fill.quantity for fill in result.fills), start=Decimal(0))
        if result.broker_order is not None:
            executed = max(executed, result.broker_order.executed_quantity)
        if executed == 0:
            return RiskReservationState.RELEASED
        return RiskReservationState.COMMITTED_UNREFLECTED
    raise ValueError("result is not a post-checkpoint broker outcome")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
