from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Literal

from algoedge import db
from algoedge.signal_state import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransition,
    SignalState,
    transition,
)

logger = logging.getLogger("algoedge.signal_pipeline")

DIRECTIONS = {"CALL", "PUT", "FLAT"}
RiskModel = Literal["UNDERLYING_BASED", "OPTION_PREMIUM_BASED"]

# A signal counts as "already being worked" for duplicate-order protection
# (spec section 7) once it has passed risk/option-selection and has a real
# order in flight or a resulting position - not merely received/validated.
IN_FLIGHT_ORDER_STATES = [
    SignalState.ORDER_PENDING.value,
    SignalState.PARTIALLY_FILLED.value,
    SignalState.FILLED.value,
    SignalState.POSITION_OPEN.value,
    SignalState.EXIT_PENDING.value,
]


def build_signal_id(source: str, underlying_symbol: str, direction: str, candle_close_time: datetime) -> str:
    """Deterministic, not random - re-evaluating the exact same closed
    candle must always produce the exact same signal_id, or duplicate
    detection can never trigger. `candle_close_time` should be the
    timestamp of the candle the signal was generated from (its "as of"
    moment), not wall-clock now."""
    stamp = candle_close_time.strftime("%Y%m%d%H%M")
    return f"AE-{source}-{underlying_symbol}-{direction}-{stamp}"


@dataclass(frozen=True)
class OptionSignal:
    """A single trading signal, keeping the underlying and the option it
    implies strictly separate per the spec - underlying_price is NEVER the
    same thing as an option premium, and the two must never be confused
    downstream."""

    signal_id: str
    source: str
    underlying_symbol: str
    underlying_price: float
    direction: str  # CALL | PUT | FLAT
    created_at: datetime
    expires_at: datetime
    risk_model: RiskModel = "OPTION_PREMIUM_BASED"
    option_symbol: str | None = None
    option_strike: int | None = None
    option_type: str | None = None  # CE | PE
    option_expiry: date | None = None


class IngestOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"


@dataclass(frozen=True)
class SignalIngestResult:
    outcome: IngestOutcome
    signal_id: str
    state: SignalState
    reason: str | None = None


def _validate(signal: OptionSignal) -> str | None:
    """Returns a rejection reason, or None if the signal is well-formed.
    Pure - no I/O, easy to test exhaustively."""
    if not signal.underlying_symbol:
        return "underlying_symbol is required"
    if signal.direction not in DIRECTIONS:
        return f"Unsupported direction: {signal.direction!r}"
    if signal.underlying_price <= 0:
        return "underlying_price must be positive"
    if signal.direction in {"CALL", "PUT"}:
        if signal.option_strike is None or signal.option_strike <= 0:
            return "option_strike is required and must be positive for CALL/PUT signals"
        if signal.option_type not in {"CE", "PE"}:
            return f"Unsupported option_type: {signal.option_type!r}"
    return None


