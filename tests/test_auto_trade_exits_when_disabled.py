"""Phase 7 B2 (audit H1): risk-reducing paper actions continue while NEW
entries are disabled.

With Auto Trading disabled or the kill switch engaged, an open paper
position must still reach its canonical SL/target exit and the forced 15:20
square-off, while entries stay blocked. These tests drive the dashboard's
real scheduler (web_server._scheduler._tick() -> _run_and_persist_cycle ->
auto_trader.run_cycle), not only run_cycle() directly; only the candle
fetch and the clock are replaced.
"""

import asyncio
from datetime import date, datetime

import pandas as pd
import pytest

from algoedge import auto_trader, web_server
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from algoedge.scheduler import AutoTradingScheduler

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)


def bars(n: int, start_price: float, step: float, start: str) -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [start_price + step * i for i in range(n)]
    return pd.DataFrame({
        "Open": closes, "High": [c + abs(step) / 2 + 1 for c in closes],
        "Low": [c - abs(step) / 2 - 1 for c in closes], "Close": closes, "Volume": [0.0] * n,
    }, index=index)


# A held CALL (entry 100, SL 90, target 130) whose target is touched by the
# 10:25 bar (High 130) - the canonical exit fills at the target level.
TARGET_WINDOW = bars(15, 100.0, 2.0, "2026-09-23 09:15")
# A held PUT (entry 100, SL 110, target 70) whose stop is touched by the
# 09:35 bar (High 110).
STOP_WINDOW = bars(5, 100.0, 2.0, "2026-09-23 09:15")
# Constant price through the close: no strategy event, only square-off can act.
FLAT_WINDOW = bars(20, 110.0, 0.0, "2026-09-23 14:50")
# A genuine new ENTRY_CALL setup (same fixture as test_auto_trade_square_off's
# stray-entry test): rising prices produce an entry at 15:00.
ENTRY_WINDOW = bars(60, 100.0, 6.0, "2026-09-23 13:50")


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=IST)


@pytest.fixture()
def dashboard(monkeypatch):
    """The dashboard's scheduler wired to a fresh RiskManager and fresh paper
    accounts; returns a function that runs one scheduler tick at `now` on
    `window`, and records which indices fetched candles."""
    risk_manager = RiskManager()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})
    fetched: list[str] = []

    def tick(window: pd.DataFrame, now: datetime) -> None:
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(auto_trader, "datetime", FixedNow)

        def fetch(ticker, **_kwargs):
            fetched.append(ticker)
            return window

        monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
        asyncio.run(web_server._scheduler._tick())

    return risk_manager, accounts, tick, fetched


def hold_call(account: SimulatedAccount) -> None:
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.stop_loss, account.target = 90.0, 130.0
    account.last_event_at = TARGET_WINDOW.index[0]  # the entry bar


def hold_put(account: SimulatedAccount) -> None:
    account.quantity, account.average_price, account.side = 1, 100.0, "PUT"
    account.stop_loss, account.target = 110.0, 70.0
    account.last_event_at = STOP_WINDOW.index[0]


# ---------------- 1-2: SL/TP exits still processed ----------------


def test_disabled_open_position_still_exits_at_its_target_via_the_scheduler(dashboard) -> None:
    risk_manager, accounts, tick, fetched = dashboard
    hold_call(accounts["nifty-50"])
    assert risk_manager.state.auto_trading_enabled is False

    tick(TARGET_WINDOW, at(10, 32))

    account = accounts["nifty-50"]
    assert (account.quantity, account.side) == (0, None)
    assert risk_manager.state.realized_pnl_today == pytest.approx(30.0)  # filled at the 130 target
    assert len(fetched) == 1  # only the index holding a position was cycled


def test_kill_switch_open_position_still_exits_at_its_stop_via_the_scheduler(dashboard) -> None:
    risk_manager, accounts, tick, fetched = dashboard
    risk_manager.enable_auto_trading()
    risk_manager.trip_kill_switch("test")
    hold_put(accounts["sensex"])

    tick(STOP_WINDOW, at(9, 42))

    account = accounts["sensex"]
    assert (account.quantity, account.side) == (0, None)
    assert risk_manager.state.realized_pnl_today == pytest.approx(-10.0)  # PUT stopped at 110
    assert risk_manager.state.kill_switch is True  # the switch itself is untouched
    assert len(fetched) == 1


# ---------------- 3-4: 15:20 square-off still occurs ----------------


def test_disabled_open_position_is_squared_off_at_15_20_via_the_scheduler(dashboard) -> None:
    risk_manager, accounts, tick, _fetched = dashboard
    hold_call(accounts["nifty-50"])

    tick(FLAT_WINDOW, at(15, 20))

    account = accounts["nifty-50"]
    assert account.quantity == 0
    assert account.square_off_date == "2026-09-23"
    assert risk_manager.state.realized_pnl_today == pytest.approx(10.0)  # closed at the latest close, 110


