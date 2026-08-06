from __future__ import annotations

from decimal import Decimal

import pytest

from quantdesk_v2.domain.protection import (
    ProtectionAction,
    ProtectionCoverage,
    ProtectionPlan,
)
from quantdesk_v2.domain.trading import OrderSide, OrderType, PositionSide


def test_long_protection_plan_is_an_atomic_stop_and_target_pair() -> None:
    plan = ProtectionPlan.create(
        symbol="btcusdt",
        close_side="SELL",
        position_side="BOTH",
        quantity=None,
        signal_time=1_700_000_000,
        stop=Decimal("59000"),
        target=Decimal("63000"),
    )

    assert plan.symbol == "BTCUSDT"
    assert plan.close_side is OrderSide.SELL
    assert [order.action for order in plan.orders] == [
        ProtectionAction.STOP,
        ProtectionAction.TAKE_PROFIT,
    ]
    assert [order.order_type for order in plan.orders] == [
        OrderType.STOP_MARKET,
        OrderType.TAKE_PROFIT_MARKET,
    ]
    assert plan.signal_key(17, ProtectionAction.STOP) == (
        "live:17:BTCUSDT:BOTH:1700000000:stop"
    )


def test_short_and_hedge_protection_require_consistent_prices_and_quantity() -> None:
    plan = ProtectionPlan.create(
        symbol="ETHUSDT",
        close_side="BUY",
        position_side="SHORT",
        quantity=Decimal("1.25"),
        signal_time=1,
        stop=Decimal("3400"),
        target=Decimal("3000"),
    )

    assert plan.position_side is PositionSide.SHORT

    with pytest.raises(ValueError, match="requires quantity"):
        ProtectionPlan.create(
            symbol="ETHUSDT",
            close_side="BUY",
            position_side="SHORT",
            quantity=None,
            signal_time=1,
            stop=Decimal("3400"),
            target=Decimal("3000"),
        )
    with pytest.raises(ValueError, match="above target"):
        ProtectionPlan.create(
            symbol="ETHUSDT",
            close_side="BUY",
            position_side="BOTH",
            quantity=None,
            signal_time=1,
            stop=Decimal("2900"),
            target=Decimal("3000"),
        )


def test_protection_coverage_requires_both_known_actions() -> None:
    partial = ProtectionCoverage.from_actions({"stop"})
    complete = ProtectionCoverage.from_actions({"stop", "take_profit"})
    unexpected = ProtectionCoverage.from_actions({"stop", "take_profit", "mystery"})

    assert not partial.complete
    assert partial.missing == frozenset({ProtectionAction.TAKE_PROFIT})
    assert complete.complete
    assert not unexpected.complete
    assert unexpected.unexpected_actions == frozenset({"mystery"})

