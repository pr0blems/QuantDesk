"""Pure decision engine for the Martingale TP4 basket strategy.

The engine emits intents only.  It never calls a broker, reads a database, or
changes the supplied basket snapshot.  Research compatibility and production
exit safety are explicit policies instead of hidden condition changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .martingale_tp4 import StrategyParameters, TradingMode


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class DecisionAction(StrEnum):
    HOLD = "hold"
    OPEN = "open"
    ADD = "add"
    CLOSE_DIRECTION = "close_direction"
    CLOSE_ALL = "close_all"
    CLOSE_PAIR = "close_pair"


class EnginePolicy(StrEnum):
    RESEARCH_COMPATIBILITY = "research_compatibility"
    LIVE_SAFE = "live_safe"


@dataclass(frozen=True, slots=True)
class BasketLeg:
    leg_index: int
    direction: Direction
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.leg_index < 0:
            raise ValueError("leg_index must not be negative")
        if self.quantity <= 0:
            raise ValueError("leg quantity must be positive")
        if self.entry_price <= 0:
            raise ValueError("leg entry price must be positive")


@dataclass(frozen=True, slots=True)
class BasketSnapshot:
    legs: tuple[BasketLeg, ...] = ()
    box_high: Decimal | None = None
    box_low: Decimal | None = None
    previous_bid: Decimal | None = None
    favorable_high: Decimal | None = None
    favorable_low: Decimal | None = None

    def __post_init__(self) -> None:
        indices = [leg.leg_index for leg in self.legs]
        if indices != sorted(set(indices)):
            raise ValueError("basket leg indices must be unique and ordered")
        if (self.box_high is None) != (self.box_low is None):
            raise ValueError("box_high and box_low must be supplied together")
        if self.box_high is not None and self.box_low is not None:
            if self.box_high <= self.box_low:
                raise ValueError("box_high must be greater than box_low")


@dataclass(frozen=True, slots=True)
class MarketTick:
    bid: Decimal
    ask: Decimal
    point_size: Decimal
    hour: int

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("tick must contain a valid bid/ask pair")
        if self.point_size <= 0:
            raise ValueError("point_size must be positive")
        if not 0 <= self.hour <= 23:
            raise ValueError("hour must be between 0 and 23")

    @property
    def spread_points(self) -> Decimal:
        return (self.ask - self.bid) / self.point_size


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    action: DecisionAction
    reason_code: str
    effective_mode: TradingMode
    direction: Direction | None = None
    quantity: Decimal | None = None
    target_price: Decimal | None = None
    leg_indices: tuple[int, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


def effective_mode(parameters: StrategyParameters, order_count: int) -> TradingMode:
    if (
        parameters.mode in {"auto", "recovery"}
        and order_count >= parameters.ladder.grid_drift_order_count
    ):
        return "grid"
    return parameters.mode


def first_leg_quantity(parameters: StrategyParameters, account_balance: Decimal) -> Decimal:
    if account_balance <= 0:
        raise ValueError("account_balance must be positive")
    sizing = parameters.sizing
    quantity = sizing.initial_lot
    if sizing.autolot:
        quantity = account_balance / sizing.autolot_balance_unit * sizing.initial_lot
    return min(quantity, sizing.max_lot)


def next_leg_quantity(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    *,
    direction: Direction,
) -> Decimal:
    if not basket.legs:
        raise ValueError("next-leg sizing requires an existing basket")
    sizing = parameters.sizing
    mode = effective_mode(parameters, len(basket.legs))
    if mode == "grid":
        same_direction = [leg for leg in basket.legs if leg.direction == direction]
        exponent = len(same_direction)
        base = same_direction[0].quantity if same_direction else basket.legs[0].quantity
    else:
        exponent = len(basket.legs)
        base = basket.legs[0].quantity
    return min(base * (sizing.lot_multiplier**exponent), sizing.max_lot)


def _direction_legs(basket: BasketSnapshot, direction: Direction) -> list[BasketLeg]:
    return [leg for leg in basket.legs if leg.direction == direction]


def _weighted_price(legs: list[BasketLeg]) -> Decimal | None:
    quantity = sum((leg.quantity for leg in legs), Decimal("0"))
    if quantity == 0:
        return None
    return sum((leg.entry_price * leg.quantity for leg in legs), Decimal("0")) / quantity


def _combined_breakeven(basket: BasketSnapshot, tick: MarketTick) -> tuple[Direction, Decimal] | None:
    buys = _direction_legs(basket, Direction.BUY)
    sells = _direction_legs(basket, Direction.SELL)
    buy_quantity = sum((leg.quantity for leg in buys), Decimal("0"))
    sell_quantity = sum((leg.quantity for leg in sells), Decimal("0"))
    net_quantity = buy_quantity - sell_quantity
    if net_quantity == 0:
        return None
    numerator = sum((leg.entry_price * leg.quantity for leg in buys), Decimal("0"))
    numerator -= sum((leg.entry_price * leg.quantity for leg in sells), Decimal("0"))
    # MQ4 expresses the combined no-loss level relative to Bid and adds the
    # current spread contribution of sell legs.
    numerator += tick.spread_points * tick.point_size * sell_quantity
    direction = Direction.BUY if net_quantity > 0 else Direction.SELL
    return direction, numerator / net_quantity


def _hold(reason: str, mode: TradingMode, **evidence: Any) -> StrategyDecision:
    return StrategyDecision(
        action=DecisionAction.HOLD,
        reason_code=reason,
        effective_mode=mode,
        evidence=evidence,
    )


def _exit_blocked_by_legacy_spread(
    parameters: StrategyParameters,
    tick: MarketTick,
    policy: EnginePolicy,
) -> bool:
    return (
        policy == EnginePolicy.RESEARCH_COMPATIBILITY
        and tick.spread_points > parameters.execution.max_spread_points
    )


def _evaluate_currency_stop(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    mode: TradingMode,
    policy: EnginePolicy,
) -> StrategyDecision | None:
    stop = parameters.stop.basket_loss_currency
    basket_pnl = sum((leg.unrealized_pnl for leg in basket.legs), Decimal("0"))
    if stop == 0 or basket_pnl >= -stop:
        return None
    if _exit_blocked_by_legacy_spread(parameters, tick, policy):
        return _hold(
            "legacy_exit_blocked_by_spread",
            mode,
            intended_exit="basket_currency_stop",
            basket_pnl=str(basket_pnl),
            spread_points=str(tick.spread_points),
        )
    return StrategyDecision(
        action=DecisionAction.CLOSE_ALL,
        reason_code="basket_currency_stop",
        effective_mode=mode,
        evidence={"basket_pnl": str(basket_pnl), "stop": str(stop)},
    )


def _evaluate_overlap(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    mode: TradingMode,
    policy: EnginePolicy,
) -> StrategyDecision | None:
    overlap = parameters.overlap
    if mode != "grid" or not overlap.enabled:
        return None
    if _exit_blocked_by_legacy_spread(parameters, tick, policy):
        return None
    for direction in (Direction.BUY, Direction.SELL):
        legs = _direction_legs(basket, direction)
        if len(legs) < overlap.min_orders:
            continue
        first, last = legs[0], legs[-1]
        required_profit = -first.unrealized_pnl * overlap.required_coverage_percent / Decimal("100")
        if last.unrealized_pnl > 0 and last.unrealized_pnl >= required_profit:
            return StrategyDecision(
                action=DecisionAction.CLOSE_PAIR,
                reason_code="overlap_first_last_covered",
                effective_mode=mode,
                direction=direction,
                leg_indices=(first.leg_index, last.leg_index),
                evidence={
                    "first_leg_pnl": str(first.unrealized_pnl),
                    "last_leg_pnl": str(last.unrealized_pnl),
                    "required_profit": str(required_profit),
                    "coverage_percent": str(overlap.required_coverage_percent),
                },
            )
    return None


def _trailing_target(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    *,
    direction: Direction,
    breakeven: Decimal,
    take_profit_points: Decimal,
) -> Decimal | None:
    trailing = parameters.trailing
    if not trailing.active_for_take_profit(take_profit_points):
        return None
    if direction == Direction.BUY:
        favorable = max(filter(lambda value: value is not None, (basket.favorable_high, tick.bid)))
        start_price = breakeven + trailing.start_points * tick.point_size
        if favorable > start_price:
            return favorable - trailing.distance_points * tick.point_size
        return None
    favorable = min(filter(lambda value: value is not None, (basket.favorable_low, tick.ask)))
    start_price = breakeven - trailing.start_points * tick.point_size
    if favorable < start_price:
        return favorable + trailing.distance_points * tick.point_size
    return None


def _evaluate_direction_exit(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    *,
    mode: TradingMode,
    policy: EnginePolicy,
    direction: Direction,
    breakeven: Decimal,
    order_count: int,
    close_all: bool,
) -> StrategyDecision | None:
    tp_points = parameters.take_profit.points_for_order_count(order_count)
    target = (
        breakeven + tp_points * tick.point_size
        if direction == Direction.BUY
        else breakeven - tp_points * tick.point_size
    )
    trailing_target = _trailing_target(
        parameters,
        basket,
        tick,
        direction=direction,
        breakeven=breakeven,
        take_profit_points=tp_points,
    )
    trailing_hit = trailing_target is not None and (
        tick.bid <= trailing_target if direction == Direction.BUY else tick.bid >= trailing_target
    )
    if trailing_hit:
        if policy == EnginePolicy.RESEARCH_COMPATIBILITY:
            gap = Decimal("100") * tick.point_size
            trailing_hit = (
                tick.bid > trailing_target - gap
                if direction == Direction.BUY
                else tick.bid < trailing_target + gap
            )
        if trailing_hit and not _exit_blocked_by_legacy_spread(parameters, tick, policy):
            return StrategyDecision(
                action=DecisionAction.CLOSE_ALL if close_all else DecisionAction.CLOSE_DIRECTION,
                reason_code="basket_trailing_stop",
                effective_mode=mode,
                direction=direction,
                target_price=trailing_target,
            )
    take_profit_hit = tick.bid >= target if direction == Direction.BUY else tick.bid <= target
    if take_profit_hit and not _exit_blocked_by_legacy_spread(parameters, tick, policy):
        return StrategyDecision(
            action=DecisionAction.CLOSE_ALL if close_all else DecisionAction.CLOSE_DIRECTION,
            reason_code="basket_take_profit",
            effective_mode=mode,
            direction=direction,
            target_price=target,
            evidence={"take_profit_points": str(tp_points), "order_count": order_count},
        )
    return None


def _evaluate_exits(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    mode: TradingMode,
    policy: EnginePolicy,
) -> StrategyDecision | None:
    stop = _evaluate_currency_stop(parameters, basket, tick, mode, policy)
    if stop is not None:
        return stop
    overlap = _evaluate_overlap(parameters, basket, tick, mode, policy)
    if overlap is not None:
        return overlap
    if mode == "grid":
        for direction in (Direction.BUY, Direction.SELL):
            legs = _direction_legs(basket, direction)
            breakeven = _weighted_price(legs)
            if not legs or breakeven is None:
                continue
            decision = _evaluate_direction_exit(
                parameters,
                basket,
                tick,
                mode=mode,
                policy=policy,
                direction=direction,
                breakeven=breakeven,
                order_count=len(legs),
                close_all=False,
            )
            if decision is not None:
                return decision
        return None
    combined = _combined_breakeven(basket, tick)
    if combined is None:
        return None
    direction, breakeven = combined
    return _evaluate_direction_exit(
        parameters,
        basket,
        tick,
        mode=mode,
        policy=policy,
        direction=direction,
        breakeven=breakeven,
        order_count=len(basket.legs),
        close_all=True,
    )


def _entry_decision(
    action: DecisionAction,
    reason: str,
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    mode: TradingMode,
    direction: Direction,
    account_balance: Decimal,
    manual_quantity: Decimal | None = None,
) -> StrategyDecision:
    if tick.spread_points > parameters.execution.max_spread_points:
        return _hold(
            "entry_spread_too_wide",
            mode,
            spread_points=str(tick.spread_points),
            maximum=str(parameters.execution.max_spread_points),
        )
    quantity = (
        first_leg_quantity(parameters, account_balance)
        if not basket.legs
        else next_leg_quantity(parameters, basket, direction=direction)
    )
    if manual_quantity is not None and not basket.legs:
        if manual_quantity <= 0:
            raise ValueError("manual_quantity must be positive")
        quantity = min(manual_quantity, parameters.sizing.max_lot)
    return StrategyDecision(
        action=action,
        reason_code=reason,
        effective_mode=mode,
        direction=direction,
        quantity=quantity,
    )


def evaluate_tick(
    parameters: StrategyParameters,
    basket: BasketSnapshot,
    tick: MarketTick,
    *,
    account_balance: Decimal,
    policy: EnginePolicy = EnginePolicy.LIVE_SAFE,
    manual_direction: Direction | None = None,
    manual_quantity: Decimal | None = None,
    legacy_visible_range_points: Decimal | None = None,
) -> StrategyDecision:
    """Evaluate one tick and return at most one deterministic intent."""

    mode = effective_mode(parameters, len(basket.legs))
    if basket.legs:
        exit_decision = _evaluate_exits(parameters, basket, tick, mode, policy)
        if exit_decision is not None:
            return exit_decision

    if (
        not basket.legs
        and parameters.compatibility.section_policy == "legacy_external_range"
        and (
            legacy_visible_range_points is None
            or legacy_visible_range_points < parameters.compatibility.section_points
        )
    ):
        return _hold("legacy_section_guard_blocked", mode)

    if manual_direction is not None and not basket.legs and mode in {"recovery", "grid"}:
        return _entry_decision(
            DecisionAction.OPEN,
            "manual_cycle_entry",
            parameters,
            basket,
            tick,
            mode,
            manual_direction,
            account_balance,
            manual_quantity,
        )

    if mode == "auto":
        if not basket.legs:
            if not parameters.new_cycle:
                return _hold("new_auto_cycle_disabled", mode)
            if not parameters.session.allows_new_auto_cycle(tick.hour):
                return _hold("outside_auto_cycle_session", mode)
            if basket.box_high is None or basket.box_low is None or basket.previous_bid is None:
                return _hold("auto_box_not_ready", mode)
            if tick.bid > basket.box_high and basket.previous_bid <= basket.box_high:
                return _entry_decision(
                    DecisionAction.OPEN,
                    "auto_box_breakout_high",
                    parameters,
                    basket,
                    tick,
                    mode,
                    Direction.BUY,
                    account_balance,
                )
            if tick.bid < basket.box_low and basket.previous_bid >= basket.box_low:
                return _entry_decision(
                    DecisionAction.OPEN,
                    "auto_box_breakout_low",
                    parameters,
                    basket,
                    tick,
                    mode,
                    Direction.SELL,
                    account_balance,
                )
            return _hold("auto_box_not_crossed", mode)
        if len(basket.legs) >= parameters.sizing.max_orders:
            return _hold("maximum_total_orders_reached", mode)
        if basket.box_high is None or basket.box_low is None:
            return _hold("basket_box_missing", mode)
        last = basket.legs[-1]
        if last.direction == Direction.SELL and tick.bid > basket.box_high:
            return _entry_decision(
                DecisionAction.ADD,
                "auto_reverse_to_buy",
                parameters,
                basket,
                tick,
                mode,
                Direction.BUY,
                account_balance,
            )
        if last.direction == Direction.BUY and tick.bid < basket.box_low:
            return _entry_decision(
                DecisionAction.ADD,
                "auto_reverse_to_sell",
                parameters,
                basket,
                tick,
                mode,
                Direction.SELL,
                account_balance,
            )
        return _hold("auto_reverse_boundary_not_reached", mode)

    if mode == "recovery":
        if not basket.legs:
            return _hold("recovery_requires_manual_first_leg", mode)
        if len(basket.legs) >= parameters.sizing.max_orders:
            return _hold("maximum_total_orders_reached", mode)
        if basket.box_high is None or basket.box_low is None:
            return _hold("basket_box_missing", mode)
        last = basket.legs[-1]
        if last.direction == Direction.BUY and tick.bid <= basket.box_low:
            direction = Direction.SELL
        elif last.direction == Direction.SELL and tick.ask >= basket.box_high:
            direction = Direction.BUY
        else:
            return _hold("recovery_boundary_not_reached", mode)
        return _entry_decision(
            DecisionAction.ADD,
            "recovery_opposite_leg",
            parameters,
            basket,
            tick,
            mode,
            direction,
            account_balance,
        )

    for direction in (Direction.BUY, Direction.SELL):
        legs = _direction_legs(basket, direction)
        if not legs or len(legs) >= parameters.sizing.max_orders:
            continue
        last = legs[-1]
        distance = parameters.ladder.distance_points * tick.point_size
        reached = (
            tick.ask <= last.entry_price - distance
            if direction == Direction.BUY
            else tick.bid >= last.entry_price + distance
        )
        if reached:
            return _entry_decision(
                DecisionAction.ADD,
                "grid_adverse_distance_reached",
                parameters,
                basket,
                tick,
                mode,
                direction,
                account_balance,
            )
    return _hold("grid_distance_not_reached", mode)
