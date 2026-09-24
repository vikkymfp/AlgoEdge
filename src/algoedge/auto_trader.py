from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from algoedge.market_pulse import get_index_candles
from algoedge.order_manager import OrderManager, OrderResult
from algoedge.risk_manager import RiskDecision, RiskManager
from algoedge.strategy_engine import (
    DEFAULT_STRATEGY_CONFIG,
    StrategyConfig,
    StrategySignal,
    evaluate,
)


@dataclass(frozen=True)
class AutoTradeCycleResult:
    signal: StrategySignal
    risk: RiskDecision
    order: OrderResult | None


def run_cycle(
    index_id: str,
    timeframe: str,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    quantity: int = 1,
    now: datetime | None = None,
    total_open_positions: int | None = None,
) -> AutoTradeCycleResult:
    """One full pass of the blueprint's end-to-end flow, minus the broker:

    Indicators -> Strategy Engine -> Signal -> Risk Manager -> Order Manager.

    `total_open_positions` lets a caller running several indices against
    independent `SimulatedAccount`s (one per index) enforce a single,
    global `max_open_positions` limit across all of them. Defaults to just
    this cycle's own account when not given, matching the original
    single-account behaviour.
    """
    account = order_manager.account
    entry_price = account.average_price if account.quantity > 0 else None
    own_open_position = 1 if account.quantity > 0 else 0
    open_positions = total_open_positions if total_open_positions is not None else own_open_position

    candles = get_index_candles(index_id, timeframe)
    closes = pd.Series([candle["close"] for candle in candles])
    signal = evaluate(closes, config, entry_price=entry_price)

    if signal.action not in {"BUY", "SELL"}:
        return AutoTradeCycleResult(signal, RiskDecision(False, "No actionable signal"), None)

    order_value = quantity * signal.price if signal.price is not None and not pd.isna(signal.price) else None
    decision = risk_manager.check(signal.action, quantity, open_positions, now=now, order_value=order_value)
    if not decision.allowed:
        return AutoTradeCycleResult(signal, decision, None)

    order_result = order_manager.place(signal.action, signal.price, quantity, index_id=index_id)
    if order_result.status == "PLACED":
        # SELL always closes the account's only long position in this
        # model (no short-selling path exists), so it's always the "exit"
        # side for consecutive-loss/cooldown tracking purposes.
        risk_manager.record_trade(
            realized_pnl=order_result.realized_pnl, now=now, is_exit=(signal.action == "SELL"),
        )
    return AutoTradeCycleResult(signal, decision, order_result)
