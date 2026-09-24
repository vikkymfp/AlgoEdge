from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFException

_DATA_ERRORS = (YFException, OSError, ValueError, KeyError, IndexError, TypeError)

INDEX_DEFINITIONS: dict[str, tuple[str, str]] = {
    "nifty-50": ("NIFTY 50", "^NSEI"),
    "sensex": ("S&P BSE Sensex", "^BSESN"),
    "bank-nifty": ("Nifty Bank Index", "^NSEBANK"),
}

TIMEFRAMES: dict[str, tuple[str, str]] = {
    "1m": ("1d", "1m"),
    "5m": ("5d", "5m"),
    "15m": ("5d", "15m"),
    "1h": ("1mo", "1h"),
    "1d": ("1y", "1d"),
}

_CACHE_TTL_SECONDS = 10.0
_cache: dict[tuple[str, ...], tuple[float, object]] = {}


def _cached(key: tuple[str, ...], compute):
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    value = compute()
    _cache[key] = (now, value)
    return value


@dataclass(frozen=True)
class IndexSummary:
    id: str
    name: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    sparkline: list[float] = field(default_factory=list)


def _summarize(history: pd.DataFrame) -> dict:
    closes = history["Close"].dropna()
    if closes.empty:
        return {"price": None, "change": None, "change_percent": None, "sparkline": []}
    dates = history.index.date
    unique_dates = sorted(set(dates))
    last_date = unique_dates[-1]
    today_closes = closes[dates == last_date]
    price = float(today_closes.iloc[-1])
    if len(unique_dates) > 1:
        previous_date = unique_dates[-2]
        previous_closes = closes[dates == previous_date]
        previous_close = float(previous_closes.iloc[-1])
    else:
        previous_close = float(today_closes.iloc[0])
    change = price - previous_close
    change_percent = (change / previous_close * 100) if previous_close else None
    sparkline = [float(value) for value in today_closes.tail(24)]
    return {
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "sparkline": sparkline,
    }


def _format_candles(history: pd.DataFrame) -> list[dict]:
    rows = history.dropna(subset=["Open", "High", "Low", "Close"])
    return [
        {
            "time": int(timestamp.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
        for timestamp, row in rows.iterrows()
    ]


def get_index_summary(index_id: str) -> IndexSummary:
    name, ticker = INDEX_DEFINITIONS[index_id]

    def compute() -> dict:
        try:
            history = yf.Ticker(ticker).history(period="2d", interval="5m")
            return _summarize(history)
        except _DATA_ERRORS:
            return {"price": None, "change": None, "change_percent": None, "sparkline": []}

    result = _cached(("summary", index_id), compute)
    return IndexSummary(
        id=index_id,
        name=name,
        price=result["price"],
        change=result["change"],
        change_percent=result["change_percent"],
        sparkline=result["sparkline"],
    )


def get_index_candles(index_id: str, timeframe: str) -> list[dict]:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    _name, ticker = INDEX_DEFINITIONS[index_id]
    period, interval = TIMEFRAMES[timeframe]

    def compute() -> list[dict]:
        try:
            history = yf.Ticker(ticker).history(period=period, interval=interval)
            return _format_candles(history)
        except _DATA_ERRORS:
            return []

    return _cached(("candles", index_id, timeframe), compute)
