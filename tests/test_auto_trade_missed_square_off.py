"""Phase 7 B4: recovery of a missed 15:20 paper square-off.

Paper never holds overnight. A position still open whose entry bar is from
an earlier IST date than today missed that day's 15:20-15:30 square-off
(scheduler/data failures, restart); it is closed - before any strategy event,
so no new entry comes first - on the first in-session cycle whose market
data already has a bar from today, at today's latest close. A position
opened today is never treated as stale. Inside today's window a failed
attempt is simply retried by the next cycle.
"""

import asyncio
from datetime import date, datetime

import pandas as pd
import pytest

from algoedge import auto_trader, web_server
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)
YESTERDAY, TODAY = "2026-09-22", "2026-09-23"


def flat(start: str, n: int, price: float) -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({c: [price] * n for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * n},
                        index=index)


def rising(start: str, n: int, price: float, step: float) -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [price + step * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + step / 2 + 1 for c in closes],
                         "Low": [c - step / 2 - 1 for c in closes], "Close": closes, "Volume": [0.0] * n},
                        index=index)


YESTERDAY_BARS = flat(f"{YESTERDAY} 14:00", 18, 100.0)  # 14:00 .. 15:25
TWO_DAYS = pd.concat([YESTERDAY_BARS, flat(f"{TODAY} 09:15", 2, 105.0)])  # + today's 09:15, 09:20
TODAY_AFTERNOON = flat(f"{TODAY} 14:00", 18, 110.0)  # 14:00 .. 15:25


def at(day: str, hour: int, minute: int) -> datetime:
    return datetime.fromisoformat(f"{day} {hour:02d}:{minute:02d}").replace(tzinfo=IST)


def enabled() -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    return manager


def holding_since(entry_bar, side: str = "CALL") -> SimulatedAccount:
    return SimulatedAccount(quantity=1, average_price=100.0, side=side, last_event_at=entry_bar,
                            stop_loss=50.0 if side == "CALL" else 150.0, target=150.0 if side == "CALL" else 50.0)


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


def cycle(monkeypatch, window, risk_manager, account, now):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: window)
    return auto_trader.run_cycle("nifty-50", "5m", risk_manager, OrderManager(account), now=now,
                                 resolve_contract_fn=contract)


@pytest.fixture()
def dashboard(monkeypatch):
    """The dashboard's real scheduler on fresh state; tick(fetch, now)."""
    risk_manager = RiskManager()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})

    def tick(fetch, now: datetime) -> None:
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(auto_trader, "datetime", FixedNow)
        monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
        asyncio.run(web_server._scheduler._tick())

    return risk_manager, accounts, tick


# ---------------- 1-2: today's window ----------------


def test_normal_15_20_square_off_is_unchanged(monkeypatch) -> None:
    account = holding_since(TODAY_AFTERNOON.index[6])  # opened today 14:30
    result = cycle(monkeypatch, TODAY_AFTERNOON, enabled(), account, at(TODAY, 15, 20))
    assert result.event.kind == "SQUARE_OFF" and result.risk.reason == "Forced end-of-day square-off"
    assert account.quantity == 0 and account.square_off_date == TODAY
    assert result.order.fill_price == 110.0


def test_a_failed_square_off_attempt_is_retried_later_in_the_window(dashboard) -> None:
    risk_manager, accounts, tick = dashboard
    risk_manager.enable_auto_trading()
    account = accounts["nifty-50"]
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.last_event_at = TODAY_AFTERNOON.index[6]

    def failing(*_a, **_k):
        raise RuntimeError("No data returned for ^NSEI")

    tick(failing, at(TODAY, 15, 21))  # the scheduler logs the failure and carries on
    assert account.quantity == 1 and account.square_off_date is None  # still eligible

    tick(lambda *_a, **_k: TODAY_AFTERNOON, at(TODAY, 15, 26))
    assert account.quantity == 0 and account.square_off_date == TODAY
    assert risk_manager.state.realized_pnl_today == pytest.approx(10.0)


# ---------------- 3: whole window missed -> next session closes it ----------------


def test_a_position_from_a_missed_window_is_closed_on_the_next_sessions_first_cycle(monkeypatch) -> None:
    account = holding_since(YESTERDAY_BARS.index[6])  # opened yesterday 14:30, never squared off
    risk_manager = enabled()
    result = cycle(monkeypatch, TWO_DAYS, risk_manager, account, at(TODAY, 9, 26))
    assert result.event.kind == "SQUARE_OFF"
    assert result.risk.allowed and result.risk.reason == (
        f"Missed square-off from {YESTERDAY} - closed at today's latest price")
    assert result.order.fill_price == 105.0  # today's latest close, not yesterday's
    assert account.quantity == 0 and account.side is None
    assert account.last_event_at == TWO_DAYS.index[-1]
    assert account.square_off_date is None  # not "squared off today": today's trading stays open
    assert risk_manager.state.realized_pnl_today == pytest.approx(5.0)


@pytest.mark.parametrize("window, now", [
    (YESTERDAY_BARS, at(TODAY, 9, 26)),  # in session, but no bar from today yet
    (YESTERDAY_BARS, at(TODAY, 9, 5)),  # before the session opens
    (TWO_DAYS, at(TODAY, 15, 45)),  # after the session closes
])
def test_a_stale_position_is_never_closed_at_a_stale_price_or_out_of_session(monkeypatch, window, now) -> None:
    account = holding_since(YESTERDAY_BARS.index[6])
    risk_manager = enabled()
    result = cycle(monkeypatch, window, risk_manager, account, now)
    assert result.order is None and result.event is None
    assert result.risk.reason.startswith(f"Open position from {YESTERDAY} missed its square-off")
    assert account.quantity == 1 and risk_manager.state.trades_today == 0


