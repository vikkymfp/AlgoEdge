import inspect
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager
from fno_signals import strategy as strategy_module
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import TradeEvent
from fno_signals.strategy import run as run_strategy

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=IST)


def fake_resolve_contract(event: TradeEvent) -> OptionContract:
    """A stand-in for Phase 5's real instrument-master resolution - this
    file (Phase 1-3's own orchestration tests) isn't about resolution
    itself (see tests/test_option_contract.py / tests/test_auto_trade_
    contract_resolution.py for that), so every entry resolves cleanly by
    default here."""
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}",
        underlying="NIFTY", right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def run_cycle(*args, **kwargs):
    kwargs.setdefault("resolve_contract_fn", fake_resolve_contract)
    return auto_trader.run_cycle(*args, **kwargs)


def make_df(n: int = 5) -> pd.DataFrame:
    """A minimal OHLCV DataFrame shaped like fno_signals.main.fetch_underlying_data()'s
    real return value - what run_cycle() now requires instead of
    market_pulse's Close-only candle dicts."""
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0] * n
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [0.0] * n},
        index=index,
    )


def call_entry_event(price: float = 100.0, timestamp: str = "2026-09-23 10:00") -> TradeEvent:
    return TradeEvent(
        timestamp=pd.Timestamp(timestamp, tz="Asia/Kolkata"),
        kind="ENTRY_CALL", underlying_price=price, option_symbol="NIFTY 50 100 CE",
        stop_loss=price - 15.0, target=price + 45.0, exit_level=None, strike=100, right="CE",
    )


def put_entry_event(price: float = 100.0, timestamp: str = "2026-09-23 10:00") -> TradeEvent:
    return TradeEvent(
        timestamp=pd.Timestamp(timestamp, tz="Asia/Kolkata"),
        kind="ENTRY_PUT", underlying_price=price, option_symbol="NIFTY 50 100 PE",
        stop_loss=price + 15.0, target=price - 45.0, exit_level=None, strike=100, right="PE",
    )


def exit_event(kind: str, price: float, timestamp: str = "2026-09-23 10:05") -> TradeEvent:
    return TradeEvent(
        timestamp=pd.Timestamp(timestamp, tz="Asia/Kolkata"),
        kind=kind, underlying_price=price, option_symbol=None,
        stop_loss=None, target=None, exit_level=price,
    )


def patch_data_and_events(monkeypatch, events: list[TradeEvent]) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: make_df())
    monkeypatch.setattr(auto_trader, "run_strategy", lambda *_a, **_kw: (None, events))


def test_no_events_produces_no_risk_check_or_order(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle("nifty-50", "5m", risk_manager, order_manager)

    assert result.order is None
    assert result.event is None
    assert result.risk.reason == "No actionable signal"


def test_call_entry_blocked_when_auto_trading_disabled(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [call_entry_event()])
    risk_manager = RiskManager()  # auto trading NOT enabled
    order_manager = OrderManager()

    result = run_cycle("nifty-50", "5m", risk_manager, order_manager)

    assert result.risk.allowed is False
    assert result.order is None
    assert order_manager.account.quantity == 0


def test_call_entry_places_order_and_opens_call_position(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [call_entry_event(100.0)])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=2, now=TRADING_HOURS_NOW
    )

    assert result.risk.allowed is True
    assert result.order.status == "PLACED"
    assert result.event.kind == "ENTRY_CALL"
    assert order_manager.account.side == "CALL"
    assert order_manager.account.quantity == 2
    assert order_manager.account.average_price == pytest.approx(100.0)
    assert risk_manager.state.trades_today == 1


def test_put_entry_places_order_and_opens_put_position(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [put_entry_event(100.0)])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=3, now=TRADING_HOURS_NOW
    )

    assert result.risk.allowed is True
    assert result.order.status == "PLACED"
    assert result.event.kind == "ENTRY_PUT"
    assert order_manager.account.side == "PUT"
    assert order_manager.account.quantity == 3
    assert order_manager.account.average_price == pytest.approx(100.0)


def test_call_exit_realizes_profit_when_price_rises(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [exit_event("EXIT_TARGET", 110.0)])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    account = SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0, side="CALL")
    order_manager = OrderManager(account)

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW
    )

    assert result.order.status == "PLACED"
    assert result.order.realized_pnl == pytest.approx(100.0)
    assert order_manager.account.quantity == 0
    assert order_manager.account.side is None


def test_put_exit_realizes_profit_when_price_falls(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [exit_event("EXIT_TARGET", 90.0)])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    account = SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0, side="PUT")
    order_manager = OrderManager(account)

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW
    )

    assert result.order.status == "PLACED"
    # PUT profits when price falls - a $10 drop on 10 units is +100, the
    # mirror image of the CALL case above, not a loss.
    assert result.order.realized_pnl == pytest.approx(100.0)
    assert order_manager.account.quantity == 0
    assert order_manager.account.side is None


def test_put_exit_realizes_loss_when_price_rises(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [exit_event("EXIT_SL", 110.0)])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    account = SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0, side="PUT")
    order_manager = OrderManager(account)

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW
    )

    assert result.order.realized_pnl == pytest.approx(-100.0)


