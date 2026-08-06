from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from test_execution_service import MemoryFeed

from quantdesk_v2.application.ports import Broker
from quantdesk_v2.application.risk import RiskPolicy
from quantdesk_v2.domain.execution import (
    ExecutionMode,
    ExecutionState,
    IntentAction,
    OrderIntent,
)
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    InstrumentRules,
    OrderSide,
    PositionDirection,
    Quote,
)
from quantdesk_v2.infrastructure.shadow_execution import ShadowExecutionRuntime

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _rules(symbol: str = "BTCUSDT") -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("100"),
        price_tick=Decimal("0.1"),
        minimum_notional=Decimal("5"),
    )


def _runtime(
    *,
    feed: MemoryFeed | None = None,
    rules: dict[str, InstrumentRules] | None = None,
    risk_policy: RiskPolicy | None = None,
) -> ShadowExecutionRuntime:
    account = AccountSnapshot(
        account_type=AccountType.USD_M_FUTURES,
        can_trade=True,
        wallet_balance=Decimal("1000"),
        available_balance=Decimal("900"),
        unrealized_pnl=Decimal("0"),
        currency="USDT",
        updated_at=NOW,
    )
    return ShadowExecutionRuntime(
        account=account,
        feed=feed or MemoryFeed(),
        rules=rules or {"BTCUSDT": _rules()},
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="shadow-account-1",
        physical_account_id="shadow-wallet-1",
        slippage_bps=Decimal("5"),
        risk_policy=risk_policy,
        clock=lambda: NOW,
    )


def _intent(
    sequence: str,
    *,
    action: IntentAction = IntentAction.OPEN,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("0.01"),
    symbol: str = "BTCUSDT",
    reduce_only: bool = False,
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"shadow-{sequence}",
        idempotency_key=f"shadow:strategy-v1:{sequence}",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="shadow-account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol=symbol,
        timeframe="4h",
        action=action,
        side=side,
        quantity=quantity,
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
        reduce_only=reduce_only,
    )


def test_shadow_runtime_is_a_real_broker_entry_without_exchange_writes() -> None:
    runtime = _runtime()
    intent = OrderIntent(
        intent_id="shadow-1",
        idempotency_key="shadow:strategy-v1:signal-1",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="shadow-account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=IntentAction.OPEN,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
    )

    result = runtime.execute(intent)
    replay = runtime.execute(intent)

    assert isinstance(runtime.broker, Broker)
    assert runtime.broker.name == "shadow"
    assert result.state is ExecutionState.FILLED
    assert result.fills[0].price == Decimal("60030.0000")
    assert result.fills[0].simulated is True
    assert replay is result
    assert runtime.broker.open_orders() == ()


def test_shadow_runtime_honors_the_same_kill_switch_as_other_modes() -> None:
    runtime = _runtime()
    runtime.safety.engage_kill_switch("release_freeze")
    intent = OrderIntent(
        intent_id="shadow-killed",
        idempotency_key="shadow:strategy-v1:killed",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="shadow-account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=IntentAction.OPEN,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
    )

    result = runtime.execute(intent)

    assert result.state is ExecutionState.BLOCKED
    assert result.error_code == "release_freeze"


def test_shadow_runtime_tracks_open_add_reduce_close_and_balances() -> None:
    feed = MemoryFeed()
    runtime = _runtime(feed=feed)

    opened = runtime.execute(_intent("open", quantity=Decimal("0.1")))
    opened_price = opened.fills[0].price
    after_open = runtime.broker.account_snapshot()

    assert opened.state is ExecutionState.FILLED
    assert len(after_open.positions) == 1
    assert after_open.positions[0].direction is PositionDirection.LONG
    assert after_open.positions[0].quantity == Decimal("0.1")
    assert after_open.positions[0].entry_price == opened_price
    assert after_open.available_balance == Decimal("900") - opened_price * Decimal("0.1") / 10
    assert after_open.wallet_balance == Decimal("1000")

    feed.quote = Quote("BTCUSDT", Decimal("61000"), NOW)
    added = runtime.execute(_intent("add", quantity=Decimal("0.02")))
    added_price = added.fills[0].price
    after_add = runtime.broker.account_snapshot()
    expected_entry = (
        opened_price * Decimal("0.1") + added_price * Decimal("0.02")
    ) / Decimal("0.12")

    assert added.state is ExecutionState.FILLED
    assert after_add.positions[0].quantity == Decimal("0.12")
    assert after_add.positions[0].entry_price == expected_entry

    feed.quote = Quote("BTCUSDT", Decimal("62000"), NOW)
    reduced = runtime.execute(
        _intent(
            "reduce",
            action=IntentAction.CLOSE,
            side=OrderSide.SELL,
            quantity=Decimal("0.04"),
            reduce_only=True,
        )
    )
    reduced_price = reduced.fills[0].price
    first_realized = (reduced_price - expected_entry) * Decimal("0.04")
    after_reduce = runtime.broker.account_snapshot()

    assert reduced.state is ExecutionState.FILLED
    assert after_reduce.positions[0].quantity == Decimal("0.08")
    assert after_reduce.positions[0].entry_price == expected_entry
    assert runtime.broker.realized_pnl == first_realized
    assert after_reduce.wallet_balance == Decimal("1000") + first_realized

    feed.quote = Quote("BTCUSDT", Decimal("63000"), NOW)
    closed = runtime.execute(
        _intent(
            "close",
            action=IntentAction.CLOSE,
            side=OrderSide.SELL,
            quantity=Decimal("0.08"),
            reduce_only=True,
        )
    )
    closed_price = closed.fills[0].price
    total_realized = first_realized + (closed_price - expected_entry) * Decimal("0.08")
    after_close = runtime.broker.account_snapshot()

    assert closed.state is ExecutionState.FILLED
    assert after_close.positions == ()
    assert after_close.unrealized_pnl == 0
    assert runtime.broker.realized_pnl == total_realized
    assert after_close.wallet_balance == Decimal("1000") + total_realized
    assert after_close.available_balance == Decimal("900") + total_realized


