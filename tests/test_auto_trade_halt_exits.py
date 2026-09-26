"""Phase 7 B3 (audit H3): the consecutive-loss halt blocks NEW entries only.

The halt lives on the one RiskManager shared by every index, so a losing
streak on one index must not stop another index's open paper position from
reaching its canonical SL/target exit. The halt itself stays active until
its explicit reset; square-off, daily-loss and max-trades behavior is
unchanged, and so is every caller that does not pass risk_reducing=True
(the live fno_signals CLI only ever calls check("BUY", ...)).
"""

import asyncio
from datetime import date, datetime

import pandas as pd
import pytest

from algoedge import auto_trader, web_server
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)
HALTED_REASON = "Trading halted after"


def bars(n: int, start_price: float, step: float, start: str) -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [start_price + step * i for i in range(n)]
    return pd.DataFrame({
        "Open": closes, "High": [c + abs(step) / 2 + 1 for c in closes],
        "Low": [c - abs(step) / 2 - 1 for c in closes], "Close": closes, "Volume": [0.0] * n,
    }, index=index)


# Rising prices whose 09:35 bar reaches High 110: a CALL with target 110 exits
# there (+10); a PUT with stop 110 is stopped there (-10).
EXIT_WINDOW = bars(5, 100.0, 2.0, "2026-09-23 09:15")
FLAT_WINDOW = bars(20, 110.0, 0.0, "2026-09-23 14:50")
ENTRY_WINDOW = bars(60, 100.0, 6.0, "2026-09-23 13:50")  # a genuine ENTRY_CALL at 15:00


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=IST)


def halted() -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.state.consecutive_losses = 3
    manager.state.consecutive_loss_halt = True
    return manager


def holding(side: str) -> SimulatedAccount:
    stop_loss, target = (90.0, 110.0) if side == "CALL" else (110.0, 70.0)
    return SimulatedAccount(quantity=1, average_price=100.0, side=side, stop_loss=stop_loss, target=target,
                            last_event_at=EXIT_WINDOW.index[0])


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


def cycle(monkeypatch, window, risk_manager, account, now):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: window)
    return auto_trader.run_cycle("nifty-50", "5m", risk_manager, OrderManager(account), now=now,
                                 resolve_contract_fn=contract)


# ---------------- 1-2, 5: exits fill while halted; the halt stays ----------------


def test_halted_call_still_exits_at_its_target(monkeypatch) -> None:
    risk_manager, account = halted(), holding("CALL")
    result = cycle(monkeypatch, EXIT_WINDOW, risk_manager, account, at(9, 42))
    assert result.event.kind == "EXIT_TARGET"
    assert result.order.status == "PLACED" and result.order.fill_price == 110.0
    assert (account.quantity, account.side) == (0, None)
    assert risk_manager.state.consecutive_loss_halt is True  # 5: not reset by the exit


def test_halted_put_still_exits_at_its_stop(monkeypatch) -> None:
    risk_manager, account = halted(), holding("PUT")
    result = cycle(monkeypatch, EXIT_WINDOW, risk_manager, account, at(9, 42))
    assert result.event.kind == "EXIT_SL"
    assert result.order.status == "PLACED" and result.order.realized_pnl == pytest.approx(-10.0)
    assert account.quantity == 0
    assert risk_manager.state.consecutive_loss_halt is True
    assert risk_manager.state.consecutive_losses == 4  # the loss is still counted


# ---------------- 3: entries stay blocked ----------------


def test_halted_entry_signal_is_still_blocked(monkeypatch) -> None:
    risk_manager, account = halted(), SimulatedAccount()
    result = cycle(monkeypatch, ENTRY_WINDOW, risk_manager, account, at(15, 5))
    assert result.event.kind == "ENTRY_CALL"
    assert result.order is None and result.risk.reason.startswith(HALTED_REASON)
    assert account.quantity == 0


def test_a_risk_reducing_flag_never_opens_a_buy_while_halted() -> None:
    decision = halted().check("BUY", 1, 0, now=at(10, 0), order_value=24_000.0, risk_reducing=True)
    assert decision.allowed is False and decision.reason.startswith(HALTED_REASON)


# ---------------- 4: one index's halt does not freeze another index's exit ----------------


def test_index_a_reaching_the_halt_does_not_block_index_b_exit_via_the_scheduler(monkeypatch) -> None:
    a, b = INDEX_IDS[0], INDEX_IDS[1]  # the scheduler cycles A before B in the same tick
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.state.consecutive_losses = 2  # A's stop below is the 3rd loss in a row
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    accounts[a], accounts[b] = holding("PUT"), holding("CALL")
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(acc) for i, acc in accounts.items()})
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: EXIT_WINDOW)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(9, 42)

    monkeypatch.setattr(auto_trader, "datetime", FixedNow)

    asyncio.run(web_server._scheduler._tick())

    assert accounts[a].quantity == 0  # A was stopped out ...
    assert risk_manager.state.consecutive_loss_halt is True  # ... which tripped the halt
    assert accounts[b].quantity == 0  # B still reached its target in the same tick
    assert risk_manager.state.realized_pnl_today == pytest.approx(-10.0 + 10.0)
    assert risk_manager.state.consecutive_loss_halt is True  # B's win does not lift the halt


# ---------------- 6: square-off unchanged ----------------


def test_halted_position_is_still_squared_off_at_15_20(monkeypatch) -> None:
    risk_manager, account = halted(), holding("CALL")
    result = cycle(monkeypatch, FLAT_WINDOW, risk_manager, account, at(15, 20))
    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0 and account.square_off_date == "2026-09-23"
    assert risk_manager.state.consecutive_loss_halt is True


# ---------------- 7: daily-loss and max-trades entry blocking unchanged ----------------


@pytest.mark.parametrize("setup, reason", [
    (lambda s: setattr(s, "realized_pnl_today", -RiskLimits().daily_loss_limit), "Daily loss limit reached"),
    (lambda s: setattr(s, "trades_today", RiskLimits().max_trades_per_day), "Max trades per day reached"),
])
def test_daily_loss_and_max_trades_still_block_entries_and_never_exits(setup, reason) -> None:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.state.trade_day = "2026-09-23"
    setup(manager.state)
    assert manager.check("BUY", 1, 0, now=at(10, 0), order_value=24_000.0).reason == reason
    assert manager.check("BUY", 1, 0, now=at(10, 0), order_value=24_000.0, risk_reducing=True).reason == reason
    assert manager.check("SELL", 1, 1, now=at(10, 0)).allowed is True
    assert manager.check("SELL", 1, 1, now=at(10, 0), risk_reducing=True).allowed is True


# ---------------- 8: callers without the flag are unchanged ----------------


def test_a_halt_still_blocks_a_plain_sell_for_callers_without_the_flag() -> None:
    decision = halted().check("SELL", 1, 1, now=at(10, 0))
    assert decision.allowed is False and decision.reason.startswith(HALTED_REASON)


def test_a_halt_still_blocks_a_plain_buy_like_the_live_cli_sends() -> None:
    # fno_signals.main calls check("BUY", lot_size, open_positions, now=now) - no flag.
    decision = halted().check("BUY", 75, 0, now=at(10, 0))
    assert decision.allowed is False and decision.reason.startswith(HALTED_REASON)
