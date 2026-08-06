from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2.application.execution_service import (
    ExecutionService,
    deterministic_client_order_id,
)
from quantdesk_v2.application.ports import Broker, IdempotencyStore, MarketDataFeed
from quantdesk_v2.application.risk import DeterministicRiskEvaluator, RiskPolicy
from quantdesk_v2.application.safety import ExecutionSafetyController, PreflightService
from quantdesk_v2.domain.execution import (
    ExecutionCheckpoint,
    ExecutionMode,
    ExecutionState,
    IdempotencyClaimState,
    IntentAction,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
)
from quantdesk_v2.domain.safety import PreflightCheck, PreflightReport
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    Bar,
    BrokerError,
    BrokerOrder,
    InstrumentRules,
    MarketOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionMode,
    Quote,
    TriggerOrder,
)
from quantdesk_v2.infrastructure.memory_execution import InMemoryIdempotencyStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class MemoryFeed:
    def __init__(self, quote: Quote | None = None) -> None:
        self.quote = quote or Quote("BTCUSDT", Decimal("60000"), NOW)

    def latest_quote(self, symbol: str) -> Quote | None:
        return replace(self.quote, symbol=symbol) if self.quote is not None else None

    def bars(self, symbol: str, timeframe: str, *, limit: int) -> tuple[Bar, ...]:
        return ()


class MemoryBroker:
    def __init__(self, *, error: BrokerError | None = None) -> None:
        self.error = error
        self.market_orders: list[MarketOrder] = []
        self.trigger_orders: list[TriggerOrder] = []
        self._account = AccountSnapshot(
            account_type=AccountType.USD_M_FUTURES,
            can_trade=True,
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("900"),
            unrealized_pnl=Decimal("0"),
            currency="USDT",
            updated_at=NOW,
        )
        self._rules = InstrumentRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            price_tick=Decimal("0.1"),
            minimum_notional=Decimal("5"),
        )

    @property
    def name(self) -> str:
        return "memory"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.SHADOW

    @property
    def market(self) -> str:
        return "binance_usdm"

    @property
    def account_scope(self) -> str:
        return "account-1"

    @property
    def physical_account_id(self) -> str:
        return "memory-wallet-1"

    def account_snapshot(self) -> AccountSnapshot:
        return self._account

    def open_orders(self) -> tuple[BrokerOrder, ...]:
        return ()

    def instrument_rules(self, symbol: str) -> InstrumentRules:
        return replace(self._rules, symbol=symbol)

    def position_mode(self) -> PositionMode:
        return PositionMode.ONE_WAY

    def configure_leverage(self, symbol: str, leverage: int) -> None:
        return None

    def place_market_order(self, order: MarketOrder) -> BrokerOrder:
        self.market_orders.append(order)
        if self.error is not None:
            raise self.error
        return BrokerOrder(
            reference=OrderReference(order.client_order_id, order.symbol),
            exchange_order_id="memory-1",
            symbol=order.symbol,
            side=order.side,
            position_side=order.position_side,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            exchange_status="FILLED",
            quantity=order.quantity,
            executed_quantity=order.quantity,
            average_price=Decimal("60001"),
            reduce_only=order.reduce_only,
        )

    def place_trigger_order(self, order: TriggerOrder) -> BrokerOrder:
        self.trigger_orders.append(order)
        return BrokerOrder(
            reference=OrderReference(
                order.client_order_id, order.symbol, conditional=True
            ),
            exchange_order_id="memory-trigger-1",
            symbol=order.symbol,
            side=order.side,
            position_side=order.position_side,
            order_type=order.order_type,
            status=OrderStatus.NEW,
            exchange_status="NEW",
            quantity=order.quantity or Decimal(0),
            trigger_price=order.trigger_price,
        )

    def get_order(self, reference: OrderReference) -> BrokerOrder:
        raise BrokerError("order_not_found")

    def cancel_order(self, reference: OrderReference) -> None:
        return None


def _intent(
    suffix: str = "1",
    *,
    action: IntentAction = IntentAction.OPEN,
    quantity: Decimal = Decimal("0.1"),
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent-{suffix}",
        idempotency_key=f"strategy:v1:signal:{suffix}:open",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=action,
        side=OrderSide.BUY if action is IntentAction.OPEN else OrderSide.SELL,
        quantity=quantity,
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
        reduce_only=action is IntentAction.CLOSE,
    )


