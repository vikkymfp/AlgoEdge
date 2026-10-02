"""Groww historical-candle adapter (algoedge.groww_market_data) - fake client only.

No credentials and no network: every test injects a fake Groww client or
patches the adapter's client accessor.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from algoedge import groww_market_data as gmd
from fno_signals import main as fno_main
from fno_signals.config import DEFAULT_CONFIG
from fno_signals.strategy import compute_indicators, drop_invalid_bars, run

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 30, 11, 0, tzinfo=IST)  # a Wednesday, mid-session
AFTER_CLOSE = datetime(2026, 9, 30, 16, 0, tzinfo=IST)  # same day, session complete


def epoch(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).timestamp())


def weekdays(first: date, last: date) -> list[date]:
    days, cursor = [], first
    while cursor <= last:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def session_rows(day: date, step: int = 5, base: float = 20000.0, volume=None) -> list[list]:
    """Regular session bars stamped at bar START (09:15 .. 15:25 for 5m), Groww shape."""
    rows, minute = [], 9 * 60 + 15
    while minute + step <= 15 * 60 + 30:
        price = base + (minute % 97)
        rows.append([epoch(day, minute // 60, minute % 60), price, price + 5, price - 5, price + 1, volume, None])
        minute += step
    return rows


class FakeGroww:
    """Serves candles inside the requested [start, end] window, like the real API would."""

    def __init__(self, rows: list[list] | None = None, payload=None) -> None:
        self.rows = rows or []
        self.payload = payload
        self.calls: list[dict] = []

    def get_historical_candles(self, **kwargs):
        self.calls.append(kwargs)
        if self.payload is not None:
            return self.payload
        fmt = "%Y-%m-%d %H:%M:%S"
        lo = datetime.strptime(kwargs["start_time"], fmt).replace(tzinfo=IST).timestamp()
        hi = datetime.strptime(kwargs["end_time"], fmt).replace(tzinfo=IST).timestamp()
        return {"candles": [r for r in self.rows if lo <= r[0] <= hi]}


def eight_sessions_client() -> FakeGroww:
    rows = []
    for day in weekdays(date(2026, 9, 15), date(2026, 9, 30)):  # Tue 15th .. Wed 30th (12 sessions)
        rows.extend(session_rows(day))
    return FakeGroww(rows)


# A. symbol mapping -------------------------------------------------------

@pytest.mark.parametrize("ticker, exchange, symbol", [
    ("^NSEI", "NSE", "NSE-NIFTY"),
    ("^NSEBANK", "NSE", "NSE-BANKNIFTY"),
    ("^BSESN", "BSE", "BSE-SENSEX"),
])
def test_symbol_mapping_is_sent_to_groww(ticker, exchange, symbol) -> None:
    client = FakeGroww(session_rows(date(2026, 9, 30)))
    gmd.fetch_historical_candles(ticker, "1d", "5m", client=client, now=NOW)
    call = client.calls[0]
    assert (call["exchange"], call["groww_symbol"], call["segment"]) == (exchange, symbol, "CASH")


def test_unknown_ticker_is_rejected_without_a_request() -> None:
    client = FakeGroww()
    with pytest.raises(ValueError):
        gmd.fetch_historical_candles("AAPL", "5d", "5m", client=client, now=NOW)
    assert client.calls == []


# B. interval mapping -----------------------------------------------------

@pytest.mark.parametrize("interval, expected", [
    ("1m", "1minute"), ("5m", "5minute"), ("15m", "15minute"), ("1h", "1hour"), ("1d", "1day"),
])
def test_interval_mapping(interval, expected) -> None:
    client = FakeGroww(payload={"candles": [[epoch(date(2026, 9, 30), 9, 15), 1, 2, 0.5, 1.5, None, None]]})
    gmd.fetch_historical_candles("^NSEI", "1d", interval, client=client, now=NOW)
    assert client.calls[0]["candle_interval"] == expected


def test_unsupported_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        gmd.fetch_historical_candles("^NSEI", "5d", "30m", client=FakeGroww(), now=NOW)


# C/D/E. conversion, IST, bar start ---------------------------------------

def test_5m_candle_conversion_columns_and_values() -> None:
    day = date(2026, 9, 30)
    client = FakeGroww(payload={"candles": [
        [epoch(day, 9, 15), 100, 110, 95, 105, 1000, 50],
        [epoch(day, 9, 20), "105", "112", "104", "111", None, None],
    ]})
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume", "OpenInterest"]
    assert df.index.name == "timestamp"
    assert (df.dtypes == "float64").all()
    assert df.iloc[0].tolist() == [100, 110, 95, 105, 1000, 50]
    assert df.iloc[1][["Open", "High", "Low", "Close"]].tolist() == [105, 112, 104, 111]  # strings converted


def test_index_is_timezone_aware_asia_kolkata() -> None:
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=FakeGroww(session_rows(date(2026, 9, 30))), now=NOW)
    assert str(df.index.tz) == "Asia/Kolkata"
    assert df.index[0] > datetime(2026, 9, 1, tzinfo=IST)  # comparable with aware IST datetimes (Auto Trader)


@pytest.mark.parametrize("stamp", ["2026-09-30T09:15:00+05:30", "2026-09-30 09:15:00", "2026-09-30T03:45:00Z"])
def test_string_timestamps_become_ist(stamp) -> None:
    client = FakeGroww(payload={"candles": [[stamp, 1, 2, 0.5, 1.5, None, None]]})
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)
    assert str(df.index.tz) == "Asia/Kolkata"
    assert df.index[0] == pd.Timestamp("2026-09-30 09:15", tz="Asia/Kolkata")


def test_bar_start_timestamps_are_not_shifted() -> None:
    day = date(2026, 9, 30)
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=FakeGroww(session_rows(day)), now=AFTER_CLOSE)
    assert df.index[0] == pd.Timestamp("2026-09-30 09:15", tz="Asia/Kolkata")
    assert df.index[-1] == pd.Timestamp("2026-09-30 15:25", tz="Asia/Kolkata")
    assert len(df) == 75


def test_timestamps_off_the_session_grid_are_refused() -> None:
    day = date(2026, 9, 30)
    shifted = [[r[0] + 19800] + r[1:] for r in session_rows(day)]  # IST wall-clock sent as if it were UTC
    with pytest.raises(RuntimeError, match="time grid"):
        gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=FakeGroww(payload={"candles": shifted}), now=AFTER_CLOSE)


# F. Volume / OpenInterest None -> NaN ------------------------------------

def test_none_volume_and_open_interest_become_nan_float() -> None:
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=FakeGroww(session_rows(date(2026, 9, 30))), now=NOW)
    assert df["Volume"].dtype == "float64" and df["OpenInterest"].dtype == "float64"
    assert df["Volume"].isna().all() and df["OpenInterest"].isna().all()


def test_missing_volume_columns_in_a_short_row_are_nan() -> None:
    client = FakeGroww(payload={"candles": [[epoch(date(2026, 9, 30), 9, 15), 1, 2, 0.5, 1.5]]})
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)
    assert df["Volume"].isna().all() and df["Volume"].dtype == "float64"


# G/H. duplicates and ordering -------------------------------------------

def test_duplicate_timestamps_are_removed_keeping_the_later_candle() -> None:
    day = date(2026, 9, 30)
    t = epoch(day, 9, 15)
    client = FakeGroww(payload={"candles": [[t, 1, 2, 0.5, 1.5, None, None], [t, 1, 3, 0.5, 2.0, None, None]]})
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)
    assert len(df) == 1 and df["Close"].iloc[0] == 2.0 and df.index.is_unique


def test_rows_are_sorted_ascending() -> None:
    rows = session_rows(date(2026, 9, 30))
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=FakeGroww(rows[::-1]), now=AFTER_CLOSE)
    assert df.index.is_monotonic_increasing and len(df) == 75


# I. empty / bad responses -------------------------------------------------

@pytest.mark.parametrize("payload", [{"candles": []}, {"candles": None}])
def test_empty_response_raises_runtime_error(payload) -> None:
    with pytest.raises(RuntimeError, match="No data returned"):
        gmd.fetch_historical_candles("^NSEI", "5d", "5m", client=FakeGroww(payload=payload), now=NOW)


def test_unexpected_response_shape_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="Unexpected"):
        gmd.fetch_historical_candles("^NSEI", "5d", "5m", client=FakeGroww(payload={"nope": 1}), now=NOW)


def test_all_nan_ohlc_is_not_fabricated_or_accepted() -> None:
    client = FakeGroww(payload={"candles": [[epoch(date(2026, 9, 30), 9, 15), None, None, None, None, None, None]]})
    with pytest.raises(RuntimeError, match="valid OHLC"):
        gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)


def test_fetch_underlying_data_raises_runtime_error_when_empty(monkeypatch) -> None:
    monkeypatch.setattr(gmd, "_effective_client", lambda: FakeGroww(payload={"candles": []}))
    with pytest.raises(RuntimeError, match="No data returned"):
        fno_main.fetch_underlying_data("^NSEI", "5d", "5m")


def test_fetch_underlying_data_keeps_its_interface_and_uses_the_adapter(monkeypatch) -> None:
    client = eight_sessions_client()
    monkeypatch.setattr(gmd, "_effective_client", lambda: client)
    df = fno_main.fetch_underlying_data("^NSEI")  # defaults: 5d / 5m
    assert client.calls[0]["candle_interval"] == "5minute"
    assert df.index.normalize().nunique() == 5


# J. timeout -----------------------------------------------------------------

def test_explicit_timeout_is_passed_to_groww() -> None:
    client = FakeGroww(session_rows(date(2026, 9, 30)))
    gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client, now=NOW)
    assert client.calls[0]["timeout"] == gmd.REQUEST_TIMEOUT_SECONDS and gmd.REQUEST_TIMEOUT_SECONDS > 0
    client2 = FakeGroww(session_rows(date(2026, 9, 30)))
    gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=client2, now=NOW, timeout=7)
    assert client2.calls[0]["timeout"] == 7


# K. period handling ---------------------------------------------------------

def test_5d_returns_the_last_five_trading_sessions_not_five_calendar_days() -> None:
    client = eight_sessions_client()
    df = gmd.fetch_historical_candles("^NSEI", "5d", "5m", client=client, now=AFTER_CLOSE)
    sessions = sorted(df.index.normalize().unique())
    assert len(sessions) == 5 and len(df) == 5 * 75
    # Wed 30, Tue 29, Mon 28, Fri 25, Thu 24 - a plain 5-calendar-day window would hold only 3-4 of them.
    assert [d.date() for d in sessions] == [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28),
                                            date(2026, 9, 29), date(2026, 9, 30)]
    start = datetime.strptime(client.calls[0]["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    assert start.date() <= date(2026, 9, 23)  # looked back past weekends, then trimmed


def test_1d_period_is_one_session() -> None:
    df = gmd.fetch_historical_candles("^NSEI", "1d", "5m", client=eight_sessions_client(), now=NOW)
    assert df.index.normalize().nunique() == 1


def test_fewer_sessions_than_requested_returns_what_exists(caplog) -> None:
    client = FakeGroww(session_rows(date(2026, 9, 29)) + session_rows(date(2026, 9, 30)))
    df = gmd.fetch_historical_candles("^NSEI", "5d", "5m", client=client, now=NOW)
    assert df.index.normalize().nunique() == 2


def test_wk_and_month_periods() -> None:
    assert gmd._parse_period("2wk") == ("sessions", 10)
    assert gmd._parse_period("1mo") == ("calendar_days", 30)
    assert gmd._parse_period("2y") == ("calendar_days", 730)
    assert gmd._parse_period("60d") == ("sessions", 60)


@pytest.mark.parametrize("period", ["max", "ytd", "5", "d5", "0d", "5h"])
def test_unsupported_periods_are_rejected(period) -> None:
    with pytest.raises(ValueError):
        gmd.fetch_historical_candles("^NSEI", period, "5m", client=FakeGroww(), now=NOW)


def test_long_spans_are_chunked_and_merged_without_duplicates() -> None:
    rows = []
    for day in weekdays(date(2026, 6, 1), date(2026, 9, 30)):
        rows.extend(session_rows(day))
    client = FakeGroww(rows)
    df = gmd.fetch_historical_candles("^NSEI", "60d", "5m", client=client, now=AFTER_CLOSE)
    assert len(client.calls) >= 2
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert df.index.normalize().nunique() == 60


# L. the existing strategy consumes the frame ---------------------------------

def test_strategy_runs_on_adapter_output_with_nan_volume() -> None:
    rng = np.random.default_rng(7)
    rows, price = [], 20000.0
    for day in weekdays(date(2026, 9, 17), date(2026, 9, 30)):
        for row in session_rows(day):
            price += rng.normal(0, 6)
            row[1:5] = [price - 1, price + 4, price - 4, price]
            rows.append(row)
    df = gmd.fetch_historical_candles("^NSEI", "10d", "5m", client=FakeGroww(rows), now=NOW)
    assert drop_invalid_bars(df) is df  # nothing dropped: OHLC valid
    indicators = compute_indicators(df, DEFAULT_CONFIG)
    assert indicators["vwap"].isna().all()  # NaN volume -> VWAP NaN, filter is off by default
    assert indicators["ema_fast"].notna().iloc[-1] and indicators["rsi"].notna().iloc[-1]
    results, events = run(df, DEFAULT_CONFIG, "NIFTY 50")
    assert len(results) == len(df)
    assert all(e.timestamp.tzinfo is not None for e in events)


# Shared TokenService, never a new one per request -----------------------------

class _Service:
    def __init__(self, client) -> None:
        self.client, self.calls = client, 0

    def effective_client(self):
        self.calls += 1
        return self.client


def test_registered_token_service_is_used(monkeypatch) -> None:
    monkeypatch.setattr(gmd, "_default_token_service", None)
    service = _Service(FakeGroww(session_rows(date(2026, 9, 30))))
    monkeypatch.setattr(gmd, "_registered_token_service", service)
    gmd.fetch_historical_candles("^NSEI", "1d", "5m", now=NOW)
    assert service.calls == 1 and len(service.client.calls) == 1


def test_default_token_service_is_created_once_not_per_request(monkeypatch) -> None:
    created = []

    class CountingTokenService(_Service):
        def __init__(self, settings) -> None:
            created.append(settings)
            super().__init__(FakeGroww(session_rows(date(2026, 9, 30))))

    monkeypatch.setattr(gmd, "_registered_token_service", None)
    monkeypatch.setattr(gmd, "_default_token_service", None)
    monkeypatch.setattr("algoedge.token_service.TokenService", CountingTokenService)
    monkeypatch.setattr("algoedge.config.get_settings", lambda: object())
    for _ in range(3):
        gmd.fetch_historical_candles("^NSEI", "1d", "5m", now=NOW)
    assert len(created) == 1
