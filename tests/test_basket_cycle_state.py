from __future__ import annotations

import pytest

from quantdesk_v2.domain.basket_cycle import (
    BasketState,
    state_allows_exit,
    state_allows_new_risk,
    transition_basket,
)


def test_normal_cycle_transitions_increment_optimistic_version() -> None:
    opening = transition_basket(
        BasketState.ARMING,
        BasketState.OPENING,
        version=1,
        reason_code="box_breakout_confirmed",
    )
    opened = transition_basket(
        opening.current,
        BasketState.OPEN,
        version=opening.current_version,
        reason_code="first_leg_filled",
    )

    assert opened.current == BasketState.OPEN
    assert opened.current_version == 3
    assert state_allows_new_risk(opened.current) is True


def test_failed_closed_forbids_additions_but_never_forbids_exit() -> None:
    assert state_allows_new_risk(BasketState.FAILED_CLOSED) is False
    assert state_allows_exit(BasketState.FAILED_CLOSED) is True
    transition = transition_basket(
        BasketState.FAILED_CLOSED,
        BasketState.EXITING,
        version=7,
        reason_code="operator_flatten",
    )
    assert transition.current == BasketState.EXITING


def test_closed_cycle_cannot_be_reopened() -> None:
    assert state_allows_exit(BasketState.CLOSED) is False
    with pytest.raises(ValueError, match="invalid basket transition"):
        transition_basket(
            BasketState.CLOSED,
            BasketState.OPENING,
            version=4,
            reason_code="invalid_restart",
        )
