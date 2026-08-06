"""Exchange-neutral protective-order planning and coverage checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .trading import OrderSide, OrderType, PositionSide


class ProtectionAction(StrEnum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True, slots=True)
class ProtectionOrderSpec:
    action: ProtectionAction
    order_type: OrderType
    trigger_price: Decimal

    def __post_init__(self) -> None:
        if self.order_type not in {
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_MARKET,
        }:
            raise ValueError("protection requires a trigger order type")
        if not self.trigger_price.is_finite() or self.trigger_price <= 0:
            raise ValueError("protection trigger must be positive and finite")


@dataclass(frozen=True, slots=True)
class ProtectionPlan:
    """The atomic stop-loss/take-profit pair required for one position."""

    symbol: str
    close_side: OrderSide
    position_side: PositionSide
    quantity: Decimal | None
    signal_time: int
    orders: tuple[ProtectionOrderSpec, ProtectionOrderSpec]

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        close_side: OrderSide | str,
        position_side: PositionSide | str,
        quantity: Decimal | None,
        signal_time: int,
        stop: Decimal,
        target: Decimal,
    ) -> ProtectionPlan:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise ValueError("protection symbol is required")
        normalized_close_side = OrderSide(close_side)
        normalized_position_side = PositionSide(position_side)
        if isinstance(signal_time, bool) or signal_time < 0:
            raise ValueError("protection signal time must be non-negative")
        if quantity is not None and (not quantity.is_finite() or quantity <= 0):
            raise ValueError("protection quantity must be positive and finite")
        if normalized_position_side is not PositionSide.BOTH and quantity is None:
            raise ValueError("hedge-mode protection requires quantity")
        if normalized_close_side is OrderSide.SELL and stop >= target:
            raise ValueError("long protection stop must be below target")
        if normalized_close_side is OrderSide.BUY and stop <= target:
            raise ValueError("short protection stop must be above target")
        return cls(
            symbol=normalized_symbol,
            close_side=normalized_close_side,
            position_side=normalized_position_side,
            quantity=quantity,
            signal_time=signal_time,
            orders=(
                ProtectionOrderSpec(
                    action=ProtectionAction.STOP,
                    order_type=OrderType.STOP_MARKET,
                    trigger_price=stop,
                ),
                ProtectionOrderSpec(
                    action=ProtectionAction.TAKE_PROFIT,
                    order_type=OrderType.TAKE_PROFIT_MARKET,
                    trigger_price=target,
                ),
            ),
        )

    def signal_key(self, deployment_id: int | str, action: ProtectionAction) -> str:
        return (
            f"live:{deployment_id}:{self.symbol}:{self.position_side.value}:"
            f"{self.signal_time}:{action.value}"
        )


@dataclass(frozen=True, slots=True)
class ProtectionCoverage:
    actions: frozenset[ProtectionAction]
    unexpected_actions: frozenset[str] = frozenset()

    @classmethod
    def from_actions(cls, actions: set[str] | frozenset[str]) -> ProtectionCoverage:
        recognized: set[ProtectionAction] = set()
        unexpected: set[str] = set()
        for raw_action in actions:
            try:
                recognized.add(ProtectionAction(str(raw_action)))
            except ValueError:
                unexpected.add(str(raw_action))
        return cls(frozenset(recognized), frozenset(unexpected))

    @property
    def complete(self) -> bool:
        return self.actions == frozenset(ProtectionAction) and not self.unexpected_actions

    @property
    def missing(self) -> frozenset[ProtectionAction]:
        return frozenset(ProtectionAction).difference(self.actions)

