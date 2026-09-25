"""Phase 7 B1 regression tests: the paper RiskManager's persisted
last_exit_at comes back from the DB as a timezone-naive DateTime and must
be restored as Asia/Kolkata, so the cooldown comparison in check() against
the timezone-aware current time never raises TypeError after a restart."""

from datetime import datetime, timedelta

import pytest

from algoedge import db, web_server
from algoedge.risk_manager import IST, RiskManager, restore_risk_state

# The last exit before the restart, exactly as a naive DB DateTime returns it.
SAVED_EXIT = datetime(2026, 9, 25, 10, 0)


def snapshot(last_exit_at=SAVED_EXIT, **overrides):
    return {
        "auto_trading_enabled": True, "kill_switch": False, "kill_switch_reason": None,
        "trades_today": 2, "realized_pnl_today": -40.0, "trade_day": "2026-09-25",
        "consecutive_losses": 1, "consecutive_loss_halt": False, "last_exit_at": last_exit_at,
        **overrides,
    }


def restored(last_exit_at=SAVED_EXIT) -> RiskManager:
    manager = RiskManager()
    restore_risk_state(manager, snapshot(last_exit_at))
    return manager


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=IST)


def test_naive_saved_last_exit_is_restored_as_ist_aware() -> None:
    value = restored().state.last_exit_at
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(hours=5, minutes=30)
    assert value == datetime(2026, 9, 25, 10, 0, tzinfo=IST)  # same wall-clock, not shifted


def test_the_original_bug_raised_on_a_naive_last_exit() -> None:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.state.last_exit_at = SAVED_EXIT  # naive, as the old restore left it
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        manager.check("BUY", 1, 0, now=at(10, 30))


def test_entry_after_restart_is_blocked_inside_the_cooldown() -> None:
    decision = restored().check("BUY", 1, 0, now=at(10, 3))  # cooldown is 5 minutes
    assert decision.allowed is False
    assert decision.reason == "Cooldown active until 10:05:00"


def test_entry_after_restart_is_allowed_once_the_cooldown_has_passed() -> None:
    decision = restored().check("BUY", 1, 0, now=at(10, 5), order_value=24_000.0)
    assert decision.allowed is True


def test_exit_after_restart_is_unaffected_by_the_restored_cooldown() -> None:
    assert restored().check("SELL", 1, 1, now=at(10, 1)).allowed is True


def test_aware_saved_last_exit_is_kept_unchanged() -> None:
    aware = datetime(2026, 9, 25, 4, 30, tzinfo=IST)
    assert restored(aware).state.last_exit_at is aware


def test_no_saved_last_exit_stays_none_and_entry_is_allowed() -> None:
    manager = restored(last_exit_at=None)
    assert manager.state.last_exit_at is None
    assert manager.check("BUY", 1, 0, now=at(10, 1), order_value=24_000.0).allowed is True


def test_every_other_field_is_restored_as_before() -> None:
    manager = RiskManager()
    restore_risk_state(manager, snapshot(kill_switch=True, kill_switch_reason="left on", consecutive_loss_halt=True))
    state = manager.state
    assert (state.auto_trading_enabled, state.kill_switch, state.kill_switch_reason) == (True, True, "left on")
    assert (state.trades_today, state.realized_pnl_today, state.trade_day) == (2, -40.0, "2026-09-25")
    assert (state.consecutive_losses, state.consecutive_loss_halt) == (1, True)


def test_dashboard_startup_restore_uses_ist_for_a_saved_last_exit(monkeypatch) -> None:
    manager = RiskManager()
    monkeypatch.setattr(web_server, "risk_manager", manager)
    monkeypatch.setattr(db, "load_latest_risk_state", lambda *, scope: snapshot() if scope == "paper" else None)
    web_server._restore_paper_risk_state()
    assert manager.state.last_exit_at == datetime(2026, 9, 25, 10, 0, tzinfo=IST)
    assert manager.check("BUY", 1, 0, now=at(10, 3)).reason == "Cooldown active until 10:05:00"


def test_dashboard_startup_restore_without_a_snapshot_leaves_a_fresh_state(monkeypatch) -> None:
    manager = RiskManager()
    monkeypatch.setattr(web_server, "risk_manager", manager)
    monkeypatch.setattr(db, "load_latest_risk_state", lambda *, scope: None)
    web_server._restore_paper_risk_state()
    assert manager.state.last_exit_at is None and manager.state.auto_trading_enabled is False
