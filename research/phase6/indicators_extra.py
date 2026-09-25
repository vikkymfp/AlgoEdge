"""Phase 6 research-only indicators. NOT used by the canonical strategy.

ADX / DI+ / DI- do not exist anywhere in the canonical strategy
(fno_signals.strategy / fno_signals.indicators); they are implemented here
only so their value as an additional filter can be measured.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fno_signals.indicators import true_range


def pine_rma(series: pd.Series, length: int) -> pd.Series:
    """Pine's ta.rma: seeded with ta.sma(src, length) - which is na until
    `length` consecutive non-na values exist - then alpha = 1/length.

    Unlike fno_signals.indicators.rma (which seeds from nanmean() of the
    first `length` raw values), this handles a series whose first valid
    value comes late, as DX does: DX only exists once the DI smoothing has
    warmed up, so seeding from a window that is mostly NaN would never
    produce a value at all.
    """
    values = series.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    valid_run = 0
    seeded = False
    for i, value in enumerate(values):
        if not seeded:
            valid_run = valid_run + 1 if not np.isnan(value) else 0
            if valid_run >= length:
                result[i] = values[i - length + 1:i + 1].mean()
                seeded = True
            continue
        result[i] = (result[i - 1] * (length - 1) + value) / length if not np.isnan(value) else result[i - 1]
    return pd.Series(result, index=series.index)


def dmi(
    high: pd.Series, low: pd.Series, close: pd.Series, di_length: int = 14, adx_smoothing: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (di_plus, di_minus, adx), matching Pine's ta.dmi(diLength, adxSmoothing)."""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm[up.isna()] = np.nan
    minus_dm[down.isna()] = np.nan

    tr_rma = pine_rma(true_range(high, low, close), di_length)
    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = (100 * pine_rma(plus_dm, di_length) / tr_rma).ffill()  # Pine fixnan()
        di_minus = (100 * pine_rma(minus_dm, di_length) / tr_rma).ffill()
        total = di_plus + di_minus
        dx = (di_plus - di_minus).abs() / total.where(total != 0, 1.0)
    adx = 100 * pine_rma(dx, adx_smoothing)
    return di_plus, di_minus, adx
