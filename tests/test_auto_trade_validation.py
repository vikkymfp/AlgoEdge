"""Phase 2 - Paper Auto Trade Validation.

End-to-end regression coverage for the Phase 1 unification (Auto Trade now
runs fno_signals.strategy.run(), the same canonical strategy as Backtest/
Pine/fno_signals --live - see tests/test_auto_trader.py for the unit-level
orchestration tests and project_production_hardening.md for the full audit
trail). This file validates the whole paper-execution stack end-to-end
using realistic 5m OHLCV data run through the REAL, unmodified canonical
strategy (no internal monkeypatching of compute_indicators/compute_setups),
plus every safety/risk boundary Auto Trade must respect.

Nothing here connects to Groww - every test uses OrderManager wired to a
SimulatedAccount and monkeypatches only the data-fetch boundary
(fetch_underlying_data), never a broker client.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=IST)
CONFIG = strategy_config_for(INDEX_MAP[1])  # nifty-50 -> choice 1


def trending_df(n: int, start_price: float, step: float, start: str = "2026-09-23 09:15") -> pd.DataFrame:
    """A realistic-shaped OHLCV DataFrame with a real, sustained trend -
    real EMA/RSI/Supertrend/ATR are computed on this by the unmodified
    canonical strategy, not injected."""
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


UPTREND_60 = trending_df(60, start_price=100.0, step=2.0)  # produces ENTRY_CALL then EXIT_TARGET
DOWNTREND_60 = trending_df(60, start_price=300.0, step=-2.0)  # produces ENTRY_PUT then EXIT_TARGET


def patch_fetch(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)


# ---------- 1. Signal parity, on real indicator-driven data (not mocked) ----------


def test_signal_parity_on_real_uptrend_data(monkeypatch) -> None:
    """The critical requirement, restated with the real indicator pipeline
    (EMA/RSI/Supertrend/ATR actually computed, not monkeypatched): identical
    OHLCV input through auto_trader.run_cycle() and a direct
    fno_signals.strategy.run() call must produce an identical event."""
    patch_fetch(monkeypatch, UPTREND_60)
    _expected_results, expected_events = run_strategy(UPTREND_60, CONFIG, "NIFTY 50")
    assert expected_events, "fixture must actually produce a signal to be meaningful"
    expected = expected_events[-1]

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    assert result.event.kind == expected.kind
    assert result.event.underlying_price == pytest.approx(expected.underlying_price)
    assert result.event.stop_loss == pytest.approx(expected.stop_loss)
    assert result.event.target == pytest.approx(expected.target)
    assert result.event.option_symbol == expected.option_symbol


def test_signal_parity_on_real_downtrend_data(monkeypatch) -> None:
    patch_fetch(monkeypatch, DOWNTREND_60)
    _expected_results, expected_events = run_strategy(DOWNTREND_60, CONFIG, "NIFTY 50")
    assert expected_events
    expected = expected_events[-1]

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    assert result.event.kind == expected.kind
    assert result.event.stop_loss == pytest.approx(expected.stop_loss)
    assert result.event.target == pytest.approx(expected.target)


# ---------- 2 & 3. CALL/PUT entries+exits and ATR-based SL/TP, end-to-end
# across separate cycles (real data, not injected TradeEvents) ----------


def test_call_entry_then_exit_round_trip_across_two_real_cycles(monkeypatch) -> None:
    # Cycle 1: window truncated to just past the entry bar (index 14 of
    # UPTREND_60, see the fixture derivation below) - only the entry has
    # happened "so far".
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    entry_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )
    assert entry_result.event.kind == "ENTRY_CALL"
    assert entry_result.order.status == "PLACED"
    assert order_manager.account.side == "CALL"
    assert order_manager.account.quantity == 1
    entry_price = order_manager.account.average_price

    # Cycle 2, 5 minutes later: the full window now also contains the
    # target exit that happens further along the same real trend.
    patch_fetch(monkeypatch, UPTREND_60)
    exit_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )

    assert exit_result.event.kind == "EXIT_TARGET"
    assert exit_result.order.status == "PLACED"
    assert order_manager.account.quantity == 0
    assert order_manager.account.side is None
    # Paper P&L reconciliation: realized P&L must equal (exit - entry) * qty
    # for a CALL, exactly - not approximately-plausible.
    expected_pnl = (exit_result.event.underlying_price - entry_price) * 1
    assert exit_result.order.realized_pnl == pytest.approx(expected_pnl)


def test_put_entry_then_exit_round_trip_across_two_real_cycles(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    patch_fetch(monkeypatch, DOWNTREND_60.iloc[:14])  # entry bar is index 13
    entry_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=2, now=TRADING_HOURS_NOW
    )
    assert entry_result.event.kind == "ENTRY_PUT"
    entry_price = order_manager.account.average_price

    patch_fetch(monkeypatch, DOWNTREND_60)
    exit_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=2,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )

    assert exit_result.event.kind == "EXIT_TARGET"
    assert order_manager.account.quantity == 0
    # PUT P&L reconciliation: (entry - exit) * qty, the mirror of CALL.
    expected_pnl = (entry_price - exit_result.event.underlying_price) * 2
    assert exit_result.order.realized_pnl == pytest.approx(expected_pnl)


def test_atr_based_sl_and_target_keep_the_configured_risk_reward_ratio(monkeypatch) -> None:
    # RiskConfig defaults: sl_multiplier=1.5, tp_multiplier=4.5 -> target
    # distance is always 3x the stop-loss distance, regardless of the
    # actual ATR value on the day - this is what replaced the old
    # strategy_engine 1%/2% percent calculation in Phase 1.
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])  # truncated to just the entry bar
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    event = result.event
    assert event.kind == "ENTRY_CALL"
    sl_distance = event.underlying_price - event.stop_loss
    tp_distance = event.target - event.underlying_price
    assert sl_distance > 0
    ratio = CONFIG.risk.tp_multiplier / CONFIG.risk.sl_multiplier
    assert tp_distance == pytest.approx(sl_distance * ratio)


# ---------- 5. Position reversal, across separate cycles ----------


def test_position_reversal_call_to_put_across_separate_cycles(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    # Cycle 1: uptrend -> CALL entry.
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)
    assert order_manager.account.side == "CALL"

    # Cycle 2: full uptrend window -> the CALL's target exit fires, flat again.
    patch_fetch(monkeypatch, UPTREND_60)
    r2 = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )
    assert r2.event.kind == "EXIT_TARGET"
    assert order_manager.account.side is None

    # Cycle 3: a fresh downtrend window starting chronologically after the
    # uptrend's exit (a real reversal in market direction, not merely a
    # different fixture) -> a brand-new PUT entry opens cleanly, no
    # leftover state from the CALL blocks it and it isn't mistaken for a
    # duplicate of an earlier, unrelated event.
    reversal_downtrend = trending_df(60, start_price=300.0, step=-2.0, start="2026-09-23 11:20")
    patch_fetch(monkeypatch, reversal_downtrend.iloc[:14])
    r3 = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=10),
    )
    assert r3.event.kind == "ENTRY_PUT"
    assert r3.order.status == "PLACED"
    assert order_manager.account.side == "PUT"


# ---------- 6 & 7. Duplicate signal prevention ----------


def test_duplicate_signal_is_not_reprocessed_on_a_later_cycle(monkeypatch) -> None:
    # Same fixed window fetched twice in a row (the realistic case of a
    # scheduler tick landing before any new bar has closed) - the second
    # cycle must not re-fill the same historical entry a second time.
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    r1 = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)
    assert r1.order.status == "PLACED"
    assert order_manager.account.quantity == 1

    r2 = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5), total_open_positions=0,
    )
    assert r2.order is None
    assert "duplicate" in r2.risk.reason.lower()
    assert order_manager.account.quantity == 1  # unchanged, not averaged-in again


def test_duplicate_signal_is_not_reprocessed_even_with_a_higher_position_cap(monkeypatch) -> None:
    # Regression for the exact gap found during Phase 2 validation: with
    # max_open_positions > 1, the accidental protection max_open_positions
    # provided under the default (=1) config no longer applies - only
    # last_event_at-based deduplication prevents silent re-averaging.
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager(RiskLimits(max_open_positions=5))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    for i in range(4):
        auto_trader.run_cycle(
            "nifty-50", "5m", risk_manager, order_manager, quantity=1,
            now=TRADING_HOURS_NOW + timedelta(minutes=5 * i), total_open_positions=0,
        )

    assert order_manager.account.quantity == 1


def test_a_signal_blocked_by_risk_is_retried_on_a_later_cycle_not_dropped(monkeypatch) -> None:
    # last_event_at only advances on an actual PLACED fill - a signal that
    # was merely blocked (e.g. auto trading briefly off) must still be
    # eligible once the gate reopens, not permanently treated as "seen".
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()  # auto trading NOT enabled yet
    order_manager = OrderManager()

    r1 = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)
    assert r1.order is None
    assert r1.risk.reason == "Auto trading is disabled"
    assert order_manager.account.quantity == 0

    risk_manager.enable_auto_trading()
    r2 = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )
    assert r2.order.status == "PLACED"
    assert order_manager.account.quantity == 1


# ---------- 8. Restart / recovery with an open position (documents current,
# known behaviour - see the gap noted in the final report) ----------


def test_restart_loses_in_memory_paper_position_state() -> None:
    # web_server.py builds `order_managers` as fresh module-level
    # OrderManager()/SimulatedAccount() instances - there is no DB or disk
    # persistence of paper position state. This test pins down that
    # CURRENT behaviour precisely (a "restart" is just constructing a new
    # OrderManager) so any future persistence work has a regression test
    # to flip, rather than leaving this undocumented.
    pre_restart = OrderManager(SimulatedAccount(quantity=5, average_price=150.0, side="CALL"))
    assert pre_restart.account.quantity == 5

    post_restart = OrderManager()  # what web_server.py does on process start

    assert post_restart.account.quantity == 0
    assert post_restart.account.side is None
    assert post_restart.account.last_event_at is None


# ---------- 9-10. Kill switch, cooldown, consecutive-loss halt ----------


def test_kill_switch_blocks_entry_via_run_cycle(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.trip_kill_switch("Manual test halt")
    order_manager = OrderManager()

    result = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)

    assert result.order is None
    assert "kill switch" in result.risk.reason.lower()
    assert order_manager.account.quantity == 0


def test_consecutive_loss_halt_blocks_entry_via_run_cycle(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits(max_consecutive_losses=2, cooldown_minutes=0))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    # Two losing exits in a row trips the halt - drive it directly via
    # RiskManager.record_trade(), the same call run_cycle makes internally,
    # rather than re-deriving a losing price series.
    risk_manager.record_trade(realized_pnl=-100.0, now=TRADING_HOURS_NOW, is_exit=True)
    risk_manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    assert risk_manager.state.consecutive_loss_halt is True

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=10),
    )

    assert result.order is None
    assert "consecutive losses" in result.risk.reason.lower()


def test_daily_loss_limit_blocks_entry_via_run_cycle(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits(daily_loss_limit=100.0))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    risk_manager.record_trade(realized_pnl=-150.0, now=TRADING_HOURS_NOW, is_exit=True)

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=10),
    )

    assert result.order is None
    assert "daily loss limit" in result.risk.reason.lower()


# ---------- 11. Trading session / entry cutoff / square-off boundaries ----------


def test_entry_blocked_before_trading_session_start(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    before_open = datetime(2026, 9, 23, 9, 0, tzinfo=IST)  # market opens 09:15

    result = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=before_open)

    assert result.order is None
    assert "trading hours" in result.risk.reason.lower()


def test_entry_blocked_after_trading_session_end(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    after_close = datetime(2026, 9, 23, 15, 45, tzinfo=IST)  # market closes 15:30

    result = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=after_close)

    assert result.order is None
    assert "trading hours" in result.risk.reason.lower()


def test_new_entry_blocked_past_entry_cutoff_but_exit_still_allowed(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager(SimulatedAccount(quantity=1, average_price=120.0, side="CALL"))
    past_cutoff = datetime(2026, 9, 23, 15, 10, tzinfo=IST)  # entry_cutoff=15:00, trading_end=15:30

    # A fresh entry must be refused past the cutoff...
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    entry_attempt = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=past_cutoff
    )
    assert entry_attempt.order is None
    assert "entry cutoff" in entry_attempt.risk.reason.lower()

    # ...but an exit of the already-open position must still go through.
    patch_fetch(monkeypatch, UPTREND_60)
    exit_attempt = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=past_cutoff + timedelta(minutes=1), total_open_positions=1,
    )
    assert exit_attempt.event.kind in ("EXIT_TARGET", "EXIT_SL")
    assert exit_attempt.order.status == "PLACED"


def test_square_off_time_is_not_enforced_yet_documented_gap() -> None:
    # RiskLimits.square_off_time is explicitly documented (risk_manager.py)
    # as informational only - no forced auto-exit exists. This test pins
    # that down so it isn't silently assumed to be enforced; see "remaining
    # gaps" in the Phase 2 report.
    limits = RiskLimits()
    assert limits.square_off_time is not None
    # RiskManager.check() never references square_off_time at all.
    import inspect

    from algoedge import risk_manager as risk_manager_module
    source = inspect.getsource(risk_manager_module.RiskManager.check)
    assert "square_off_time" not in source


# ---------- 12. No broker/order API calls ----------


def test_run_cycle_never_touches_a_broker_client(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)

    assert not hasattr(order_manager, "client")
    assert not hasattr(order_manager.account, "client")
