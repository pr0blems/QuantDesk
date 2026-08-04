from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    CREATED = "created"
    RISK_REJECTED = "risk_rejected"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


TERMINAL_STATES = frozenset(
    {
        OrderState.RISK_REJECTED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)

_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.RISK_REJECTED, OrderState.APPROVED}),
    OrderState.APPROVED: frozenset(
        {OrderState.SUBMITTING, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    OrderState.SUBMITTING: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
}


class InvalidOrderTransition(ValueError):
    def __init__(self, current: OrderState, target: OrderState):
        super().__init__(f"invalid order transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


def transition_order(current: OrderState | str, target: OrderState | str) -> OrderState:
    """Validate one idempotent transition in the append-only order event stream."""

    current_state = OrderState(current)
    target_state = OrderState(target)
    if current_state == target_state:
        return current_state
    if target_state not in _TRANSITIONS.get(current_state, frozenset()):
        raise InvalidOrderTransition(current_state, target_state)
    return target_state
