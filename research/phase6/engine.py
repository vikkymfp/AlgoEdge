"""Phase 6 research simulator and metrics.

`simulate()` reproduces fno_signals.strategy.run()'s bar-by-bar state
machine exactly - using the canonical compute_indicators(),
compute_setups() and compute_session_filter() unchanged - and adds only
OPTIONAL, research-only behaviour on top, all OFF by default:

- an ADX / DI+ / DI- filter (the canonical strategy has none)
- paper Auto Trade's session rules (15:00 entry cutoff, 15:20 square-off),
  which the canonical backtest does not model
- alternative exit-fill assumptions (gap-aware, or the pre-Phase-6 paper
  bar-close fill)

With every option off, test_phase6.py asserts the events are identical
to fno_signals.strategy.run() (including its Phase 6 invalid-bar handling
and ATR warm-up guard). Nothing here is imported by production code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time, timedelta

import numpy as np
import pandas as pd

from algoedge.backtest import (
    BacktestTrade,
    compute_backtest_metrics,
    compute_regime_labels,
    split_train_validation_test,
)
from fno_signals.config import StrategyConfig
from fno_signals.indicators import round_to_strike
from fno_signals.strategy import (
    TradeEvent,
    compute_indicators,
    compute_session_filter,
    compute_setups,
    drop_invalid_bars,
)
from research.phase6.indicators_extra import dmi

EXIT_FILLS = ("level", "gap_aware", "bar_close")
IST_TZ = "Asia/Kolkata"


@dataclass(frozen=True)
class AdxFilter:
    di_length: int = 14
    adx_smoothing: int = 14
    threshold: float = 25.0
    require_di: bool = True  # CALL needs DI+ > DI-, PUT needs DI- > DI+
    # "gate": the canonical EMA/RSI/Supertrend setup edge still triggers the
    # entry, ADX/DI only vetoes it. "setup": ADX/DI is ANDed into the setup
    # itself (like the canonical VWAP option), so ADX crossing its threshold
    # can itself create a new setup edge.
    mode: str = "gate"


@dataclass(frozen=True)
class Variant:
    name: str
    config: StrategyConfig
    adx: AdxFilter | None = None
    entry_cutoff: time | None = None  # bar END time must be <= this for a new entry
    square_off: time | None = None  # close any open position on the bar whose END time >= this
    exit_fill: str = "level"
    # Mirrors the canonical run()'s warm-up guard (added in Phase 6): never
    # enter with a non-finite or non-positive SL/TP distance. False
    # reproduces the PRE-fix canonical behaviour (an entry during the ATR
    # warm-up opens a position that can never exit) for comparison only.
    guard_nan_risk: bool = True
    family: str = "baseline"
    notes: str = ""


def _bar_minutes(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 0.0
    return float(pd.Series(index).diff().dropna().dt.total_seconds().median() / 60)


def simulate(df: pd.DataFrame, variant: Variant, underlying_label: str = "INDEX") -> list[TradeEvent]:
    config = variant.config
    if variant.exit_fill not in EXIT_FILLS:
        raise ValueError(f"Unknown exit_fill: {variant.exit_fill}")
    df = drop_invalid_bars(df)  # identical to the canonical run()
    indicators = compute_indicators(df, config)
    bull_setup, bear_setup = compute_setups(indicators, config)
    in_session = compute_session_filter(df.index, config)

    adx_bull = adx_bear = None
    if variant.adx is not None:
        f = variant.adx
        di_plus, di_minus, adx = dmi(df["High"], df["Low"], df["Close"], f.di_length, f.adx_smoothing)
        strong = adx > f.threshold
        adx_bull = strong & ((di_plus > di_minus) if f.require_di else True)
        adx_bear = strong & ((di_minus > di_plus) if f.require_di else True)
        adx_bull, adx_bear = adx_bull.fillna(False).astype(bool), adx_bear.fillna(False).astype(bool)
        if f.mode == "setup":
            bull_setup, bear_setup = bull_setup & adx_bull, bear_setup & adx_bear
        elif f.mode != "gate":
            raise ValueError(f"Unknown ADX mode: {f.mode}")

    bull_prev = bull_setup.shift(1, fill_value=False)
    bear_prev = bear_setup.shift(1, fill_value=False)
    gate = variant.adx is not None and variant.adx.mode == "gate"

    local_index = df.index.tz_convert(config.session.timezone) if df.index.tz is not None else df.index
    bar_len = timedelta(minutes=_bar_minutes(df.index))
    bar_end_times = [(ts + bar_len).time() for ts in local_index]
    bar_dates = local_index.date

    n = len(df)
    open_ = df["Open"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    atr_values = indicators["atr"].to_numpy(dtype=float)

    pos = 0
    sl_price = tp_price = np.nan
    entry_date = None
    events: list[TradeEvent] = []

    def exit_event(i: int, kind: str, level: float) -> TradeEvent:
        return TradeEvent(
            timestamp=df.index[i], kind=kind, underlying_price=float(close[i]),
            option_symbol=None, stop_loss=None, target=None, exit_level=float(level),
        )

    for i in range(n):
        sl_hit = (pos == 1 and low[i] <= sl_price) or (pos == -1 and high[i] >= sl_price)
        tp_hit = (pos == 1 and high[i] >= tp_price) or (pos == -1 and low[i] <= tp_price)
        exit_now = pos != 0 and (sl_hit or tp_hit)

        forced = False
        if pos != 0 and not exit_now and variant.square_off is not None:
            # A new day while still open means the square-off bar was missing
            # from the data - close at that day's first open instead.
            if bar_dates[i] != entry_date:
                forced, forced_level = True, open_[i]
            elif bar_end_times[i] >= variant.square_off:
                forced, forced_level = True, close[i]

        can_enter = pos == 0 and bool(in_session.iloc[i])
        if can_enter and variant.entry_cutoff is not None:
            can_enter = bar_end_times[i] <= variant.entry_cutoff
        call_signal = can_enter and bool(bull_setup.iloc[i]) and not bool(bull_prev.iloc[i])
        put_signal = can_enter and bool(bear_setup.iloc[i]) and not bool(bear_prev.iloc[i])
        if gate:
            call_signal = call_signal and bool(adx_bull.iloc[i])
            put_signal = put_signal and bool(adx_bear.iloc[i])

        sl_dist = max(atr_values[i] * config.risk.sl_multiplier, config.risk.min_sl_points)
        tp_dist = sl_dist * (config.risk.tp_multiplier / config.risk.sl_multiplier)
        if variant.guard_nan_risk and not (np.isfinite(sl_dist) and np.isfinite(tp_dist) and sl_dist > 0 and tp_dist > 0):
            call_signal = put_signal = False

        if exit_now:
            level = sl_price if sl_hit else tp_price
            if variant.exit_fill == "gap_aware":
                # A bar that OPENS beyond the level fills at the open, not the level.
                if pos == 1:
                    level = min(level, open_[i]) if sl_hit else max(level, open_[i])
                else:
                    level = max(level, open_[i]) if sl_hit else min(level, open_[i])
            elif variant.exit_fill == "bar_close":
                level = close[i]
            events.append(exit_event(i, "EXIT_SL" if sl_hit else "EXIT_TARGET", level))
            pos, sl_price, tp_price = 0, np.nan, np.nan
        elif forced:
            events.append(exit_event(i, "SQUARE_OFF", forced_level))
            pos, sl_price, tp_price = 0, np.nan, np.nan
        elif call_signal or put_signal:
            pos = 1 if call_signal else -1
            sl_price = close[i] - pos * sl_dist
            tp_price = close[i] + pos * tp_dist
            entry_date = bar_dates[i]
            strike = round_to_strike(close[i], config.option.strike_step)
            right = "CE" if pos == 1 else "PE"
            events.append(TradeEvent(
                timestamp=df.index[i], kind="ENTRY_CALL" if pos == 1 else "ENTRY_PUT",
                underlying_price=float(close[i]), option_symbol=f"{underlying_label} {strike} {right}",
                stop_loss=float(sl_price), target=float(tp_price), exit_level=None,
                strike=strike, right=right,
            ))
    return events


def pair(events: list[TradeEvent], slippage_points: float = 0.0) -> list[BacktestTrade]:
    """algoedge.backtest.pair_trades() generalised to the research-only
    SQUARE_OFF exit kind. For SL/TARGET-only events it is identical to
    pair_trades() (asserted in test_phase6.py)."""
    reasons = {"EXIT_SL": "SL", "EXIT_TARGET": "TARGET", "SQUARE_OFF": "SQUARE_OFF"}
    trades: list[BacktestTrade] = []
    open_entry: TradeEvent | None = None
    for event in events:
        if event.kind in ("ENTRY_CALL", "ENTRY_PUT"):
            open_entry = event
        elif event.kind in reasons and open_entry is not None:
            direction = "CALL" if open_entry.kind == "ENTRY_CALL" else "PUT"
            level = event.exit_level
            if direction == "CALL":
                level -= slippage_points
                points = level - open_entry.underlying_price
            else:
                level += slippage_points
                points = open_entry.underlying_price - level
            trades.append(BacktestTrade(
                entry_time=open_entry.timestamp, exit_time=event.timestamp, direction=direction,
                entry_price=open_entry.underlying_price, exit_price=level, exit_reason=reasons[event.kind],
                points=points, strike=open_entry.strike or 0, option_symbol=open_entry.option_symbol or "",
            ))
            open_entry = None
    return trades


# ---------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Summary:
    trades: int
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    net_points: float
    max_drawdown: float
    max_consecutive_losses: int
    avg_winner: float | None
    avg_loser: float | None
    call_trades: int
    call_net: float
    call_win_rate: float | None
    put_trades: int
    put_net: float
    put_win_rate: float | None
    overnight_trades: int
    # Paper Auto Trade halts (manual reset required, never auto-reopens) after
    # RiskLimits.max_consecutive_losses (3) losing exits in a row. How many
    # times would this trade sequence have tripped that halt?
    halts_at_3_losses: int
    exit_reasons: dict = field(default_factory=dict)
    time_of_day: list = field(default_factory=list)
    regime: list = field(default_factory=list)


def summarize(trades: list[BacktestTrade], regime_labels: dict | None = None) -> Summary:
    m = compute_backtest_metrics(trades, regime_labels)
    winners = [t.points for t in trades if t.points > 0]
    losers = [t.points for t in trades if t.points < 0]
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    halts = streak = 0
    for t in sorted(trades, key=lambda t: t.entry_time):
        streak = streak + 1 if t.points < 0 else (streak if t.points == 0 else 0)
        if streak == 3:
            halts, streak = halts + 1, 0
    overnight = sum(1 for t in trades if pd.Timestamp(t.entry_time).date() != pd.Timestamp(t.exit_time).date())
    return Summary(
        trades=m.total_trades, win_rate=m.win_rate, profit_factor=m.profit_factor,
        expectancy=m.expectancy_points, net_points=m.net_points, max_drawdown=m.max_drawdown_points,
        max_consecutive_losses=m.max_consecutive_losses,
        avg_winner=float(np.mean(winners)) if winners else None,
        avg_loser=float(np.mean(losers)) if losers else None,
        call_trades=m.call_performance.trades, call_net=m.call_performance.net_points,
        call_win_rate=m.call_performance.win_rate,
        put_trades=m.put_performance.trades, put_net=m.put_performance.net_points,
        put_win_rate=m.put_performance.win_rate,
        overnight_trades=overnight, halts_at_3_losses=halts, exit_reasons=reasons,
        time_of_day=[vars(b) for b in m.time_of_day_performance],
        regime=[vars(b) for b in m.market_regime_performance],
    )


# ---------------------------------------------------------------- splits


SPLITS = ("train", "validation", "out_of_sample")


def production_splits(df: pd.DataFrame, variant: Variant, label: str, slippage: float = 0.0) -> dict[str, Summary]:
    """Exactly /api/backtest/run?split=true: chronological 60/20/20 by bar
    count, the strategy re-run from scratch on each slice (so each slice
    re-warms its indicators and a trade open at a slice boundary is lost)."""
    out = {}
    for name, part in split_train_validation_test(df).items():
        trades = pair(simulate(part, variant, label), slippage) if len(part) >= 10 else []
        out[name] = summarize(trades, compute_regime_labels(part))
    return out


def split_boundaries(df: pd.DataFrame) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    parts = split_train_validation_test(df)
    return {name: (part.index.min(), part.index.max()) for name, part in parts.items() if len(part)}


def continuous_splits(trades: list[BacktestTrade], df: pd.DataFrame, regime_labels: dict) -> dict[str, Summary]:
    """Same three chronological periods, but the strategy is run ONCE over
    the whole series and trades are assigned by entry time - no repeated
    indicator warm-up and no trade lost at a boundary."""
    out = {}
    for name, (start, end) in split_boundaries(df).items():
        out[name] = summarize([t for t in trades if start <= t.entry_time <= end], regime_labels)
    return out


# ---------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[dict]
    selected_oos: Summary
    baseline_oos: Summary


def _score(s: Summary, min_trades: int) -> float:
    if s.trades < min_trades or s.expectancy is None:
        return float("-inf")
    return s.expectancy


def walk_forward(
    df: pd.DataFrame,
    trades_by_variant: dict[str, list[BacktestTrade]],
    baseline_name: str,
    train_days: int = 20,
    test_days: int = 5,
    min_train_trades: int = 8,
) -> WalkForwardResult:
    """Rolling by trading day: in each window, pick the variant with the best
    TRAIN expectancy (minimum trade count, never win rate), then record how
    that pick did on the following, unseen TEST days. Trades come from one
    continuous run per variant (causal indicators), assigned by entry time."""
    local = df.index.tz_convert(IST_TZ) if df.index.tz is not None else df.index
    days = sorted(set(local.date))
    windows = []
    picked_oos: list[BacktestTrade] = []
    base_oos: list[BacktestTrade] = []
    start = 0

    def in_days(trades, day_set):
        return [t for t in trades if pd.Timestamp(t.entry_time).tz_convert(IST_TZ).date() in day_set]

    while start + train_days + test_days <= len(days):
        train_set = set(days[start:start + train_days])
        test_set = set(days[start + train_days:start + train_days + test_days])
        scores = {
            name: _score(summarize(in_days(trades, train_set)), min_train_trades)
            for name, trades in trades_by_variant.items()
        }
        best = max(scores, key=lambda k: (scores[k], k == baseline_name))
        if scores[best] == float("-inf"):
            best = baseline_name
        test_pick = in_days(trades_by_variant[best], test_set)
        test_base = in_days(trades_by_variant[baseline_name], test_set)
        picked_oos += test_pick
        base_oos += test_base
        windows.append({
            "train": f"{min(train_set)}..{max(train_set)}", "test": f"{min(test_set)}..{max(test_set)}",
            "picked": best, "picked_train_expectancy": scores[best],
            "picked_test_net": sum(t.points for t in test_pick),
            "baseline_test_net": sum(t.points for t in test_base),
        })
        start += test_days
    return WalkForwardResult(windows, summarize(picked_oos), summarize(base_oos))



# ---------------------------------------------------------------- helpers


def with_signal(config: StrategyConfig, **changes) -> StrategyConfig:
    return replace(config, signal=replace(config.signal, **changes))


def with_risk(config: StrategyConfig, **changes) -> StrategyConfig:
    return replace(config, risk=replace(config.risk, **changes))


def paper_session_variant(v: Variant, exit_fill: str = "level", suffix: str = "paper-session") -> Variant:
    """Adds paper Auto Trade's RiskLimits session rules (15:00 entry cutoff,
    15:20 square-off) to a variant - values taken from RiskLimits defaults."""
    from algoedge.risk_manager import RiskLimits

    limits = RiskLimits()
    return replace(
        v, name=f"{v.name} + {suffix}", entry_cutoff=limits.entry_cutoff,
        square_off=limits.square_off_time, exit_fill=exit_fill,
    )
