from __future__ import annotations

from enum import Enum


class SignalState(str, Enum):
    FLAT = "FLAT"
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    SIGNAL_VALIDATED = "SIGNAL_VALIDATED"
    RISK_CHECK = "RISK_CHECK"
    OPTION_SELECTED = "OPTION_SELECTED"
    ORDER_PENDING = "ORDER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"

    # Failure / halt states - each reachable from wherever it can actually occur.
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    RISK_REJECTED = "RISK_REJECTED"
    OPTION_SELECTION_FAILED = "OPTION_SELECTION_FAILED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_FAILED = "ORDER_FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    SYSTEM_HALT = "SYSTEM_HALT"


TERMINAL_STATES = frozenset({
    SignalState.CLOSED,
    SignalState.SIGNAL_REJECTED,
    SignalState.RISK_REJECTED,
    SignalState.OPTION_SELECTION_FAILED,
    SignalState.ORDER_REJECTED,
    SignalState.ORDER_FAILED,
})

# The happy-path diagram from the spec, plus a failure branch off every
# state that can plausibly fail, plus SYSTEM_HALT/RECONCILIATION_REQUIRED
# reachable from any in-flight (non-terminal) state - a kill switch or a
# reconciliation mismatch can interrupt a signal at any point before it's
# fully closed out.
_IN_FLIGHT_STATES = frozenset({
    SignalState.SIGNAL_RECEIVED, SignalState.SIGNAL_VALIDATED, SignalState.RISK_CHECK,
    SignalState.OPTION_SELECTED, SignalState.ORDER_PENDING, SignalState.PARTIALLY_FILLED,
    SignalState.FILLED, SignalState.POSITION_OPEN, SignalState.EXIT_PENDING,
})

ALLOWED_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.FLAT: frozenset({SignalState.SIGNAL_RECEIVED}),
    SignalState.SIGNAL_RECEIVED: frozenset({SignalState.SIGNAL_VALIDATED, SignalState.SIGNAL_REJECTED}),
    SignalState.SIGNAL_VALIDATED: frozenset({SignalState.RISK_CHECK, SignalState.SIGNAL_REJECTED}),
    SignalState.RISK_CHECK: frozenset({
        SignalState.OPTION_SELECTED, SignalState.RISK_REJECTED,
        SignalState.OPTION_SELECTION_FAILED,  # contract resolution attempted from here can itself fail
    }),
    SignalState.OPTION_SELECTED: frozenset({
        SignalState.ORDER_PENDING,
        SignalState.ORDER_REJECTED,  # e.g. the operator declined the confirmation prompt
    }),
    SignalState.ORDER_PENDING: frozenset({
        SignalState.PARTIALLY_FILLED, SignalState.FILLED,
        SignalState.ORDER_REJECTED, SignalState.ORDER_FAILED,
    }),
    SignalState.PARTIALLY_FILLED: frozenset({
        SignalState.FILLED, SignalState.PARTIALLY_FILLED, SignalState.ORDER_FAILED,
    }),
    SignalState.FILLED: frozenset({SignalState.POSITION_OPEN}),
    SignalState.POSITION_OPEN: frozenset({SignalState.EXIT_PENDING}),
    SignalState.EXIT_PENDING: frozenset({SignalState.CLOSED, SignalState.ORDER_FAILED}),
    SignalState.CLOSED: frozenset(),
    SignalState.SIGNAL_REJECTED: frozenset(),
    SignalState.RISK_REJECTED: frozenset(),
    SignalState.OPTION_SELECTION_FAILED: frozenset(),
    SignalState.ORDER_REJECTED: frozenset(),
    SignalState.ORDER_FAILED: frozenset(),
    SignalState.RECONCILIATION_REQUIRED: frozenset(),
    SignalState.SYSTEM_HALT: frozenset(),
}

# Every in-flight state may additionally be interrupted by a reconciliation
# mismatch or a system/kill-switch halt - added on top of the happy-path
# map above rather than duplicated into every entry by hand.
for _state in _IN_FLIGHT_STATES:
    ALLOWED_TRANSITIONS[_state] = ALLOWED_TRANSITIONS[_state] | frozenset({
        SignalState.RECONCILIATION_REQUIRED, SignalState.SYSTEM_HALT,
    })


class InvalidStateTransition(ValueError):
    """Raised when code tries to skip or reverse a signal's lifecycle
    state - the spec's "never skip a state transition" rule enforced at
    the type level rather than by convention."""


def transition(current: SignalState, target: SignalState) -> SignalState:
    """Returns `target` if the move from `current` is legal, otherwise
    raises. Pure and side-effect free - callers own persisting the result."""
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransition(
            f"Cannot transition from {current.value} to {target.value}"
        )
    return target
