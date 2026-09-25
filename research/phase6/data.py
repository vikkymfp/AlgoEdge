"""Phase 6 research: market data loading and data-quality checks.

Fetching goes through the exact production call
(`fno_signals.main.fetch_underlying_data`) with the exact production
Backtest periods, so research data is the same data /api/backtest/run
would see. Fetched frames are cached as CSV under data/ (git-ignored by
the existing `data/*.csv` rule) so every experiment in one research run
uses one identical snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import time
from pathlib import Path

import pandas as pd

# Copied (not imported) from algoedge.web_server.BACKTEST_TIMEFRAMES:
# importing web_server has startup side effects (DB init, TokenService).
# test_phase6.py asserts the two stay identical.
BACKTEST_TIMEFRAMES: dict[str, tuple[str, str]] = {
    "1m": ("7d", "1m"),
    "5m": ("60d", "5m"),
    "15m": ("60d", "15m"),
    "1h": ("730d", "1h"),
    "1d": ("5y", "1d"),
}

# algoedge.auto_trader._INDEX_CHOICE - dashboard index id -> fno_signals.config.INDEX_MAP key.
INDEX_CHOICE: dict[str, int] = {"nifty-50": 1, "bank-nifty": 2, "sensex": 3}

IST = "Asia/Kolkata"
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
DEFAULT_CACHE_DIR = Path("data")


def cache_path(index_id: str, interval: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    return cache_dir / f"phase6_{index_id}_{interval}.csv"


def fetch(index_id: str, interval: str) -> pd.DataFrame:
    from fno_signals.config import INDEX_MAP
    from fno_signals.main import fetch_underlying_data

    period, yf_interval = BACKTEST_TIMEFRAMES[interval]
    ticker = INDEX_MAP[INDEX_CHOICE[index_id]].ticker
    return fetch_underlying_data(ticker, period=period, interval=yf_interval)


def save_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index_label="Datetime")


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    index = pd.to_datetime(df.pop(df.columns[0]), utc=True).dt.tz_convert(IST)
    df.index = pd.DatetimeIndex(index, name="Datetime")
    return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def load(index_id: str, interval: str, cache_dir: Path = DEFAULT_CACHE_DIR, refresh: bool = False) -> pd.DataFrame:
    path = cache_path(index_id, interval, cache_dir)
    if refresh or not path.exists():
        save_csv(fetch(index_id, interval), path)
    return load_csv(path)


@dataclass(frozen=True)
class DataQuality:
    rows: int
    start: str | None
    end: str | None
    trading_days: int
    duplicate_timestamps: int
    nan_rows: int
    non_positive_prices: int
    inconsistent_ohlc: int  # High < Low, or Open/Close outside [Low, High]
    zero_volume_fraction: float
    bars_outside_market_hours: int
    expected_bars_per_day: int | None
    days_with_missing_bars: int
    missing_bars_total: int
    max_intraday_gap_minutes: float | None
    timezone: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _interval_minutes(interval: str) -> int | None:
    return {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(interval)


def quality_report(df: pd.DataFrame, interval: str) -> DataQuality:
    minutes = _interval_minutes(interval)
    tz = str(df.index.tz) if df.index.tz is not None else None
    local = df.index.tz_convert(IST) if df.index.tz is not None else df.index
    times = pd.Series(local.time, index=df.index)
    outside = int(((times < MARKET_OPEN) | (times > MARKET_CLOSE)).sum()) if minutes else 0

    expected = None
    days_missing = 0
    missing_total = 0
    max_gap = None
    if minutes:
        # 09:15..15:29 inclusive for bars labelled by their start time. The 1h
        # grid (09:15, 10:15, ... 15:15) gives 7 bars.
        session_minutes = (15 * 60 + 30) - (9 * 60 + 15)
        expected = -(-session_minutes // minutes)
        dates = pd.Series(local.date, index=df.index)
        # Unique timestamps only - a duplicated bar must not mask a missing one.
        per_day = pd.Series(local.unique().date).value_counts()
        # The most recent day can legitimately be incomplete (still trading).
        complete_days = per_day.drop(index=max(per_day.index)) if len(per_day) > 1 else per_day
        shortfall = (expected - complete_days).clip(lower=0)
        days_missing = int((shortfall > 0).sum())
        missing_total = int(shortfall.sum())
        gaps = []
        for _day, group in pd.Series(local, index=df.index).groupby(dates.values):
            if len(group) > 1:
                gaps.append((group.diff().dropna().dt.total_seconds() / 60).max())
        max_gap = float(max(gaps)) if gaps else None

    prices = df[["Open", "High", "Low", "Close"]]
    inconsistent = (
        (df["High"] < df["Low"])
        | (df["Open"] > df["High"]) | (df["Open"] < df["Low"])
        | (df["Close"] > df["High"]) | (df["Close"] < df["Low"])
    )
    return DataQuality(
        rows=len(df),
        start=str(df.index.min()) if len(df) else None,
        end=str(df.index.max()) if len(df) else None,
        trading_days=int(pd.Series(local.date).nunique()),
        duplicate_timestamps=int(df.index.duplicated().sum()),
        nan_rows=int(prices.isna().any(axis=1).sum()),
        non_positive_prices=int((prices <= 0).any(axis=1).sum()),
        inconsistent_ohlc=int(inconsistent.sum()),
        zero_volume_fraction=float((df["Volume"].fillna(0) == 0).mean()) if len(df) else 0.0,
        bars_outside_market_hours=outside,
        expected_bars_per_day=expected,
        days_with_missing_bars=days_missing,
        missing_bars_total=missing_total,
        max_intraday_gap_minutes=max_gap,
        timezone=tz,
    )
