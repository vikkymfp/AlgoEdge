from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Signal:
    action: str
    price: float


def moving_average_signal(
    prices: pd.Series,
    short_window: int = 20,
    long_window: int = 50,
) -> Signal:
    if short_window >= long_window:
        raise ValueError("short_window must be less than long_window")
    if len(prices.dropna()) < long_window:
        raise ValueError("not enough prices for the configured windows")

    short_average = prices.rolling(short_window).mean().iloc[-1]
    long_average = prices.rolling(long_window).mean().iloc[-1]
    action = "buy" if short_average > long_average else "sell"
    return Signal(action=action, price=float(prices.iloc[-1]))
