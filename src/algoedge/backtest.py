from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from fno_signals.strategy import TradeEvent

# IMPORTANT LIMITATION, stated up front rather than buried: this backtest
# operates on the UNDERLYING's own price (NIFTY/BANK NIFTY/SENSEX spot),
# in POINTS - not on real option premiums. This account's Groww tier has
# no historical option-quote access (the same constraint documented for
# the live liquidity check and slippage tracking in earlier phases), so a
# rupee P&L figure would require inventing an assumed premium-per-point
# multiplier - option Greeks (delta, theta decay, IV changes) make that a
# genuinely unreliable approximation, not a real one. Points-based results
# are still a real, honest diagnostic of the STRATEGY LOGIC's historical
# entry/exit timing - they are NOT validated real-money option-trading
# profitability. Never present one as the other.


@dataclass(frozen=True)
class BacktestTrade:
    entry_time: Any
    exit_time: Any
    direction: str  # "CALL" | "PUT"
    entry_price: float  # underlying price at entry
    exit_price: float  # underlying price at exit (after any assumed slippage)
    exit_reason: str  # "SL" | "TARGET"
    points: float  # signed points gained/lost on the UNDERLYING - not a rupee option P&L
    strike: int
    option_symbol: str


def pair_trades(events: list[TradeEvent], assumed_slippage_points: float = 0.0) -> list[BacktestTrade]:
    """Pairs each ENTRY event with the EXIT event that follows it into a
    round-trip trade. `fno_signals.strategy.run()`'s state machine already
    guarantees exactly one open position at a time and an exit always
    precedes the next entry, so a simple "most recent open entry" pairing
    is correct here - no separate position-tracking needed.

    `assumed_slippage_points` (points, not rupees) worsens every exit by
    that amount - a real, definable concept even without option-premium
    data (execution rarely happens at the exact SL/target level). Zero by
    default; the caller decides what's realistic for a given index/
    timeframe rather than this function guessing one.
    """
    trades: list[BacktestTrade] = []
    open_entry: TradeEvent | None = None

    for event in events:
        if event.kind in ("ENTRY_CALL", "ENTRY_PUT"):
            open_entry = event
        elif event.kind in ("EXIT_SL", "EXIT_TARGET") and open_entry is not None:
            direction = "CALL" if open_entry.kind == "ENTRY_CALL" else "PUT"
            exit_level = event.exit_level
            # Slippage always makes the exit worse, regardless of direction.
            if direction == "CALL":
                exit_level -= assumed_slippage_points
                points = exit_level - open_entry.underlying_price
            else:
                exit_level += assumed_slippage_points
                points = open_entry.underlying_price - exit_level
            trades.append(BacktestTrade(
                entry_time=open_entry.timestamp, exit_time=event.timestamp, direction=direction,
                entry_price=open_entry.underlying_price, exit_price=exit_level,
                exit_reason="SL" if event.kind == "EXIT_SL" else "TARGET",
                points=points, strike=open_entry.strike or 0, option_symbol=open_entry.option_symbol or "",
            ))
            open_entry = None

    return trades


@dataclass(frozen=True)
class DirectionBreakdown:
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    net_points: float


@dataclass(frozen=True)
class BucketBreakdown:
    label: str
    trades: int
    win_rate: float | None
    net_points: float


@dataclass(frozen=True)
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    win_rate: float | None
    profit_factor: float | None  # gross wins / abs(gross losses); None if no losses at all
    net_points: float
    average_trade_points: float
    expectancy_points: float | None
    largest_win_points: float | None
    largest_loss_points: float | None
    max_consecutive_losses: int
    max_drawdown_points: float  # largest peak-to-trough decline in the cumulative-points equity curve
    call_performance: DirectionBreakdown
    put_performance: DirectionBreakdown
    time_of_day_performance: list[BucketBreakdown] = field(default_factory=list)
    market_regime_performance: list[BucketBreakdown] = field(default_factory=list)


def _direction_breakdown(trades: list[BacktestTrade]) -> DirectionBreakdown:
    if not trades:
        return DirectionBreakdown(0, 0, 0, None, 0.0)
    wins = sum(1 for trade in trades if trade.points > 0)
    losses = sum(1 for trade in trades if trade.points < 0)
    return DirectionBreakdown(
        trades=len(trades), wins=wins, losses=losses,
        win_rate=(wins / len(trades)) * 100, net_points=sum(trade.points for trade in trades),
    )


def _max_consecutive_losses(trades_oldest_first: list[BacktestTrade]) -> int:
    longest = current = 0
    for trade in trades_oldest_first:
        if trade.points < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _max_drawdown(trades_oldest_first: list[BacktestTrade]) -> float:
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades_oldest_first:
        cumulative += trade.points
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _time_of_day_breakdown(trades_oldest_first: list[BacktestTrade]) -> list[BucketBreakdown]:
    """Buckets by the hour the trade was entered. A coarse but objective,
    non-invented proxy for "does this strategy perform differently at
    different times of the trading day" - exactly what the spec asks for,
    without pretending to know anything more granular than the data supports.
    """
    buckets: dict[int, list[BacktestTrade]] = {}
    for trade in trades_oldest_first:
        hour = trade.entry_time.hour if hasattr(trade.entry_time, "hour") else 0
        buckets.setdefault(hour, []).append(trade)
    result = []
    for hour in sorted(buckets):
        bucket_trades = buckets[hour]
        wins = sum(1 for t in bucket_trades if t.points > 0)
        result.append(BucketBreakdown(
            label=f"{hour:02d}:00-{hour:02d}:59", trades=len(bucket_trades),
            win_rate=(wins / len(bucket_trades)) * 100 if bucket_trades else None,
            net_points=sum(t.points for t in bucket_trades),
        ))
    return result


