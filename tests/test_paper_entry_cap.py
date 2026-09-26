"""Phase 7 B9: paper max_trades_per_day counts successful NEW ENTRY fills.

For paper Auto Trade, max_trades_per_day (10) is the maximum number of
successful new entries per IST day, across all indices - exits, square-offs,
missed-square-off recoveries, failed fills and blocked/expired decisions never
use it up. The count is RiskState.entries_today (record_entry()); the older
trades_today keeps counting every fill, and is still what the live
fno_signals CLI's cap counts (it never passes cap_new_entries=True).
"""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from algoedge import auto_trader, db, web_server
from algoedge.models import Base
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, OrderResult, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager, restore_risk_state
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

REPO = Path(__file__).resolve().parents[1]
CAP_REASON = "Max trades per day reached"
FIVE = timedelta(minutes=5)


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


_FULL = rising(40)
ENTRY = next(e for e in run_strategy(_FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
             if e.kind == "ENTRY_CALL")
ENTRY_WINDOW = _FULL.loc[:ENTRY.timestamp]  # ends on the completed entry candle
AFTER_CLOSE = (ENTRY.timestamp + FIVE + timedelta(seconds=30)).to_pydatetime()
TODAY = AFTER_CLOSE.date().isoformat()
FLAT = pd.DataFrame({c: [110.0] * 20 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 20},
                    index=pd.date_range("2026-09-23 14:00", periods=20, freq="5min", tz="Asia/Kolkata"))


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


def enabled(entries_today: int = 0, trades_today: int = 0) -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.state.trade_day = TODAY
    manager.state.entries_today, manager.state.trades_today = entries_today, trades_today
    return manager


def cycle(monkeypatch, window, risk_manager, account=None, now=AFTER_CLOSE, index_id="nifty-50", order_manager=None,
          total_open_positions=None):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: window)
    order_manager = order_manager or OrderManager(account or SimulatedAccount())
    return auto_trader.run_cycle(index_id, "5m", risk_manager, order_manager, now=now, resolve_contract_fn=contract,
                                 total_open_positions=total_open_positions)


def holding_call(**overrides) -> SimulatedAccount:
    return SimulatedAccount(quantity=1, average_price=100.0, side="CALL", **overrides)


# ---------------- entries count; the 11th is blocked ----------------


def test_a_new_entry_fill_increments_the_paper_entry_counter(monkeypatch) -> None:
    risk_manager = enabled()
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager)
    assert result.event.kind == "ENTRY_CALL" and result.order.status == "PLACED"
    assert risk_manager.state.entries_today == 1
    assert risk_manager.state.trades_today == 1  # every fill, as before


def test_the_tenth_entry_is_allowed_and_the_eleventh_is_blocked(monkeypatch) -> None:
    risk_manager = enabled(entries_today=9, trades_today=18)  # 9 round trips already today
    tenth = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, index_id="bank-nifty")
    assert tenth.order.status == "PLACED" and risk_manager.state.entries_today == 10
    # Isolate the cap from max_open_positions: this index has no open position.
    eleventh = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, index_id="sensex", total_open_positions=0)
    assert eleventh.order is None and eleventh.risk.reason == CAP_REASON
    assert risk_manager.state.entries_today == 10


def test_ten_entries_across_indices_share_one_cap() -> None:
    risk_manager = enabled()
    indices = ["nifty-50", "sensex", "bank-nifty"] * 4
    now = datetime(2026, 9, 23, 10, 0, tzinfo=IST)
    allowed = []
    for i, _index_id in enumerate(indices[:11]):  # one shared RiskManager, as the dashboard has
        decision = risk_manager.check("BUY", 1, 0, now=now + timedelta(minutes=10 * i), order_value=24_000.0,
                                      cap_new_entries=True)
        allowed.append(decision.allowed)
        if decision.allowed:
            risk_manager.record_trade(0.0, now=now + timedelta(minutes=10 * i))
            risk_manager.record_entry(now=now + timedelta(minutes=10 * i))
            risk_manager.record_trade(1.0, now=now + timedelta(minutes=10 * i + 5), is_exit=True)
    assert allowed == [True] * 10 + [False]
    assert (risk_manager.state.entries_today, risk_manager.state.trades_today) == (10, 20)


# ---------------- nothing else counts ----------------


def test_a_strategy_exit_does_not_count(monkeypatch) -> None:
    risk_manager = enabled(entries_today=3)
    account = holding_call(stop_loss=ENTRY.underlying_price - 500.0, target=ENTRY.underlying_price - 1.0,
                           last_event_at=ENTRY.timestamp - FIVE)
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, account)
    assert result.event.kind == "EXIT_TARGET" and result.order.status == "PLACED"
    assert risk_manager.state.entries_today == 3 and risk_manager.state.trades_today == 1


