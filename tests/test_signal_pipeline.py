from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algoedge import db as db_module
from algoedge.signal_pipeline import (
    IngestOutcome,
    OptionSignal,
    SignalService,
    build_signal_id,
)
from algoedge.signal_state import InvalidStateTransition, SignalState

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def reset_db_module_state():
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


def make_signal(
    direction="CALL", signal_id=None, candle_time=NOW, expires_at=None,
    strike=24550, option_type="CE",
):
    return OptionSignal(
        signal_id=signal_id or build_signal_id("fno_signals", "NIFTY", direction, candle_time),
        source="fno_signals",
        underlying_symbol="NIFTY",
        underlying_price=24582.0,
        direction=direction,
        created_at=candle_time,
        expires_at=expires_at or (candle_time + timedelta(minutes=2)),
        option_strike=strike,
        option_type=option_type,
        option_expiry=None,
    )


# -- deterministic ids ----------------------------------------------


def test_build_signal_id_is_deterministic_for_the_same_candle() -> None:
    a = build_signal_id("fno_signals", "NIFTY", "CALL", NOW)
    b = build_signal_id("fno_signals", "NIFTY", "CALL", NOW)

    assert a == b


def test_build_signal_id_differs_for_a_different_candle() -> None:
    a = build_signal_id("fno_signals", "NIFTY", "CALL", NOW)
    b = build_signal_id("fno_signals", "NIFTY", "CALL", NOW + timedelta(minutes=5))

    assert a != b


# -- CALL / PUT accepted ----------------------------------------------


def test_call_signal_is_accepted() -> None:
    service = SignalService()

    result = service.ingest(make_signal(direction="CALL"), now=NOW)

    assert result.outcome == IngestOutcome.ACCEPTED
    assert result.state == SignalState.SIGNAL_VALIDATED


def test_put_signal_is_accepted() -> None:
    service = SignalService()

    result = service.ingest(make_signal(direction="PUT", option_type="PE"), now=NOW)

    assert result.outcome == IngestOutcome.ACCEPTED


# -- duplicate signal ----------------------------------------------


def test_duplicate_signal_is_rejected_and_leaves_original_untouched() -> None:
    service = SignalService()
    signal = make_signal()

    first = service.ingest(signal, now=NOW)
    second = service.ingest(signal, now=NOW)

    assert first.outcome == IngestOutcome.ACCEPTED
    assert second.outcome == IngestOutcome.DUPLICATE_SIGNAL
    assert second.state == first.state  # original untouched, not re-rejected


def test_same_candle_scanned_twice_produces_the_same_signal_id_and_dedupes() -> None:
    service = SignalService()
    first_signal = make_signal(candle_time=NOW)
    second_signal = make_signal(candle_time=NOW)  # simulates re-scanning the same closed candle

    service.ingest(first_signal, now=NOW)
    second = service.ingest(second_signal, now=NOW)

    assert first_signal.signal_id == second_signal.signal_id
    assert second.outcome == IngestOutcome.DUPLICATE_SIGNAL


# -- expired signal ----------------------------------------------


def test_expired_signal_is_rejected() -> None:
    service = SignalService()
    signal = make_signal(candle_time=NOW, expires_at=NOW + timedelta(minutes=2))

    result = service.ingest(signal, now=NOW + timedelta(minutes=5))

    assert result.outcome == IngestOutcome.SIGNAL_EXPIRED
    assert result.state == SignalState.SIGNAL_REJECTED


def test_signal_within_expiry_window_is_accepted() -> None:
    service = SignalService()
    signal = make_signal(candle_time=NOW, expires_at=NOW + timedelta(minutes=2))

    result = service.ingest(signal, now=NOW + timedelta(minutes=1))

    assert result.outcome == IngestOutcome.ACCEPTED


# -- invalid signal ----------------------------------------------


def test_invalid_direction_is_rejected() -> None:
    service = SignalService()
    signal = make_signal(direction="SIDEWAYS")

    result = service.ingest(signal, now=NOW)

    assert result.outcome == IngestOutcome.SIGNAL_REJECTED


