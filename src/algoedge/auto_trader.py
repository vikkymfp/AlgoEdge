from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from algoedge.market_pulse import TIMEFRAMES
from algoedge.order_manager import OrderManager, OrderResult
from algoedge.risk_manager import RiskDecision, RiskManager
from fno_signals.config import INDEX_MAP, StrategyConfig, strategy_config_for
from fno_signals.main import fetch_underlying_data
from fno_signals.strategy import TradeEvent
from fno_signals.strategy import run as run_strategy

# Maps algoedge's kebab-case index ids (algoedge.market_pulse.INDEX_DEFINITIONS,
# used throughout the dashboard/web_server) to fno_signals' numeric IndexConfig
# choice (fno_signals.config.INDEX_MAP) - the same three underlyings, under
# two different existing key schemes that predate this module sharing a
# strategy engine with fno_signals.
_INDEX_CHOICE = {"nifty-50": 1, "bank-nifty": 2, "sensex": 3}

# fno_signals.strategy.TradeEvent.kind values that represent an entry
# (opening a paper CALL/PUT position) vs an exit (closing one).
_ENTRY_KINDS = {"ENTRY_CALL", "ENTRY_PUT"}
_EXIT_KINDS = {"EXIT_SL", "EXIT_TARGET"}


@dataclass(frozen=True)
class AutoTradeCycleResult:
    event: TradeEvent | None
    risk: RiskDecision
    order: OrderResult | None


def run_cycle(
    index_id: str,
    timeframe: str,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    config: StrategyConfig | None = None,
    quantity: int = 1,
    now: datetime | None = None,
    total_open_positions: int | None = None,
) -> AutoTradeCycleResult:
    """One full pass of Auto Trading's paper flow, using the exact same
    signal engine as Backtest and `fno_signals --live`:

    fno_signals.strategy.run() -> Risk Manager -> Order Manager (paper only).

    Auto Trading never places a real Groww order - `OrderManager` here is
    always wired to a `SimulatedAccount`. Using the canonical strategy
    means Auto Trade's paper fills are directly comparable to Backtest's
    results for the same OHLCV window (see the signal-parity test in
    tests/test_auto_trader.py) - previously Auto Trade ran a separate,
    simpler RSI+EMA long-only strategy (`algoedge.strategy_engine`) that
    shared no code with Backtest/Pine and couldn't represent PUT signals
    at all.

    `total_open_positions` lets a caller running several indices against
    independent `SimulatedAccount`s (one per index) enforce a single,
    global `max_open_positions` limit across all of them. Defaults to just
    this cycle's own account when not given, matching the original
    single-account behaviour.
    """
    if index_id not in _INDEX_CHOICE:
        raise ValueError(f"Unsupported index_id for Auto Trade: {index_id}")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe for Auto Trade: {timeframe}")

    index_config = INDEX_MAP[_INDEX_CHOICE[index_id]]
    strategy_config = config or strategy_config_for(index_config)

    period, interval = TIMEFRAMES[timeframe]
    data = fetch_underlying_data(index_config.ticker, period=period, interval=interval)
    _results, events = run_strategy(data, strategy_config, underlying_label=index_config.name)

    account = order_manager.account
    own_open_position = 1 if account.quantity > 0 else 0
    open_positions = total_open_positions if total_open_positions is not None else own_open_position

    if not events:
        return AutoTradeCycleResult(None, RiskDecision(False, "No actionable signal"), None)

    event = events[-1]

    # RiskManager.check()/record_trade() only understand BUY/SELL (kept
    # unchanged, per the canonical-strategy unification's own scope) - an
    # entry (CALL or PUT) always "opens"/uses capital like a BUY, an exit
    # (SL or target) always "closes"/realizes P&L like a SELL. The actual
    # CALL/PUT accounting happens in OrderManager.place_event() below,
    # driven by event.kind directly, not this simplification.
    action = "BUY" if event.kind in _ENTRY_KINDS else "SELL"
    order_value = quantity * event.underlying_price
    decision = risk_manager.check(action, quantity, open_positions, now=now, order_value=order_value)
    if not decision.allowed:
        return AutoTradeCycleResult(event, decision, None)

    order_result = order_manager.place_event(event.kind, event.underlying_price, quantity, index_id=index_id)
    if order_result.status == "PLACED":
        is_exit = event.kind in _EXIT_KINDS
        risk_manager.record_trade(realized_pnl=order_result.realized_pnl, now=now, is_exit=is_exit)
    return AutoTradeCycleResult(event, decision, order_result)