def test_kill_switch_open_position_is_squared_off_at_15_20_via_the_scheduler(dashboard) -> None:
    risk_manager, accounts, tick, _fetched = dashboard
    risk_manager.enable_auto_trading()
    risk_manager.trip_kill_switch("test")
    hold_put(accounts["bank-nifty"])

    tick(FLAT_WINDOW, at(15, 25))

    account = accounts["bank-nifty"]
    assert account.quantity == 0
    assert account.square_off_date == "2026-09-23"
    assert risk_manager.state.realized_pnl_today == pytest.approx(-10.0)


# ---------------- 5: no new entry while disabled / kill-switched ----------------


@pytest.mark.parametrize("kill_switch", [False, True])
def test_disabled_or_kill_switch_with_no_position_cycles_nothing(dashboard, kill_switch) -> None:
    risk_manager, accounts, tick, fetched = dashboard
    if kill_switch:
        risk_manager.enable_auto_trading()
        risk_manager.trip_kill_switch("test")

    tick(ENTRY_WINDOW, at(15, 5))  # a genuine entry setup exists in the data

    assert fetched == []
    assert all(a.quantity == 0 for a in accounts.values())
    assert risk_manager.state.trades_today == 0


def test_only_indices_holding_a_position_are_cycled_while_disabled(dashboard) -> None:
    risk_manager, accounts, tick, fetched = dashboard
    hold_call(accounts["nifty-50"])

    tick(ENTRY_WINDOW, at(15, 5))

    assert fetched == [web_server.INDEX_MAP[web_server.DASHBOARD_INDEX_IDS["nifty-50"]].ticker]
    assert accounts["sensex"].quantity == accounts["bank-nifty"].quantity == 0


def test_run_cycle_still_blocks_a_new_entry_while_disabled_or_kill_switched(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: ENTRY_WINDOW)
    for configure, reason in [
        (lambda rm: None, "Auto trading is disabled"),
        (lambda rm: (rm.enable_auto_trading(), rm.trip_kill_switch("test")), "kill switch"),
    ]:
        risk_manager = RiskManager()
        configure(risk_manager)
        order_manager = OrderManager()
        result = auto_trader.run_cycle(
            "nifty-50", "5m", risk_manager, order_manager, now=at(15, 5), resolve_contract_fn=_contract,
        )
        assert result.event.kind == "ENTRY_CALL"
        assert result.order is None and reason.lower() in result.risk.reason.lower()
        assert order_manager.account.quantity == 0


def _contract(event):
    from algoedge.option_contract import OptionContract

    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


# ---------------- 6: existing gating unchanged ----------------


def test_risk_reducing_flag_never_opens_a_buy() -> None:
    killed = RiskManager()
    killed.enable_auto_trading()
    killed.trip_kill_switch("test")
    assert "kill switch" in killed.check("BUY", 1, 0, now=at(10, 0), risk_reducing=True).reason
    disabled = RiskManager()
    assert disabled.check("BUY", 1, 0, now=at(10, 0), risk_reducing=True).reason == "Auto trading is disabled"


def test_default_check_is_unchanged_for_other_callers() -> None:
    # Without the flag (the live CLI and every other caller) a SELL is still
    # blocked by the kill switch and by disabled auto trading, as before.
    killed = RiskManager()
    killed.enable_auto_trading()
    killed.trip_kill_switch("test")
    assert killed.check("SELL", 1, 1, now=at(10, 0)).allowed is False
    assert RiskManager().check("SELL", 1, 1, now=at(10, 0)).reason == "Auto trading is disabled"


def test_risk_reducing_exit_still_respects_the_other_gates() -> None:
    manager = RiskManager()
    assert manager.check("SELL", 1, 0, now=at(10, 0), risk_reducing=True).reason == "No open position to exit"
    assert manager.check("SELL", 1, 1, now=at(16, 0), risk_reducing=True).reason == (
        "Outside configured trading hours")
    assert manager.check("SELL", 1, 1, now=at(10, 0), risk_reducing=True).allowed is True


def test_enabled_scheduler_still_cycles_every_index() -> None:
    calls: list[str] = []
    scheduler = AutoTradingScheduler(index_ids=INDEX_IDS, run_one=calls.append, is_enabled=lambda: True,
                                     has_open_position=lambda _i: False)
    asyncio.run(scheduler._tick())
    assert calls == INDEX_IDS


def test_scheduler_without_a_position_hook_still_skips_everything_when_disabled() -> None:
    calls: list[str] = []
    scheduler = AutoTradingScheduler(index_ids=INDEX_IDS, run_one=calls.append, is_enabled=lambda: False)
    asyncio.run(scheduler._tick())
    assert calls == []


def test_a_failing_position_hook_does_not_stop_other_indices() -> None:
    calls: list[str] = []

    def has_open_position(index_id: str) -> bool:
        if index_id == INDEX_IDS[0]:
            raise RuntimeError("boom")
        return True

    scheduler = AutoTradingScheduler(index_ids=INDEX_IDS, run_one=calls.append, is_enabled=lambda: False,
                                     has_open_position=has_open_position)
    asyncio.run(scheduler._tick())
    assert calls == INDEX_IDS[1:]
