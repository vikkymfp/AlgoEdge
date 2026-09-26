"""Phase 7 B8: paper entries use completed 5-minute candles.

yfinance intraday history includes the still-forming candle as its last row
during market hours. A flat paper account now evaluates the canonical
strategy WITHOUT that candle, so a new entry comes from a completed candle's
final OHLC and fills at its close - the Backtest/Pine bar-close model.
Exits of an open position, the 15:20 square-off and B4 recovery keep using
the full window and the latest price.
"""

import asyncio
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader, web_server
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

CONFIG = strategy_config_for(INDEX_MAP[1])
FIVE = timedelta(minutes=5)


def rising(n: int, start: str = "2026-09-23 09:15") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


FINAL = rising(40)  # every bar completed, final OHLC


def canonical_entries(df: pd.DataFrame):
    return [e for e in run_strategy(df, CONFIG, underlying_label="NIFTY 50")[1] if e.kind.startswith("ENTRY")]


ENTRY = canonical_entries(FINAL)[0]  # the canonical (completed-bar) entry
E = ENTRY.timestamp  # its bar start; the bar closes at E + 5 min


def forming(bar: pd.Series, ts) -> pd.DataFrame:
    """The same candle mid-formation: a partial close 0.5 points short of the
    final one, and the range so far. On this fixture the entry candle's
    partial OHLC still shows the entry signal, at a different price and
    SL/TP than the completed candle - exactly what B8 must not act on."""
    close = bar["Close"] - 0.5
    return pd.DataFrame({"Open": [bar["Open"]], "High": [close + 1.0], "Low": [min(bar["Low"], close - 1.0)],
                         "Close": [close], "Volume": [0.0]}, index=pd.DatetimeIndex([ts]))


def window_at(now: datetime) -> pd.DataFrame:
    """What yfinance returns at `now`: every bar that has closed, plus the
    still-forming one."""
    completed = FINAL.loc[FINAL.index + FIVE <= now]
    in_progress = FINAL.loc[(FINAL.index <= now) & (FINAL.index + FIVE > now)]
    if in_progress.empty:
        return completed
    return pd.concat([completed, forming(in_progress.iloc[0], in_progress.index[0])])


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


def enabled() -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    return manager


def cycle(monkeypatch, window, risk_manager, account, now):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: window)
    return auto_trader.run_cycle("nifty-50", "5m", risk_manager, OrderManager(account), now=now,
                                 resolve_contract_fn=contract)


def at(ts, minutes: float) -> datetime:
    return (ts + timedelta(minutes=minutes)).to_pydatetime()


# ---------------- forming vs completed ----------------


def test_a_signal_only_on_the_forming_candle_creates_no_entry(monkeypatch) -> None:
    now = at(E, 2.5)  # 2.5 minutes into the entry candle
    window = window_at(now)
    assert window.index[-1] == E  # the entry candle is the forming last row ...
    assert canonical_entries(window)[-1].timestamp == E  # ... and its partial OHLC DOES show the signal
    account = SimulatedAccount()
    result = cycle(monkeypatch, window, enabled(), account, now)
    assert result.order is None and account.quantity == 0
    assert result.risk.reason == "No actionable signal"


def test_the_same_signal_on_the_completed_candle_creates_the_entry(monkeypatch) -> None:
    now = at(E, 5.5)  # the entry candle has closed; the next one is forming
    account = SimulatedAccount()
    result = cycle(monkeypatch, window_at(now), enabled(), account, now)
    assert result.event.kind == "ENTRY_CALL" and result.event.timestamp == E
    assert result.order.status == "PLACED" and account.quantity == 1


def test_the_entry_price_is_the_completed_candles_close(monkeypatch) -> None:
    now = at(E, 5.5)
    window = window_at(now)
    account = SimulatedAccount()
    result = cycle(monkeypatch, window, enabled(), account, now)
    assert result.order.fill_price == FINAL.loc[E, "Close"] == ENTRY.underlying_price
    assert result.order.fill_price != window["Close"].iloc[-1]  # not the forming candle's price
    assert account.average_price == FINAL.loc[E, "Close"]


def test_sl_and_tp_come_from_the_completed_candles_close_and_atr(monkeypatch) -> None:
    now = at(E, 5.5)
    account = SimulatedAccount()
    result = cycle(monkeypatch, window_at(now), enabled(), account, now)
    assert (result.event.stop_loss, result.event.target) == (ENTRY.stop_loss, ENTRY.target)
    assert (account.stop_loss, account.target) == (ENTRY.stop_loss, ENTRY.target)
    partial = canonical_entries(window_at(at(E, 2.5)))[-1]  # what the forming candle would have given
    assert (partial.stop_loss, partial.target) != (ENTRY.stop_loss, ENTRY.target)


# ---------------- scheduler: repeated ticks, no duplicate ----------------


