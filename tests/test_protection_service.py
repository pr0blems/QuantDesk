from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantdesk_v2.application.protection import (
    ProtectionInstallationError,
    ProtectionService,
    protection_client_order_id,
)
from quantdesk_v2.domain.protection import ProtectionAction, ProtectionPlan
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    Bar,
    BrokerError,
    InstrumentRules,
    OrderSide,
    PositionMode,
    PositionSide,
    Quote,
    TriggerOrder,
)
from quantdesk_v2.infrastructure.shadow_broker import ShadowBroker

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class StaticFeed:
    def latest_quote(self, symbol: str) -> Quote | None:
        return Quote(symbol, Decimal("60000"), NOW)

    def bars(self, symbol: str, timeframe: str, *, limit: int) -> tuple[Bar, ...]:
        return ()


def _broker() -> ShadowBroker:
    return ShadowBroker(
        account=AccountSnapshot(
            account_type=AccountType.USD_M_FUTURES,
            can_trade=True,
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("900"),
            unrealized_pnl=Decimal(0),
            currency="USDT",
            updated_at=NOW,
        ),
        feed=StaticFeed(),
        account_scope="protection-shadow-1",
        physical_account_id="protection-shadow-wallet-1",
        market="binance_usdm",
        rules={
            "BTCUSDT": InstrumentRules(
                symbol="BTCUSDT",
                quantity_step=Decimal("0.001"),
                minimum_quantity=Decimal("0.001"),
                maximum_quantity=Decimal("100"),
                price_tick=Decimal("0.1"),
                minimum_notional=Decimal("5"),
            )
        },
        position_mode=PositionMode.ONE_WAY,
        clock=lambda: NOW,
    )


def _plan() -> ProtectionPlan:
    return ProtectionPlan.create(
        symbol="BTCUSDT",
        close_side=OrderSide.SELL,
        position_side=PositionSide.BOTH,
        quantity=None,
        signal_time=1_754_474_400_000,
        stop=Decimal("58000"),
        target=Decimal("65000"),
    )


def test_protection_service_installs_verifies_and_reuses_the_exact_pair() -> None:
    broker = _broker()
    service = ProtectionService(broker)

    installed = service.ensure(_plan(), execution_scope="tenant:1/account:7/deployment:4")
    replayed = service.ensure(_plan(), execution_scope="tenant:1/account:7/deployment:4")
    status = service.verify(_plan(), execution_scope="tenant:1/account:7/deployment:4")

    assert len(installed) == 2
    assert replayed == installed
    assert len(broker.open_orders()) == 2
    assert status.complete is True
    assert status.present_actions == frozenset(ProtectionAction)
    assert status.missing_actions == frozenset()


def test_protection_ids_are_scoped_deterministic_and_exchange_safe() -> None:
    plan = _plan()
    first = protection_client_order_id(plan, "tenant:1/account:7", ProtectionAction.STOP)

    assert first == protection_client_order_id(
        plan,
        "tenant:1/account:7",
        ProtectionAction.STOP,
    )
    assert first != protection_client_order_id(
        plan,
        "tenant:2/account:7",
        ProtectionAction.STOP,
    )
    assert first != protection_client_order_id(
        plan,
        "tenant:1/account:7",
        ProtectionAction.TAKE_PROFIT,
    )
    assert len(first) <= 36


def test_protection_service_rolls_back_a_known_partial_installation() -> None:
    class FailSecondBroker(ShadowBroker):
        calls = 0

        def place_trigger_order(self, order: TriggerOrder):
            self.calls += 1
            if self.calls == 2:
                raise BrokerError("timeout")
            return super().place_trigger_order(order)

    base = _broker()
    broker = FailSecondBroker(
        account=base.account_snapshot(),
        feed=StaticFeed(),
            rules={"BTCUSDT": base.instrument_rules("BTCUSDT")},
            account_scope="protection-shadow-1",
            physical_account_id=base.physical_account_id,
            market="binance_usdm",
        clock=lambda: NOW,
    )

    with pytest.raises(ProtectionInstallationError) as caught:
        ProtectionService(broker).ensure(_plan(), execution_scope="scope:rollback")

    assert caught.value.code == "unknown_take_profit_outcome"
    assert caught.value.rollback_complete is False
    assert broker.open_orders() == ()


def test_protection_service_fails_closed_on_mismatched_existing_order() -> None:
    class MismatchedBroker(ShadowBroker):
        def open_orders(self):
            orders = super().open_orders()
            if not orders:
                return orders
            return (replace(orders[0], trigger_price=Decimal("57000")), *orders[1:])

    base = _broker()
    broker = MismatchedBroker(
        account=base.account_snapshot(),
        feed=StaticFeed(),
            rules={"BTCUSDT": base.instrument_rules("BTCUSDT")},
            account_scope="protection-shadow-1",
            physical_account_id=base.physical_account_id,
            market="binance_usdm",
        clock=lambda: NOW,
    )
    service = ProtectionService(broker)
    service.ensure(_plan(), execution_scope="scope:mismatch")

    status = service.verify(_plan(), execution_scope="scope:mismatch")
    assert status.complete is False
    assert status.invalid_actions
    with pytest.raises(ProtectionInstallationError, match="protection installation failed"):
        service.repair(_plan(), execution_scope="scope:mismatch")