class SignalService:
    """The single entry point every signal producer (fno_signals today,
    algoedge.auto_trader and any future webhook later) must call before a
    signal is allowed to influence risk checks or order placement.

    Falls back to a small in-memory cache when the database isn't
    configured/reachable, so duplicate-signal protection still works
    within a single running process even without persistence - matching
    every other module's "DB is optional, trading must still work safely"
    philosophy. The in-memory cache is intentionally NOT a substitute for
    the DB's unique constraint across restarts; it only covers this
    process's own lifetime.
    """

    def __init__(self) -> None:
        self._memory_signals: dict[str, dict] = {}

    def _get_existing(self, signal_id: str) -> dict | None:
        existing = db.get_signal_by_id(signal_id)
        if existing is not None:
            return existing
        return self._memory_signals.get(signal_id)

    def _persist_new(self, signal: OptionSignal, state: SignalState, reason: str | None) -> None:
        record = {
            "signalId": signal.signal_id, "source": signal.source,
            "createdAt": signal.created_at, "expiresAt": signal.expires_at,
            "underlyingSymbol": signal.underlying_symbol, "underlyingPrice": signal.underlying_price,
            "direction": signal.direction, "optionSymbol": signal.option_symbol,
            "optionStrike": signal.option_strike, "optionType": signal.option_type,
            "optionExpiry": signal.option_expiry, "riskModel": signal.risk_model,
            "state": state.value, "reason": reason,
        }
        self._memory_signals[signal.signal_id] = record
        persisted = db.create_signal_record(
            signal_id=signal.signal_id, source=signal.source, expires_at=signal.expires_at,
            underlying_symbol=signal.underlying_symbol, underlying_price=signal.underlying_price,
            direction=signal.direction, option_symbol=signal.option_symbol,
            option_strike=signal.option_strike, option_type=signal.option_type,
            option_expiry=signal.option_expiry, risk_model=signal.risk_model,
            state=state.value, reason=reason,
        )
        if persisted:
            db.record_signal_state_event(
                signal_id=signal.signal_id, from_state=SignalState.FLAT.value,
                to_state=state.value, reason=reason,
            )

    def ingest(self, signal: OptionSignal, *, now: datetime | None = None) -> SignalIngestResult:
        """Duplicate check -> expiry check -> field validation, in that
        order, per the spec. A duplicate leaves the existing record
        completely untouched (never re-validated, never re-persisted)."""
        now = now or signal.created_at

        existing = self._get_existing(signal.signal_id)
        if existing is not None:
            logger.info("Duplicate signal ignored: %s", signal.signal_id)
            return SignalIngestResult(
                IngestOutcome.DUPLICATE_SIGNAL, signal.signal_id, SignalState(existing["state"]),
                reason="An identical signal was already recorded",
            )

        if now > signal.expires_at:
            self._persist_new(signal, SignalState.SIGNAL_REJECTED, "SIGNAL_EXPIRED")
            logger.info("Signal expired before ingest: %s", signal.signal_id)
            return SignalIngestResult(
                IngestOutcome.SIGNAL_EXPIRED, signal.signal_id, SignalState.SIGNAL_REJECTED,
                reason="SIGNAL_EXPIRED",
            )

        rejection = _validate(signal)
        if rejection is not None:
            self._persist_new(signal, SignalState.SIGNAL_REJECTED, rejection)
            logger.info("Signal rejected (%s): %s", rejection, signal.signal_id)
            return SignalIngestResult(
                IngestOutcome.SIGNAL_REJECTED, signal.signal_id, SignalState.SIGNAL_REJECTED,
                reason=rejection,
            )

        self._persist_new(signal, SignalState.SIGNAL_VALIDATED, None)
        return SignalIngestResult(IngestOutcome.ACCEPTED, signal.signal_id, SignalState.SIGNAL_VALIDATED)

    def advance(self, signal_id: str, target: SignalState, *, reason: str | None = None) -> SignalState:
        """Validates the transition against signal_state.py's map, then
        persists both the new state and an audit row. Raises
        InvalidStateTransition rather than silently allowing a skipped
        step - callers (risk engine, execution engine) are expected to
        call this at every real step of the pipeline, not just at the
        start and end."""
        existing = self._get_existing(signal_id)
        if existing is None:
            raise ValueError(f"No such signal: {signal_id}")
        current = SignalState(existing["state"])
        new_state = transition(current, target)

        existing["state"] = new_state.value
        if reason is not None:
            existing["reason"] = reason
        self._memory_signals[signal_id] = existing
        db.update_signal_state(signal_id, new_state.value, reason=reason)
        db.record_signal_state_event(
            signal_id=signal_id, from_state=current.value, to_state=new_state.value, reason=reason,
        )
        return new_state

    def check_duplicate_order(self, *, source: str, underlying_symbol: str, direction: str) -> dict | None:
        """Returns the existing in-flight signal (as a dict) if one is
        already being worked for this exact source+symbol+direction, or
        None if it's safe to proceed. Callers must check this immediately
        before placing an order - spec section 7's "do not place another
        order" rule. Falls back to the in-memory cache when the DB isn't
        configured, same as ingest()."""
        found = db.find_open_signal_for(
            source=source, underlying_symbol=underlying_symbol, direction=direction,
            in_flight_states=IN_FLIGHT_ORDER_STATES,
        )
        if found is not None:
            return found
        matches = [
            record for record in self._memory_signals.values()
            if record["source"] == source
            and record["underlyingSymbol"] == underlying_symbol
            and record["direction"] == direction
            and record["state"] in IN_FLIGHT_ORDER_STATES
        ]
        return matches[-1] if matches else None


__all__ = [
    "ALLOWED_TRANSITIONS",
    "IN_FLIGHT_ORDER_STATES",
    "IngestOutcome",
    "InvalidStateTransition",
    "OptionSignal",
    "SignalIngestResult",
    "SignalService",
    "SignalState",
    "build_signal_id",
]