def test_repeated_scheduler_ticks_fill_the_completed_bar_entry_once(monkeypatch) -> None:
    risk_manager = enabled()
    account = SimulatedAccount()
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {"nifty-50": OrderManager(account)})
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: contract(event))
    monkeypatch.setattr(web_server._scheduler, "_index_ids", ["nifty-50"])
    fills = []
    for minutes in (2.5, 5.5, 8.0, 10.5):  # wall-clock ticks, not aligned to candle boundaries
        tick = at(E, minutes)

        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None, _tick=tick):
                return _tick

        monkeypatch.setattr(auto_trader, "datetime", FixedNow)
        monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, _w=window_at(tick), **_k: _w)
        asyncio.run(web_server._scheduler._tick())
        fills.append(risk_manager.state.trades_today)
    assert fills == [0, 1, 1, 1]  # nothing while forming, once after close, never again
    assert account.quantity == 1 and account.last_event_at == E


# ---------------- freshness of a completed candle ----------------


@pytest.mark.parametrize("minutes_after_close, fills", [(0, True), (10, True), (10 + 1 / 60, False)])
def test_a_completed_candle_is_fresh_from_its_close_for_two_bar_lengths(monkeypatch, minutes_after_close, fills):
    now = at(E, 5 + minutes_after_close)
    account = SimulatedAccount()
    result = cycle(monkeypatch, FINAL.loc[:E], enabled(), account, now)
    if fills:
        assert result.order.status == "PLACED" and result.event.timestamp == E
    else:
        assert result.order is None and result.risk.reason.startswith("Stale signal expired")
        assert account.quantity == 0


# ---------------- exits, square-off and B4 keep the latest price ----------------


def test_an_open_positions_stop_is_still_detected_on_the_forming_candle(monkeypatch) -> None:
    # A held CALL whose stop is touched inside the still-forming candle exits
    # now, at its stop level - exit timing is unchanged by B8.
    completed = FINAL.loc[:E]
    tick = at(E, 7)  # the candle after E is forming
    dip = pd.DataFrame({"Open": [completed["Close"].iloc[-1]], "High": [completed["Close"].iloc[-1] + 1],
                        "Low": [ENTRY.underlying_price - 50.0], "Close": [ENTRY.underlying_price - 40.0],
                        "Volume": [0.0]}, index=pd.DatetimeIndex([E + FIVE]))
    account = SimulatedAccount(quantity=1, average_price=ENTRY.underlying_price, side="CALL", last_event_at=E,
                               stop_loss=ENTRY.stop_loss, target=ENTRY.target)
    result = cycle(monkeypatch, pd.concat([completed, dip]), enabled(), account, tick)
    assert result.event.kind == "EXIT_SL" and result.event.timestamp == E + FIVE
    assert result.order.fill_price == ENTRY.stop_loss and account.quantity == 0


def test_square_off_uses_the_latest_price_including_the_forming_candle(monkeypatch) -> None:
    index = pd.date_range("2026-09-23 14:00", periods=17, freq="5min", tz="Asia/Kolkata")  # .. 15:20 forming
    window = pd.DataFrame({c: [110.0] * 17 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 17},
                          index=index)
    window.loc[window.index[-1], ["Close", "High"]] = 112.5  # the 15:20 candle so far
    account = SimulatedAccount(quantity=1, average_price=100.0, side="CALL", last_event_at=index[3])
    result = cycle(monkeypatch, window, enabled(), account, datetime(2026, 9, 23, 15, 21, tzinfo=IST))
    assert result.event.kind == "SQUARE_OFF" and result.order.fill_price == 112.5
    assert account.quantity == 0 and account.square_off_date == "2026-09-23"


def test_completed_bars_drops_only_a_forming_last_candle() -> None:
    now = at(E, 2.5)
    window = window_at(now)
    assert auto_trader._completed_bars(window, FIVE, now).index[-1] == E - FIVE
    assert auto_trader._completed_bars(window, FIVE, at(E, 5)).equals(window)  # closed exactly now: kept
    assert auto_trader._completed_bars(window.iloc[:0], FIVE, now).empty


# ---------------- parity with the canonical completed-bar strategy ----------------


def test_paper_entries_match_the_canonical_completed_bar_timing_across_a_session(monkeypatch) -> None:
    # Walk wall-clock ticks (2.5 minutes into every candle and just after each
    # close), feeding what yfinance would return at each moment - completed
    # candles plus a forming one. The first paper entry is exactly the
    # canonical strategy's first entry on completed candles: same bar, same
    # close price, same SL/TP.
    risk_manager, account = enabled(), SimulatedAccount()
    first_fill = None
    for k in range(len(FINAL)):
        for offset in (2.5, 5.2):
            now = at(FINAL.index[k], offset)
            result = cycle(monkeypatch, window_at(now), risk_manager, account, now)
            if first_fill is None and result.order is not None and result.order.status == "PLACED":
                first_fill = (result.event.timestamp, result.order.fill_price, result.event.stop_loss,
                              result.event.target, now)
        if first_fill:
            break
    assert first_fill[:4] == (ENTRY.timestamp, ENTRY.underlying_price, ENTRY.stop_loss, ENTRY.target)
    assert first_fill[4] >= (E + FIVE).to_pydatetime()  # never before the canonical candle closed