def test_call_signal_without_strike_is_rejected() -> None:
    service = SignalService()
    signal = make_signal(strike=None)

    result = service.ingest(signal, now=NOW)

    assert result.outcome == IngestOutcome.SIGNAL_REJECTED


def test_call_signal_with_bad_option_type_is_rejected() -> None:
    service = SignalService()
    signal = make_signal(option_type="XX")

    result = service.ingest(signal, now=NOW)

    assert result.outcome == IngestOutcome.SIGNAL_REJECTED


def test_nonpositive_underlying_price_is_rejected() -> None:
    service = SignalService()
    signal = OptionSignal(
        signal_id="AE-test-bad-price", source="fno_signals", underlying_symbol="NIFTY",
        underlying_price=0.0, direction="CALL", created_at=NOW, expires_at=NOW + timedelta(minutes=2),
        option_strike=24550, option_type="CE",
    )

    result = service.ingest(signal, now=NOW)

    assert result.outcome == IngestOutcome.SIGNAL_REJECTED


# -- advance() / state transitions ----------------------------------------------


def test_advance_moves_a_validated_signal_through_the_pipeline() -> None:
    service = SignalService()
    signal = make_signal()
    service.ingest(signal, now=NOW)

    state = service.advance(signal.signal_id, SignalState.RISK_CHECK)
    state = service.advance(signal.signal_id, SignalState.OPTION_SELECTED)
    state = service.advance(signal.signal_id, SignalState.ORDER_PENDING)
    state = service.advance(signal.signal_id, SignalState.FILLED)

    assert state == SignalState.FILLED


def test_advance_raises_on_illegal_skip() -> None:
    service = SignalService()
    signal = make_signal()
    service.ingest(signal, now=NOW)

    with pytest.raises(InvalidStateTransition):
        service.advance(signal.signal_id, SignalState.ORDER_PENDING)  # skips RISK_CHECK/OPTION_SELECTED


def test_advance_on_unknown_signal_raises() -> None:
    service = SignalService()

    with pytest.raises(ValueError, match="No such signal"):
        service.advance("does-not-exist", SignalState.RISK_CHECK)


# -- conflicting signal / duplicate order protection ----------------------------------------------


def test_no_duplicate_order_when_nothing_in_flight() -> None:
    service = SignalService()

    found = service.check_duplicate_order(source="fno_signals", underlying_symbol="NIFTY", direction="CALL")

    assert found is None


def test_conflicting_signal_is_detected_once_order_is_in_flight() -> None:
    service = SignalService()
    signal = make_signal(direction="CALL")
    service.ingest(signal, now=NOW)
    service.advance(signal.signal_id, SignalState.RISK_CHECK)
    service.advance(signal.signal_id, SignalState.OPTION_SELECTED)
    service.advance(signal.signal_id, SignalState.ORDER_PENDING)

    found = service.check_duplicate_order(source="fno_signals", underlying_symbol="NIFTY", direction="CALL")

    assert found is not None
    assert found["signalId"] == signal.signal_id


def test_a_different_direction_is_not_treated_as_a_conflict() -> None:
    service = SignalService()
    signal = make_signal(direction="CALL")
    service.ingest(signal, now=NOW)
    service.advance(signal.signal_id, SignalState.RISK_CHECK)
    service.advance(signal.signal_id, SignalState.OPTION_SELECTED)
    service.advance(signal.signal_id, SignalState.ORDER_PENDING)

    found = service.check_duplicate_order(source="fno_signals", underlying_symbol="NIFTY", direction="PUT")

    assert found is None


def test_a_signal_that_only_reached_validated_does_not_block_a_new_order() -> None:
    # SIGNAL_VALIDATED is not "in flight" for order-duplicate purposes -
    # only once risk/option-selection has actually started an order.
    service = SignalService()
    signal = make_signal(direction="CALL")
    service.ingest(signal, now=NOW)

    found = service.check_duplicate_order(source="fno_signals", underlying_symbol="NIFTY", direction="CALL")

    assert found is None
