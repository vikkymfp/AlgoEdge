from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from algoedge.reconciliation_gate import ReconciliationGate

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=IST)


def make_order(symbol="NIFTY26SEP24500CE", side="BUY", quantity=75, outcome="SUCCESS", live=True):
    return {"tradingSymbol": symbol, "side": side, "quantity": quantity, "outcome": outcome, "live": live}


def make_position(symbol="NIFTY26SEP24500CE", quantity=75):
    return {"trading_symbol": symbol, "quantity": quantity}


# -- fail-closed by default ----------------------------------------------


def test_blocks_before_evaluate_has_ever_run() -> None:
    gate = ReconciliationGate()

    assert gate.is_blocking() is True
    assert gate.last_check.status == "UNKNOWN"


def test_mark_unavailable_stays_blocking() -> None:
    gate = ReconciliationGate()

    gate.mark_unavailable("Groww is not connected")

    assert gate.is_blocking() is True
    assert gate.last_check.status == "UNKNOWN"
    assert gate.last_check.reason == "Groww is not connected"


# -- clean reconciliation unblocks ----------------------------------------------


def test_matching_positions_and_no_unconfirmed_orders_unblocks() -> None:
    gate = ReconciliationGate()

    check = gate.evaluate([make_order()], [make_position()], now=NOW)

    assert check.status == "OK"
    assert gate.is_blocking() is False


def test_no_orders_and_no_positions_is_ok() -> None:
    gate = ReconciliationGate()

    check = gate.evaluate([], [], now=NOW)

    assert check.status == "OK"
    assert gate.is_blocking() is False


# -- mismatch blocks ----------------------------------------------


def test_quantity_mismatch_blocks() -> None:
    gate = ReconciliationGate()

    check = gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True
    assert "NIFTY26SEP24500CE" in check.reason


def test_unexpected_broker_position_with_no_db_order_blocks() -> None:
    gate = ReconciliationGate()

    check = gate.evaluate([], [make_position()], now=NOW)

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True


def test_expected_position_missing_from_broker_blocks() -> None:
    gate = ReconciliationGate()

    check = gate.evaluate([make_order()], [], now=NOW)

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True


# -- unconfirmed orders block ----------------------------------------------


def test_unconfirmed_order_blocks_even_without_a_position_mismatch() -> None:
    gate = ReconciliationGate()
    unconfirmed = make_order(outcome="TIMEOUT")

    check = gate.evaluate([unconfirmed], [], now=NOW)

    assert check.status == "UNCONFIRMED"
    assert gate.is_blocking() is True


# -- override ----------------------------------------------


def test_override_temporarily_unblocks_a_mismatch() -> None:
    gate = ReconciliationGate()
    gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)
    assert gate.is_blocking() is True

    gate.override("Manually verified - extra 75 qty is a pre-existing position outside AlgoEdge", now=NOW)

    assert gate.is_blocking(now=NOW + timedelta(minutes=5)) is False


def test_override_expires() -> None:
    gate = ReconciliationGate()
    gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)
    gate.override("temporary", duration_minutes=10, now=NOW)

    assert gate.is_blocking(now=NOW + timedelta(minutes=15)) is True


def test_a_genuine_ok_check_clears_a_prior_override() -> None:
    gate = ReconciliationGate()
    gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)
    gate.override("temporary", now=NOW)
    assert gate.is_blocking(now=NOW + timedelta(minutes=1)) is False

    # The mismatch resolves itself for real (not a corrective trade -
    # just the underlying data catching up).
    gate.evaluate([make_order(quantity=150)], [make_position(quantity=150)], now=NOW + timedelta(minutes=2))

    assert gate.is_blocking(now=NOW + timedelta(minutes=3)) is False
    assert gate.last_check.status == "OK"


def test_clear_override_re_blocks_immediately() -> None:
    gate = ReconciliationGate()
    gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)
    gate.override("temporary", now=NOW)
    assert gate.is_blocking(now=NOW + timedelta(minutes=1)) is False

    gate.clear_override()

    assert gate.is_blocking(now=NOW + timedelta(minutes=1)) is True


# -- status payload ----------------------------------------------


def test_status_payload_reflects_current_state() -> None:
    gate = ReconciliationGate()
    gate.evaluate([make_order(quantity=75)], [make_position(quantity=150)], now=NOW)

    payload = gate.status_payload()

    assert payload["status"] == "MISMATCH"
    assert payload["blocking"] is True
    assert payload["checkedAt"] == NOW