def _service(
    broker: MemoryBroker,
    *,
    feed: MemoryFeed | None = None,
    store: InMemoryIdempotencyStore | None = None,
    safety: ExecutionSafetyController | None = None,
    risk_policy: RiskPolicy | None = None,
) -> ExecutionService:
    market_feed = feed or MemoryFeed()
    return ExecutionService(
        broker=broker,
        preflight=PreflightService(broker, market_feed, clock=lambda: NOW),
        risk=DeterministicRiskEvaluator(risk_policy, clock=lambda: NOW),
        idempotency=store or InMemoryIdempotencyStore(),
        safety=safety or ExecutionSafetyController(clock=lambda: NOW),
        expected_mode=ExecutionMode.SHADOW,
        expected_broker_name="memory",
        expected_physical_account_id="memory-wallet-1",
        expected_market="binance_usdm",
        expected_account_scope="account-1",
        expected_tenant_scope="tenant-1",
        expected_user_scope="user-1",
        clock=lambda: NOW,
    )


def test_execution_service_preserves_the_full_state_chain_and_is_idempotent() -> None:
    broker = MemoryBroker()
    store = InMemoryIdempotencyStore()
    service = _service(broker, store=store)
    intent = _intent()

    first = service.execute(intent)
    replay = service.execute(intent)

    assert first.state is ExecutionState.FILLED
    assert first.preflight is not None and first.preflight.ready
    assert first.risk_decision is not None and first.risk_decision.approved
    assert first.broker_order is not None
    assert first.fills[0].price == Decimal("60001")
    assert first.fills[0].simulated is True
    assert replay is first
    assert len(broker.market_orders) == 1
    assert broker.market_orders[0].client_order_id == deterministic_client_order_id(intent)
    assert store.lookup(intent) is first
    assert isinstance(broker, Broker)
    assert isinstance(store, IdempotencyStore)
    assert isinstance(MemoryFeed(), MarketDataFeed)


def test_risk_rejection_never_crosses_the_broker_write_boundary() -> None:
    broker = MemoryBroker()
    service = _service(
        broker,
        risk_policy=RiskPolicy(max_order_notional=Decimal("100")),
    )

    result = service.execute(_intent())

    assert result.state is ExecutionState.RISK_REJECTED
    assert result.error_code == "order_notional_limit"
    assert broker.market_orders == []


def test_unknown_broker_outcome_is_recorded_and_not_blindly_retried() -> None:
    broker = MemoryBroker(error=BrokerError("timeout"))
    store = InMemoryIdempotencyStore()
    service = _service(broker, store=store)
    intent = _intent()

    first = service.execute(intent)
    replay = service.execute(intent)

    assert first.state is ExecutionState.UNKNOWN
    assert first.error_code == "broker_timeout"
    assert replay is first
    assert len(broker.market_orders) == 1


def test_unknown_outcome_can_be_reconciled_by_deterministic_client_id() -> None:
    class RecoveringBroker(MemoryBroker):
        def __init__(self) -> None:
            super().__init__()
            self.recovered_order: BrokerOrder | None = None

        def place_market_order(self, order: MarketOrder) -> BrokerOrder:
            self.recovered_order = super().place_market_order(order)
            raise BrokerError("timeout")

        def get_order(self, reference: OrderReference) -> BrokerOrder:
            if self.recovered_order is None:
                raise BrokerError("order_not_found")
            assert reference.client_order_id == self.recovered_order.reference.client_order_id
            return self.recovered_order

    broker = RecoveringBroker()
    store = InMemoryIdempotencyStore()
    service = _service(broker, store=store)
    intent = _intent()
    unknown = service.execute(intent)

    reconciled = service.reconcile_unknown(intent)

    assert unknown.state is ExecutionState.UNKNOWN
    assert reconciled.state is ExecutionState.FILLED
    assert reconciled.fills[0].price == Decimal("60001")
    assert store.lookup(intent) is reconciled
    assert service.execute(intent) is reconciled
    assert len(broker.market_orders) == 1


