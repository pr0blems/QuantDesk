"""Broker boundary for the durable paper ledger.

The adapter deliberately owns no strategy decisions.  It exposes the existing
paper account as a normal broker and delegates the atomic fill write to the
paper persistence boundary.  Because fills are stored by deterministic client
order id, the shared execution service can reconcile a worker restart without
submitting the simulated order twice.
"""

from __future__ import annotations

from collections.abc import Callable

from ..application.ports import MarketDataFeed
from ..domain.execution import ExecutionMode
from ..domain.trading import (
    AccountSnapshot,
    BrokerError,
    BrokerOrder,
    InstrumentRules,
    MarketOrder,
    OrderReference,
    PositionMode,
    TriggerOrder,
)


class PaperBroker:
    """Synchronous, persistent paper broker used by ``ExecutionService``."""

    __slots__ = (
        "_account_scope",
        "_account_snapshot",
        "_feed",
        "_lookup",
        "_physical_account_id",
        "_rules",
        "_submit",
    )

    def __init__(
        self,
        *,
        account_scope: str,
        physical_account_id: str,
        feed: MarketDataFeed,
        account_snapshot: Callable[[], AccountSnapshot],
        rules: Callable[[str], InstrumentRules],
        submit: Callable[[MarketOrder], BrokerOrder],
        lookup: Callable[[OrderReference], BrokerOrder],
    ) -> None:
        self._account_scope = str(account_scope).strip()
        self._physical_account_id = str(physical_account_id).strip()
        if not self._account_scope or not self._physical_account_id:
            raise ValueError("paper broker scopes must not be empty")
        self._feed = feed
        self._account_snapshot = account_snapshot
        self._rules = rules
        self._submit = submit
        self._lookup = lookup

    @property
    def name(self) -> str:
        return "paper"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.PAPER

    @property
    def market(self) -> str:
        return "binance_usdm"

    @property
    def account_scope(self) -> str:
        return self._account_scope

    @property
    def physical_account_id(self) -> str:
        return self._physical_account_id

    @property
    def feed(self) -> MarketDataFeed:
        return self._feed

    def account_snapshot(self) -> AccountSnapshot:
        return self._account_snapshot()

    def open_orders(self) -> tuple[BrokerOrder, ...]:
        # Paper market orders settle in the same database transaction.
        return ()

    def instrument_rules(self, symbol: str) -> InstrumentRules:
        return self._rules(symbol)

    def position_mode(self) -> PositionMode:
        return PositionMode.ONE_WAY

    def configure_leverage(self, symbol: str, leverage: int) -> None:
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
            raise BrokerError("invalid_leverage")
        self._rules(symbol)

    def place_market_order(self, order: MarketOrder) -> BrokerOrder:
        return self._submit(order)

    def place_trigger_order(self, order: TriggerOrder) -> BrokerOrder:
        raise BrokerError("paper_trigger_orders_are_policy_managed")

    def get_order(self, reference: OrderReference) -> BrokerOrder:
        return self._lookup(reference)

    def cancel_order(self, reference: OrderReference) -> None:
        raise BrokerError("paper_market_order_already_terminal")
