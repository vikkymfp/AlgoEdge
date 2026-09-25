"""Phase 3 - restart persistence + event-processing reliability for paper
Auto Trade.

Mirrors tests/test_restart_safety.py's established approach: exercise the
real, reusable restart-safety unit this app actually calls at startup
(auto_trader.restore_account_state(), the same function web_server.py's
module-level restoration block calls) directly against hand-built snapshot
dicts, rather than spinning up a real database - matching how RiskManager's
restart restoration is already tested. algoedge.db.record_auto_trade_
account_snapshot()/load_latest_auto_trade_account_state() themselves follow
the project's existing db.py patterns (record_risk_snapshot/
load_latest_risk_state) exactly and are not re-tested here.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.auto_trader import restore_account_state
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.strategy import TradeEvent

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=IST)


def trending_df(n: int, start_price: float, step: float, start: str = "2026-09-23 09:15") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [start_price + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + abs(step) / 2 + 1 for c in closes],
            "Low": [c - abs(step) / 2 - 1 for c in closes],
            "Close": closes,
            "Volume": [0.0] * n,
        },
        index=index,
    )


UPTREND_60 = trending_df(60, start_price=100.0, step=2.0)  # ENTRY_CALL at bar 16, EXIT_TARGET at bar 24
DOWNTREND_60 = trending_df(60, start_price=300.0, step=-2.0)  # ENTRY_PUT at bar 13, EXIT_TARGET at bar 21


def patch_fetch(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)


def _fake_resolve_contract(event: TradeEvent) -> OptionContract:
    """Phase 5 stand-in - this file is about restart persistence, not
    resolution itself, so entries resolve cleanly by default."""
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}",
        underlying="NIFTY", right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def run_cycle(*args, **kwargs):
    kwargs.setdefault("resolve_contract_fn", _fake_resolve_contract)
    return auto_trader.run_cycle(*args, **kwargs)


# ---------- 1. Restart while flat ----------


def test_restart_while_flat_restores_a_flat_account() -> None:
    snapshot = {"cash": 1_000_000.0, "quantity": 0, "average_price": None, "side": None, "last_event_at": None}
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.quantity == 0
    assert account.side is None
    assert account.last_event_at is None


def test_no_prior_snapshot_leaves_a_fresh_account_untouched() -> None:
    # db.load_latest_auto_trade_account_state() returning None (brand new
    # account/day) means the caller simply never calls restore_account_state
    # at all - matching web_server.py's `if _prior_account_state is not None`.
    account = SimulatedAccount()
    assert account.quantity == 0
    assert account.last_event_at is None


# ---------- 2 & 3. Restart with an open CALL / PUT position ----------


def test_restart_with_open_call_position_restores_it_exactly() -> None:
    last_event_at = pd.Timestamp("2026-09-23 10:35", tz="Asia/Kolkata")
    snapshot = {
        "cash": 998_680.0, "quantity": 2, "average_price": 132.0, "side": "CALL",
        "last_event_at": last_event_at,
    }
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.quantity == 2
    assert account.side == "CALL"
    assert account.average_price == pytest.approx(132.0)
    assert account.last_event_at == last_event_at


def test_restart_with_open_put_position_restores_it_exactly() -> None:
    last_event_at = pd.Timestamp("2026-09-23 10:20", tz="Asia/Kolkata")
    snapshot = {
        "cash": 999_452.0, "quantity": 1, "average_price": 274.0, "side": "PUT",
        "last_event_at": last_event_at,
    }
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.quantity == 1
    assert account.side == "PUT"
    assert account.average_price == pytest.approx(274.0)


# ---------- 4. Restart after an entry was already filled: the restored
# state must prevent that exact entry from being filled again ----------


def test_restart_after_an_entry_fill_does_not_refill_it(monkeypatch) -> None:
    entry_timestamp = UPTREND_60.index[16]  # ENTRY_CALL's real bar
    snapshot = {
        "cash": 999_868.0, "quantity": 1, "average_price": 132.0, "side": "CALL",
        "last_event_at": entry_timestamp,
    }
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])  # window still ends right at the same entry bar

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    assert result.order is None
    assert "duplicate" in result.risk.reason.lower()
    assert account.quantity == 1  # unchanged - not re-filled


# ---------- 5. Restart after an exit was filled: account restored flat,
# ready for a genuinely new entry ----------


def test_restart_after_an_exit_fill_restores_flat_and_allows_a_new_entry(monkeypatch) -> None:
    exit_timestamp = UPTREND_60.index[24]  # EXIT_TARGET's real bar
    snapshot = {"cash": 1_000_016.0, "quantity": 0, "average_price": None, "side": None, "last_event_at": exit_timestamp}
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    # A brand-new, later reversal window - a genuinely new entry after the
    # restart must still be possible, not permanently blocked.
    reversal_downtrend = trending_df(60, start_price=300.0, step=-2.0, start="2026-09-23 11:20")
    patch_fetch(monkeypatch, reversal_downtrend.iloc[:14])

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(hours=2),
    )

    assert result.event.kind == "ENTRY_PUT"
    assert result.order.status == "PLACED"
    assert account.side == "PUT"


# ---------- 6. Restart with a pending/risk-blocked event: never persisted
# (last_event_at only advances on a PLACED fill), so it must still be
# retried after restart, exactly as if the process had never gone down ----------


def test_restart_with_a_risk_blocked_event_still_retries_it_after_restart(monkeypatch) -> None:
    # No snapshot was ever written for the blocked attempt (db.record_
    # auto_trade_account_snapshot() is only called after a PLACED fill -
    # see web_server.py's _run_and_persist_cycle) - simulated here by a
    # fresh, never-restored account, matching db.load_latest_auto_trade_
    # account_state() returning None for an index that has never filled.
    account = SimulatedAccount()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()  # auto trading NOT enabled - the "blocked" condition
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])

    blocked = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )
    assert blocked.order is None
    assert blocked.risk.reason == "Auto trading is disabled"
    assert account.last_event_at is None  # never advanced - nothing to restore

    # "Restart": a fresh account, restoring nothing (no prior snapshot).
    post_restart_account = SimulatedAccount()
    post_restart_manager = OrderManager(post_restart_account)
    risk_manager.enable_auto_trading()  # the gate that was blocking it reopens

    retried = run_cycle(
        "nifty-50", "5m", risk_manager, post_restart_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )

    assert retried.event.kind == "ENTRY_CALL"
    assert retried.order.status == "PLACED"


# ---------- 7. Corrupted/missing persistence state ----------


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        {},  # missing every key
        {"cash": "not-a-number", "quantity": 1, "average_price": 100.0, "side": "CALL", "last_event_at": None},
        {"cash": 100.0, "quantity": "not-an-int", "average_price": 100.0, "side": "CALL", "last_event_at": None},
        {"cash": 100.0, "quantity": 1, "average_price": 100.0, "side": "BOGUS", "last_event_at": None},
    ],
)
def test_corrupted_persistence_state_fails_safe_and_leaves_account_flat(bad_snapshot) -> None:
    account = SimulatedAccount()  # already at safe, flat defaults

    restore_account_state(account, bad_snapshot)  # must not raise

    assert account.quantity == 0
    assert account.side is None
    assert account.cash == pytest.approx(1_000_000.0)


def test_missing_persistence_state_is_simply_not_restored_from() -> None:
    # db.load_latest_auto_trade_account_state() returning None means the
    # caller (web_server.py) never calls restore_account_state at all -
    # there is nothing to "fail safely" from, the account just stays at
    # its fresh, flat construction defaults.
    account = SimulatedAccount()
    assert account.quantity == 0
    assert account.cash == pytest.approx(1_000_000.0)


def test_naive_timestamp_from_sql_server_is_localized_to_ist_not_rejected() -> None:
    # SQL Server's DATETIME2 carries no UTC offset - pyodbc can hand back a
    # naive datetime on read. Must not crash comparisons in run_cycle()
    # (which always compares against tz-aware pd.Timestamps), and must not
    # be treated as corrupted data either.
    naive = datetime(2026, 9, 23, 10, 35)  # noqa: DTZ001 - deliberately naive, simulating SQL Server's DATETIME2
    snapshot = {"cash": 998_680.0, "quantity": 1, "average_price": 132.0, "side": "CALL", "last_event_at": naive}
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.last_event_at.tzinfo is not None
    assert account.quantity == 1


# ---------- 8. Duplicate events after restart ----------


def test_duplicate_event_after_restart_is_still_refused_with_max_positions_above_one(monkeypatch) -> None:
    # The exact Phase 2 regression, replayed post-restart: the accidental
    # protection from max_open_positions=1 must not be the only thing
    # standing between a restored account and a silent re-fill.
    entry_timestamp = UPTREND_60.index[16]
    snapshot = {
        "cash": 999_868.0, "quantity": 1, "average_price": 132.0, "side": "CALL",
        "last_event_at": entry_timestamp,
    }
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)

    from algoedge.risk_manager import RiskLimits
    risk_manager = RiskManager(RiskLimits(max_open_positions=5))
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])

    for i in range(3):
        run_cycle(
            "nifty-50", "5m", risk_manager, order_manager, quantity=1,
            now=TRADING_HOURS_NOW + timedelta(minutes=5 * i), total_open_positions=0,
        )

    assert account.quantity == 1  # still exactly what was restored, never re-averaged


# ---------- 9. Multiple new events between polling cycles, resumed after
# a restart mid-sequence ----------


def test_multiple_new_events_after_restart_are_processed_one_per_cycle_in_order(monkeypatch) -> None:
    # Restart happens with NOTHING restored yet (index never traded
    # before) - the very first post-restart window already contains both
    # the entry and its exit. Must process the entry first, not jump
    # straight to the exit.
    account = SimulatedAccount()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    patch_fetch(monkeypatch, UPTREND_60)  # full window: entry (bar 16) + exit (bar 24), both new
    first = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )
    assert first.event.kind == "ENTRY_CALL"
    assert account.quantity == 1

    second = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )
    assert second.event.kind == "EXIT_TARGET"
    assert account.quantity == 0
