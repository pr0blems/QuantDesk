"""Interfaces implemented by exchange and market-data infrastructure."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.execution import (
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionMode,
    ExecutionResult,
    IdempotencyClaim,
    OrderIntent,
    RecoveryClaim,
    RiskDecision,
)
from ..domain.trading import (
    AccountSnapshot,
    Bar,
    BrokerOrder,
    InstrumentRules,
    MarketOrder,
    OrderReference,
    PositionMode,
    Quote,
    TriggerOrder,
)


@runtime_checkable
class Broker(Protocol):
    """Synchronous broker boundary for one configured trading account."""

    @property
    def name(self) -> str: ...

    @property
    def execution_mode(self) -> ExecutionMode: ...

    @property
    def market(self) -> str: ...

    @property
    def account_scope(self) -> str: ...

    @property
    def physical_account_id(self) -> str: ...

    def account_snapshot(self) -> AccountSnapshot: ...

    def open_orders(self) -> tuple[BrokerOrder, ...]: ...

    def instrument_rules(self, symbol: str) -> InstrumentRules: ...

    def position_mode(self) -> PositionMode: ...

    def configure_leverage(self, symbol: str, leverage: int) -> None: ...

    def place_market_order(self, order: MarketOrder) -> BrokerOrder: ...

    def place_trigger_order(self, order: TriggerOrder) -> BrokerOrder: ...

    def get_order(self, reference: OrderReference) -> BrokerOrder: ...

    def cancel_order(self, reference: OrderReference) -> None: ...


@runtime_checkable
class MarketDataFeed(Protocol):
    """Read-only market-data boundary shared by every execution mode."""

    def latest_quote(self, symbol: str) -> Quote | None: ...

    def bars(self, symbol: str, timeframe: str, *, limit: int) -> tuple[Bar, ...]: ...


@runtime_checkable
class RiskEvaluator(Protocol):
    """Deterministic risk boundary shared by every execution mode."""

    def evaluate(self, intent: OrderIntent, context: ExecutionContext) -> RiskDecision: ...


@runtime_checkable
class IdempotencyStore(Protocol):
    """Fail-closed reservation store for the broker write boundary.

    ``claim`` must atomically return exactly one ``CLAIMED`` result for a key.
    An abandoned in-progress reservation is intentionally not retried blindly;
    a durable implementation must reconcile it against the broker first.
    ``claim_recovery`` must be exclusive and permanently fence the original
    claim owner before any broker query can be persisted.
    """

    def claim(self, intent: OrderIntent) -> IdempotencyClaim: ...

    def save_checkpoint(
        self,
        intent: OrderIntent,
        claim_token: str,
        checkpoint: ExecutionCheckpoint,
    ) -> None: ...

    def load_checkpoint(self, intent: OrderIntent) -> ExecutionCheckpoint | None: ...

    def pending_checkpoints(
        self,
        *,
        tenant_scope: str,
        user_scope: str,
        account_scope: str,
        limit: int = 100,
    ) -> tuple[ExecutionCheckpoint, ...]: ...

    def complete(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        claim_token: str,
    ) -> None: ...

    def claim_recovery(self, intent: OrderIntent) -> RecoveryClaim: ...

    def complete_recovered(
        self,
        intent: OrderIntent,
        checkpoint: ExecutionCheckpoint,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None: ...

    def lookup(self, intent: OrderIntent) -> ExecutionResult | None: ...

    def advance_recovered(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        recovery_token: str,
    ) -> None: ...
