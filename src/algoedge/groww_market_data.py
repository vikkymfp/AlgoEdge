"""Read-only Groww market data (paid Live Data API): index LTP/OHLC, candles for
indices and options, expiries, the option chain, option quotes and Greeks.

Nothing here can place, modify or cancel an order. Every call goes through the
process-wide TokenService (see `use_token_service`) and carries an explicit
timeout - the SDK's own default is "infinite".

A value Groww does not return is None / NaN, never a default. Response field
names for quotes, chains and Greeks are matched against a few known spellings
(`_first`); an unrecognised shape yields missing values rather than a guess.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from algoedge.config import Settings
from algoedge.token_service import TokenService

# ticker -> (exchange, groww_symbol for candles, trading symbol for LTP/OHLC)
_TICKER_MAP = {
    "^NSEI": ("NSE", "NSE-NIFTY", "NIFTY"),
    "^NSEBANK": ("NSE", "NSE-BANKNIFTY", "BANKNIFTY"),
    "^BSESN": ("BSE", "BSE-SENSEX", "SENSEX"),
}

_INTERVAL_MAP = {
    "1m": ("1minute", 1),
    "5m": ("5minute", 5),
    "15m": ("15minute", 15),
    "1h": ("1hour", 60),
    "1d": ("1day", 1440),
}

_COLUMNS = ["timestamp", "Open", "High", "Low", "Close", "Volume", "OpenInterest"]

_IST = ZoneInfo("Asia/Kolkata")

# Seconds. The SDK default is no timeout at all, which would let one hung call
# stall the dashboard's scheduler.
QUOTE_TIMEOUT_SECONDS = 10
CANDLE_TIMEOUT_SECONDS = 30

GREEK_FIELDS = ("delta", "gamma", "theta", "vega", "rho")

_EXCHANGES = ("NSE", "BSE")
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9&_.-]{0,63}")
# No Indian exchange candle predates this; an earlier parsed timestamp means a
# number was read in the wrong unit (epoch 0 is 1970), so it is rejected.
_EARLIEST_PLAUSIBLE = pd.Timestamp("1990-01-01", tz=_IST)

_shared_lock = threading.Lock()
_shared_service: TokenService | None = None


def use_token_service(service: TokenService | None) -> None:
    """Registers the process-wide TokenService every call here uses (the
    dashboard's own). Passing None forgets it; the next call builds one."""
    global _shared_service
    with _shared_lock:
        _shared_service = service


def _client(settings: Settings) -> Any:
    """One TokenService per process, not one per call: building one validates
    the session over the network and writes credentials/audit rows."""
    global _shared_service
    with _shared_lock:
        if _shared_service is None:
            _shared_service = TokenService(settings)
        service = _shared_service
    return service.effective_client()


def _exchange(value: str) -> str:
    if value not in _EXCHANGES:
        raise ValueError(f"Unsupported exchange {value!r}; expected one of {_EXCHANGES}")
    return value


def _symbol(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def _expiry(value: str) -> str:
    """A YYYY-MM-DD expiry, validated as a real calendar date."""
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else _bad_expiry(value)
    except (TypeError, ValueError):
        return _bad_expiry(value)


def _bad_expiry(value: object) -> str:
    raise ValueError(f"Invalid expiry {value!r}; expected YYYY-MM-DD")


def _as_ist(moment: datetime) -> datetime:
    """Naive datetimes are IST wall-clock times (the engine's convention), not
    the host's local time - astimezone() on a naive value would use the latter."""
    return moment.replace(tzinfo=_IST) if moment.tzinfo is None else moment.astimezone(_IST)


def _resolve_symbol(ticker: str) -> tuple[str, str, str]:
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


def _number(value: Any) -> float:
    """A finite float, or NaN for anything missing/unparseable."""
    if value is None or isinstance(value, bool):
        return math.nan
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _first(mapping: Any, *keys: str) -> float:
    """The first of `keys` holding a finite number in `mapping` (NaN if none)."""
    if not isinstance(mapping, dict):
        return math.nan
    for key in keys:
        number = _number(mapping.get(key))
        if not math.isnan(number):
            return number
    return math.nan


def parse_timestamps(values: pd.Series) -> pd.Series:
    """Candle timestamps as tz-aware IST.

    Strings without an offset are IST wall-clock times (the candle START).
    Numbers (or all-digit strings) are epoch instants - seconds, or
    milliseconds when >= 1e12 - and so absolute; pandas would otherwise read
    them as nanoseconds and silently return 1970 dates. Mixed numbers and
    text, missing values and implausibly early results are rejected."""
    numbers = pd.to_numeric(values, errors="coerce")
    if values.isna().any():
        raise ValueError("candle timestamps contain missing values")
    if numbers.notna().all():
        numbers = numbers.astype("float64")
        seconds = numbers.where(numbers.abs() < 1e12, numbers / 1000.0)
        parsed = pd.to_datetime(seconds, unit="s", utc=True).dt.tz_convert(_IST)
    elif numbers.notna().any():
        raise ValueError("candle timestamps mix numeric epochs and text")
    else:
        parsed = pd.to_datetime(values, errors="raise")
        parsed = parsed.dt.tz_localize(_IST) if parsed.dt.tz is None else parsed.dt.tz_convert(_IST)
    if (parsed < _EARLIEST_PLAUSIBLE).any():
        raise ValueError(f"implausible candle timestamp {parsed.min()} (wrong epoch unit?)")
    return parsed


def _candles_frame(candles: list[Any], label: str) -> pd.DataFrame:
    if not isinstance(candles, list):
        raise TypeError(f"Malformed Groww candle data for {label}")
    if not candles:
        raise RuntimeError(f"No Groww historical data returned for {label}")
    if not all(isinstance(row, list | tuple) for row in candles) or len({len(row) for row in candles}) != 1:
        raise RuntimeError(f"Malformed Groww candle rows for {label}")
    width = len(candles[0])
    if width == len(_COLUMNS):
        columns = _COLUMNS
    elif width == len(_COLUMNS) - 1:  # no open-interest column
        columns = _COLUMNS[:-1]
    else:
        raise RuntimeError(f"Unexpected Groww candle shape ({width} fields) for {label}")
    df = pd.DataFrame(candles, columns=columns)
    if "OpenInterest" not in df:
        df["OpenInterest"] = math.nan
    df["timestamp"] = parse_timestamps(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    # Unavailable volume/OI stay missing rather than becoming zeros.
    for column in ("Open", "High", "Low", "Close", "Volume", "OpenInterest"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def fetch_candles(
    settings: Settings,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    start: datetime,
    end: datetime,
    interval: str,
) -> pd.DataFrame:
    """Candles for any instrument (index, option, ...) between two moments.
    The index is the candle START time, IST."""
    candle_interval, _minutes = _resolve_interval(interval)
    if segment not in ("CASH", "FNO"):
        raise ValueError(f"Unsupported segment {segment!r}")
    exchange = _exchange(exchange)
    groww_symbol = _symbol(groww_symbol, "Groww symbol")
    start, end = _as_ist(start), _as_ist(end)
    if start >= end:
        raise ValueError(f"Candle window must start before it ends ({start} >= {end})")
    result = _client(settings).get_historical_candles(
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
        candle_interval=candle_interval,
        timeout=CANDLE_TIMEOUT_SECONDS,
    )
    if not isinstance(result, dict):
        raise TypeError(f"Unexpected Groww candle response for {groww_symbol}")
    candles = result.get("candles", [])
    return _candles_frame(candles, f"{groww_symbol} ({segment}, {interval})")


def fetch_historical_candles(
    settings: Settings,
    ticker: str,
    period: str = "5d",
    interval: str = "5m",
) -> pd.DataFrame:
    """Index candles (the engine's feed): the last `period` up to now."""
    exchange, groww_symbol, _trading = _resolve_symbol(ticker)
    now = datetime.now(_IST)
    return fetch_candles(
        settings, exchange=exchange, segment="CASH", groww_symbol=groww_symbol,
        start=_period_to_start(period, now), end=now, interval=interval,
    )


def fetch_option_candles(
    settings: Settings,
    *,
    exchange: str,
    groww_symbol: str,
    start: datetime,
    end: datetime,
    interval: str = "5m",
) -> pd.DataFrame:
    """Historical candles of one option contract (FNO segment), including its
    volume and open interest. `groww_symbol` is the contract's Groww symbol."""
    return fetch_candles(
        settings, exchange=exchange, segment="FNO", groww_symbol=groww_symbol,
        start=start, end=end, interval=interval,
    )


# ------------------------------------------------------------------ index quotes


def _exchange_symbols(tickers: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    if not tickers:
        raise ValueError("At least one ticker is required")
    keys: dict[str, str] = {}
    for ticker in tickers:
        exchange, _candle, trading = _resolve_symbol(ticker)
        keys[f"{exchange}_{trading}"] = ticker
    return keys, tuple(keys)


def fetch_index_ltp(settings: Settings, tickers: list[str] | None = None) -> dict[str, float]:
    """Live last traded price per ticker (NaN when Groww returns none)."""
    keys, symbols = _exchange_symbols(list(_TICKER_MAP) if tickers is None else tickers)
    response = _client(settings).get_ltp(
        exchange_trading_symbols=symbols, segment="CASH", timeout=QUOTE_TIMEOUT_SECONDS,
    )
    payload = response if isinstance(response, dict) else {}
    return {ticker: _number(payload.get(key)) for key, ticker in keys.items()}


def fetch_index_ohlc(settings: Settings, tickers: list[str] | None = None) -> dict[str, dict[str, float]]:
    """Current-session open/high/low/close per ticker."""
    keys, symbols = _exchange_symbols(list(_TICKER_MAP) if tickers is None else tickers)
    response = _client(settings).get_ohlc(
        exchange_trading_symbols=symbols, segment="CASH", timeout=QUOTE_TIMEOUT_SECONDS,
    )
    payload = response if isinstance(response, dict) else {}
    result: dict[str, dict[str, float]] = {}
    for key, ticker in keys.items():
        row = payload.get(key)
        result[ticker] = {name: _first(row, name) for name in ("open", "high", "low", "close")}
    return result


# ------------------------------------------------------------------ options


def fetch_option_expiries(
    settings: Settings, underlying: str, exchange: str = "NSE",
    year: int | None = None, month: int | None = None,
) -> list[str]:
    """Expiry dates (YYYY-MM-DD, ascending) of an underlying's options."""
    if year is not None and not 2000 <= year <= 5000:
        raise ValueError(f"year must be 2000-5000, got {year}")
    if month is not None and not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    response = _client(settings).get_expiries(
        exchange=_exchange(exchange), underlying_symbol=_symbol(underlying, "underlying"),
        year=year, month=month,
        timeout=QUOTE_TIMEOUT_SECONDS,
    )
    if not isinstance(response, dict):
        raise TypeError(f"Unexpected Groww expiries response for {underlying}")
    expiries = response.get("expiries", [])
    if not isinstance(expiries, list):
        raise TypeError(f"Malformed Groww expiries for {underlying}")
    return sorted(str(value) for value in expiries)


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    expiry: str
    underlying_ltp: float
    rows: pd.DataFrame  # one row per strike and right; see CHAIN_COLUMNS


CHAIN_COLUMNS = [
    "strike", "right", "trading_symbol", "ltp", "open", "high", "low", "close",
    "volume", "open_interest", "bid", "ask", "iv", *GREEK_FIELDS,
]


def _option_fields(leg: Any) -> dict[str, Any]:
    """Normalised fields of one chain leg or quote. Greeks and IV may sit in a
    nested `greeks` object; OHLC in a nested `ohlc` object."""
    if not isinstance(leg, dict):
        leg = {}
    greeks = leg.get("greeks") if isinstance(leg.get("greeks"), dict) else leg
    ohlc = leg.get("ohlc") if isinstance(leg.get("ohlc"), dict) else leg
    symbol = leg.get("trading_symbol")
    return {
        "trading_symbol": str(symbol) if symbol else None,
        "ltp": _first(leg, "ltp", "last_price"),
        "open": _first(ohlc, "open"),
        "high": _first(ohlc, "high"),
        "low": _first(ohlc, "low"),
        "close": _first(ohlc, "close", "prev_close"),
        "volume": _first(leg, "volume", "total_volume", "traded_volume"),
        "open_interest": _first(leg, "open_interest", "oi"),
        "bid": _first(leg, "bid_price", "bid"),
        "ask": _first(leg, "offer_price", "ask_price", "ask"),
        "iv": _first(greeks, "iv", "implied_volatility"),
        **{name: _first(greeks, name) for name in GREEK_FIELDS},
    }


def fetch_option_chain(
    settings: Settings, underlying: str, expiry: str, exchange: str = "NSE",
) -> OptionChain:
    """The full chain for one expiry with per-strike LTP, volume, open
    interest, IV and Greeks. Strikes keyed by price, each with CE and/or PE."""
    expiry = _expiry(expiry)
    response = _client(settings).get_option_chain(
        exchange=_exchange(exchange), underlying=_symbol(underlying, "underlying"), expiry_date=expiry,
        timeout=QUOTE_TIMEOUT_SECONDS,
    )
    if not isinstance(response, dict):
        raise TypeError(f"Unexpected Groww option-chain response for {underlying} {expiry}")
    strikes = response.get("strikes")
    if not isinstance(strikes, dict) or not strikes:
        raise RuntimeError(f"Groww returned no option chain for {underlying} {expiry}")
    rows: list[dict[str, Any]] = []
    for strike_text, legs in strikes.items():
        strike = _number(strike_text)
        if math.isnan(strike) or not isinstance(legs, dict):
            continue
        for right in ("CE", "PE"):
            if right in legs:
                rows.append({"strike": strike, "right": right, **_option_fields(legs[right])})
    frame = pd.DataFrame(rows, columns=CHAIN_COLUMNS).sort_values(["strike", "right"], ignore_index=True)
    return OptionChain(
        underlying=underlying, expiry=expiry,
        underlying_ltp=_first(response, "underlying_ltp"), rows=frame,
    )


def fetch_option_quote(settings: Settings, trading_symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Live quote of one option contract (LTP, OHLC, volume, open interest,
    best bid/ask)."""
    response = _client(settings).get_quote(
        trading_symbol=_symbol(trading_symbol, "trading symbol"), exchange=_exchange(exchange), segment="FNO",
        timeout=QUOTE_TIMEOUT_SECONDS,
    )
    if not isinstance(response, dict):
        raise TypeError(f"Unexpected Groww quote response for {trading_symbol}")
    fields = _option_fields(response)
    fields["trading_symbol"] = fields["trading_symbol"] or trading_symbol
    return fields


def fetch_option_greeks(
    settings: Settings, underlying: str, trading_symbol: str, expiry: str, exchange: str = "NSE",
) -> dict[str, float]:
    """Delta, Gamma, Theta, Vega, Rho and IV of one option contract.

    growwapi's `get_greeks` takes no timeout argument, so unlike every other
    call here this one is not bounded by QUOTE_TIMEOUT_SECONDS."""
    response = _client(settings).get_greeks(
        exchange=_exchange(exchange), underlying=_symbol(underlying, "underlying"),
        trading_symbol=_symbol(trading_symbol, "trading symbol"), expiry=_expiry(expiry),
    )
    if not isinstance(response, dict):
        raise TypeError(f"Unexpected Groww Greeks response for {trading_symbol}")
    fields = _option_fields(response)
    return {name: fields[name] for name in (*GREEK_FIELDS, "iv")}
