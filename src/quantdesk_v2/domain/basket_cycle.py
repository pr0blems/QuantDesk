"""Fail-closed state machine shared by basket backtest and runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BasketState(StrEnum):
    ARMING = "arming"
    OPENING = "opening"
    OPEN = "open"
    ADDING = "adding"
    EXITING = "exiting"
    CLOSED = "closed"
    RECOVERY_REQUIRED = "recovery_required"
    FAILED_CLOSED = "failed_closed"


_TRANSITIONS: dict[BasketState, frozenset[BasketState]] = {
    BasketState.ARMING: frozenset(
        {BasketState.OPENING, BasketState.CLOSED, BasketState.FAILED_CLOSED}
    ),
    BasketState.OPENING: frozenset(
        {
            BasketState.OPEN,
            BasketState.EXITING,
            BasketState.RECOVERY_REQUIRED,
            BasketState.FAILED_CLOSED,
        }
    ),
    BasketState.OPEN: frozenset(
        {
            BasketState.ADDING,
            BasketState.EXITING,
            BasketState.RECOVERY_REQUIRED,
            BasketState.FAILED_CLOSED,
        }
    ),
    BasketState.ADDING: frozenset(
        {
            BasketState.OPEN,
            BasketState.EXITING,
            BasketState.RECOVERY_REQUIRED,
            BasketState.FAILED_CLOSED,
        }
    ),
    BasketState.EXITING: frozenset(
        {
            BasketState.CLOSED,
            BasketState.RECOVERY_REQUIRED,
            BasketState.FAILED_CLOSED,
        }
    ),
    BasketState.RECOVERY_REQUIRED: frozenset(
        {BasketState.OPEN, BasketState.EXITING, BasketState.FAILED_CLOSED}
    ),
    BasketState.FAILED_CLOSED: frozenset(
        {BasketState.RECOVERY_REQUIRED, BasketState.EXITING, BasketState.CLOSED}
    ),
    BasketState.CLOSED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class BasketTransition:
    previous: BasketState
    current: BasketState
    previous_version: int
    current_version: int
    reason_code: str


def transition_basket(
    state: BasketState | str,
    target: BasketState | str,
    *,
    version: int,
    reason_code: str,
) -> BasketTransition:
    current = BasketState(state)
    requested = BasketState(target)
    if version < 1:
        raise ValueError("basket version must be positive")
    if not reason_code.strip():
        raise ValueError("basket transition requires a reason code")
    if requested not in _TRANSITIONS[current]:
        raise ValueError(f"invalid basket transition: {current} -> {requested}")
    return BasketTransition(
        previous=current,
        current=requested,
        previous_version=version,
        current_version=version + 1,
        reason_code=reason_code.strip(),
    )


def state_allows_new_risk(state: BasketState | str) -> bool:
    return BasketState(state) in {BasketState.OPENING, BasketState.OPEN, BasketState.ADDING}


def state_allows_exit(state: BasketState | str) -> bool:
    """Every non-terminal state permits an operator or protection exit."""

    return BasketState(state) != BasketState.CLOSED
