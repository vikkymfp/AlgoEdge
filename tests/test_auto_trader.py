from datetime import datetime, timedelta

import pytest

from algoedge import auto_trader
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager
from algoedge.strategy_engine import StrategySignal

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=IST)


def make_candles() -> list[dict]:
    return [{"time": i, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for i in range(5)]


def test_hold_signal_produces_no_risk_check_or_order(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("HOLD", "no edge", 100.0, 50.0, 100.0)
    )
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager)

    assert result.order is None
    assert result.risk.reason == "No actionable signal"


def test_buy_signal_blocked_when_auto_trading_disabled(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("BUY", "entry", 100.0, 60.0, 90.0)
    )
    risk_manager = RiskManager()  # auto trading NOT enabled
    order_manager = OrderManager()

    result = auto_trader.run_cycle("nifty-50", "5m", risk_manager, order_manager)

    assert result.risk.allowed is False
    assert result.order is None
    assert order_manager.account.quantity == 0


def test_buy_signal_places_order_and_records_trade_when_allowed(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("BUY", "entry", 100.0, 60.0, 90.0)
    )
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=2, now=TRADING_HOURS_NOW
    )

    assert result.risk.allowed is True
    assert result.order.status == "PLACED"
    assert order_manager.account.quantity == 2
    assert risk_manager.state.trades_today == 1


def test_total_open_positions_overrides_own_account_for_global_risk_cap(monkeypatch) -> None:
    # max_open_positions defaults to 1. This index's own account is flat,
    # but another index's account already holds a position - the global
    # cap must still block a new BUY.
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("BUY", "entry", 100.0, 60.0, 90.0)
    )
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()  # flat

    result = auto_trader.run_cycle(
        "sensex", "5m", risk_manager, order_manager,
        quantity=1, now=TRADING_HOURS_NOW, total_open_positions=1,
    )

    assert result.risk.allowed is False
    assert result.risk.reason == "Max open positions reached"
    assert result.order is None


def test_exit_evaluation_uses_open_position_average_price(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    captured_entry_price = {}

    def fake_evaluate(_closes, _config, entry_price=None):
        captured_entry_price["value"] = entry_price
        return StrategySignal("SELL", "exit", 90.0, 40.0, 95.0)

    monkeypatch.setattr(auto_trader, "evaluate", fake_evaluate)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager(SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0))

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW
    )

    assert captured_entry_price["value"] == pytest.approx(100.0)
    assert result.order.status == "PLACED"
    assert result.order.realized_pnl == pytest.approx(-100.0)
    assert order_manager.account.quantity == 0


def test_buy_order_value_over_the_limit_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("BUY", "entry", 100.0, 60.0, 90.0)
    )
    risk_manager = RiskManager(RiskLimits(max_order_value=500.0))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW,
    )  # 10 * price(100.0) = 1000, over the 500 limit

    assert result.risk.allowed is False
    assert "order value" in result.risk.reason.lower()
    assert result.order is None


def test_a_losing_exit_starts_a_cooldown_that_blocks_the_next_entry(monkeypatch) -> None:
    monkeypatch.setattr(auto_trader, "get_index_candles", lambda *_a, **_kw: make_candles())
    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("SELL", "exit", 90.0, 40.0, 95.0)
    )
    risk_manager = RiskManager(RiskLimits(cooldown_minutes=5))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager(SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0))

    exit_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW,
    )
    assert exit_result.order.status == "PLACED"
    assert risk_manager.state.last_exit_at == TRADING_HOURS_NOW

    monkeypatch.setattr(
        auto_trader, "evaluate", lambda *_a, **_kw: StrategySignal("BUY", "re-entry", 90.0, 60.0, 80.0)
    )
    reentry_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=1),
    )

    assert reentry_result.risk.allowed is False
    assert "cooldown" in reentry_result.risk.reason.lower()
