from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from fno_signals.config import StrategyConfig
from fno_signals.indicators import atr, ema, round_to_strike, rsi, session_vwap, supertrend


@dataclass(frozen=True)
class TradeEvent:
    """A single entry or exit, in the shape the "Execution Output" needs."""

    timestamp: pd.Timestamp
    kind: str  # "ENTRY_CALL" | "ENTRY_PUT" | "EXIT_SL" | "EXIT_TARGET"
    underlying_price: float
    option_symbol: str | None  # e.g. "NIFTY 24500 CE" — None for exits
    stop_loss: float | None
    target: float | None
    exit_level: float | None
    strike: int | None = None  # ATM strike — None for exits
    right: str | None = None  # "CE" | "PE" — None for exits


def compute_indicators(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    result = df.copy()
    result["ema_fast"] = ema(df["Close"], config.signal.ema_fast_length)
    result["ema_slow"] = ema(df["Close"], config.signal.ema_slow_length)
    result["rsi"] = rsi(df["Close"], config.signal.rsi_length)
    result["atr"] = atr(df["High"], df["Low"], df["Close"], config.risk.atr_length)
    # Supertrend uses its own ATR length (stLen), independent of the risk ATR above.
    st_line, st_dir = supertrend(
        df["High"], df["Low"], df["Close"],
        config.signal.supertrend_length, config.signal.supertrend_multiplier,
    )
    result["supertrend"] = st_line
    result["supertrend_dir"] = st_dir
    result["vwap"] = session_vwap(df)
    return result


def compute_setups(indicators: pd.DataFrame, config: StrategyConfig) -> tuple[pd.Series, pd.Series]:
    close = indicators["Close"]
    if config.signal.use_vwap:
        vwap_ok_bull = close > indicators["vwap"]
        vwap_ok_bear = close < indicators["vwap"]
    else:
        vwap_ok_bull = pd.Series(True, index=close.index)
        vwap_ok_bear = pd.Series(True, index=close.index)

    bull_setup = (
        (indicators["ema_fast"] > indicators["ema_slow"])
        & (indicators["rsi"] > config.signal.rsi_bull)
        & (indicators["supertrend_dir"] < 0)
        & vwap_ok_bull
    )
    bear_setup = (
        (indicators["ema_fast"] < indicators["ema_slow"])
        & (indicators["rsi"] < config.signal.rsi_bear)
        & (indicators["supertrend_dir"] > 0)
        & vwap_ok_bear
    )
    return bull_setup.fillna(False), bear_setup.fillna(False)


def compute_session_filter(index: pd.DatetimeIndex, config: StrategyConfig) -> pd.Series:
    local_index = (
        index.tz_convert(config.session.timezone)
        if index.tz is not None
        else index.tz_localize(config.session.timezone)
    )
    in_session = [
        config.session.start <= t <= config.session.end for t in local_index.time
    ]
    return pd.Series(in_session, index=index)


def run(
    df: pd.DataFrame,
    config: StrategyConfig,
    underlying_label: str,
) -> tuple[pd.DataFrame, list[TradeEvent]]:
    """Runs the full bar-by-bar state machine, matching the Pine script's
    `var` position tracker exactly: every condition on a given bar (exits,
    entries) is evaluated against the position as it stood at the END of the
    PREVIOUS bar, and only then is the state updated — an exit on bar i can
    never be immediately followed by a same-bar re-entry, matching Pine's
    single-pass execution order.
    """
    indicators = compute_indicators(df, config)
    bull_setup, bear_setup = compute_setups(indicators, config)
    in_session = compute_session_filter(df.index, config)
    bull_prev = bull_setup.shift(1, fill_value=False)
    bear_prev = bear_setup.shift(1, fill_value=False)

    n = len(df)
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    atr_values = indicators["atr"].to_numpy(dtype=float)

    pos = 0
    entry_price = np.nan
    sl_price = np.nan
    tp_price = np.nan

    out_pos = np.zeros(n, dtype=int)
    out_call_signal = np.zeros(n, dtype=bool)
    out_put_signal = np.zeros(n, dtype=bool)
    out_exit_now = np.zeros(n, dtype=bool)
    out_exit_reason: list[str | None] = [None] * n
    out_exit_level = np.full(n, np.nan)
    out_entry_price = np.full(n, np.nan)
    out_sl_price = np.full(n, np.nan)
    out_tp_price = np.full(n, np.nan)
    out_atm_strike = np.zeros(n, dtype=int)

    events: list[TradeEvent] = []

    for i in range(n):
        sl_hit = (pos == 1 and low[i] <= sl_price) or (pos == -1 and high[i] >= sl_price)
        tp_hit = (pos == 1 and high[i] >= tp_price) or (pos == -1 and low[i] <= tp_price)
        exit_now = pos != 0 and (sl_hit or tp_hit)
        exit_level = np.nan
        exit_reason: str | None = None
        if exit_now:
            exit_reason = "SL" if sl_hit else "TARGET"
            exit_level = sl_price if sl_hit else tp_price

        can_enter = pos == 0 and bool(in_session.iloc[i])
        call_signal = can_enter and bool(bull_setup.iloc[i]) and not bool(bull_prev.iloc[i])
        put_signal = can_enter and bool(bear_setup.iloc[i]) and not bool(bear_prev.iloc[i])

        atm_strike = round_to_strike(close[i], config.option.strike_step)
        sl_dist = max(atr_values[i] * config.risk.sl_multiplier, config.risk.min_sl_points)
        tp_dist = sl_dist * (config.risk.tp_multiplier / config.risk.sl_multiplier)

        out_exit_now[i] = exit_now
        out_exit_reason[i] = exit_reason
        out_exit_level[i] = exit_level
        out_call_signal[i] = call_signal
        out_put_signal[i] = put_signal
        out_atm_strike[i] = atm_strike

        if exit_now:
            events.append(TradeEvent(
                timestamp=df.index[i],
                kind="EXIT_SL" if exit_reason == "SL" else "EXIT_TARGET",
                underlying_price=float(close[i]),
                option_symbol=None,
                stop_loss=None,
                target=None,
                exit_level=float(exit_level),
            ))
            pos, entry_price, sl_price, tp_price = 0, np.nan, np.nan, np.nan
        elif call_signal:
            pos = 1
            entry_price = close[i]
            sl_price = close[i] - sl_dist
            tp_price = close[i] + tp_dist
            events.append(TradeEvent(
                timestamp=df.index[i],
                kind="ENTRY_CALL",
                underlying_price=float(close[i]),
                option_symbol=f"{underlying_label} {atm_strike} CE",
                stop_loss=float(sl_price),
                target=float(tp_price),
                exit_level=None,
                strike=atm_strike,
                right="CE",
            ))
        elif put_signal:
            pos = -1
            entry_price = close[i]
            sl_price = close[i] + sl_dist
            tp_price = close[i] - tp_dist
            events.append(TradeEvent(
                timestamp=df.index[i],
                kind="ENTRY_PUT",
                underlying_price=float(close[i]),
                option_symbol=f"{underlying_label} {atm_strike} PE",
                stop_loss=float(sl_price),
                target=float(tp_price),
                exit_level=None,
                strike=atm_strike,
                right="PE",
            ))

        out_pos[i] = pos
        out_entry_price[i] = entry_price
        out_sl_price[i] = sl_price
        out_tp_price[i] = tp_price

    results = indicators.copy()
    results["bull_setup"] = bull_setup
    results["bear_setup"] = bear_setup
    results["in_session"] = in_session
    results["pos"] = out_pos
    results["call_signal"] = out_call_signal
    results["put_signal"] = out_put_signal
    results["exit_now"] = out_exit_now
    results["exit_reason"] = out_exit_reason
    results["exit_level"] = out_exit_level
    results["entry_price"] = out_entry_price
    results["sl_price"] = out_sl_price
    results["tp_price"] = out_tp_price
    results["atm_strike"] = out_atm_strike

    return results, events