def test_a_whole_missed_window_then_the_next_morning_closes_it_via_the_scheduler(dashboard) -> None:
    risk_manager, accounts, tick = dashboard
    risk_manager.enable_auto_trading()
    account = accounts["nifty-50"]
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.last_event_at = YESTERDAY_BARS.index[6]

    def failing(*_a, **_k):
        raise RuntimeError("yfinance down")

    for minute in (21, 26):  # both ticks of yesterday's window fail
        tick(failing, at(YESTERDAY, 15, minute))
    assert account.quantity == 1

    tick(lambda *_a, **_k: TWO_DAYS, at(TODAY, 9, 26))
    assert account.quantity == 0
    assert risk_manager.state.realized_pnl_today == pytest.approx(5.0)


# ---------------- 4: a position opened today is not stale ----------------


def test_a_position_opened_today_is_not_mistaken_for_a_missed_one(monkeypatch) -> None:
    today_morning = pd.concat([YESTERDAY_BARS, flat(f"{TODAY} 09:15", 10, 105.0)])  # .. 10:00 today
    account = holding_since(today_morning.index[-8])  # opened today 09:25
    result = cycle(monkeypatch, today_morning, enabled(), account, at(TODAY, 10, 3))
    assert result.event is None or result.event.kind != "SQUARE_OFF"
    assert account.quantity == 1 and account.side == "CALL"


def test_stale_day_detection_uses_the_ist_entry_date() -> None:
    now = at(TODAY, 9, 30)
    assert auto_trader._stale_position_day(holding_since(YESTERDAY_BARS.index[0]), now) == date(2026, 9, 22)
    assert auto_trader._stale_position_day(holding_since(pd.Timestamp(f"{TODAY} 09:15", tz=IST)), now) is None
    # 23:59 UTC on the 22nd is 05:29 IST on the 23rd - today in IST, not stale.
    assert auto_trader._stale_position_day(holding_since(pd.Timestamp("2026-09-22 23:59", tz="UTC")), now) is None
    assert auto_trader._stale_position_day(SimulatedAccount(), now) is None  # flat
    unknown = SimulatedAccount(quantity=1, average_price=100.0, side="CALL", last_event_at=None)
    assert auto_trader._stale_position_day(unknown, now) is None  # unknown date: never closed on a guess


# ---------------- 5-6: compatible with B2 (disabled / kill switch) and B3 (halt) ----------------


@pytest.mark.parametrize("kill_switch", [False, True])
def test_a_stale_position_is_recovered_while_disabled_or_kill_switched(dashboard, kill_switch) -> None:
    risk_manager, accounts, tick = dashboard
    if kill_switch:
        risk_manager.enable_auto_trading()
        risk_manager.trip_kill_switch("test")
    account = accounts["sensex"]
    account.quantity, account.average_price, account.side = 1, 100.0, "PUT"
    account.last_event_at = YESTERDAY_BARS.index[6]

    tick(lambda *_a, **_k: TWO_DAYS, at(TODAY, 9, 26))

    assert account.quantity == 0
    assert risk_manager.state.realized_pnl_today == pytest.approx(-5.0)  # PUT closed at 105
    assert risk_manager.state.kill_switch is kill_switch
    assert risk_manager.state.auto_trading_enabled is kill_switch  # switches untouched


def test_a_stale_position_is_recovered_while_halted_and_the_halt_stays(monkeypatch) -> None:
    risk_manager = enabled()
    risk_manager.state.consecutive_losses, risk_manager.state.consecutive_loss_halt = 3, True
    account = holding_since(YESTERDAY_BARS.index[6], side="PUT")
    result = cycle(monkeypatch, TWO_DAYS, risk_manager, account, at(TODAY, 9, 26))
    assert result.event.kind == "SQUARE_OFF" and account.quantity == 0
    assert risk_manager.state.consecutive_loss_halt is True


# ---------------- 7: no new entry before the stale position is resolved ----------------


def test_no_new_entry_is_taken_before_a_stale_position_is_closed(monkeypatch) -> None:
    window = pd.concat([YESTERDAY_BARS, rising(f"{TODAY} 09:15", 40, 100.0, 6.0)])
    config = strategy_config_for(INDEX_MAP[1])
    entry = next(e for e in run_strategy(window, config, underlying_label="NIFTY 50")[1]
                 if e.kind.startswith("ENTRY") and str(e.timestamp.date()) == TODAY)
    now = (entry.timestamp + pd.Timedelta(minutes=7)).to_pydatetime()
    view = window.loc[:entry.timestamp]

    control = SimulatedAccount()  # a flat account DOES take this entry now ...
    assert cycle(monkeypatch, view, enabled(), control, now).event.kind.startswith("ENTRY")
    assert control.quantity == 1

    stale = holding_since(YESTERDAY_BARS.index[6])  # ... but a stale position is closed first
    result = cycle(monkeypatch, view, enabled(), stale, now)
    assert result.event.kind == "SQUARE_OFF"
    assert stale.quantity == 0 and stale.side is None


def test_no_new_entry_while_waiting_for_todays_data(monkeypatch) -> None:
    account = holding_since(YESTERDAY_BARS.index[6])
    result = cycle(monkeypatch, YESTERDAY_BARS, enabled(), account, at(TODAY, 9, 16))
    assert result.order is None and account.quantity == 1 and account.side == "CALL"
