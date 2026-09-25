"""Phase 6 research: READ-ONLY candle source for historical_candles.

RESEARCH / BACKTEST DATA ONLY. Reads the research table written by
load_historical_data.py and returns candles in exactly the DataFrame shape the
canonical strategy (fno_signals.strategy.run) and the research engine consume
- the same shape yfinance produces:

    index    DatetimeIndex, tz=Asia/Kolkata, name "Datetime", ascending, unique
    columns  Open, High, Low, Close, Volume  (float64; NULL volume -> NaN)

Guarantees:
- SELECT statements only; nothing is ever inserted, updated, deleted or
  created. The caller must pass the SQLAlchemy engine explicitly - there is
  no fallback to production algoedge.db.init_db(), and this module never
  imports algoedge.db or algoedge.models.
- Database timestamps are naive IST wall-clock values. They are LOCALIZED to
  Asia/Kolkata (never read as UTC and converted).
- Nothing is repaired: duplicate or unparseable timestamps raise instead of
  being dropped; no candle is filled, interpolated or fabricated.

Known gaps in the baseline master_5min.csv load (never filled):
- 2015-06-22 .. 2015-11-13: excluded contaminated period - a ~150-day hole
  (2015-06-19 15:25 -> 2015-11-16 09:15). contiguous_segments() splits here
  so a backtest never treats the two sides as consecutive bars.
- Single missing trading days: 2015-01-16, 2025-03-20, 2025-03-21. These are
  short enough (<= 4d 18h, same as a normal long holiday weekend) that they
  stay inside a segment: the strategy simply sees one longer overnight gap.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from sqlalchemy import Engine, select

from research.phase6 import historical_db as hdb

IST = "Asia/Kolkata"
OHLCV = ("Open", "High", "Low", "Close", "Volume")
INDEX_NAME = "Datetime"

# Longest normal NSE closure between two 5m bars is a long holiday weekend
# (Thu 15:25 -> Mon 09:15 = 4d 17h50m in master_5min.csv). Anything longer is
# a data hole, not a market closure.
DEFAULT_MAX_GAP_DAYS = 7.0

KNOWN_MISSING_TRADING_DAYS = (date(2015, 1, 16), date(2025, 3, 20), date(2025, 3, 21))


class CandleSourceError(ValueError):
    """The stored candles violate the reader's guarantees (e.g. duplicates)."""


def _empty_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz=IST, name=INDEX_NAME)
    return pd.DataFrame({c: pd.Series([], dtype="float64", index=index) for c in OHLCV}, index=index)


def _naive_ist(value: datetime | pd.Timestamp | None) -> datetime | None:
    """A filter bound in the DB's convention: aware -> IST wall-clock; naive is
    already taken as IST wall-clock."""
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(IST).tz_localize(None)
    return ts.to_pydatetime()


def read_candles(
    engine: Engine,
    index_id: str = "nifty-50",
    timeframe: str = "5m",
    start: datetime | pd.Timestamp | None = None,
    end: datetime | pd.Timestamp | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """Candles for one index/timeframe, optionally bounded (inclusive, by
    bar_start) and restricted to one source. One SELECT, fetched in bulk."""
    if engine is None:
        raise ValueError("an explicit SQLAlchemy engine is required")
    c = hdb.HistoricalCandle.__table__.c
    stmt = (
        select(c.bar_start, c.open, c.high, c.low, c.close, c.volume)
        .where(c.index_id == index_id, c.timeframe == timeframe)
        .order_by(c.bar_start)
    )
    if (lo := _naive_ist(start)) is not None:
        stmt = stmt.where(c.bar_start >= lo)
    if (hi := _naive_ist(end)) is not None:
        stmt = stmt.where(c.bar_start <= hi)
    if source is not None:
        stmt = stmt.where(c.source == source)

    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    if not rows:
        return _empty_frame()

    raw = pd.DataFrame(rows, columns=["bar_start", *OHLCV])
    stamps = pd.to_datetime(raw.pop("bar_start"), errors="coerce")
    if stamps.isna().any():
        raise CandleSourceError(f"{int(stamps.isna().sum())} bar_start value(s) could not be parsed")
    if stamps.dt.tz is not None:  # the table is naive IST by definition - refuse a surprise
        raise CandleSourceError("bar_start came back timezone-aware; expected naive IST wall-clock")
    index = pd.DatetimeIndex(stamps, name=INDEX_NAME).tz_localize(IST, ambiguous="raise", nonexistent="raise")
    if index.has_duplicates:
        dupes = index[index.duplicated()].unique()[:5]
        raise CandleSourceError(
            f"{int(index.duplicated().sum())} duplicate bar_start value(s), e.g. {[t.isoformat() for t in dupes]}"
            " - pass source= to read a single dataset"
        )
    frame = raw.astype("float64")  # NULL volume (None) -> NaN
    frame.index = index
    if not frame.index.is_monotonic_increasing:  # ORDER BY guarantees it; checked, not assumed
        raise CandleSourceError("bar_start is not in ascending order")
    return frame


def contiguous_segments(df: pd.DataFrame, *, max_gap_days: float | None = None) -> list[pd.DataFrame]:
    """Splits wherever two consecutive bars are more than `max_gap_days`
    (default DEFAULT_MAX_GAP_DAYS) apart, so a backtest never bridges a data
    hole as if the bars were adjacent. Rows, values and timezone are kept
    exactly; each segment is an independent copy."""
    if df.empty:
        return []
    if not df.index.is_monotonic_increasing:
        raise CandleSourceError("candles must be in ascending time order")
    limit = pd.Timedelta(days=DEFAULT_MAX_GAP_DAYS if max_gap_days is None else max_gap_days)
    breaks = [i for i, step in enumerate(df.index.to_series().diff(), start=0) if pd.notna(step) and step > limit]
    bounds = [0, *breaks, len(df)]
    return [df.iloc[a:b].copy() for a, b in zip(bounds, bounds[1:], strict=False)]


def load_db_segments(
    engine: Engine,
    index_id: str = "nifty-50",
    timeframe: str = "5m",
    start: datetime | pd.Timestamp | None = None,
    end: datetime | pd.Timestamp | None = None,
    source: str | None = None,
    *,
    max_gap_days: float | None = None,
) -> list[pd.DataFrame]:
    """read_candles() + contiguous_segments(). Does not run any strategy."""
    frame = read_candles(engine, index_id=index_id, timeframe=timeframe, start=start, end=end, source=source)
    return contiguous_segments(frame, max_gap_days=max_gap_days)
