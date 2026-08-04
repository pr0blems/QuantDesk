import pytest

from quantdesk_v2.execution import InvalidOrderTransition, OrderState, transition_order


def test_happy_path_supports_partial_fill_and_cancel() -> None:
    state = transition_order(OrderState.CREATED, OrderState.APPROVED)
    state = transition_order(state, OrderState.SUBMITTING)
    state = transition_order(state, OrderState.ACKNOWLEDGED)
    state = transition_order(state, OrderState.PARTIALLY_FILLED)
    state = transition_order(state, OrderState.CANCEL_PENDING)
    assert transition_order(state, OrderState.CANCELLED) == OrderState.CANCELLED


def test_same_state_event_is_idempotent() -> None:
    assert (
        transition_order(OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED)
        == OrderState.PARTIALLY_FILLED
    )


def test_timeout_must_reconcile_unknown_before_retrying() -> None:
    state = transition_order(OrderState.SUBMITTING, OrderState.UNKNOWN)
    assert transition_order(state, OrderState.FILLED) == OrderState.FILLED
    with pytest.raises(InvalidOrderTransition):
        transition_order(state, OrderState.SUBMITTING)


def test_terminal_order_cannot_be_reopened() -> None:
    with pytest.raises(InvalidOrderTransition):
        transition_order(OrderState.FILLED, OrderState.ACKNOWLEDGED)