@pytest.mark.parametrize("configure", [
    lambda rm: rm.trip_kill_switch("ops"),  # B2: exit while kill-switched
    lambda rm: rm.disable_auto_trading(),  # B2: exit while disabled
    lambda rm: setattr(rm.state, "consecutive_loss_halt", True),  # B3: exit while halted
])
def test_risk_reducing_exits_do_not_count(monkeypatch, configure) -> None:
    risk_manager = enabled(entries_today=2)
    configure(risk_manager)
    account = holding_call(stop_loss=ENTRY.underlying_price - 500.0, target=ENTRY.underlying_price - 1.0,
                           last_event_at=ENTRY.timestamp - FIVE)
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, account)
    assert result.order.status == "PLACED" and account.quantity == 0
    assert risk_manager.state.entries_today == 2


def test_square_off_does_not_count(monkeypatch) -> None:
    risk_manager = enabled(entries_today=4)
    account = holding_call(last_event_at=FLAT.index[3])
    result = cycle(monkeypatch, FLAT, risk_manager, account, now=datetime(2026, 9, 23, 15, 21, tzinfo=IST))
    assert result.event.kind == "SQUARE_OFF" and account.quantity == 0
    assert risk_manager.state.entries_today == 4


def test_missed_square_off_recovery_does_not_count(monkeypatch) -> None:
    yesterday = pd.DataFrame({c: [100.0] * 18 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 18},
                             index=pd.date_range("2026-09-22 14:00", periods=18, freq="5min", tz="Asia/Kolkata"))
    today = pd.DataFrame({c: [105.0] * 2 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 2},
                         index=pd.date_range("2026-09-23 09:15", periods=2, freq="5min", tz="Asia/Kolkata"))
    risk_manager = enabled(entries_today=0)
    account = holding_call(last_event_at=yesterday.index[6])
    result = cycle(monkeypatch, pd.concat([yesterday, today]), risk_manager, account,
                   now=datetime(2026, 9, 23, 9, 26, tzinfo=IST))
    assert result.risk.reason.startswith("Missed square-off") and account.quantity == 0
    assert risk_manager.state.entries_today == 0


def test_a_failed_entry_does_not_count(monkeypatch) -> None:
    risk_manager = enabled()
    order_manager = OrderManager()
    order_manager.place_event = lambda *a, **k: OrderResult("FAILED", "fill rejected")
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, order_manager=order_manager)
    assert result.order.status == "FAILED"
    assert (risk_manager.state.entries_today, risk_manager.state.trades_today) == (0, 0)


def test_blocked_and_expired_decisions_do_not_count(monkeypatch) -> None:
    blocked = enabled()
    blocked.trip_kill_switch("ops")
    assert "kill switch" in cycle(monkeypatch, ENTRY_WINDOW, blocked).risk.reason
    expired = enabled()
    stale = (ENTRY.timestamp + FIVE + timedelta(minutes=11)).to_pydatetime()
    assert cycle(monkeypatch, ENTRY_WINDOW, expired, now=stale).risk.reason.startswith("Stale signal expired")
    assert blocked.state.entries_today == expired.state.entries_today == 0


def test_exits_remain_allowed_after_the_entry_cap_is_reached(monkeypatch) -> None:
    risk_manager = enabled(entries_today=10)
    assert risk_manager.check("BUY", 1, 0, now=AFTER_CLOSE, order_value=24_000.0,
                              cap_new_entries=True).reason == CAP_REASON
    account = holding_call(stop_loss=ENTRY.underlying_price - 500.0, target=ENTRY.underlying_price - 1.0,
                           last_event_at=ENTRY.timestamp - FIVE)
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, account)
    assert result.event.kind == "EXIT_TARGET" and result.order.status == "PLACED"
    square = holding_call(last_event_at=FLAT.index[3])
    assert cycle(monkeypatch, FLAT, risk_manager, square,
                 now=datetime(2026, 9, 23, 15, 21, tzinfo=IST)).event.kind == "SQUARE_OFF"
    assert risk_manager.state.entries_today == 10


# ---------------- IST day, halt reset, restart ----------------


def test_the_counter_resets_at_the_ist_day_boundary() -> None:
    risk_manager = enabled(entries_today=10)  # trade_day 2026-09-23
    risk_manager.check("SELL", 1, 1, now=datetime(2026, 9, 23, 23, 59, tzinfo=IST))
    assert risk_manager.state.entries_today == 10  # same IST day: kept
    # 00:05 IST on the 24th is still 18:35 UTC on the 23rd - the reset follows the IST date.
    just_after_midnight = datetime(2026, 9, 23, 18, 35, tzinfo=timezone.utc).astimezone(IST)
    risk_manager.check("SELL", 1, 1, now=just_after_midnight)
    assert (risk_manager.state.entries_today, risk_manager.state.trade_day) == (0, "2026-09-24")