def test_checkpointed_in_progress_recovers_by_query_without_resubmission() -> None:
    intent = _intent()
    client_order_id = deterministic_client_order_id(intent)
    recovered_order = BrokerOrder(
        reference=OrderReference(client_order_id, intent.symbol),
        exchange_order_id="recovered-1",
        symbol=intent.symbol,
        side=intent.side,
        position_side=intent.position_side,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        exchange_status="FILLED",
        quantity=intent.quantity,
        executed_quantity=intent.quantity,
        average_price=Decimal("60000"),
    )

    class RecoverOnlyBroker(MemoryBroker):
        def __init__(self) -> None:
            super().__init__()
            self.get_order_calls = 0

        def get_order(self, reference: OrderReference) -> BrokerOrder:
            self.get_order_calls += 1
            assert reference.client_order_id == client_order_id
            return recovered_order

        def place_market_order(self, order: MarketOrder) -> BrokerOrder:
            raise AssertionError("recovery must not submit another order")

    broker = RecoverOnlyBroker()
    store = InMemoryIdempotencyStore()
    claim = store.claim(intent)
    assert claim.claim_token is not None
    preflight = PreflightReport(NOW, (PreflightCheck("ready", True),))
    risk = RiskDecision(
        intent_id=intent.intent_id,
        outcome=RiskOutcome.APPROVED,
        reason_codes=("approved",),
        decided_at=NOW,
        requested_quantity=intent.quantity,
        approved_quantity=intent.quantity,
        reference_price=Decimal("60000"),
        notional=Decimal("6000"),
    )
    checkpoint = ExecutionCheckpoint(
        intent=intent,
        preflight=preflight,
        risk_decision=risk,
        broker_name=broker.name,
        physical_account_id=broker.physical_account_id,
        client_order_id=client_order_id,
        checkpointed_at=NOW,
    )
    store.save_checkpoint(intent, claim.claim_token, checkpoint)

    service = _service(broker, store=store)
    result = service.reconcile_in_progress(intent)
    replay = service.reconcile_in_progress(intent)

    assert result.state is ExecutionState.FILLED
    assert replay is result
    assert store.lookup(intent) is result
    assert broker.market_orders == []
    assert broker.get_order_calls == 1


def test_execution_service_rejects_wrong_account_binding_before_claim() -> None:
    broker = MemoryBroker()
    store = InMemoryIdempotencyStore()
    intent = replace(_intent(), account_scope="account-2")

    result = _service(broker, store=store).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "broker_account_scope_mismatch"
    assert store.lookup(intent) is None
    assert broker.market_orders == []


def test_submission_preflight_enforces_max_slippage_before_broker_write() -> None:
    class MovingFeed(MemoryFeed):
        calls = 0

        def latest_quote(self, symbol: str) -> Quote | None:
            self.calls += 1
            price = Decimal("60000") if self.calls == 1 else Decimal("60100")
            return Quote(symbol, price, NOW)

    broker = MemoryBroker()
    intent = replace(_intent(), max_slippage_bps=Decimal("10"))

    result = _service(broker, feed=MovingFeed()).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "max_slippage_exceeded"
    assert broker.market_orders == []


def test_in_progress_idempotency_reservation_fails_closed() -> None:
    broker = MemoryBroker()
    store = InMemoryIdempotencyStore()
    intent = _intent()
    assert store.claim(intent).state is IdempotencyClaimState.CLAIMED

    result = _service(broker, store=store).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "idempotency_in_progress"
    assert broker.market_orders == []


def test_expired_intent_is_recorded_before_any_dependency_or_broker_call() -> None:
    broker = MemoryBroker()
    intent = replace(
        _intent(),
        signal_time=NOW - timedelta(minutes=2),
        valid_until=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=2),
    )

    result = _service(broker).execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "intent_expired"
    assert broker.market_orders == []


def test_execution_service_rejects_forged_tenant_and_user_before_claim() -> None:
    broker = MemoryBroker()
    store = InMemoryIdempotencyStore()
    service = _service(broker, store=store)
    tenant_forgery = replace(
        _intent(),
        intent_id="intent-forged-tenant",
        tenant_scope="tenant-2",
    )
    user_forgery = replace(
        _intent("forged-user"),
        user_scope="user-2",
    )

    tenant_result = service.execute(tenant_forgery)
    user_result = service.execute(user_forgery)

    assert tenant_result.state is ExecutionState.BLOCKED
    assert tenant_result.error_code == "broker_tenant_scope_mismatch"
    assert user_result.state is ExecutionState.BLOCKED
    assert user_result.error_code == "broker_user_scope_mismatch"
    assert broker.market_orders == []
    assert store.lookup(tenant_forgery) is None
    assert store.lookup(user_forgery) is None


def test_quantity_rounding_is_owned_by_risk_and_passed_to_the_broker() -> None:
    broker = MemoryBroker()

    result = _service(broker).execute(_intent(quantity=Decimal("0.1009")))

    assert result.state is ExecutionState.FILLED
    assert result.risk_decision is not None
    assert result.risk_decision.approved_quantity == Decimal("0.100")
    assert broker.market_orders[0].quantity == Decimal("0.100")
