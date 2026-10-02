"""Groww historical-candle adapter for the FNO strategy's underlying (index) data.

Replaces yfinance as the source behind `fno_signals.main.fetch_underlying_data`
while keeping that function's contract: callers pass a yfinance-style ticker
(`^NSEI`), period (`5d`) and interval (`5m`) and get back a DataFrame the
strategy and Auto Trader already understand.

Returned frame (see `fetch_historical_candles`):
- index named `timestamp`, tz-aware `Asia/Kolkata`, sorted ascending, unique;
  every stamp is the candle's START (Groww's own convention - never shifted);
- columns `Open, High, Low, Close, Volume, OpenInterest`, all float64. Groww
  returns `None` volume/open interest for index feeds; those become NaN, never
  an invented value (NaN volume is compatible with `compute_indicators`; the
  VWAP filter is disabled by default).

Authentication: every request goes through `TokenService.effective_client()` -
the app's one place that owns Groww credentials and the session. This module
never builds a `GrowwAPI`. A process uses one shared `TokenService`: the one
registered with `register_token_service()` (the dashboard and the live CLI do
this), otherwise a single lazily-created process-wide instance - never one per
request.

Period handling (yfinance semantics preserved where they are about trading
sessions): `Nd` means the last N TRADING SESSIONS present in the data, not N
calendar days. The request therefore looks back further in calendar days
(`_calendar_lookback_days`: weekends plus a holiday margin) and the result is
trimmed to the last N distinct IST trading dates. `Nwk` means N*5 sessions.
`Nmo`/`Ny` are calendar spans (30*N / 365*N days), as in yfinance.

Known limitations (documented, not hidden):
- Groww's maximum historical window per request, history depth per interval,
  and rate limits are NOT documented in this repository. Long spans are split
  into conservative chunks (`_CHUNK_CALENDAR_DAYS`); these are this adapter's
  own request-size choices, NOT Groww limits. If Groww silently returns less
  history than requested, fewer sessions come back and a warning is logged;
  the adapter does not invent data.
- If fewer than N sessions are available, what exists is returned.
- The last candle may still be forming. It is returned as-is (Auto Trader
  already drops a forming last bar for entries); nothing is added to
  timestamps.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("algoedge.groww_market_data")

IST = ZoneInfo("Asia/Kolkata")

SEGMENT_CASH = "CASH"

# yfinance index ticker -> (Groww exchange, Groww symbol). IndexConfig.ticker
# keeps its yfinance meaning; the mapping lives here only.
GROWW_INDEX_SYMBOLS: dict[str, tuple[str, str]] = {
    "^NSEI": ("NSE", "NSE-NIFTY"),
    "^NSEBANK": ("NSE", "NSE-BANKNIFTY"),
    "^BSESN": ("BSE", "BSE-SENSEX"),
}

# yfinance interval -> Groww candle_interval.
GROWW_INTERVALS: dict[str, str] = {
    "1m": "1minute",
    "5m": "5minute",
    "15m": "15minute",
    "1h": "1hour",
    "1d": "1day",
}
_INTRADAY_INTERVALS = frozenset({"1m", "5m", "15m", "1h"})

# Explicit per-request timeout (seconds). The SDK default is "no timeout".
REQUEST_TIMEOUT_SECONDS = 30

# Conservative calendar-day size of one request, per interval. This adapter's
# own choice to avoid asking for very large windows at once - NOT a Groww limit.
_CHUNK_CALENDAR_DAYS: dict[str, int] = {"1m": 7, "5m": 30, "15m": 30, "1h": 90, "1d": 365}

# Sanity check on the timestamp basis: intraday bar-start stamps must fall
# inside the exchange day. A wrong epoch basis (e.g. IST wall-clock sent as
# UTC) would shift every bar by 5h30 and silently break session filters.
_SESSION_MIN_MINUTE = 9 * 60            # 09:00 (pre-open allowed)
_SESSION_MAX_MINUTE = 15 * 60 + 30      # 15:30
_MIN_ON_GRID_FRACTION = 0.5

OUTPUT_COLUMNS = ["Open", "High", "Low", "Close", "Volume", "OpenInterest"]

_PERIOD_RE = re.compile(r"^(\d+)(d|wk|mo|y)$")


# --------------------------------------------------------------------------
# Client access (TokenService only)
# --------------------------------------------------------------------------

_registered_token_service: Any = None
_default_token_service: Any = None
_service_lock = threading.Lock()


def register_token_service(token_service: Any) -> None:
    """Makes `token_service` the one used for market-data requests (the
    dashboard and the live CLI register theirs so session, renewal and
    capability tracking stay shared)."""
    global _registered_token_service
    with _service_lock:
        _registered_token_service = token_service


def _effective_client() -> Any:
    global _default_token_service
    with _service_lock:
        service = _registered_token_service
        if service is None:
            if _default_token_service is None:
                from algoedge.config import get_settings
                from algoedge.token_service import TokenService

                _default_token_service = TokenService(get_settings())
            service = _default_token_service
    return service.effective_client()


# --------------------------------------------------------------------------
# Request planning
# --------------------------------------------------------------------------

def _parse_period(period: str) -> tuple[str, int]:
    """-> ("sessions", N) or ("calendar_days", D)."""
    match = _PERIOD_RE.match(str(period).strip().lower())
    if not match:
        raise ValueError(f"Unsupported period {period!r} (use Nd, Nwk, Nmo or Ny)")
    count, unit = int(match.group(1)), match.group(2)
    if count <= 0:
        raise ValueError(f"Unsupported period {period!r}")
    if unit == "d":
        return "sessions", count
    if unit == "wk":
        return "sessions", count * 5
    if unit == "mo":
        return "calendar_days", count * 30
    return "calendar_days", count * 365


def _calendar_lookback_days(sessions: int) -> int:
    """Calendar days wide enough to contain `sessions` trading sessions:
    weekends (7/5) plus a one-week margin for exchange holidays."""
    return math.ceil(sessions * 7 / 5) + 7


def _chunks(start: datetime, end: datetime, chunk_days: int) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=chunk_days), end)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows or [(start, end)]


# --------------------------------------------------------------------------
# Response normalization
# --------------------------------------------------------------------------

def _candle_rows(payload: Any) -> list[Any]:
    if not isinstance(payload, dict) or "candles" not in payload:
        keys = sorted(map(str, payload)) if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(f"Unexpected Groww candle response (keys={keys})")
    candles = payload["candles"]
    return list(candles) if candles else []


def _row_to_tuple(row: Any) -> tuple[Any, ...]:
    """[timestamp, open, high, low, close, volume, open_interest] (volume and
    open interest optional) or a dict with the same names."""
    if isinstance(row, dict):
        lowered = {str(k).lower(): v for k, v in row.items()}
        ts = next((lowered[k] for k in ("timestamp", "time", "ts", "datetime") if k in lowered), None)
        return (ts, lowered.get("open"), lowered.get("high"), lowered.get("low"), lowered.get("close"),
                lowered.get("volume"), lowered.get("open_interest", lowered.get("oi")))
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        padded = list(row[:7]) + [None] * (7 - len(row[:7]))
        return tuple(padded)
    raise RuntimeError(f"Unexpected Groww candle row: {type(row).__name__}")


def _to_ist_index(raw: pd.Series) -> pd.DatetimeIndex:
    numeric = pd.to_numeric(raw, errors="coerce")
    if numeric.notna().all():
        unit = "ms" if float(numeric.abs().max()) >= 1e11 else "s"
        stamps = pd.to_datetime(numeric, unit=unit, utc=True).dt.tz_convert(IST)
    else:
        stamps = pd.to_datetime(raw, errors="coerce")
        if stamps.isna().any():
            raise RuntimeError("Groww candle response has unparseable timestamps")
        stamps = stamps.dt.tz_localize(IST) if stamps.dt.tz is None else stamps.dt.tz_convert(IST)
    if stamps.isna().any():
        raise RuntimeError("Groww candle response has unparseable timestamps")
    return pd.DatetimeIndex(stamps)


def _normalize(rows: list[Any]) -> pd.DataFrame:
    frame = pd.DataFrame([_row_to_tuple(r) for r in rows],
                         columns=["ts", "Open", "High", "Low", "Close", "Volume", "OpenInterest"])
    index = _to_ist_index(frame.pop("ts"))
    for column in OUTPUT_COLUMNS:
        # None / non-numeric -> NaN (float64). Nothing is filled or invented.
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame.index = index
    frame.index.name = "timestamp"
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]      # a re-sent candle: the later one wins
    return frame[OUTPUT_COLUMNS]


def _check_session_grid(frame: pd.DataFrame, ticker: str) -> None:
    minutes = frame.index.hour * 60 + frame.index.minute
    on_grid = ((minutes >= _SESSION_MIN_MINUTE) & (minutes <= _SESSION_MAX_MINUTE)).mean()
    if on_grid < _MIN_ON_GRID_FRACTION:
        raise RuntimeError(
            f"Groww candles for {ticker} are not on the exchange-session time grid after IST conversion "
            "(timestamp basis mismatch?) - refusing to return shifted data"
        )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def fetch_historical_candles(
    ticker: str,
    period: str = "5d",
    interval: str = "5m",
    *,
    client: Any = None,
    now: datetime | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    """Historical index candles from Groww for a yfinance-style ticker/period/interval.

    `client` and `now` exist for tests; production callers leave them unset
    (the client then comes from `TokenService.effective_client()`).
    Raises RuntimeError when Groww returns no candles, ValueError for an
    unsupported ticker/interval/period; Groww API errors propagate unchanged.
    """
    if ticker not in GROWW_INDEX_SYMBOLS:
        raise ValueError(f"No Groww symbol mapping for ticker {ticker!r}")
    if interval not in GROWW_INTERVALS:
        raise ValueError(f"Unsupported interval {interval!r} (supported: {sorted(GROWW_INTERVALS)})")
    exchange, groww_symbol = GROWW_INDEX_SYMBOLS[ticker]
    groww_interval = GROWW_INTERVALS[interval]
    mode, amount = _parse_period(period)

    end = (now or datetime.now(IST)).astimezone(IST).replace(microsecond=0)
    lookback = _calendar_lookback_days(amount) if mode == "sessions" else amount
    start = (end - timedelta(days=lookback)).replace(hour=0, minute=0, second=0)

    groww = client if client is not None else _effective_client()
    fmt = "%Y-%m-%d %H:%M:%S"
    rows: list[Any] = []
    for window_start, window_end in _chunks(start, end, _CHUNK_CALENDAR_DAYS[interval]):
        payload = groww.get_historical_candles(
            exchange=exchange, segment=SEGMENT_CASH, groww_symbol=groww_symbol,
            start_time=window_start.strftime(fmt), end_time=window_end.strftime(fmt),
            candle_interval=groww_interval, timeout=timeout,
        )
        rows.extend(_candle_rows(payload))
    if not rows:
        raise RuntimeError(f"No data returned for {ticker}")

    frame = _normalize(rows)
    if frame[["Open", "High", "Low", "Close"]].notna().all(axis=1).sum() == 0:
        raise RuntimeError(f"No valid OHLC data returned for {ticker}")
    if interval in _INTRADAY_INTERVALS:
        _check_session_grid(frame, ticker)

    if mode == "sessions":
        dates = frame.index.normalize()
        sessions = sorted(dates.unique())
        if len(sessions) < amount:
            logger.warning("Groww returned %d trading session(s) for %s %s, %d requested",
                           len(sessions), ticker, period, amount)
        frame = frame[dates.isin(sessions[-amount:])]
    return frame
