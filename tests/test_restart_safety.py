"""Restart-safety tests matching spec section 31's exact four scenarios:
restart while flat, restart with an open position, restart during a
pending order, and restart after a partial fill.

These exercise the real, reusable restart-safety units this app actually
uses at startup - ReconciliationGate.evaluate() (web_server.py's
_run_reconciliation_check() / fno_signals main.py's
_run_reconciliation_check()) and RiskManager state restoration
(db.load_latest_risk_state() -> RiskState assignment, done identically in
both processes) - rather than spinning up the whole FastAPI app or CLI
process, which would test wiring rather than restart-safety behavior
itself.
"""

from algoedge.reconciliation_gate import ReconciliationGate
from algoedge.risk_manager import RiskManager


def make_order(
    trading_symbol="NIFTY26SEP24500CE", side="BUY", quantity=75, outcome="SUCCESS",
    filled_quantity=None, live=True,
):
    return {
        "tradingSymbol": trading_symbol, "side": side, "quantity": quantity, "outcome": outcome,
        "filledQuantity": filled_quantity, "live": live,
    }


def make_position(trading_symbol="NIFTY26SEP24500CE", quantity=75):
    return {"trading_symbol": trading_symbol, "quantity": quantity}


# -- restart while flat ----------------------------------------------


def test_restart_while_flat_permits_new_orders_immediately() -> None:
    gate = ReconciliationGate()
    assert gate.is_blocking() is True  # before the check runs, fails closed

    check = gate.evaluate(orders=[], live_positions=[])

    assert check.status == "OK"
    assert gate.is_blocking() is False


def test_restart_while_flat_restores_a_clean_risk_state() -> None:
    # No prior snapshot exists yet (a brand new account/day) - RiskManager
    # just starts at its defaults, same as db.load_latest_risk_state()
    # returning None.
    manager = RiskManager()

    assert manager.state.trades_today == 0
    assert manager.state.kill_switch is False
    assert manager.state.consecutive_loss_halt is False


# -- restart with an open position ----------------------------------------------


def test_restart_with_a_genuinely_open_position_reconciles_cleanly() -> None:
    # The process restarted, but the position it opened before restarting
    # is still exactly what Groww reports - must NOT be treated as "no
    # position" just because the app forgot about it in memory.
    orders = [make_order(side="BUY", quantity=75, outcome="SUCCESS")]
    live_positions = [make_position(quantity=75)]
    gate = ReconciliationGate()

    check = gate.evaluate(orders, live_positions)

    assert check.status == "OK"
    assert gate.is_blocking() is False


def test_restart_with_an_open_position_the_broker_no_longer_shows_blocks() -> None:
    # DB says a position should exist; Groww disagrees (e.g. it was closed
    # manually, outside AlgoEdge, while the process was down) - must block
    # rather than silently trust the stale DB record.
    orders = [make_order(side="BUY", quantity=75, outcome="SUCCESS")]
    gate = ReconciliationGate()

    check = gate.evaluate(orders, live_positions=[])

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True


def test_restart_with_an_unexpected_broker_position_blocks() -> None:
    # Groww shows a position AlgoEdge's own DB has no record of at all
    # (e.g. a manual trade placed outside the app, or a DB row lost) -
    # spec section 11's "Expected: No position, Broker: Position exists"
    # example exactly.
    gate = ReconciliationGate()

    check = gate.evaluate(orders=[], live_positions=[make_position(quantity=75)])

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True


# -- restart during a pending order ----------------------------------------------


def test_restart_during_a_pending_order_blocks_until_resolved() -> None:
    # The process restarted between placing an order and ever learning its
    # final status (outcome stayed TIMEOUT/UNKNOWN in the DB) - its fate is
    # genuinely unknown, so new orders must stay blocked, not proceed on
    # the assumption it failed (or succeeded).
    orders = [make_order(outcome="TIMEOUT")]
    gate = ReconciliationGate()

    check = gate.evaluate(orders, live_positions=[])

    assert check.status == "UNCONFIRMED"
    assert gate.is_blocking() is True


def test_restart_during_a_pending_order_unblocks_once_the_order_resolves() -> None:
    # The pending order is later confirmed (a human checked the Groww app
    # and updated the record, or a later poll resolved it) - the very next
    # real check clears the block on its own, no manual reset needed.
    gate = ReconciliationGate()
    gate.evaluate([make_order(outcome="TIMEOUT")], live_positions=[])
    assert gate.is_blocking() is True

    check = gate.evaluate([make_order(outcome="SUCCESS")], live_positions=[make_position(quantity=75)])

    assert check.status == "OK"
    assert gate.is_blocking() is False


# -- restart after a partial fill ----------------------------------------------


def test_restart_after_a_partial_fill_counts_the_filled_portion_and_still_flags_it() -> None:
    # 25 of 75 filled before the process restarted - the 25 is a real
    # position (must reconcile against Groww's actual 25), and the
    # unfilled 50 is still unresolved (must still be flagged for review) -
    # both at once, neither alone would be honest about a partial fill.
    orders = [make_order(quantity=75, outcome="PARTIAL", filled_quantity=25)]
    live_positions = [make_position(quantity=25)]
    gate = ReconciliationGate()

    check = gate.evaluate(orders, live_positions)

    assert check.status == "UNCONFIRMED"  # the remainder is still unresolved
    assert gate.is_blocking() is True
    assert len(check.report.unconfirmed_orders) == 1
    assert check.report.comparisons[0].matches is True  # but the filled portion itself reconciles cleanly


def test_restart_after_a_partial_fill_with_wrong_broker_quantity_is_a_real_mismatch() -> None:
    # The filled portion itself doesn't even match Groww - a genuinely
    # worse situation than "just unconfirmed".
    orders = [make_order(quantity=75, outcome="PARTIAL", filled_quantity=25)]
    live_positions = [make_position(quantity=10)]
    gate = ReconciliationGate()

    check = gate.evaluate(orders, live_positions)

    assert check.status == "MISMATCH"
    assert gate.is_blocking() is True


# -- risk-state restoration alongside reconciliation ----------------------------------------------


def test_restart_restores_kill_switch_state_from_a_prior_snapshot() -> None:
    # Mirrors what web_server.py / fno_signals main.py do with
    # db.load_latest_risk_state(): a prior snapshot dict is applied onto a
    # fresh RiskManager's state.
    prior_snapshot = {
        "auto_trading_enabled": True, "kill_switch": True, "kill_switch_reason": "unresolved from last session",
        "trades_today": 4, "realized_pnl_today": -1200.0, "trade_day": "2026-09-24",
        "consecutive_losses": 2, "consecutive_loss_halt": False, "last_exit_at": None,
    }
    manager = RiskManager()

    manager.state.auto_trading_enabled = prior_snapshot["auto_trading_enabled"]
    manager.state.kill_switch = prior_snapshot["kill_switch"]
    manager.state.kill_switch_reason = prior_snapshot["kill_switch_reason"]
    manager.state.trades_today = prior_snapshot["trades_today"]

    decision = manager.check("BUY", 1, 0)

    assert decision.allowed is False
    assert "unresolved from last session" in decision.reason