def _market_regime_breakdown(
    trades_oldest_first: list[BacktestTrade], regime_by_time: dict[Any, str],
) -> list[BucketBreakdown]:
    """Buckets by the regime label supplied for each trade's entry time -
    see compute_regime_labels() for the exact (documented, non-authoritative)
    definition used. Trades whose entry time has no regime label (e.g. at
    the very start of the series, before the lookback window fills) are
    skipped rather than guessed into a bucket.
    """
    buckets: dict[str, list[BacktestTrade]] = {}
    for trade in trades_oldest_first:
        label = regime_by_time.get(trade.entry_time)
        if label is None:
            continue
        buckets.setdefault(label, []).append(trade)
    result = []
    for label in sorted(buckets):
        bucket_trades = buckets[label]
        wins = sum(1 for t in bucket_trades if t.points > 0)
        result.append(BucketBreakdown(
            label=label, trades=len(bucket_trades),
            win_rate=(wins / len(bucket_trades)) * 100 if bucket_trades else None,
            net_points=sum(t.points for t in bucket_trades),
        ))
    return result


def compute_regime_labels(df: pd.DataFrame, sma_length: int = 50) -> dict[Any, str]:
    """A deliberately simple, documented proxy for "market regime": close
    price above/below its own `sma_length`-period simple moving average at
    that bar. This is NOT an authoritative regime classifier (real regime
    detection - trending/ranging/volatile - is a much deeper topic) - it's
    an objective, reproducible split chosen so "market-regime performance"
    means something concrete rather than being invented per-trade.
    """
    sma = df["Close"].rolling(sma_length).mean()
    labels: dict[Any, str] = {}
    for timestamp, close, avg in zip(df.index, df["Close"], sma, strict=True):
        if pd.isna(avg):
            continue
        labels[timestamp] = "ABOVE_SMA" if close > avg else "BELOW_SMA"
    return labels


def compute_backtest_metrics(
    trades: list[BacktestTrade], regime_by_time: dict[Any, str] | None = None,
) -> BacktestMetrics:
    """`trades` may be given in any order - sorted oldest-first internally,
    since consecutive-loss streaks and drawdown are sequence-dependent."""
    trades_oldest_first = sorted(trades, key=lambda trade: trade.entry_time)

    if not trades_oldest_first:
        return BacktestMetrics(
            total_trades=0, wins=0, losses=0, win_rate=None, profit_factor=None,
            net_points=0.0, average_trade_points=0.0, expectancy_points=None,
            largest_win_points=None, largest_loss_points=None, max_consecutive_losses=0,
            max_drawdown_points=0.0,
            call_performance=_direction_breakdown([]), put_performance=_direction_breakdown([]),
        )

    points = [trade.points for trade in trades_oldest_first]
    wins = [p for p in points if p > 0]
    losses = [p for p in points if p < 0]
    total = len(trades_oldest_first)
    win_rate = (len(wins) / total) * 100
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    net_points = sum(points)
    average_trade = net_points / total
    loss_rate = len(losses) / total
    win_rate_fraction = len(wins) / total
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (win_rate_fraction * avg_win) - (loss_rate * avg_loss)

    return BacktestMetrics(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        profit_factor=profit_factor,
        net_points=net_points,
        average_trade_points=average_trade,
        expectancy_points=expectancy,
        largest_win_points=max(points) if points else None,
        largest_loss_points=min(points) if points else None,
        max_consecutive_losses=_max_consecutive_losses(trades_oldest_first),
        max_drawdown_points=_max_drawdown(trades_oldest_first),
        call_performance=_direction_breakdown([t for t in trades_oldest_first if t.direction == "CALL"]),
        put_performance=_direction_breakdown([t for t in trades_oldest_first if t.direction == "PUT"]),
        time_of_day_performance=_time_of_day_breakdown(trades_oldest_first),
        market_regime_performance=(
            _market_regime_breakdown(trades_oldest_first, regime_by_time) if regime_by_time else []
        ),
    )


# -- anti-overfitting: chronological splits + walk-forward (spec section 34) ----------------------------------------------


def split_train_validation_test(
    df: pd.DataFrame, train_fraction: float = 0.6, validation_fraction: float = 0.2,
) -> dict[str, pd.DataFrame]:
    """A chronological (never shuffled) three-way split. Shuffling time-
    series data before splitting would leak future information into the
    training set - a walk-forward-compatible split is always
    chronological, train earliest, out-of-sample latest.
    """
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("train_fraction and validation_fraction must each be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train_fraction + validation_fraction must leave room for an out-of-sample split")
    n = len(df)
    train_end = int(n * train_fraction)
    validation_end = int(n * (train_fraction + validation_fraction))
    return {
        "train": df.iloc[:train_end],
        "validation": df.iloc[train_end:validation_end],
        "out_of_sample": df.iloc[validation_end:],
    }


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train: pd.DataFrame
    test: pd.DataFrame


def walk_forward_windows(df: pd.DataFrame, train_size: int, test_size: int, step: int | None = None) -> list[WalkForwardWindow]:
    """Rolling train/test windows, each test window strictly AFTER its own
    train window and never reused as training data for a later window's
    test period - the walk-forward discipline the spec asks for instead of
    a single train/test split. `step` defaults to `test_size` (windows
    tile the series with no overlap or gap); a smaller step re-uses more
    of the series at the cost of overlapping test windows.
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    step = step or test_size
    windows: list[WalkForwardWindow] = []
    start = 0
    n = len(df)
    index = 0
    while start + train_size + test_size <= n:
        windows.append(WalkForwardWindow(
            index=index,
            train=df.iloc[start:start + train_size],
            test=df.iloc[start + train_size:start + train_size + test_size],
        ))
        start += step
        index += 1
    return windows