def test_resetting_the_loss_halt_does_not_reset_the_entry_counter() -> None:
    risk_manager = enabled(entries_today=7)
    risk_manager.state.consecutive_loss_halt = True
    risk_manager.reset_consecutive_loss_halt()
    assert risk_manager.state.entries_today == 7


def snapshot(**overrides):
    return {"auto_trading_enabled": True, "kill_switch": False, "kill_switch_reason": None, "trades_today": 7,
            "entries_today": 4, "realized_pnl_today": 0.0, "trade_day": TODAY, "consecutive_losses": 0,
            "consecutive_loss_halt": False, "last_exit_at": None} | overrides


def test_restart_restores_the_saved_entry_count() -> None:
    manager = RiskManager()
    restore_risk_state(manager, snapshot())
    assert (manager.state.entries_today, manager.state.trades_today) == (4, 7)


@pytest.mark.parametrize("trades_today, expected", [(0, 0), (1, 1), (7, 4), (18, 9), (20, 10)])
def test_a_snapshot_from_before_the_counter_existed_derives_it_conservatively(trades_today, expected) -> None:
    for legacy in (snapshot(trades_today=trades_today, entries_today=None),
                   {k: v for k, v in snapshot(trades_today=trades_today).items() if k != "entries_today"}):
        manager = RiskManager()
        restore_risk_state(manager, legacy)
        assert manager.state.entries_today == expected  # ceil(fills / 2): entry/exit alternate in paper


def test_the_counter_survives_a_database_round_trip(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    saved = enabled(entries_today=6, trades_today=11)
    db.record_risk_snapshot(saved, event="TRADE_RECORDED")
    restored = RiskManager()
    monkeypatch.setattr(web_server, "risk_manager", restored)
    web_server._restore_paper_risk_state()
    assert (restored.state.entries_today, restored.state.trades_today) == (6, 11)
    assert restored.check("BUY", 1, 0, now=AFTER_CLOSE, order_value=24_000.0, cap_new_entries=True).allowed
    engine.dispose()


def test_the_new_columns_are_registered_as_migrations() -> None:
    assert db._PENDING_COLUMN_MIGRATIONS["risk_state_events"]["entries_today"] == "INT NULL"
    assert db._PENDING_COLUMN_MIGRATIONS["paper_decision_events"]["entries_today"] == "INT NULL"


# ---------------- B7 audit context and the dashboard API ----------------


def test_a_cap_blocked_entry_is_audited_with_the_entry_count(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    risk_manager = enabled(entries_today=10, trades_today=19)
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {"nifty-50": OrderManager()})
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: contract(event))

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return AFTER_CLOSE

    monkeypatch.setattr(auto_trader, "datetime", FixedNow)
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: ENTRY_WINDOW)
    response = web_server._run_and_persist_cycle("nifty-50", "5m", 1)
    assert response["risk"]["reason"] == CAP_REASON
    (row,) = db.list_paper_decision_events()
    assert (row["decision"], row["reason"]) == ("BLOCKED", CAP_REASON)
    assert (row["entriesToday"], row["tradesToday"]) == (10, 19)
    engine.dispose()


def test_the_status_api_states_what_the_cap_counts(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "risk_manager", enabled(entries_today=3, trades_today=5))
    status = web_server.auto_trading_status()
    assert (status["entriesToday"], status["tradesToday"]) == (3, 5)  # tradesToday kept for existing consumers
    assert status["limits"]["maxTradesPerDay"] == 10
    assert status["limits"]["maxTradesPerDayCounts"] == "NEW_ENTRY_FILLS"


# ---------------- the live CLI's semantics are unchanged ----------------


def test_without_the_paper_flag_the_cap_still_counts_every_fill() -> None:
    # What the live fno_signals CLI calls: check("BUY", lot_size, open_positions, now=now).
    by_fills = RiskManager(RiskLimits(max_quantity=500))
    by_fills.enable_auto_trading()
    by_fills.state.trade_day, by_fills.state.trades_today, by_fills.state.entries_today = TODAY, 10, 0
    assert by_fills.check("BUY", 75, 0, now=AFTER_CLOSE).reason == CAP_REASON
    by_fills.state.trades_today, by_fills.state.entries_today = 0, 10
    assert by_fills.check("BUY", 75, 0, now=AFTER_CLOSE).allowed is True


def test_the_live_cli_never_uses_the_paper_entry_semantics() -> None:
    source = (REPO / "src/fno_signals/main.py").read_text()
    assert "cap_new_entries" not in source and "record_entry" not in source
    assert re.search(r'risk_manager\.check\("BUY", index_config\.lot_size, open_positions, now=now\)', source)
