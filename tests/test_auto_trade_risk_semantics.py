"""Pre-Phase 6 risk semantics for paper Auto Trade.

- The daily loss limit is measured in the same unit as paper realized P&L:
  underlying index points x quantity, never rupees - and the API says so.
- The daily-loss and max-trades gates only block NEW entries; an existing
  position's normal SL/target exit, and the forced end-of-day square-off,
  always go through.

Like tests/test_auto_trade_validation.py, everything runs the real,
unmodified canonical strategy and only monkeypatches the data fetch.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader, web_server
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, PNL_UNIT_UNDERLYING_POINTS, RiskLimits, RiskManager
from fno_signals.strategy import TradeEvent

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 40, tzinfo=IST)  # the 10:35 entry bar has just closed (B8)
AT_SQUARE_OFF = datetime(2026, 9, 23, 15, 20, tzinfo=IST)


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


def flat_df(n: int = 20, price: float = 100.0, start: str = "2026-09-23 14:50") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [price] * n
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [0.0] * n}, index=index,
    )


UPTREND_60 = trending_df(60, start_price=100.0, step=2.0)  # produces ENTRY_CALL then EXIT_TARGET


def _fake_resolve_contract(event: TradeEvent) -> OptionContract:
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}",
        underlying="NIFTY", right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def run_cycle(*args, **kwargs):
    kwargs.setdefault("resolve_contract_fn", _fake_resolve_contract)
    return auto_trader.run_cycle(*args, **kwargs)


def patch_fetch(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)


def open_call_via_real_entry(monkeypatch, risk_manager: RiskManager, order_manager: OrderManager) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])  # truncated to just the entry bar
    entry = run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW)
    assert entry.event.kind == "ENTRY_CALL"
    assert entry.order.status == "PLACED"
    assert order_manager.account.quantity == 1


# ---------- unit semantics ----------


def test_daily_loss_limit_unit_defaults_to_underlying_points() -> None:
    limits = RiskLimits()

    assert limits.daily_loss_limit == 5000.0
    assert limits.daily_loss_limit_unit == PNL_UNIT_UNDERLYING_POINTS == "UNDERLYING_POINTS"


def test_api_exposes_the_loss_limit_and_its_unit(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.state.realized_pnl_today = -42.5
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)

    status = web_server.auto_trading_status()

    assert status["limits"]["dailyLossLimit"] == 5000.0
    assert status["limits"]["dailyLossLimitUnit"] == "UNDERLYING_POINTS"
    assert status["realizedPnlToday"] == -42.5
    assert status["realizedPnlTodayUnit"] == "UNDERLYING_POINTS"


def test_api_reports_the_runtime_risk_managers_actual_limit(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "risk_manager", RiskManager(RiskLimits(daily_loss_limit=250.0)))

    limits = web_server.auto_trading_status()["limits"]

    assert limits["dailyLossLimit"] == 250.0
    assert limits["dailyLossLimitUnit"] == PNL_UNIT_UNDERLYING_POINTS


# ---------- exact daily-loss boundary ----------


@pytest.mark.parametrize(
    ("realized_pnl", "allowed"),
    [(-4999.99, True), (-5000.0, False), (-5000.01, False)],
)
def test_daily_loss_boundary_is_inclusive_at_exactly_the_limit(realized_pnl, allowed) -> None:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.record_trade(realized_pnl=realized_pnl, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW + timedelta(minutes=10))

    assert decision.allowed is allowed
    if not allowed:
        assert decision.reason == "Daily loss limit reached"


# ---------- entry blocked, exit allowed after the loss limit ----------


def test_entry_blocked_after_daily_loss_limit_via_run_cycle(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.record_trade(realized_pnl=-5000.0, now=TRADING_HOURS_NOW, is_exit=True)
    order_manager = OrderManager()

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=10),
    )

    assert result.event.kind == "ENTRY_CALL"
    assert result.order is None
    assert result.risk.reason == "Daily loss limit reached"
    assert order_manager.account.quantity == 0


def test_sell_is_not_blocked_by_daily_loss_limit() -> None:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.record_trade(realized_pnl=-6000.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("SELL", quantity=1, open_positions=1, now=TRADING_HOURS_NOW)

    assert decision.allowed is True


def test_normal_exit_still_fills_after_the_daily_loss_limit_is_breached(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    open_call_via_real_entry(monkeypatch, risk_manager, order_manager)
    entry_price = order_manager.account.average_price

    # Losses booked elsewhere (e.g. another index's account) push the shared
    # daily P&L past the limit while this position is still open.
    risk_manager.record_trade(realized_pnl=-5000.0, now=TRADING_HOURS_NOW, is_exit=True)

    patch_fetch(monkeypatch, UPTREND_60)
    exit_result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )

    assert exit_result.event.kind == "EXIT_TARGET"
    assert exit_result.risk.allowed is True
    assert exit_result.order.status == "PLACED"
    assert order_manager.account.quantity == 0
    # Paper P&L model itself is unchanged: underlying points x quantity,
    # exiting at the target level (exit_level), like the canonical backtest.
    assert exit_result.order.realized_pnl == pytest.approx(exit_result.event.exit_level - entry_price)


# ---------- max trades per day ----------


def test_exit_still_fills_when_max_trades_per_day_is_reached(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits(max_trades_per_day=1))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    open_call_via_real_entry(monkeypatch, risk_manager, order_manager)
    assert risk_manager.state.trades_today == 1  # the limit is now reached

    patch_fetch(monkeypatch, UPTREND_60)
    exit_result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5),
    )

    assert exit_result.event.kind == "EXIT_TARGET"
    assert exit_result.order.status == "PLACED"
    assert order_manager.account.quantity == 0


def test_new_entry_still_blocked_when_max_trades_per_day_is_reached() -> None:
    manager = RiskManager(RiskLimits(max_trades_per_day=1))
    manager.enable_auto_trading()
    manager.record_trade(now=TRADING_HOURS_NOW)

    assert manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW).reason == "Max trades per day reached"
    assert manager.check("SELL", 1, 1, now=TRADING_HOURS_NOW).allowed is True


# ---------- forced end-of-day square-off ----------


def test_eod_square_off_still_closes_when_loss_and_trade_limits_are_both_hit(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits(max_trades_per_day=1))
    risk_manager.enable_auto_trading()
    risk_manager.record_trade(realized_pnl=-5000.0, now=AT_SQUARE_OFF, is_exit=True)
    account = SimulatedAccount(quantity=2, average_price=100.0, side="CALL")
    order_manager = OrderManager(account)

    patch_fetch(monkeypatch, flat_df(price=95.0))
    result = run_cycle("nifty-50", "5m", risk_manager, order_manager, quantity=1, now=AT_SQUARE_OFF)

    assert result.event.kind == "SQUARE_OFF"
    assert result.order.status == "PLACED"
    assert account.quantity == 0
    assert account.square_off_date == "2026-09-23"
    # (95 - 100) x 2 underlying points, added onto the already-breached day.
    assert result.order.realized_pnl == pytest.approx(-10.0)
    assert risk_manager.state.realized_pnl_today == pytest.approx(-5010.0)
