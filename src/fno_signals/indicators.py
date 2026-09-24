from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average, matching Pine Script's ta.ema."""
    return series.ewm(span=length, adjust=False).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average, matching Pine Script's ta.rma exactly.

    ta.rma seeds its first output with the SMA of the first `length` values,
    then recurses with alpha = 1/length. This differs from a plain
    pandas .ewm(adjust=False), which seeds from the very first raw value —
    the two converge over time but disagree early in the series, and
    ta.atr / ta.rsi both use ta.rma internally, so the seed matters for a
    faithful translation.
    """
    values = series.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    if len(values) < length:
        return pd.Series(result, index=series.index)
    result[length - 1] = np.nanmean(values[:length])
    for i in range(length, len(values)):
        result[i] = (result[i - 1] * (length - 1) + values[i]) / length
    return pd.Series(result, index=series.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Average True Range, matching Pine Script's ta.atr (Wilder's RMA of true range)."""
    return rma(true_range(high, low, close), length)


def rsi(close: pd.Series, length: int) -> pd.Series:
    """Relative Strength Index, matching Pine Script's ta.rsi.

    Pine: rsi = down == 0 ? 100 : up == 0 ? 0 : 100 - (100 / (1 + up/down))
    — note down == 0 takes precedence even if up is also 0.
    """
    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        result = 100 - (100 / (1 + rs))
    result = result.where(avg_gain != 0, 0.0)
    result = result.where(avg_loss != 0, 100.0)  # applied last: down == 0 wins
    return result


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int,
    multiplier: float,
) -> tuple[pd.Series, pd.Series]:
    """Supertrend, matching Pine Script's built-in ta.supertrend(multiplier, length).

    Returns (supertrend_line, direction) where direction < 0 means uptrend
    (the line tracks the lower band) and direction > 0 means downtrend (the
    line tracks the upper band) — same convention as the Pine script.
    """
    atr_series = atr(high, low, close, length)
    hl2 = (high + low) / 2
    basic_upper = (hl2 + multiplier * atr_series).to_numpy(dtype=float)
    basic_lower = (hl2 - multiplier * atr_series).to_numpy(dtype=float)
    atr_values = atr_series.to_numpy(dtype=float)
    close_values = close.to_numpy(dtype=float)

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    for i in range(n):
        if i == 0 or np.isnan(atr_values[i - 1]):
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1.0
            line[i] = final_upper[i]
            continue

        final_upper[i] = (
            basic_upper[i]
            if (basic_upper[i] < final_upper[i - 1] or close_values[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower[i]
            if (basic_lower[i] > final_lower[i - 1] or close_values[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )

        if line[i - 1] == final_upper[i - 1]:
            direction[i] = -1.0 if close_values[i] > final_upper[i] else 1.0
        else:
            direction[i] = 1.0 if close_values[i] < final_lower[i] else -1.0

        line[i] = final_lower[i] if direction[i] == -1.0 else final_upper[i]

    index = close.index
    return pd.Series(line, index=index), pd.Series(direction, index=index)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP, matching Pine Script's ta.vwap(hlc3) default
    behaviour of resetting at the start of each new trading day.

    Requires a tz-aware DatetimeIndex (yfinance's intraday history for NSE
    tickers already comes back localized to Asia/Kolkata).

    NOTE: yfinance reports Volume as 0 for pure index tickers (^NSEI,
    ^NSEBANK, ^BSESN), so this will be NaN for an index-only feed — the
    Pine script's `close > vw` / `close < vw` checks then behave like Pine's
    na comparisons (always false), which safely disables the VWAP filter
    rather than producing a misleading result. A real underlying with
    traded volume (e.g. the futures contract or an equity) is required for
    this filter to actually do anything.
    """
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    session_date = df.index.date
    price_volume = typical_price * df["Volume"]
    cumulative_pv = price_volume.groupby(session_date).cumsum()
    cumulative_volume = df["Volume"].groupby(session_date).cumsum()
    return cumulative_pv / cumulative_volume.replace(0, np.nan)


def round_to_strike(price: float, step: int) -> int:
    """Nearest strike, matching Pine Script's math.round(close / strikeStep) * strikeStep
    (round-half-away-from-zero; safe here since prices are always positive)."""
    return int(np.floor(price / step + 0.5) * step)