def test_shadow_runtime_rejects_reduce_only_quantity_above_position_atomically() -> None:
    runtime = _runtime()
    opened = runtime.execute(_intent("small-open", quantity=Decimal("0.01")))
    before_rejection = runtime.broker.account_snapshot()

    rejected = runtime.execute(
        _intent(
            "oversized-close",
            action=IntentAction.CLOSE,
            side=OrderSide.SELL,
            quantity=Decimal("0.02"),
            reduce_only=True,
        )
    )
    after_rejection = runtime.broker.account_snapshot()

    assert opened.state is ExecutionState.FILLED
    assert rejected.state is ExecutionState.UNKNOWN
    assert rejected.error_code == "broker_reduce_quantity_exceeds_position"
    assert after_rejection.positions == before_rejection.positions
    assert after_rejection.wallet_balance == before_rejection.wallet_balance
    assert after_rejection.available_balance == before_rejection.available_balance
    assert runtime.broker.realized_pnl == 0


def test_shadow_runtime_tracks_short_position_profit() -> None:
    feed = MemoryFeed()
    runtime = _runtime(feed=feed)

    opened = runtime.execute(
        _intent("short-open", side=OrderSide.SELL, quantity=Decimal("0.01"))
    )
    opened_price = opened.fills[0].price
    after_open = runtime.broker.account_snapshot()

    assert opened.state is ExecutionState.FILLED
    assert after_open.positions[0].direction is PositionDirection.SHORT

    feed.quote = Quote("BTCUSDT", Decimal("58000"), NOW)
    closed = runtime.execute(
        _intent(
            "short-close",
            action=IntentAction.CLOSE,
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            reduce_only=True,
        )
    )
    closed_price = closed.fills[0].price
    realized = (opened_price - closed_price) * Decimal("0.01")
    after_close = runtime.broker.account_snapshot()

    assert closed.state is ExecutionState.FILLED
    assert after_close.positions == ()
    assert runtime.broker.realized_pnl == realized
    assert after_close.wallet_balance == Decimal("1000") + realized
    assert after_close.available_balance == Decimal("900") + realized


def test_shadow_runtime_reverses_one_way_position_atomically() -> None:
    feed = MemoryFeed()
    runtime = _runtime(feed=feed)

    opened = runtime.execute(_intent("reverse-open", quantity=Decimal("0.01")))
    opened_price = opened.fills[0].price
    opened_margin = opened_price * Decimal("0.01") / 10

    feed.quote = Quote("BTCUSDT", Decimal("61000"), NOW)
    reversed_order = runtime.execute(
        _intent(
            "reverse-to-short",
            side=OrderSide.SELL,
            quantity=Decimal("0.015"),
        )
    )
    reversed_price = reversed_order.fills[0].price
    realized = (reversed_price - opened_price) * Decimal("0.01")
    remaining_quantity = Decimal("0.005")
    short_margin = reversed_price * remaining_quantity / 10
    snapshot = runtime.broker.account_snapshot()

    assert reversed_order.state is ExecutionState.FILLED
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].direction is PositionDirection.SHORT
    assert snapshot.positions[0].quantity == remaining_quantity
    assert snapshot.positions[0].entry_price == reversed_price
    assert snapshot.positions[0].initial_margin == short_margin
    assert runtime.broker.realized_pnl == realized
    assert snapshot.wallet_balance == Decimal("1000") + realized
    assert snapshot.available_balance == (
        Decimal("900") - opened_margin + opened_margin + realized - short_margin
    )


def test_shadow_positions_make_the_shared_position_count_limit_effective() -> None:
    runtime = _runtime(
        rules={"BTCUSDT": _rules(), "ETHUSDT": _rules("ETHUSDT")},
        risk_policy=RiskPolicy(max_open_positions=1),
    )

    first = runtime.execute(_intent("btc-open"))
    second = runtime.execute(_intent("eth-open", symbol="ETHUSDT"))
    snapshot = runtime.broker.account_snapshot()

    assert first.state is ExecutionState.FILLED
    assert second.state is ExecutionState.RISK_REJECTED
    assert second.error_code == "position_count_limit"
    assert [position.symbol for position in snapshot.positions] == ["BTCUSDT"]
