"""Phase 6: the canonical strategy's position must follow the REAL paper
Auto Trade account, not a replay of the data window.

Before this fix, run_cycle() replayed fno_signals.strategy.run() over the
whole window every cycle. Whenever paper didn't do what the replay assumed
(a risk-blocked or expired entry, the 15:20 square-off, a restart), the
replay sat in a phantom position and suppressed genuine new setup edges
until that phantom trade exited. The fixtures below are found by search
against the real canonical strategy (never hand-tuned), and each scenario
asserts both the old (replay) behaviour and the fixed (synced) behaviour.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import OpenPosition, TradeEvent
from fno_signals.strategy import run as run_strategy

CONFIG = strategy_config_for(INDEX_MAP[1])


def session_walk(days: int, seed: int, start: str = "2026-09-21") -> pd.DataFrame:
    """Random walk on the real NSE 5m grid: 75 bars/day, 09:15-15:25 IST."""
    rng = np.random.default_rng(seed)
    index = []
    for day in pd.bdate_range(start, periods=days):
        first = pd.Timestamp(f"{day.date()} 09:15", tz="Asia/Kolkata")
        index += [first + pd.Timedelta(minutes=5 * k) for k in range(75)]
    n = len(index)
    close = 24000 + np.cumsum(rng.normal(0, 12, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 6, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 6, n))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 0.0}, index=pd.DatetimeIndex(index),
    )


def _resolve(event: TradeEvent) -> OptionContract:
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
        right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def cycle(monkeypatch, df, risk_manager, order_manager, now):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)
    return auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, now=now.to_pydatetime(), resolve_contract_fn=_resolve,
    )


def enabled() -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    return manager


def bar_close(ts: pd.Timestamp) -> pd.Timestamp:
    return ts + pd.Timedelta(minutes=5)


# ---------------- the seeding API itself ----------------


def test_unseeded_run_is_the_plain_canonical_run() -> None:
    df = session_walk(3, seed=1)
    plain = run_strategy(df, CONFIG, underlying_label="X")
    seeded_before_start = run_strategy(
        df, CONFIG, underlying_label="X", start_after=df.index[0] - pd.Timedelta(minutes=5),
    )
    assert plain[1] == seeded_before_start[1]
    pd.testing.assert_frame_equal(plain[0], seeded_before_start[0])


def test_initial_position_requires_start_after() -> None:
    with pytest.raises(ValueError):
        run_strategy(session_walk(1, seed=1), CONFIG, underlying_label="X",
                     initial_position=OpenPosition("CALL", 1.0, 0.5, 2.0))


def test_seeded_position_exits_on_its_given_levels_with_canonical_rules() -> None:
    df = session_walk(3, seed=2)
    _results, events = run_strategy(df, CONFIG, underlying_label="X")
    entry, exit_ = events[0], events[1]
    held = OpenPosition(
        "CALL" if entry.kind == "ENTRY_CALL" else "PUT", entry.underlying_price, entry.stop_loss, entry.target,
    )
    _r, seeded = run_strategy(df, CONFIG, underlying_label="X", start_after=entry.timestamp, initial_position=held)
    # Seeding the canonical position at its own entry bar reproduces the rest
    # of the canonical run exactly - the seeding changes nothing else.
    assert seeded == events[1:]
    assert seeded[0] == exit_


def test_signals_are_the_canonical_ones_only_the_position_differs() -> None:
    df = session_walk(3, seed=4)
    results, _events = run_strategy(df, CONFIG, underlying_label="X")
    _r, seeded = run_strategy(df, CONFIG, underlying_label="X", start_after=df.index[100])
    edges = (results["bull_setup"] & ~results["bull_setup"].shift(1, fill_value=False)) | (
        results["bear_setup"] & ~results["bear_setup"].shift(1, fill_value=False)
    )
    for event in seeded:
        if event.kind.startswith("ENTRY"):
            assert edges.loc[event.timestamp]  # every seeded entry is a canonical setup edge


# ---------------- scenario 1: blocked / expired entry ----------------


def _expired_entry_case():
    """A window where a canonical entry E, if not taken by paper, leaves the
    replay in a phantom trade during which a genuine new entry N occurs."""
    for seed in range(200):
        df = session_walk(1, seed=seed)
        _results, events = run_strategy(df, CONFIG, underlying_label="X")
        for k in range(len(events) - 1):
            entry, phantom_exit = events[k], events[k + 1]
            if not entry.kind.startswith("ENTRY") or entry.timestamp.time() > pd.Timestamp("13:00").time():
                continue
            _r, synced = run_strategy(df, CONFIG, underlying_label="X", start_after=entry.timestamp)
            genuine = next((e for e in synced if e.kind.startswith("ENTRY")), None)
            if genuine is not None and genuine.timestamp < phantom_exit.timestamp \
                    and genuine.timestamp.time() < pd.Timestamp("14:50").time():
                return df, entry, genuine
    pytest.fail("no fixture found")


def test_an_expired_entry_no_longer_suppresses_the_next_genuine_entry(monkeypatch) -> None:
    df, blocked_entry, genuine = _expired_entry_case()
    risk_manager = RiskManager()  # auto trading OFF when the first entry fires
    order_manager = OrderManager()
    window_1 = df.loc[:blocked_entry.timestamp]
    first = cycle(monkeypatch, window_1, risk_manager, order_manager, now=bar_close(blocked_entry.timestamp))
    assert first.risk.reason == "Auto trading is disabled"

    risk_manager.enable_auto_trading()
    window_2 = df.loc[:genuine.timestamp]
    now = bar_close(genuine.timestamp) + pd.Timedelta(minutes=1)
    # The pre-fix replay was still "in" the blocked trade at this point:
    replay_events = run_strategy(window_2, CONFIG, underlying_label="X")[1]
    assert not any(e.timestamp == genuine.timestamp for e in replay_events)

    result = cycle(monkeypatch, window_2, risk_manager, order_manager, now=now)

    assert result.event.timestamp == genuine.timestamp
    assert result.order.status == "PLACED"
    assert order_manager.account.side == ("CALL" if genuine.kind == "ENTRY_CALL" else "PUT")
    assert order_manager.account.stop_loss == pytest.approx(genuine.stop_loss)
    assert order_manager.account.target == pytest.approx(genuine.target)


# ---------------- scenario 2: forced square-off ----------------


def _square_off_case():
    """Day 1: a canonical entry the strategy would carry overnight. Day 2: a
    genuine entry while that (squared-off) trade would still be open."""
    for seed in range(300):
        df = session_walk(2, seed=seed)
        _results, events = run_strategy(df, CONFIG, underlying_label="X")
        day1 = df.index[0].date()
        square_off_bar = df.index[df.index.date == day1][-3]  # 15:15 bar, ends 15:20
        for k in range(len(events) - 1):
            entry, exit_ = events[k], events[k + 1]
            if not entry.kind.startswith("ENTRY") or entry.timestamp.date() != day1:
                continue
            if exit_.timestamp <= square_off_bar or entry.timestamp.time() > pd.Timestamp("14:30").time():
                continue
            _r, synced = run_strategy(df, CONFIG, underlying_label="X", start_after=square_off_bar)
            genuine = next((e for e in synced if e.kind.startswith("ENTRY")), None)
            if genuine is not None and genuine.timestamp < exit_.timestamp \
                    and genuine.timestamp.date() != day1 and genuine.timestamp.time() < pd.Timestamp("14:50").time():
                return df, entry, square_off_bar, genuine
    pytest.fail("no fixture found")


def test_after_a_square_off_the_next_days_genuine_entry_is_taken(monkeypatch) -> None:
    df, entry, square_off_bar, genuine = _square_off_case()
    risk_manager = enabled()
    order_manager = OrderManager()

    opened = cycle(monkeypatch, df.loc[:entry.timestamp], risk_manager, order_manager,
                   now=bar_close(entry.timestamp))
    assert opened.order.status == "PLACED"

    squared = cycle(monkeypatch, df.loc[:square_off_bar], risk_manager, order_manager,
                    now=bar_close(square_off_bar))
    assert squared.event.kind == "SQUARE_OFF"
    assert order_manager.account.quantity == 0
    assert order_manager.account.stop_loss is None and order_manager.account.target is None

    window = df.loc[:genuine.timestamp]
    replay_events = run_strategy(window, CONFIG, underlying_label="X")[1]
    assert not any(e.timestamp == genuine.timestamp for e in replay_events)  # pre-fix: suppressed

    result = cycle(monkeypatch, window, risk_manager, order_manager,
                   now=bar_close(genuine.timestamp) + pd.Timedelta(minutes=1))
    assert result.event.timestamp == genuine.timestamp
    assert result.order.status == "PLACED"


# ---------------- scenario 3: restart with an open position ----------------


def test_restart_rederives_the_same_levels_and_exits_identically(monkeypatch) -> None:
    df = session_walk(1, seed=7)
    _results, events = run_strategy(df, CONFIG, underlying_label="X")
    entry, exit_ = next((events[k], events[k + 1]) for k in range(len(events) - 1)
                        if events[k].kind.startswith("ENTRY") and events[k].timestamp.time()
                        < pd.Timestamp("13:00").time() and events[k + 1].timestamp.time() < pd.Timestamp("15:15").time())
    side = "CALL" if entry.kind == "ENTRY_CALL" else "PUT"
    window = df.loc[:exit_.timestamp]
    now = bar_close(exit_.timestamp) + pd.Timedelta(minutes=1)

    uninterrupted = SimulatedAccount(quantity=1, average_price=entry.underlying_price, side=side,
                                     last_event_at=entry.timestamp, stop_loss=entry.stop_loss, target=entry.target)
    restored = SimulatedAccount(quantity=1, average_price=entry.underlying_price, side=side,
                                last_event_at=entry.timestamp)  # levels are not persisted
    a = cycle(monkeypatch, window, enabled(), OrderManager(uninterrupted), now=now)
    b = cycle(monkeypatch, window, enabled(), OrderManager(restored), now=now)

    assert a.event == b.event == exit_
    assert a.order.fill_price == pytest.approx(b.order.fill_price) == pytest.approx(exit_.exit_level)


def test_restart_without_the_entry_bar_leaves_the_exit_to_square_off(monkeypatch) -> None:
    df = session_walk(1, seed=7)
    # Entry bar outside the fetched window: no strategy levels can be derived.
    # The entry is earlier the SAME day (the window starts after it); a
    # prior-day entry is instead a missed square-off, closed at the next
    # session's first cycle - see tests/test_auto_trade_missed_square_off.py.
    account = SimulatedAccount(quantity=1, average_price=24000.0, side="CALL",
                               last_event_at=df.index[5])
    result = cycle(monkeypatch, df.loc[df.index[10]:df.index[40]], enabled(), OrderManager(account),
                   now=bar_close(df.index[40]))
    assert result.order is None or not result.event.kind.startswith("ENTRY")
    assert account.quantity == 1  # never re-entered or double-opened

    square_off = cycle(monkeypatch, df.loc[df.index[10]:df.index[72]], enabled(), OrderManager(account),
                       now=bar_close(df.index[72]))
    assert square_off.event.kind == "SQUARE_OFF" and account.quantity == 0


# ---------------- nothing else changes ----------------


def test_holding_a_position_suppresses_new_entries_like_the_canonical_strategy(monkeypatch) -> None:
    df, _blocked, genuine = _expired_entry_case()
    # Paper really holds a position opened before `genuine` whose levels are
    # never hit: the synced strategy must not open a second one.
    account = SimulatedAccount(quantity=1, average_price=24000.0, side="CALL",
                               last_event_at=df.index[0], stop_loss=1.0, target=1e9)
    window = df.loc[:genuine.timestamp]
    result = cycle(monkeypatch, window, enabled(), OrderManager(account),
                   now=bar_close(genuine.timestamp) + timedelta(minutes=1))
    assert result.order is None
    assert account.quantity == 1
