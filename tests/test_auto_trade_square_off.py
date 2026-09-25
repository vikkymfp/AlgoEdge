"""Phase 4 - forced end-of-day square-off for paper Auto Trade.

Uses a FLAT (constant-price) OHLCV fixture wherever the test wants to
isolate square-off behavior from the canonical strategy's own signal
generation (a flat series never produces an EMA/RSI/Supertrend setup, so
`events` is always empty and only the time-driven square-off path can act)
- and the same real trending fixtures from tests/test_auto_trade_validation.py
/tests/test_auto_trade_restart_safety.py where a test specifically needs a
pending strategy event alongside square-off.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from algoedge import auto_trader
from algoedge.auto_trader import restore_account_state
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.strategy import TradeEvent

TODAY = "2026-09-23"
BEFORE_SQUARE_OFF = datetime(2026, 9, 23, 15, 15, tzinfo=IST)  # square_off_time default = 15:20
AT_SQUARE_OFF = datetime(2026, 9, 23, 15, 20, tzinfo=IST)
AFTER_SQUARE_OFF = datetime(2026, 9, 23, 15, 25, tzinfo=IST)
STILL_IN_SESSION_LATER = datetime(2026, 9, 23, 15, 28, tzinfo=IST)  # trading_end default = 15:30


def flat_df(n: int = 20, price: float = 100.0, start: str = "2026-09-23 14:50") -> pd.DataFrame:
    """A constant-price window spanning through square-off time - produces
    zero canonical-strategy events, isolating square-off from signal
    generation."""
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [price] * n
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [0.0] * n}, index=index,
    )


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


def patch_fetch(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)


def _fake_resolve_contract(event: TradeEvent) -> OptionContract:
    """Phase 5 stand-in - this file is about square-off timing, not
    resolution itself, so entries resolve cleanly by default."""
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}",
        underlying="NIFTY", right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def run_cycle(*args, **kwargs):
    kwargs.setdefault("resolve_contract_fn", _fake_resolve_contract)
    return auto_trader.run_cycle(*args, **kwargs)


def open_call_account(quantity: int = 1, average_price: float = 100.0, **overrides) -> SimulatedAccount:
    return SimulatedAccount(quantity=quantity, average_price=average_price, side="CALL", **overrides)


def open_put_account(quantity: int = 1, average_price: float = 100.0, **overrides) -> SimulatedAccount:
    return SimulatedAccount(quantity=quantity, average_price=average_price, side="PUT", **overrides)


# ---------- open CALL / PUT -> square-off ----------


def test_open_call_position_is_squared_off_after_the_configured_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=105.0))
    account = open_call_account(quantity=2, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert result.order.status == "PLACED"
    assert account.quantity == 0
    assert account.side is None
    assert account.square_off_date == TODAY
    # P&L reconciliation: CALL profits when price rose from 100 to 105.
    assert result.order.realized_pnl == pytest.approx((105.0 - 100.0) * 2)


def test_open_put_position_is_squared_off_after_the_configured_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=95.0))
    account = open_put_account(quantity=3, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert result.order.status == "PLACED"
    assert account.quantity == 0
    assert account.side is None
    # PUT profits when price fell from 100 to 95.
    assert result.order.realized_pnl == pytest.approx((100.0 - 95.0) * 3)


# ---------- already flat -> no-op ----------


def test_already_flat_account_is_a_no_op_at_square_off_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df())
    account = SimulatedAccount()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event is None
    assert result.order is None
    assert account.square_off_date is None  # nothing was ever squared off


# ---------- exact time boundary ----------


def test_square_off_fires_exactly_at_the_configured_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=100.0))
    account = open_call_account()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AT_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0


def test_square_off_fires_after_the_configured_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=100.0))
    account = open_call_account()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=STILL_IN_SESSION_LATER
    )

    assert result.event.kind == "SQUARE_OFF"


def test_no_square_off_on_a_cycle_before_the_configured_time(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=100.0))
    account = open_call_account()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=BEFORE_SQUARE_OFF
    )

    assert result.event is None  # flat_df produces no strategy events either
    assert account.quantity == 1  # still open, untouched
    assert account.square_off_date is None


# ---------- restart scenarios ----------


def test_restart_before_square_off_still_squares_off_the_restored_position(monkeypatch) -> None:
    snapshot = {
        "cash": 999_900.0, "quantity": 1, "average_price": 100.0, "side": "CALL",
        "last_event_at": pd.Timestamp("2026-09-23 10:00", tz="Asia/Kolkata"), "square_off_date": None,
    }
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, flat_df(price=110.0))

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0
    assert account.square_off_date == TODAY


def test_restart_after_square_off_does_not_square_off_again(monkeypatch) -> None:
    snapshot = {
        "cash": 1_000_010.0, "quantity": 0, "average_price": None, "side": None,
        "last_event_at": AFTER_SQUARE_OFF - timedelta(minutes=5), "square_off_date": TODAY,
    }
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, flat_df(price=110.0))

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=STILL_IN_SESSION_LATER
    )

    assert result.event is None
    assert result.order is None
    assert account.quantity == 0


# ---------- only once per day ----------


def test_multiple_cycles_after_square_off_only_square_off_once(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=110.0))
    account = open_call_account(quantity=1, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    first = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )
    assert first.event.kind == "SQUARE_OFF"
    trades_after_first = risk_manager.state.trades_today

    for i in range(3):
        result = run_cycle(
            "nifty-50", "5m", risk_manager, order_manager, quantity=1,
            now=AFTER_SQUARE_OFF + timedelta(minutes=5 * (i + 1)),
        )
        assert result.event is None or result.order is None

    assert account.quantity == 0
    assert risk_manager.state.trades_today == trades_after_first  # no further fills counted


# ---------- pending strategy event + square-off time (square-off wins) ----------


def test_square_off_takes_precedence_over_a_pending_strategy_event(monkeypatch) -> None:
    # A real, unprocessed ENTRY_CALL event sits in the window (step=6
    # starting 13:50 produces one at 15:00, verified against the actual
    # canonical strategy), AND square-off time has been reached with a
    # genuinely open position - square-off must win, not the strategy's
    # own pending event.
    uptrend = trending_df(60, start_price=100.0, step=6.0, start="2026-09-23 13:50")
    account = open_call_account(quantity=1, average_price=90.0, last_event_at=uptrend.index[0])
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, uptrend)  # contains a later, unprocessed ENTRY_CALL

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0


# ---------- stale ENTRY after square-off cannot reopen ----------


def test_stale_entry_after_square_off_cannot_reopen_the_position(monkeypatch) -> None:
    # step=6 starting 13:50 produces a real ENTRY_CALL at 15:00 (verified
    # against the actual canonical strategy, not assumed).
    uptrend = trending_df(60, start_price=100.0, step=6.0, start="2026-09-23 13:50")
    # Square-off already happened today; a later cycle's freshly-recomputed
    # window still contains an (unprocessed-by-timestamp) ENTRY_CALL.
    account = SimulatedAccount(square_off_date=TODAY, last_event_at=uptrend.index[0])
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, uptrend)

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=STILL_IN_SESSION_LATER
    )

    assert result.order is None
    assert "square-off" in result.risk.reason.lower()
    assert account.quantity == 0  # never reopened


# ---------- persistence failure during square-off fails safe ----------


def test_persistence_failure_during_square_off_does_not_raise_or_corrupt_state() -> None:
    account = open_call_account(quantity=1, average_price=100.0)

    def broken_factory():
        raise OperationalError("connect", {}, Exception("connection dropped"))

    from algoedge import db

    original_factory = db._session_factory
    db._session_factory = broken_factory
    try:
        # Must never raise, even though the DB write itself would fail.
        db.record_auto_trade_account_snapshot("nifty-50", account, event="SQUARE_OFF")
    finally:
        db._session_factory = original_factory

    # The in-memory account (the actual paper fill) is completely
    # unaffected by the persistence layer failing - it was never rolled
    # back, and a failed *write* must never be read back as "closed" by a
    # caller that didn't get a successful snapshot.
    assert account.quantity == 1
    assert account.side == "CALL"
    assert db.load_latest_auto_trade_account_state("nifty-50") is None


# ---------- existing risk/kill-switch behavior preserved ----------


def test_square_off_still_fires_even_with_the_kill_switch_engaged(monkeypatch) -> None:
    # Square-off is a risk-REDUCING forced close, not a new-risk decision -
    # it must not be suppressible by the kill switch, unlike a fresh entry.
    patch_fetch(monkeypatch, flat_df(price=110.0))
    account = open_call_account(quantity=1, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.trip_kill_switch("test halt")

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0


def test_square_off_still_fires_even_when_auto_trading_is_disabled(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=110.0))
    account = open_call_account(quantity=1, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()  # auto trading NOT enabled

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0


def test_a_stray_entry_is_still_blocked_by_kill_switch_after_square_off(monkeypatch) -> None:
    # Regression: square-off bypassing risk gates must not weaken the
    # gates for genuinely NEW entries afterward. step=6 starting 13:50
    # produces a real ENTRY_CALL at 15:00, before BEFORE_SQUARE_OFF (15:15).
    uptrend = trending_df(60, start_price=100.0, step=6.0, start="2026-09-23 13:50")
    account = SimulatedAccount()  # flat, never squared off today
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.trip_kill_switch("test halt")
    patch_fetch(monkeypatch, uptrend)

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=BEFORE_SQUARE_OFF
    )

    assert result.order is None
    assert "kill switch" in result.risk.reason.lower()
    assert account.quantity == 0


def test_square_off_updates_daily_realized_pnl_via_risk_manager(monkeypatch) -> None:
    patch_fetch(monkeypatch, flat_df(price=108.0))
    account = open_call_account(quantity=1, average_price=100.0)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF)

    assert risk_manager.state.realized_pnl_today == pytest.approx(8.0)
    assert risk_manager.state.trades_today == 1
    assert risk_manager.state.last_exit_at == AFTER_SQUARE_OFF


# ---------- regression: canonical parity, dedup, restart persistence untouched ----------


def test_square_off_event_has_the_same_shape_as_a_strategy_exit_event(monkeypatch) -> None:
    # Confirms Phase 4 reuses the exact same TradeEvent/order_manager
    # plumbing as Phase 1-3's EXIT_SL/EXIT_TARGET path, not a parallel one.
    patch_fetch(monkeypatch, flat_df(price=100.0))
    account = open_call_account()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AFTER_SQUARE_OFF
    )

    from fno_signals.strategy import TradeEvent
    assert isinstance(result.event, TradeEvent)
    assert result.event.option_symbol is None
    assert result.event.exit_level == pytest.approx(100.0)


def test_ordinary_duplicate_signal_dedup_still_works_when_not_near_square_off(monkeypatch) -> None:
    # Phase 2 regression, replayed with square-off logic now also present
    # in run_cycle - the two must not interfere with each other.
    uptrend = trending_df(60, start_price=100.0, step=2.0)  # starts 09:15, well before square-off
    account = SimulatedAccount()
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, uptrend.iloc[:17])

    first = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=datetime(2026, 9, 23, 10, 0, tzinfo=IST),
    )
    assert first.order.status == "PLACED"

    second = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=datetime(2026, 9, 23, 10, 5, tzinfo=IST),
    )
    assert second.order is None
    assert "duplicate" in second.risk.reason.lower()
    assert account.quantity == 1
