from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from algoedge.config import Settings
from algoedge.token_service import TokenService


_TICKER_MAP = {
    "^NSEI": ("NSE", "NSE-NIFTY"),
    "^NSEBANK": ("NSE", "NSE-BANKNIFTY"),
    "^BSESN": ("BSE", "BSE-SENSEX"),
}

_INTERVAL_MAP = {
    "1m": ("1minute", 1),
    "5m": ("5minute", 5),
    "15m": ("15minute", 15),
    "1h": ("1hour", 60),
    "1d": ("1day", 1440),
}

_COLUMNS = [
    "timestamp",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "OpenInterest",
]

_IST = ZoneInfo("Asia/Kolkata")


def _resolve_symbol(ticker: str) -> tuple[str, str]:
    try:
        return _TICKER_MAP[ticker]
    except KeyError as exc:
        raise ValueError(f"Unsupported Groww market-data ticker: {ticker}") from exc


def _resolve_interval(interval: str) -> tuple[str, int]:
    try:
        return _INTERVAL_MAP[interval]
    except KeyError as exc:
        raise ValueError(f"Unsupported Groww candle interval: {interval}") from exc


def _period_to_start(period: str, now: datetime) -> datetime:
    if period.endswith("d"):
        # Treat day-based periods as trading-session windows rather than
        # blindly equating N trading days with N calendar days.
        sessions = int(period[:-1])
        calendar_days = max(sessions, (sessions * 7 + 4) // 5) + 2
        return now - timedelta(days=calendar_days)
    if period.endswith("mo"):
        return now - timedelta(days=30 * int(period[:-2]))
    if period.endswith("y"):
        return now - timedelta(days=365 * int(period[:-1]))
    raise ValueError(f"Unsupported historical period: {period}")


def fetch_historical_candles(
    settings: Settings,
    ticker: str,
    period: str = "5d",
    interval: str = "5m",
) -> pd.DataFrame:
    exchange, groww_symbol = _resolve_symbol(ticker)
    candle_interval, _interval_minutes = _resolve_interval(interval)

    now = datetime.now(_IST)
    start = _period_to_start(period, now)

    # Give Groww the full current local date through the current time.
    # The API expects yyyy-MM-dd HH:mm:ss.
    start_time = start.strftime("%Y-%m-%d %H:%M:%S")
    end_time = now.strftime("%Y-%m-%d %H:%M:%S")

    token_service = TokenService(settings)
    client = token_service.effective_client()

    result = client.get_historical_candles(
        exchange=exchange,
        segment="CASH",
        groww_symbol=groww_symbol,
        start_time=start_time,
        end_time=end_time,
        candle_interval=candle_interval,
    )

    candles = result.get("candles", [])
    if not candles:
        raise RuntimeError(
            f"No Groww historical data returned for {ticker} "
            f"({groww_symbol}, {interval}, {period})"
        )

    df = pd.DataFrame(candles, columns=_COLUMNS)

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="raise")

    # Groww timestamps are returned without an offset and represent the
    # candle START time. Treat them explicitly as India time.
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(_IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(_IST)

    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for column in ("Open", "High", "Low", "Close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    # Keep unavailable Groww index volume/OI as missing rather than
    # manufacturing zero values.
    for column in ("Volume", "OpenInterest"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return df
