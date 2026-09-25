"""Phase 6 correctness & consistency fixes.

1. The canonical strategy never opens a position whose SL/target is NaN
   or non-positive (risk ATR still warming up).
2. Invalid OHLC bars are dropped - never repaired or invented - so one bad
   bar can neither crash run() nor poison ATR for the rest of the series.
3. Paper Auto Trade never fills a stale signal at its obsolete price.
4. A paper SL/target exit fills at the SL/target level, exactly like the
   canonical backtest; forced square-off still fills at the latest close.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.backtest import pair_trades
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import TradeEvent, compute_indicators, drop_invalid_bars, invalid_bar_mask
from fno_signals.strategy import run as run_strategy

CONFIG = strategy_config_for(INDEX_MAP[1])


def frame(closes, start: str = "2026-09-23 09:15", wick: float = 2.0) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="5min", tz="Asia/Kolkata")
    closes = [float(c) for c in closes]
    return pd.DataFrame(
        {"Open": closes, "High": [c + wick for c in closes], "Low": [c - wick for c in closes],
         "Close": closes, "Volume": [0.0] * len(closes)},
        index=index,
    )


def random_walk(n: int = 1500, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 24000 + np.cumsum(rng.normal(0, 12, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 6, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 6, n))
    index = pd.date_range("2026-06-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 0.0}, index=index)


UPTREND = frame([100 + 2 * i for i in range(60)])  # ENTRY_CALL 10:35 @132, EXIT_TARGET 11:15 (level 150)
CALL_SL = frame([100 + 2 * i for i in range(20)] + [138 - 8 * i for i in range(1, 10)])  # SL 126, bar close 122
PUT_SL = frame([300 - 2 * i for i in range(20)] + [262 + 8 * i for i in range(1, 10)])  # SL 280, bar close 278
# CALL entry 10:35 -> target 11:15, then PUT entry 12:40 (bar index 41).
BACKLOG = frame([100 + 2 * i for i in range(30)] + [158 - 2 * i for i in range(1, 30)]).iloc[:42]


def _resolve(event: TradeEvent) -> OptionContract:
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
        right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def run_cycle(monkeypatch, df, risk_manager, order_manager, now, **kwargs):
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)
    kwargs.setdefault("resolve_contract_fn", _resolve)
    return auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, now=now, **kwargs)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 9, 23, hh, mm, ss, tzinfo=IST)


def enabled() -> RiskManager:
    manager = RiskManager()
    manager.enable_auto_trading()
    return manager


# =================== 1. ATR warm-up safety (canonical strategy) ===================


RSI7 = replace(CONFIG, signal=replace(CONFIG.signal, rsi_length=7))
ATR21 = replace(CONFIG, risk=replace(CONFIG.risk, atr_length=21))


@pytest.mark.parametrize("config", [RSI7, ATR21], ids=["rsi7-warms-before-atr14", "atr21-warms-after-rsi14"])
def test_no_entry_is_ever_opened_with_a_nan_stop_or_target(config) -> None:
    df = random_walk()
    results, events = run_strategy(df, config, underlying_label="X")
    entries = [e for e in events if e.kind.startswith("ENTRY")]
    atr_ready = compute_indicators(df, config)["atr"].first_valid_index()

    assert entries, "the strategy must keep trading after the warm-up bars"
    assert all(np.isfinite(e.stop_loss) and np.isfinite(e.target) for e in entries)
    assert all(e.timestamp >= atr_ready for e in entries)
    # Before this fix a single warm-up entry froze the whole run: one event, never exited.
    assert len(events) > 10
    assert not (results.loc[results["atr"].isna(), ["call_signal", "put_signal"]].any().any())


def test_warm_up_guard_skips_the_setup_edge_rather_than_deferring_it() -> None:
    # Find a series whose first setup edge falls inside the ATR(21) warm-up.
    for seed in range(100):
        df = random_walk(n=300, seed=seed)
        results, events = run_strategy(df, ATR21, underlying_label="X")
        edges = (results["bull_setup"] & ~results["bull_setup"].shift(1, fill_value=False)) | (
            results["bear_setup"] & ~results["bear_setup"].shift(1, fill_value=False)
        )
        warm_up_edges = results.index[(edges & results["atr"].isna()).to_numpy()]
        if len(warm_up_edges):
            break
    else:
        pytest.fail("no seed with a setup edge during the ATR warm-up")

    entry_times = [e.timestamp for e in events if e.kind.startswith("ENTRY")]
    assert warm_up_edges[0] not in entry_times  # skipped...
    # ...and not deferred: every entry is itself a genuine setup edge with a valid ATR.
    assert all(edges.loc[t] and np.isfinite(results.loc[t, "atr"]) for t in entry_times)


def test_default_config_never_hits_the_guard() -> None:
    # RSI 14 and ATR 14 both become valid on the same bar, so the production
    # defaults behave exactly as before this guard existed.
    indicators = compute_indicators(random_walk(), CONFIG)
    assert indicators["atr"].first_valid_index() <= indicators["rsi"].first_valid_index()


def test_pine_reference_mirrors_the_warm_up_guard() -> None:
    pine = Path(__file__).resolve().parents[1] / "src/fno_signals/pine/algoedge_backtest_strategy.pine"
    source = pine.read_text()
    assert "riskValid = slDist > 0 and tpDist > 0" in source
    assert source.count("and riskValid") == 2


# =================== 2. Missing / invalid OHLC ===================


@pytest.mark.parametrize("column, value", [
    ("Close", np.nan), ("High", np.nan), ("Low", np.nan), ("Open", np.nan),
    ("Close", np.inf), ("Close", 0.0), ("Low", -5.0),
])
def test_one_bad_bar_is_dropped_and_the_run_equals_the_run_without_it(column, value) -> None:
    df = random_walk()
    bad = df.copy()
    bad_ts = bad.index[700]
    bad.loc[bad_ts, column] = value

    results, events = run_strategy(bad, CONFIG, underlying_label="X")
    expected_results, expected_events = run_strategy(df.drop(index=bad_ts), CONFIG, underlying_label="X")

    assert bad_ts not in results.index
    assert events == expected_events
    pd.testing.assert_frame_equal(results, expected_results)
    # ATR/RSI recover immediately - not NaN for the rest of the series.
    assert results.loc[results.index > bad_ts, ["atr", "rsi"]].notna().all().all()
    assert any(e.timestamp > bad_ts for e in events)


def test_high_below_low_is_invalid_but_zero_volume_and_nan_volume_are_not() -> None:
    df = frame([100, 101, 102, 103])
    df.loc[df.index[1], ["High", "Low"]] = [99.0, 104.0]
    df.loc[df.index[2], "Volume"] = np.nan
    assert invalid_bar_mask(df).tolist() == [False, True, False, False]


def test_invalid_bars_are_removed_never_repaired() -> None:
    df = frame([100, 101, 102, 103, 104])
    df.loc[df.index[2], "Close"] = np.nan
    cleaned = drop_invalid_bars(df)
    assert list(cleaned.index) == [df.index[0], df.index[1], df.index[3], df.index[4]]
    pd.testing.assert_frame_equal(cleaned, df.drop(index=df.index[2]))  # no fill, no interpolation


def test_a_fully_invalid_series_yields_no_events_instead_of_crashing() -> None:
    df = frame([100, 101, 102])
    df["Close"] = np.nan
    results, events = run_strategy(df, CONFIG, underlying_label="X")
    assert events == [] and results.empty


def test_backtest_endpoint_segment_survives_a_nan_bar() -> None:
    from algoedge import web_server

    df = random_walk()
    bad = df.copy()
    bad.loc[bad.index[700], "Close"] = np.nan  # previously: ValueError inside run()
    segment = web_server._run_backtest_segment(bad, CONFIG, INDEX_MAP[1], 0.0)
    clean = web_server._run_backtest_segment(df.drop(index=df.index[700]), CONFIG, INDEX_MAP[1], 0.0)
    assert segment["trades"] == clean["trades"]
    assert segment["metrics"]["totalTrades"] == clean["metrics"]["totalTrades"] > 0


def test_paper_cycle_never_uses_a_nan_bar_as_a_price(monkeypatch) -> None:
    # The latest bar is broken: square-off must use the last VALID close.
    df = frame([100.0] * 20, start="2026-09-23 14:30")
    df.loc[df.index[-1], ["Close", "High"]] = np.nan
    account = SimulatedAccount(quantity=1, average_price=90.0, side="CALL")
    result = run_cycle(monkeypatch, df, enabled(), OrderManager(account), now=at(15, 22))
    assert result.event.kind == "SQUARE_OFF"
    assert result.order.fill_price == 100.0
    assert result.order.realized_pnl == pytest.approx(10.0)


def test_paper_cycle_with_no_valid_bars_does_nothing(monkeypatch) -> None:
    df = frame([100.0] * 5)
    df["Close"] = np.nan
    result = run_cycle(monkeypatch, df, enabled(), OrderManager(), now=at(10, 0))
    assert result.event is None and result.order is None
    assert result.risk.reason == "No valid market data"


# =================== 3. Stale paper events ===================


def test_blocked_entry_is_never_filled_hours_later_at_its_old_price(monkeypatch) -> None:
    # The audit's reproduction: entry at 10:35 (@132) blocked while auto
    # trading was off, then re-enabled at 14:10 with the market at 218.
    risk_manager = RiskManager()
    order_manager = OrderManager()
    blocked = run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager, order_manager, now=at(10, 41))
    assert blocked.risk.reason == "Auto trading is disabled"

    risk_manager.enable_auto_trading()
    later = run_cycle(monkeypatch, UPTREND, risk_manager, order_manager, now=at(14, 10))
    assert later.order is None
    # Only the entry: once it is expired the strategy restarts flat from its
    # bar (position sync), so its would-be exit is never even generated.
    assert later.risk.reason.startswith("Stale signal expired (1 event(s)")
    assert order_manager.account.quantity == 0
    assert order_manager.account.last_event_at == UPTREND.index[16]

    # Nothing left to act on - and certainly no fill at 132.
    again = run_cycle(monkeypatch, UPTREND, risk_manager, order_manager, now=at(14, 15))
    assert again.order is None and order_manager.account.quantity == 0


@pytest.mark.parametrize("now, fills", [
    (at(10, 50, 0), True),  # entry bar 10:35 closed 10:40; exactly 2 bars (10 min) later: still fresh
    (at(10, 50, 1), False),  # one second past the window: stale
])
def test_freshness_window_boundary(monkeypatch, now, fills) -> None:
    order_manager = OrderManager()
    result = run_cycle(monkeypatch, UPTREND.iloc[:17], enabled(), order_manager, now=now)
    assert (result.order is not None and result.order.status == "PLACED") is fills
    assert order_manager.account.quantity == (1 if fills else 0)
    if not fills:
        assert "stale" in result.risk.reason.lower()


def test_freshness_window_is_two_bar_lengths_after_bar_close() -> None:
    assert auto_trader.SIGNAL_FRESHNESS_BARS == 2
    assert auto_trader._BAR_LENGTH["5m"] == timedelta(minutes=5)


def test_a_signal_on_the_still_forming_bar_counts_as_fresh(monkeypatch) -> None:
    # now is before the entry bar even closes (yfinance includes the forming bar).
    order_manager = OrderManager()
    result = run_cycle(monkeypatch, UPTREND.iloc[:17], enabled(), order_manager, now=at(10, 37))
    assert result.order.status == "PLACED"


def test_a_stale_backlog_is_expired_in_one_cycle_and_the_fresh_event_acted_on(monkeypatch) -> None:
    # First run after a restart/fresh start: the window holds an old CALL
    # round trip and a brand-new PUT entry. Only the PUT is acted on, now.
    order_manager = OrderManager()
    result = run_cycle(monkeypatch, BACKLOG, enabled(), order_manager, now=at(12, 46))
    assert result.event.kind == "ENTRY_PUT"
    assert result.order.status == "PLACED"
    assert result.order.fill_price == pytest.approx(BACKLOG["Close"].iloc[-1])
    assert order_manager.account.side == "PUT"


def test_a_risk_blocked_entry_retries_while_fresh_then_expires(monkeypatch) -> None:
    risk_manager = enabled()
    risk_manager.trip_kill_switch("test")
    order_manager = OrderManager()
    first = run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager, order_manager, now=at(10, 41))
    assert "kill switch" in first.risk.reason.lower()
    risk_manager.reset_kill_switch()

    retried = run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager, order_manager, now=at(10, 46))
    assert retried.order.status == "PLACED"  # still inside the window: filled

    risk_manager2 = enabled()
    risk_manager2.trip_kill_switch("test")
    order_manager2 = OrderManager()
    run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager2, order_manager2, now=at(10, 41))
    risk_manager2.reset_kill_switch()
    expired = run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager2, order_manager2, now=at(10, 55))
    assert expired.order is None and "stale" in expired.risk.reason.lower()


def test_a_stale_exit_with_an_open_position_closes_at_the_current_price(monkeypatch) -> None:
    # The target exit (11:15, level 150) was missed (e.g. auto trading off);
    # at 13:00 the position must still be closed - but at the current
    # price, never at the obsolete 150.
    account = SimulatedAccount(quantity=1, average_price=132.0, side="CALL", last_event_at=UPTREND.index[16])
    order_manager = OrderManager(account)
    window = UPTREND.iloc[:46]  # latest bar 13:00, close 190
    result = run_cycle(monkeypatch, window, enabled(), order_manager, now=at(13, 6))
    assert result.event.kind == "EXIT_TARGET"
    assert result.risk.reason.startswith("Late exit")
    assert result.order.fill_price == pytest.approx(190.0)
    assert result.order.fill_price != result.event.exit_level
    assert account.quantity == 0


def test_an_exit_for_an_entry_that_was_never_filled_is_skipped_not_stuck(monkeypatch) -> None:
    # With position sync the strategy can no longer produce such an exit for
    # a flat account; this pins the remaining defensive branch with a stub.
    orphan = TradeEvent(
        timestamp=UPTREND.index[24], kind="EXIT_TARGET", underlying_price=148.0, option_symbol=None,
        stop_loss=None, target=None, exit_level=150.0,
    )
    monkeypatch.setattr(auto_trader, "_account_synced_events", lambda *_a, **_kw: [orphan])
    order_manager = OrderManager(SimulatedAccount(last_event_at=UPTREND.index[16]))
    result = run_cycle(monkeypatch, UPTREND.iloc[:25], enabled(), order_manager, now=at(11, 21))
    assert result.event is orphan and result.order is None
    assert "never filled" in result.risk.reason
    assert order_manager.account.last_event_at == UPTREND.index[24]


# =================== 4. Paper exit pricing = canonical SL/TP model ===================


@pytest.mark.parametrize("df, entry_kind, stop", [(CALL_SL, "ENTRY_CALL", 126.0), (PUT_SL, "ENTRY_PUT", 280.0)])
def test_stop_loss_exit_fills_at_the_stop_level_not_the_bar_close(monkeypatch, df, entry_kind, stop) -> None:
    risk_manager = enabled()
    order_manager = OrderManager()
    _results, events = run_strategy(df, CONFIG, underlying_label="X")
    entry, exit_ = events[0], events[1]
    assert entry.kind == entry_kind and exit_.kind == "EXIT_SL"
    assert exit_.underlying_price != stop  # the bar closed beyond the stop

    entry_at = df.index.get_loc(entry.timestamp)
    run_cycle(monkeypatch, df.iloc[:entry_at + 1], risk_manager, order_manager,
              now=entry.timestamp.to_pydatetime() + timedelta(minutes=6))
    exit_result = run_cycle(monkeypatch, df, risk_manager, order_manager,
                            now=exit_.timestamp.to_pydatetime() + timedelta(minutes=6))

    assert exit_result.order.fill_price == pytest.approx(stop)
    # Paper P&L == the canonical backtest's points for the same round trip.
    backtest_points = pair_trades(events)[0].points
    assert exit_result.order.realized_pnl == pytest.approx(backtest_points)


def test_target_exit_fills_at_the_target_level_and_matches_the_backtest(monkeypatch) -> None:
    risk_manager = enabled()
    order_manager = OrderManager()
    run_cycle(monkeypatch, UPTREND.iloc[:17], risk_manager, order_manager, now=at(10, 41))
    exit_result = run_cycle(monkeypatch, UPTREND.iloc[:25], risk_manager, order_manager, now=at(11, 21))
    _results, events = run_strategy(UPTREND, CONFIG, underlying_label="X")
    assert exit_result.order.fill_price == pytest.approx(events[1].exit_level) == pytest.approx(150.0)
    assert exit_result.order.realized_pnl == pytest.approx(pair_trades(events)[0].points)


def test_square_off_still_fills_at_the_latest_close(monkeypatch) -> None:
    df = frame([105.0] * 20, start="2026-09-23 14:30")
    account = SimulatedAccount(quantity=2, average_price=100.0, side="CALL")
    result = run_cycle(monkeypatch, df, enabled(), OrderManager(account), now=at(15, 20))
    assert result.event.kind == "SQUARE_OFF"
    assert result.order.fill_price == 105.0
    assert result.order.realized_pnl == pytest.approx(10.0)


def test_persisted_order_records_the_actual_fill_price(monkeypatch) -> None:
    from algoedge import web_server

    recorded = []
    monkeypatch.setattr(web_server.db, "record_order", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda _index_id, event: _resolve(event))
    monkeypatch.setattr(web_server, "risk_manager", enabled())
    account = SimulatedAccount(quantity=1, average_price=132.0, side="CALL", last_event_at=UPTREND.index[16])
    monkeypatch.setattr(web_server, "order_managers", {"nifty-50": OrderManager(account)})
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: UPTREND.iloc[:25])
    monkeypatch.setattr(auto_trader, "datetime", _FrozenDatetime)

    web_server._run_and_persist_cycle("nifty-50", "5m", 1)

    assert recorded and recorded[-1]["side"] == "SELL"
    assert recorded[-1]["price"] == pytest.approx(150.0)  # target level, not the bar close (148)
    assert recorded[-1]["realized_pnl"] == pytest.approx(18.0)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return at(11, 21)
