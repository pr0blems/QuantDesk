from __future__ import annotations

from decimal import Decimal

from quantdesk_v2.domain.martingale_tp4 import (
    Mq4Inputs,
    strategy_parameters_from_mq4,
)
from quantdesk_v2.domain.martingale_tp4_engine import (
    BasketLeg,
    BasketSnapshot,
    DecisionAction,
    Direction,
    EnginePolicy,
    MarketTick,
    effective_mode,
    evaluate_tick,
)


def _tick(bid: str, ask: str | None = None, *, hour: int = 12) -> MarketTick:
    bid_value = Decimal(bid)
    return MarketTick(
        bid=bid_value,
        ask=Decimal(ask) if ask is not None else bid_value + Decimal("0.01"),
        point_size=Decimal("0.01"),
        hour=hour,
    )


def _leg(
    index: int,
    direction: Direction,
    quantity: str,
    price: str,
    pnl: str = "0",
) -> BasketLeg:
    return BasketLeg(index, direction, Decimal(quantity), Decimal(price), Decimal(pnl))


def test_auto_opens_only_on_a_fresh_box_cross_inside_session() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs())
    basket = BasketSnapshot(
        box_high=Decimal("100"),
        box_low=Decimal("90"),
        previous_bid=Decimal("99.99"),
    )

    decision = evaluate_tick(parameters, basket, _tick("100.01"), account_balance=Decimal("10000"))

    assert decision.action == DecisionAction.OPEN
    assert decision.direction == Direction.BUY
    assert decision.quantity == Decimal("0.01")
    assert decision.reason_code == "auto_box_breakout_high"


def test_auto_new_cycle_time_filter_does_not_apply_to_existing_basket() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs(Start_Hour=9, End_Hour=10))
    basket = BasketSnapshot(
        legs=(_leg(0, Direction.BUY, "0.01", "100"),),
        box_high=Decimal("100"),
        box_low=Decimal("90"),
    )

    decision = evaluate_tick(
        parameters,
        basket,
        _tick("89", hour=20),
        account_balance=Decimal("10000"),
    )

    assert decision.action == DecisionAction.ADD
    assert decision.direction == Direction.SELL
    assert decision.quantity == Decimal("0.02")


def test_recovery_requires_manual_first_leg_then_alternates_at_saved_boundaries() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs(ChooseTrading="recovery"))
    empty = BasketSnapshot(box_high=Decimal("105"), box_low=Decimal("95"))

    waiting = evaluate_tick(parameters, empty, _tick("100"), account_balance=Decimal("10000"))
    manual = evaluate_tick(
        parameters,
        empty,
        _tick("100"),
        account_balance=Decimal("10000"),
        manual_direction=Direction.BUY,
        manual_quantity=Decimal("0.03"),
    )
    next_leg = evaluate_tick(
        parameters,
        BasketSnapshot(
            legs=(_leg(0, Direction.BUY, "0.03", "100"),),
            box_high=Decimal("105"),
            box_low=Decimal("95"),
        ),
        _tick("95"),
        account_balance=Decimal("10000"),
    )

    assert waiting.reason_code == "recovery_requires_manual_first_leg"
    assert manual.action == DecisionAction.OPEN
    assert manual.quantity == Decimal("0.03")
    assert next_leg.action == DecisionAction.ADD
    assert next_leg.direction == Direction.SELL
    assert next_leg.quantity == Decimal("0.06")


def test_grid_adds_same_direction_after_adverse_distance() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="grid", Distance="100")
    )
    basket = BasketSnapshot(legs=(_leg(0, Direction.BUY, "0.01", "100"),))

    decision = evaluate_tick(
        parameters,
        basket,
        _tick("98.98", "99.00"),
        account_balance=Decimal("10000"),
    )

    assert decision.action == DecisionAction.ADD
    assert decision.direction == Direction.BUY
    assert decision.quantity == Decimal("0.02")


def test_grid_drift_switches_mode_without_being_limited_by_max_orders() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="auto", MaxOrders=16, GridDrift=3)
    )

    assert effective_mode(parameters, 2) == "auto"
    assert effective_mode(parameters, 3) == "grid"
    assert effective_mode(strategy_parameters_from_mq4(Mq4Inputs()), 16) == "auto"


def test_tp4_uses_direction_order_count_in_grid_mode() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs(ChooseTrading="grid"))
    legs = tuple(_leg(index, Direction.BUY, "0.01", "100") for index in range(7))

    decision = evaluate_tick(
        parameters,
        BasketSnapshot(legs=legs),
        _tick("100.30", "100.31"),
        account_balance=Decimal("10000"),
    )

    assert decision.action == DecisionAction.CLOSE_DIRECTION
    assert decision.reason_code == "basket_take_profit"
    assert decision.target_price == Decimal("100.30")
    assert decision.evidence["take_profit_points"] == "30"


