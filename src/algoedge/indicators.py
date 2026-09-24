from __future__ import annotations

import pandas as pd


def ema(closes: pd.Series, length: int) -> pd.Series:
    return closes.ewm(span=length, adjust=False).mean()


def rsi(closes: pd.Series, length: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0)


def macd(
    closes: pd.Series,
    fast_length: int = 12,
    slow_length: int = 26,
    signal_length: int = 9,
) -> pd.DataFrame:
    macd_line = ema(closes, fast_length) - ema(closes, slow_length)
    signal_line = macd_line.ewm(span=signal_length, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})


def vwap(candles: pd.DataFrame) -> pd.Series:
    typical_price = (candles["High"] + candles["Low"] + candles["Close"]) / 3
    cumulative_volume = candles["Volume"].cumsum()
    return (typical_price * candles["Volume"]).cumsum() / cumulative_volume.where(cumulative_volume != 0)
