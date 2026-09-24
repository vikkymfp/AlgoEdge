import pytest

from algoedge.signal_state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidStateTransition,
    SignalState,
    transition,
)


def test_happy_path_transitions_are_all_legal() -> None:
    happy_path = [
        SignalState.FLAT, SignalState.SIGNAL_RECEIVED, SignalState.SIGNAL_VALIDATED,
        SignalState.RISK_CHECK, SignalState.OPTION_SELECTED, SignalState.ORDER_PENDING,
        SignalState.FILLED, SignalState.POSITION_OPEN, SignalState.EXIT_PENDING,
        SignalState.CLOSED,
    ]
    current = happy_path[0]
    for target in happy_path[1:]:
        current = transition(current, target)
    assert current == SignalState.CLOSED


def test_partial_fill_then_full_fill_is_legal() -> None:
    assert transition(SignalState.ORDER_PENDING, SignalState.PARTIALLY_FILLED) == SignalState.PARTIALLY_FILLED
    assert transition(SignalState.PARTIALLY_FILLED, SignalState.FILLED) == SignalState.FILLED


def test_partial_fill_can_stay_partially_filled_across_multiple_fills() -> None:
    assert transition(SignalState.PARTIALLY_FILLED, SignalState.PARTIALLY_FILLED) == SignalState.PARTIALLY_FILLED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SignalState.SIGNAL_RECEIVED, SignalState.SIGNAL_REJECTED),
        (SignalState.SIGNAL_VALIDATED, SignalState.SIGNAL_REJECTED),
        (SignalState.RISK_CHECK, SignalState.RISK_REJECTED),
        (SignalState.RISK_CHECK, SignalState.OPTION_SELECTION_FAILED),
        (SignalState.OPTION_SELECTED, SignalState.ORDER_REJECTED),
        (SignalState.ORDER_PENDING, SignalState.ORDER_REJECTED),
        (SignalState.ORDER_PENDING, SignalState.ORDER_FAILED),
        (SignalState.PARTIALLY_FILLED, SignalState.ORDER_FAILED),
        (SignalState.EXIT_PENDING, SignalState.ORDER_FAILED),
    ],
)
def test_failure_branches_are_reachable(current: SignalState, target: SignalState) -> None:
    assert transition(current, target) == target


@pytest.mark.parametrize("state", [
    SignalState.SIGNAL_RECEIVED, SignalState.RISK_CHECK, SignalState.ORDER_PENDING,
    SignalState.POSITION_OPEN, SignalState.EXIT_PENDING,
])
def test_in_flight_states_can_be_interrupted_by_reconciliation_or_halt(state: SignalState) -> None:
    assert transition(state, SignalState.RECONCILIATION_REQUIRED) == SignalState.RECONCILIATION_REQUIRED
    assert transition(state, SignalState.SYSTEM_HALT) == SignalState.SYSTEM_HALT


def test_cannot_skip_a_state() -> None:
    with pytest.raises(InvalidStateTransition):
        transition(SignalState.SIGNAL_RECEIVED, SignalState.OPTION_SELECTED)


def test_cannot_go_backwards() -> None:
    with pytest.raises(InvalidStateTransition):
        transition(SignalState.FILLED, SignalState.ORDER_PENDING)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_every_state_has_an_entry_in_the_transition_map() -> None:
    for state in SignalState:
        assert state in ALLOWED_TRANSITIONS
