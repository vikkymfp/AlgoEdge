from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from algoedge import exit_reasons
from algoedge.indicators import ema, rsi


@dataclass(frozen=True)
class StrategyConfig:
    """Configurable parameters for the RSI-momentum strategy.

    Default values match the blueprint's example 5-minute strategy:
    entry when RSI > rsi_upper and price > EMA(ema_length); exit when
    RSI < rsi_lower, or stop loss / target is hit.
    """

    name: str = "RSI Momentum"
    rsi_length: int = 14
    rsi_lower: float = 45.0
    rsi_upper: float = 55.0
    ema_length: int = 20
    stop_loss_percent: float = 1.0
    target_percent: float = 2.0


DEFAULT_STRATEGY_CONFIG = StrategyConfig()


@dataclass(frozen=True)
class StrategySignal:
    action: str  # "BUY" | "SELL" | "HOLD"
    reason: str
    price: float
    rsi: float | None
    ema: float | None
    # Set only for SELL signals - the formal exit categorization from the
    # production-hardening spec (STOP_LOSS/TARGET/REVERSAL/...). Set
    # explicitly at the point the signal is generated rather than inferred
    # later by matching against `reason`'s free text, which would be
    # fragile and could silently miscategorize a reworded message.
    exit_reason: str | None = None


def evaluate(
    closes: pd.Series,
    config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    entry_price: float | None = None,
) -> StrategySignal:
    """Evaluate the strategy against a closing-price series.

    Generates a signal only; it never places an order. Pass `entry_price`
    to evaluate an exit for an already-open position, or leave it `None`
    to evaluate a fresh entry.
    """
    clean = closes.dropna()
    if len(clean) < max(config.rsi_length, config.ema_length) + 1:
        return StrategySignal("HOLD", "Not enough price history yet", float("nan"), None, None)

    price = float(clean.iloc[-1])
    current_rsi = float(rsi(clean, config.rsi_length).iloc[-1])
    current_ema = float(ema(clean, config.ema_length).iloc[-1])

    if entry_price is not None:
        stop_loss_price = entry_price * (1 - config.stop_loss_percent / 100)
        target_price = entry_price * (1 + config.target_percent / 100)
        if price <= stop_loss_price:
            return StrategySignal(
                "SELL", "Stop loss hit", price, current_rsi, current_ema, exit_reasons.STOP_LOSS
            )
        if price >= target_price:
            return StrategySignal(
                "SELL", "Target hit", price, current_rsi, current_ema, exit_reasons.TARGET
            )
        if current_rsi < config.rsi_lower:
            return StrategySignal(
                "SELL", f"RSI dropped below {config.rsi_lower}", price, current_rsi, current_ema,
                exit_reasons.STRATEGY_REVERSAL,
            )
        return StrategySignal("HOLD", "Position open, exit conditions not met", price, current_rsi, current_ema)

    if current_rsi > config.rsi_upper and price > current_ema:
        return StrategySignal(
            "BUY",
            f"RSI above {config.rsi_upper} and price above EMA{config.ema_length}",
            price,
            current_rsi,
            current_ema,
        )
    return StrategySignal("HOLD", "Entry conditions not met", price, current_rsi, current_ema)
