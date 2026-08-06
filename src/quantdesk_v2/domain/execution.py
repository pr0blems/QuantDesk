"""Exchange-neutral execution state and immutable audit records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .safety import PreflightReport
from .trading import (
    AccountSnapshot,
    BrokerOrder,
    InstrumentRules,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionMode,
    PositionSide,
    Quote,
)

_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:/-]{1,191}")
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9._:@/-]{1,191}")
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}")
_MARKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
_PHYSICAL_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,190}")
_TIMEFRAME_PATTERN = re.compile(r"[1-9][0-9]*[mhdw]")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _positive_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")


class ExecutionMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class IntentAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    STOP = "stop"
    TAKE_PROFIT = "take_profit"


class RiskOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ExecutionState(StrEnum):
    BLOCKED = "blocked"
    PREFLIGHT_FAILED = "preflight_failed"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    BROKER_REJECTED = "broker_rejected"
    UNKNOWN = "unknown"


class IdempotencyClaimState(StrEnum):
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class RecoveryClaimState(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    NOT_RECOVERABLE = "not_recoverable"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """One immutable request to change portfolio exposure."""

    intent_id: str
    idempotency_key: str
    strategy_version_id: str
    tenant_scope: str
    user_scope: str
    account_scope: str
    deployment_scope: str
    mode: ExecutionMode
    market: str
    symbol: str
    timeframe: str
    action: IntentAction
    side: OrderSide
    quantity: Decimal
    signal_time: datetime
    valid_until: datetime
    created_at: datetime
    position_side: PositionSide = PositionSide.BOTH
    order_type: OrderType = OrderType.MARKET
    trigger_price: Decimal | None = None
    reduce_only: bool = False
    max_slippage_bps: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "idempotency_key", "strategy_version_id"):
            value = str(getattr(self, name)).strip()
            if not _KEY_PATTERN.fullmatch(value):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, value)
        for name in ("tenant_scope", "user_scope", "account_scope", "deployment_scope"):
            value = str(getattr(self, name)).strip()
            if not _SCOPE_PATTERN.fullmatch(value):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, value)
        market = str(self.market).strip().lower()
        if not _MARKET_PATTERN.fullmatch(market):
            raise ValueError("invalid market")
        object.__setattr__(self, "market", market)
        symbol = str(self.symbol).strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("invalid instrument symbol")
        object.__setattr__(self, "symbol", symbol)
        timeframe = str(self.timeframe).strip().lower()
        if not _TIMEFRAME_PATTERN.fullmatch(timeframe):
            raise ValueError("invalid timeframe")
        object.__setattr__(self, "timeframe", timeframe)
        _positive_decimal(self.quantity, "quantity")
        _aware(self.signal_time, "signal_time")
        _aware(self.valid_until, "valid_until")
        _aware(self.created_at, "created_at")
        if self.valid_until <= self.signal_time:
            raise ValueError("valid_until must be later than signal_time")
        if self.created_at > self.valid_until:
            raise ValueError("created_at cannot be later than valid_until")
        if self.order_type is OrderType.MARKET:
            if self.trigger_price is not None:
                raise ValueError("market orders cannot have a trigger price")
            if self.action not in {IntentAction.OPEN, IntentAction.CLOSE}:
                raise ValueError("market intent action must be open or close")
        elif self.order_type in {OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET}:
            if self.trigger_price is None:
                raise ValueError("trigger orders require a trigger price")
            _positive_decimal(self.trigger_price, "trigger_price")
            expected_action = (
                IntentAction.STOP
                if self.order_type is OrderType.STOP_MARKET
                else IntentAction.TAKE_PROFIT
            )
            if self.action is not expected_action:
                raise ValueError("intent action does not match trigger order type")
        else:
            raise ValueError("unsupported execution order type")
        if self.action is IntentAction.OPEN and self.reduce_only:
            raise ValueError("an open intent cannot be reduce-only")
        if self.action is IntentAction.OPEN:
            if self.position_side is PositionSide.LONG and self.side is not OrderSide.BUY:
                raise ValueError("opening a LONG hedge position requires BUY")
            if self.position_side is PositionSide.SHORT and self.side is not OrderSide.SELL:
                raise ValueError("opening a SHORT hedge position requires SELL")
        elif self.action is IntentAction.CLOSE:
            if self.position_side is PositionSide.BOTH and not self.reduce_only:
                raise ValueError("one-way close intents must be reduce-only")
            if self.position_side is not PositionSide.BOTH and self.reduce_only:
                raise ValueError("hedge-mode close intents cannot use reduce-only")
            _require_closing_side(self.position_side, self.side)
        else:
            if self.reduce_only:
                raise ValueError("trigger protection encodes closing semantics explicitly")
            _require_closing_side(self.position_side, self.side)
        if self.max_slippage_bps is not None:
            _positive_decimal(self.max_slippage_bps, "max_slippage_bps")

    @property
    def risk_reducing(self) -> bool:
        return self.action is not IntentAction.OPEN

    @property
    def idempotency_scope(self) -> tuple[str, str, str, str, str]:
        """Tenant-safe identity for durable reservation implementations."""

        return (
            self.tenant_scope,
            self.user_scope,
            self.account_scope,
            self.deployment_scope,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    account: AccountSnapshot
    quote: Quote
    rules: InstrumentRules
    position_mode: PositionMode


@dataclass(frozen=True, slots=True)
class RiskDecision:
    intent_id: str
    outcome: RiskOutcome
    reason_codes: tuple[str, ...]
    decided_at: datetime
    requested_quantity: Decimal
    approved_quantity: Decimal | None = None
    reference_price: Decimal | None = None
    notional: Decimal | None = None

    def __post_init__(self) -> None:
        if not _KEY_PATTERN.fullmatch(str(self.intent_id)):
            raise ValueError("invalid intent_id")
        _aware(self.decided_at, "decided_at")
        _positive_decimal(self.requested_quantity, "requested_quantity")
        if not self.reason_codes:
            raise ValueError("risk decision must include at least one reason code")
        if self.outcome is RiskOutcome.APPROVED:
            if self.reason_codes != ("approved",):
                raise ValueError("approved risk decisions must use the approved reason")
            if self.approved_quantity is None or self.reference_price is None:
                raise ValueError("approved decisions require quantity and reference price")
            _positive_decimal(self.approved_quantity, "approved_quantity")
            _positive_decimal(self.reference_price, "reference_price")
            if self.approved_quantity > self.requested_quantity:
                raise ValueError("approved quantity cannot exceed requested quantity")
            expected_notional = self.approved_quantity * self.reference_price
            if self.notional is None:
                object.__setattr__(self, "notional", expected_notional)
            _positive_decimal(self.notional, "notional")
            if self.notional != expected_notional:
                raise ValueError("risk notional must equal approved quantity times price")
        elif any(value is not None for value in (self.approved_quantity, self.notional)):
            raise ValueError("rejected decisions cannot approve quantity or notional")

    @property
    def approved(self) -> bool:
        return self.outcome is RiskOutcome.APPROVED


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    """Durable fence written immediately before a broker write may begin."""

    intent: OrderIntent
    preflight: PreflightReport
    risk_decision: RiskDecision
    broker_name: str
    physical_account_id: str
    client_order_id: str
    checkpointed_at: datetime

    def __post_init__(self) -> None:
        if not self.preflight.ready:
            raise ValueError("execution checkpoint requires a passing preflight")
        if not self.risk_decision.approved:
            raise ValueError("execution checkpoint requires approved risk")
        if self.risk_decision.intent_id != self.intent.intent_id:
            raise ValueError("execution checkpoint risk belongs to another intent")
        broker_name = str(self.broker_name).strip().lower()
        if not _SCOPE_PATTERN.fullmatch(broker_name):
            raise ValueError("invalid checkpoint broker name")
        object.__setattr__(self, "broker_name", broker_name)
        physical_account_id = str(self.physical_account_id).strip()
        if not _PHYSICAL_ACCOUNT_PATTERN.fullmatch(physical_account_id):
            raise ValueError("invalid checkpoint physical_account_id")
        object.__setattr__(self, "physical_account_id", physical_account_id)
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,36}", self.client_order_id):
            raise ValueError("invalid checkpoint client order id")
        _aware(self.checkpointed_at, "checkpointed_at")
        if self.checkpointed_at > self.intent.valid_until:
            raise ValueError("checkpoint cannot be later than intent valid_until")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    simulated: bool
    fee: Decimal = Decimal(0)
    fee_asset: str | None = None

    def __post_init__(self) -> None:
        if not _KEY_PATTERN.fullmatch(str(self.fill_id)):
            raise ValueError("invalid fill_id")
        if not self.client_order_id or len(self.client_order_id) > 36:
            raise ValueError("invalid client_order_id")
        symbol = str(self.symbol).strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("invalid instrument symbol")
        object.__setattr__(self, "symbol", symbol)
        _positive_decimal(self.quantity, "quantity")
        _positive_decimal(self.price, "price")
        if not isinstance(self.fee, Decimal) or not self.fee.is_finite() or self.fee < 0:
            raise ValueError("fee must be a non-negative finite Decimal")
        _aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Auditable result preserving every completed link in the state chain."""

    intent: OrderIntent
    state: ExecutionState
    preflight: PreflightReport | None = None
    risk_decision: RiskDecision | None = None
    broker_order: BrokerOrder | None = None
    fills: tuple[Fill, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.risk_decision is not None and self.risk_decision.intent_id != self.intent.intent_id:
            raise ValueError("risk decision belongs to a different intent")
        if self.fills and self.broker_order is None:
            raise ValueError("fills require a broker order")
        if self.broker_order is not None:
            if self.risk_decision is None or not self.risk_decision.approved:
                raise ValueError("a broker order requires approved risk")
            if self.broker_order.symbol != self.intent.symbol:
                raise ValueError("broker order symbol differs from intent")
        if self.state is ExecutionState.PREFLIGHT_FAILED:
            if self.preflight is None or self.preflight.ready:
                raise ValueError("preflight_failed requires a failed preflight report")
        if self.state is ExecutionState.RISK_REJECTED:
            if self.risk_decision is None or self.risk_decision.approved:
                raise ValueError("risk_rejected requires a rejected risk decision")
        if self.state is ExecutionState.FILLED and not self.fills:
            raise ValueError("filled execution requires at least one fill")
        if self.state in {ExecutionState.BLOCKED, ExecutionState.UNKNOWN}:
            if not self.error_code:
                raise ValueError(f"{self.state.value} execution requires an error code")


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    state: IdempotencyClaimState
    result: ExecutionResult | None = None
    claim_token: str | None = None

    def __post_init__(self) -> None:
        if self.state is IdempotencyClaimState.COMPLETED and self.result is None:
            raise ValueError("a completed claim must include its result")
        if self.state is not IdempotencyClaimState.COMPLETED and self.result is not None:
            raise ValueError("only a completed claim can include a result")
        if self.state is IdempotencyClaimState.CLAIMED:
            if self.claim_token is None or not re.fullmatch(
                r"[A-Za-z0-9._-]{16,64}", self.claim_token
            ):
                raise ValueError("an acquired claim must include an owner token")
        elif self.claim_token is not None:
            raise ValueError("only an acquired claim can include an owner token")


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    state: RecoveryClaimState
    recovery_token: str | None = None

    def __post_init__(self) -> None:
        if self.state is RecoveryClaimState.ACQUIRED:
            if self.recovery_token is None or not re.fullmatch(
                r"[A-Za-z0-9._-]{16,64}", self.recovery_token
            ):
                raise ValueError("an acquired recovery claim requires an owner token")
        elif self.recovery_token is not None:
            raise ValueError("only an acquired recovery claim can include an owner token")


def execution_state_for_order(order: BrokerOrder) -> ExecutionState:
    """Translate the normalized broker status into the execution lifecycle."""

    return {
        OrderStatus.FILLED: ExecutionState.FILLED,
        OrderStatus.PARTIALLY_FILLED: ExecutionState.PARTIALLY_FILLED,
        OrderStatus.CANCELED: ExecutionState.CANCELED,
        OrderStatus.REJECTED: ExecutionState.BROKER_REJECTED,
        OrderStatus.EXPIRED: ExecutionState.CANCELED,
        OrderStatus.NEW: ExecutionState.SUBMITTED,
    }.get(order.status, ExecutionState.UNKNOWN)


def execution_result_can_advance(
    previous: ExecutionResult,
    candidate: ExecutionResult,
) -> bool:
    """Return whether broker reconciliation moves one execution monotonically forward."""

    if previous.intent != candidate.intent:
        return False
    allowed: dict[ExecutionState, frozenset[ExecutionState]] = {
        ExecutionState.UNKNOWN: frozenset(
            {
                ExecutionState.UNKNOWN,
                ExecutionState.SUBMITTED,
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
                ExecutionState.CANCELED,
                ExecutionState.BROKER_REJECTED,
            }
        ),
        ExecutionState.SUBMITTED: frozenset(
            {
                ExecutionState.SUBMITTED,
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
                ExecutionState.CANCELED,
                ExecutionState.BROKER_REJECTED,
            }
        ),
        ExecutionState.PARTIALLY_FILLED: frozenset(
            {
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
                ExecutionState.CANCELED,
            }
        ),
    }
    if candidate.state not in allowed.get(previous.state, frozenset()):
        return False
    previous_order = previous.broker_order
    candidate_order = candidate.broker_order
    if previous_order is None:
        return candidate_order is not None or candidate.state is ExecutionState.UNKNOWN
    if candidate_order is None:
        return False
    if previous_order.reference != candidate_order.reference:
        return False
    return candidate_order.executed_quantity >= previous_order.executed_quantity


def _require_closing_side(position_side: PositionSide, side: OrderSide) -> None:
    if position_side is PositionSide.LONG and side is not OrderSide.SELL:
        raise ValueError("closing a LONG hedge position requires SELL")
    if position_side is PositionSide.SHORT and side is not OrderSide.BUY:
        raise ValueError("closing a SHORT hedge position requires BUY")