def test_short_take_profit_uses_executable_ask_not_bid() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="grid", TP="100", MaxSpred="50")
    )
    basket = BasketSnapshot(
        legs=(_leg(0, Direction.SELL, "1", "100"),)
    )

    before_target = evaluate_tick(
        parameters,
        basket,
        _tick("98.99", "99.01"),
        account_balance=Decimal("10000"),
    )
    at_target = evaluate_tick(
        parameters,
        basket,
        _tick("98.98", "99.00"),
        account_balance=Decimal("10000"),
    )

    assert before_target.action == DecisionAction.HOLD
    assert at_target.action == DecisionAction.CLOSE_DIRECTION
    assert at_target.reason_code == "basket_take_profit"


def test_overlap_11_means_last_leg_covers_111_percent_of_first_loss() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="grid", OverlapOrderNumber=2, OverlapPercent="11")
    )
    basket = BasketSnapshot(
        legs=(
            _leg(0, Direction.BUY, "0.01", "100", "-10"),
            _leg(1, Direction.BUY, "0.02", "99", "11.10"),
        )
    )

    decision = evaluate_tick(
        parameters,
        basket,
        _tick("99.50"),
        account_balance=Decimal("10000"),
    )

    assert decision.action == DecisionAction.CLOSE_PAIR
    assert decision.leg_indices == (0, 1)
    assert decision.evidence["coverage_percent"] == "111"


def test_live_safe_currency_stop_is_never_blocked_by_wide_spread() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="grid", SL_Dollar="10", MaxSpred="5")
    )
    basket = BasketSnapshot(legs=(_leg(0, Direction.BUY, "0.01", "100", "-11"),))
    wide_tick = _tick("99", "100")

    safe = evaluate_tick(
        parameters,
        basket,
        wide_tick,
        account_balance=Decimal("10000"),
        policy=EnginePolicy.LIVE_SAFE,
    )
    legacy = evaluate_tick(
        parameters,
        basket,
        wide_tick,
        account_balance=Decimal("10000"),
        policy=EnginePolicy.RESEARCH_COMPATIBILITY,
    )

    assert safe.action == DecisionAction.CLOSE_ALL
    assert safe.reason_code == "basket_currency_stop"
    assert legacy.action == DecisionAction.HOLD
    assert legacy.reason_code == "legacy_exit_blocked_by_spread"


def test_wide_spread_blocks_new_risk_in_every_policy() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs(MaxSpred="5"))
    basket = BasketSnapshot(
        box_high=Decimal("100"),
        box_low=Decimal("90"),
        previous_bid=Decimal("99"),
    )

    decision = evaluate_tick(
        parameters,
        basket,
        _tick("101", "102"),
        account_balance=Decimal("10000"),
    )

    assert decision.action == DecisionAction.HOLD
    assert decision.reason_code == "entry_spread_too_wide"


def test_trailing_only_activates_when_start_is_below_selected_tp() -> None:
    default_parameters = strategy_parameters_from_mq4(Mq4Inputs(ChooseTrading="grid"))
    enabled_parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="grid", TrailStart="20", TrailDistance="10")
    )
    basket = BasketSnapshot(
        legs=(_leg(0, Direction.BUY, "0.01", "100"),),
        favorable_high=Decimal("100.50"),
    )

    default_decision = evaluate_tick(
        default_parameters,
        basket,
        _tick("100.39", "100.40"),
        account_balance=Decimal("10000"),
    )
    enabled_decision = evaluate_tick(
        enabled_parameters,
        basket,
        _tick("100.39", "100.40"),
        account_balance=Decimal("10000"),
    )

    assert default_decision.action == DecisionAction.HOLD
    assert enabled_decision.action == DecisionAction.CLOSE_DIRECTION
    assert enabled_decision.reason_code == "basket_trailing_stop"
    assert enabled_decision.target_price == Decimal("100.40")


def test_legacy_section_guard_is_explicit_and_disabled_by_default() -> None:
    default_parameters = strategy_parameters_from_mq4(
        Mq4Inputs(ChooseTrading="recovery", Section="1000")
    )
    legacy_parameters = default_parameters.model_copy(
        update={
            "compatibility": default_parameters.compatibility.model_copy(
                update={"section_policy": "legacy_external_range"}
            )
        }
    )
    basket = BasketSnapshot()

    decision = evaluate_tick(
        legacy_parameters,
        basket,
        _tick("100"),
        account_balance=Decimal("10000"),
        manual_direction=Direction.BUY,
        legacy_visible_range_points=Decimal("999"),
    )

    assert decision.reason_code == "legacy_section_guard_blocked"