def test_entry_event_carries_the_strategys_own_sl_and_target_unchanged(monkeypatch) -> None:
    # The old strategy_engine 1%/2% percent calculation must be gone -
    # run_cycle must pass the canonical strategy's own ATR-derived
    # stop_loss/target straight through on the event, not recompute them.
    event = call_entry_event(100.0)
    assert event.stop_loss == pytest.approx(85.0)
    assert event.target == pytest.approx(145.0)
    patch_data_and_events(monkeypatch, [event])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    assert result.event.stop_loss == pytest.approx(85.0)
    assert result.event.target == pytest.approx(145.0)


def test_total_open_positions_overrides_own_account_for_global_risk_cap(monkeypatch) -> None:
    # max_open_positions defaults to 1. This index's own account is flat,
    # but another index's account already holds a position - the global
    # cap must still block a new entry.
    patch_data_and_events(monkeypatch, [call_entry_event()])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()  # flat

    result = run_cycle(
        "sensex", "5m", risk_manager, order_manager,
        quantity=1, now=TRADING_HOURS_NOW, total_open_positions=1,
    )

    assert result.risk.allowed is False
    assert result.risk.reason == "Max open positions reached"
    assert result.order is None


def test_entry_order_value_over_the_limit_is_blocked(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [call_entry_event(100.0)])
    risk_manager = RiskManager(RiskLimits(max_order_value=500.0))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW,
    )  # 10 * price(100.0) = 1000, over the 500 limit

    assert result.risk.allowed is False
    assert "order value" in result.risk.reason.lower()
    assert result.order is None


def test_a_losing_exit_starts_a_cooldown_that_blocks_the_next_entry(monkeypatch) -> None:
    patch_data_and_events(monkeypatch, [exit_event("EXIT_SL", 90.0)])
    risk_manager = RiskManager(RiskLimits(cooldown_minutes=5))
    risk_manager.enable_auto_trading()
    order_manager = OrderManager(SimulatedAccount(cash=100_000.0, quantity=10, average_price=100.0, side="CALL"))

    exit_result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=10, now=TRADING_HOURS_NOW,
    )
    assert exit_result.order.status == "PLACED"
    assert risk_manager.state.last_exit_at == TRADING_HOURS_NOW

    patch_data_and_events(monkeypatch, [call_entry_event(90.0, timestamp="2026-09-23 10:10")])
    reentry_result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=1),
    )

    assert reentry_result.risk.allowed is False
    assert "cooldown" in reentry_result.risk.reason.lower()


def test_unsupported_index_id_is_rejected(monkeypatch) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    with pytest.raises(ValueError, match="Unsupported index_id"):
        run_cycle("dow-jones", "5m", risk_manager, order_manager)


def test_auto_trader_module_never_calls_the_live_broker() -> None:
    # Auto Trade must stay paper-only - it should have no import of the
    # live Groww broker client/token service, only the canonical (offline,
    # yfinance-backed) strategy and a SimulatedAccount. Checking imports
    # specifically (not a blunt text search) so this doesn't false-positive
    # on a docstring/comment that merely mentions Groww by name.
    import_lines = [
        line for line in inspect.getsource(auto_trader).splitlines()
        if line.startswith(("import ", "from "))
    ]
    joined = "\n".join(import_lines).lower()
    assert "groww" not in joined
    assert "tokenservice" not in joined


def test_signal_parity_between_auto_trade_and_the_canonical_strategy(monkeypatch) -> None:
    """The critical parity requirement: given identical OHLCV input, Auto
    Trade must consume the exact same strategy event as a direct call to
    fno_signals.strategy.run() - same kind, price, stop_loss and target."""
    bull = [False, True, True]
    bear = [False, False, False]

    def fake_compute_indicators(df, config):
        result = df.copy()
        result["atr"] = 10.0
        result["ema_fast"] = df["Close"]
        result["ema_slow"] = df["Close"]
        result["rsi"] = 50.0
        result["supertrend"] = df["Close"]
        result["supertrend_dir"] = 0.0
        result["vwap"] = df["Close"]
        return result

    def fake_compute_setups(indicators, config):
        return pd.Series(bull, index=indicators.index), pd.Series(bear, index=indicators.index)

    def fake_session_filter(index, config):
        return pd.Series([True] * len(bull), index=index)

    monkeypatch.setattr(strategy_module, "compute_indicators", fake_compute_indicators)
    monkeypatch.setattr(strategy_module, "compute_setups", fake_compute_setups)
    monkeypatch.setattr(strategy_module, "compute_session_filter", fake_session_filter)

    df = make_df(3)
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)

    index_config = INDEX_MAP[1]  # "nifty-50" -> choice 1, per auto_trader._INDEX_CHOICE
    expected_config = strategy_config_for(index_config)
    _expected_results, expected_events = run_strategy(df, expected_config, index_config.name)
    assert expected_events, "test fixture must actually produce a signal to be meaningful"
    expected = expected_events[-1]

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW
    )

    assert result.event.kind == expected.kind
    assert result.event.underlying_price == pytest.approx(expected.underlying_price)
    assert result.event.stop_loss == pytest.approx(expected.stop_loss)
    assert result.event.target == pytest.approx(expected.target)
    assert result.event.option_symbol == expected.option_symbol
