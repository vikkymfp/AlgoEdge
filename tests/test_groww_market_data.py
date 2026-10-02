"""Groww paid market-data layer: parsing, shapes, timeouts, shared connection.

A fake client stands in for growwapi; no test touches the network or Groww.
Response shapes are assumptions about Groww's payloads, so every parser is
checked for the missing-value case too (NaN, never a default).
"""

from __future__ import annotations

# ruff: noqa: DTZ001  (naive datetimes are deliberate: naive means IST here)
import inspect
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIException

from algoedge import groww_market_data as gmd

IST = ZoneInfo("Asia/Kolkata")


class FakeClient:
    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        if name not in self.responses:
            raise AttributeError(name)

        def call(**kwargs):
            self.calls.append((name, kwargs))
            result = self.responses[name]
            if isinstance(result, Exception):
                raise result
            return result

        return call


class FakeService:
    def __init__(self, client):
        self.client = client
        self.requested = 0

    def effective_client(self):
        self.requested += 1
        return self.client


@pytest.fixture(autouse=True)
def _restore_shared_service():
    """The dashboard registers its own service at import; give it back."""
    previous = gmd._shared_service
    yield
    gmd.use_token_service(previous)


@pytest.fixture
def install():
    def _install(**responses):
        client = FakeClient(**responses)
        gmd.use_token_service(FakeService(client))
        return client

    return _install


SETTINGS = SimpleNamespace()  # never read: the connection is the fake service


# --------------------------------------------------------------- timestamps


def test_naive_strings_are_ist_wall_clock():
    out = gmd.parse_timestamps(pd.Series(["2026-10-01T09:15:00", "2026-10-01T09:20:00"]))
    assert str(out.iloc[0]) == "2026-10-01 09:15:00+05:30"


def test_epoch_seconds_are_absolute_not_nanoseconds():
    # 2026-10-01 03:45:00 UTC == 09:15 IST
    epoch = int(datetime(2026, 10, 1, 3, 45, tzinfo=ZoneInfo("UTC")).timestamp())
    out = gmd.parse_timestamps(pd.Series([epoch]))
    assert str(out.iloc[0]) == "2026-10-01 09:15:00+05:30"


def test_epoch_milliseconds_are_detected():
    epoch_ms = int(datetime(2026, 10, 1, 3, 45, tzinfo=ZoneInfo("UTC")).timestamp()) * 1000
    out = gmd.parse_timestamps(pd.Series([epoch_ms]))
    assert str(out.iloc[0]) == "2026-10-01 09:15:00+05:30"


def test_offset_strings_are_converted_to_ist():
    out = gmd.parse_timestamps(pd.Series(["2026-10-01T03:45:00+00:00"]))
    assert str(out.iloc[0]) == "2026-10-01 09:15:00+05:30"


# ------------------------------------------------------------------ candles


def _candles():
    return [
        ["2026-10-01T09:20:00", 101, 103, 100, 102, 50, 7],
        ["2026-10-01T09:15:00", 100, 102, 99, 101, 40, 5],
        ["2026-10-01T09:15:00", 100, 102, 99, 101.5, 41, 5],  # duplicate, last wins
    ]


def test_historical_candles_sorted_deduped_and_index_is_start(install):
    client = install(get_historical_candles={"candles": _candles()})
    df = gmd.fetch_historical_candles(SETTINGS, "^NSEI", "5d", "5m")
    assert list(df.index.strftime("%H:%M")) == ["09:15", "09:20"]
    assert df["Close"].iloc[0] == 101.5
    _name, kwargs = client.calls[0]
    assert kwargs["groww_symbol"] == "NSE-NIFTY" and kwargs["segment"] == "CASH"
    assert kwargs["candle_interval"] == "5minute"
    assert kwargs["timeout"] == gmd.CANDLE_TIMEOUT_SECONDS


def test_candles_without_open_interest_column(install):
    install(get_historical_candles={"candles": [["2026-10-01T09:15:00", 1, 2, 0.5, 1.5, 10]]})
    df = gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    assert math.isnan(df["OpenInterest"].iloc[0]) and df["Volume"].iloc[0] == 10


def test_missing_volume_stays_nan_not_zero(install):
    install(get_historical_candles={"candles": [["2026-10-01T09:15:00", 1, 2, 0.5, 1.5, None, None]]})
    df = gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    assert math.isnan(df["Volume"].iloc[0]) and math.isnan(df["OpenInterest"].iloc[0])


def test_no_candles_and_odd_shape_raise(install):
    install(get_historical_candles={"candles": []})
    with pytest.raises(RuntimeError, match="No Groww historical data"):
        gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    install(get_historical_candles={"candles": [["2026-10-01T09:15:00", 1, 2]]})
    with pytest.raises(RuntimeError, match="Unexpected Groww candle shape"):
        gmd.fetch_historical_candles(SETTINGS, "^NSEI")


def test_unsupported_ticker_and_interval():
    with pytest.raises(ValueError):
        gmd.fetch_historical_candles(SETTINGS, "AAPL")
    with pytest.raises(ValueError):
        gmd.fetch_candles(
            SETTINGS, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY",
            start=datetime.now(IST), end=datetime.now(IST), interval="7m",
        )


def test_option_candles_use_fno_segment_and_explicit_window(install):
    client = install(get_historical_candles={"candles": _candles()})
    start, end = datetime(2026, 10, 1, 9, 15, tzinfo=IST), datetime(2026, 10, 1, 15, 30, tzinfo=IST)
    gmd.fetch_option_candles(
        SETTINGS, exchange="NSE", groww_symbol="NSE-NIFTY-06Oct26-25000-CE",
        start=start, end=end, interval="15m",
    )
    kwargs = client.calls[0][1]
    assert kwargs["segment"] == "FNO" and kwargs["candle_interval"] == "15minute"
    assert kwargs["start_time"] == "2026-10-01 09:15:00" and kwargs["end_time"] == "2026-10-01 15:30:00"


# ------------------------------------------------------------------- index


def test_index_ltp_maps_back_to_tickers_and_nans_missing(install):
    client = install(get_ltp={"NSE_NIFTY": 25010.5, "NSE_BANKNIFTY": "53000.25"})
    out = gmd.fetch_index_ltp(SETTINGS)
    assert out["^NSEI"] == 25010.5 and out["^NSEBANK"] == 53000.25
    assert math.isnan(out["^BSESN"])  # not returned: never defaulted
    kwargs = client.calls[0][1]
    assert set(kwargs["exchange_trading_symbols"]) == {"NSE_NIFTY", "NSE_BANKNIFTY", "BSE_SENSEX"}
    assert kwargs["segment"] == "CASH" and kwargs["timeout"] == gmd.QUOTE_TIMEOUT_SECONDS


def test_index_ohlc(install):
    install(get_ohlc={"NSE_NIFTY": {"open": 1, "high": 3, "low": 0.5, "close": 2}})
    out = gmd.fetch_index_ohlc(SETTINGS, ["^NSEI", "^BSESN"])
    assert out["^NSEI"] == {"open": 1.0, "high": 3.0, "low": 0.5, "close": 2.0}
    assert all(math.isnan(v) for v in out["^BSESN"].values())


# ------------------------------------------------------------------ options


CHAIN = {
    "underlying_ltp": 25010.0,
    "strikes": {
        "25050": {"CE": {"trading_symbol": "NIFTY2610625050CE", "ltp": 80, "volume": 1000,
                         "open_interest": 5000,
                         "greeks": {"delta": 0.45, "gamma": 0.001, "theta": -9, "vega": 12,
                                    "rho": 3, "iv": 14.2}},
                  "PE": {"trading_symbol": "NIFTY2610625050PE", "ltp": 90}},
        "25000": {"CE": {"trading_symbol": "NIFTY2610625000CE", "ltp": 110}},
    },
}


def test_option_chain_normalised_and_sorted(install):
    client = install(get_option_chain=CHAIN)
    chain = gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    assert chain.underlying_ltp == 25010.0
    assert list(chain.rows[["strike", "right"]].itertuples(index=False, name=None)) == [
        (25000.0, "CE"), (25050.0, "CE"), (25050.0, "PE"),
    ]
    ce = chain.rows[(chain.rows.strike == 25050) & (chain.rows.right == "CE")].iloc[0]
    assert (ce.volume, ce.open_interest, ce.iv, ce.delta) == (1000, 5000, 14.2, 0.45)
    pe = chain.rows[(chain.rows.strike == 25050) & (chain.rows.right == "PE")].iloc[0]
    assert pe.ltp == 90 and math.isnan(pe.iv) and math.isnan(pe.volume)
    assert client.calls[0][1]["expiry_date"] == "2026-10-06"
    assert client.calls[0][1]["timeout"] == gmd.QUOTE_TIMEOUT_SECONDS


@pytest.mark.parametrize("payload", [{"strikes": {}}, {"underlying_ltp": 1}])
def test_empty_chain_raises(install, payload):
    install(get_option_chain=payload)
    with pytest.raises(RuntimeError):
        gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")


def test_expiries_sorted(install):
    install(get_expiries={"expiries": ["2026-10-13", "2026-10-06"]})
    assert gmd.fetch_option_expiries(SETTINGS, "NIFTY") == ["2026-10-06", "2026-10-13"]


def test_option_quote_fields(install):
    client = install(get_quote={
        "last_price": 82.5, "ohlc": {"open": 80, "high": 90, "low": 75, "close": 79},
        "volume": 12000, "open_interest": 40000, "bid_price": 82.4, "offer_price": 82.6,
    })
    quote = gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")
    assert quote["ltp"] == 82.5 and quote["high"] == 90 and quote["open_interest"] == 40000
    assert (quote["bid"], quote["ask"]) == (82.4, 82.6)
    assert quote["trading_symbol"] == "NIFTY2610625050CE"
    assert client.calls[0][1]["segment"] == "FNO"
    assert math.isnan(quote["iv"])  # absent, so missing


def test_greeks_nested_or_flat(install):
    install(get_greeks={"greeks": {"delta": 0.5, "gamma": 0.002, "theta": -8, "vega": 11,
                                   "rho": 2, "iv": 13}})
    out = gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06")
    assert out == {"delta": 0.5, "gamma": 0.002, "theta": -8.0, "vega": 11.0, "rho": 2.0, "iv": 13.0}
    install(get_greeks={"delta": 0.1})
    flat = gmd.fetch_option_greeks(SETTINGS, "NIFTY", "X", "2026-10-06")
    assert flat["delta"] == 0.1 and math.isnan(flat["vega"])


def test_non_dict_responses_raise_type_error(install):
    install(get_quote=["x"], get_option_chain=["x"], get_greeks=["x"])
    with pytest.raises(TypeError):
        gmd.fetch_option_quote(SETTINGS, "X")
    with pytest.raises(TypeError):
        gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    with pytest.raises(TypeError):
        gmd.fetch_option_greeks(SETTINGS, "NIFTY", "X", "2026-10-06")


def test_sdk_errors_propagate_unchanged(install):
    install(get_ltp=TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        gmd.fetch_index_ltp(SETTINGS)


# ---------------------------------------------------------- shared connection


def test_one_token_service_serves_every_call(install, monkeypatch):
    built: list[object] = []

    class Counting(FakeService):
        def __init__(self, settings):
            super().__init__(FakeClient(get_ltp={"NSE_NIFTY": 1}))
            built.append(self)

    gmd.use_token_service(None)
    monkeypatch.setattr(gmd, "TokenService", Counting)
    for _ in range(3):
        gmd.fetch_index_ltp(SETTINGS, ["^NSEI"])
    gmd.use_token_service(None)
    assert len(built) == 1 and built[0].requested == 3


def test_registered_service_is_used_and_not_replaced(install, monkeypatch):
    monkeypatch.setattr(gmd, "TokenService", lambda settings: pytest.fail("built a second service"))
    install(get_ltp={"NSE_NIFTY": 5})
    assert gmd.fetch_index_ltp(SETTINGS, ["^NSEI"])["^NSEI"] == 5.0


def test_web_server_registers_its_token_service():
    """Checked in a fresh interpreter: import-time registration cannot be seen
    reliably from inside a suite where web_server may already be imported."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "from algoedge import groww_market_data as g, web_server as w;"
        "print(g._shared_service is w.token_service)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=root, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{root / 'src'}:{root}", "HOME": "/nonexistent"},
    )
    # Importing web_server may print harmless banners first; the result is the last line.
    lines = result.stdout.strip().splitlines()
    assert result.returncode == 0, result.stderr[-500:]
    assert lines and lines[-1] == "True", f"stdout={result.stdout[-300:]!r} stderr={result.stderr[-300:]!r}"


# ======================================================================
# UAT verification additions: input validation, timestamp edge cases,
# partial/malformed responses, SDK signature conformance, read-only guard.
# Response FIELD NAMES below are assumptions about Groww payloads (the SDK
# only unwraps `payload`); they need confirming against a live session.
# ======================================================================

GOOD_CANDLE = [["2026-10-01T09:15:00", 100, 102, 99, 101, 40, 5]]


# ------------------------------------------------------------ timestamps


def test_epoch_ms_is_not_read_as_nanoseconds():
    """The failure mode of the original parser: ms read as ns is 1970-01-21."""
    epoch_ms = 1_759_286_100_000  # 2025-10-01 03:55:00 UTC -> 09:25 IST
    out = gmd.parse_timestamps(pd.Series([epoch_ms]))
    assert out.iloc[0].year == 2025 and str(out.iloc[0]).endswith("+05:30")


def test_digit_strings_are_epochs():
    out = gmd.parse_timestamps(pd.Series(["1759286100", "1759286400"]))
    assert out.iloc[0].year == 2025


def test_mixed_numbers_and_text_rejected():
    with pytest.raises(ValueError, match="mix"):
        gmd.parse_timestamps(pd.Series([1_759_286_100, "2026-10-01T09:15:00"], dtype=object))


@pytest.mark.parametrize("values", [[None, "2026-10-01T09:15:00"], [float("nan"), 1.0]])
def test_missing_timestamps_rejected(values):
    with pytest.raises(ValueError):
        gmd.parse_timestamps(pd.Series(values, dtype=object))


def test_epoch_zero_and_tiny_values_rejected_as_implausible():
    with pytest.raises(ValueError, match="implausible"):
        gmd.parse_timestamps(pd.Series([0, 300]))


def test_unparseable_timestamp_text_fails():
    with pytest.raises(ValueError):
        gmd.parse_timestamps(pd.Series(["not a time"]))


def test_candles_with_epoch_timestamps_end_to_end(install):
    epoch = int(datetime(2026, 10, 1, 3, 45, tzinfo=ZoneInfo("UTC")).timestamp())
    install(get_historical_candles={"candles": [[epoch, 1, 2, 0.5, 1.5, 9, 1]]})
    df = gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    assert str(df.index[0]) == "2026-10-01 09:15:00+05:30"


# ----------------------------------------------------------- fetch_candles


def test_fetch_candles_direct_call_and_naive_datetimes_are_ist(install):
    client = install(get_historical_candles={"candles": GOOD_CANDLE})
    df = gmd.fetch_candles(
        SETTINGS, exchange="BSE", segment="CASH", groww_symbol="BSE-SENSEX",
        start=datetime(2026, 10, 1, 9, 15), end=datetime(2026, 10, 1, 15, 30), interval="1d",
    )
    assert len(df) == 1
    kwargs = client.calls[0][1]
    assert kwargs["start_time"] == "2026-10-01 09:15:00"  # naive == IST, not host-local
    assert kwargs["candle_interval"] == "1day"


def test_fetch_candles_converts_aware_datetimes_to_ist(install):
    client = install(get_historical_candles={"candles": GOOD_CANDLE})
    utc = ZoneInfo("UTC")
    gmd.fetch_candles(
        SETTINGS, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY",
        start=datetime(2026, 10, 1, 3, 45, tzinfo=utc), end=datetime(2026, 10, 1, 10, 0, tzinfo=utc),
        interval="5m",
    )
    assert client.calls[0][1]["start_time"] == "2026-10-01 09:15:00"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exchange": "MCX"}, {"segment": "COMMODITY"}, {"groww_symbol": ""},
        {"groww_symbol": "NSE NIFTY; DROP"}, {"interval": "2m"},
        {"start": datetime(2026, 10, 2), "end": datetime(2026, 10, 1)},
        {"start": datetime(2026, 10, 1), "end": datetime(2026, 10, 1)},
    ],
)
def test_fetch_candles_rejects_bad_input_before_calling_groww(install, overrides):
    client = install(get_historical_candles={"candles": GOOD_CANDLE})
    args = {
        "exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-NIFTY",
        "start": datetime(2026, 10, 1), "end": datetime(2026, 10, 2), "interval": "5m", **overrides,
    }
    with pytest.raises(ValueError):
        gmd.fetch_candles(SETTINGS, **args)
    assert client.calls == []


@pytest.mark.parametrize(
    "response, error",
    [
        ({"candles": "oops"}, TypeError),
        ({"candles": [{"t": 1}]}, RuntimeError),
        ({"candles": [["2026-10-01T09:15:00", 1, 2, 3, 4, 5, 6], ["2026-10-01T09:20:00", 1]]}, RuntimeError),
        ({"candles": None}, TypeError),
        ({}, RuntimeError),
        (["not", "a", "dict"], TypeError),
        (None, TypeError),
    ],
)
def test_malformed_candle_responses_fail_safely(install, response, error):
    install(get_historical_candles=response)
    with pytest.raises(error):
        gmd.fetch_historical_candles(SETTINGS, "^NSEI")


def test_daily_and_15m_candle_intervals(install):
    client = install(get_historical_candles={"candles": GOOD_CANDLE})
    gmd.fetch_historical_candles(SETTINGS, "^NSEBANK", "1y", "1d")
    gmd.fetch_historical_candles(SETTINGS, "^BSESN", "5d", "15m")
    assert [c[1]["candle_interval"] for c in client.calls] == ["1day", "15minute"]
    assert [c[1]["groww_symbol"] for c in client.calls] == ["NSE-BANKNIFTY", "BSE-SENSEX"]
    assert client.calls[1][1]["exchange"] == "BSE"


# --------------------------------------------------------------- index data


def test_index_ltp_null_and_garbage_values_are_nan(install):
    install(get_ltp={"NSE_NIFTY": None, "NSE_BANKNIFTY": "n/a", "BSE_SENSEX": 0})
    out = gmd.fetch_index_ltp(SETTINGS)
    assert math.isnan(out["^NSEI"]) and math.isnan(out["^NSEBANK"])
    assert out["^BSESN"] == 0.0  # a genuine zero from Groww stays zero


@pytest.mark.parametrize("payload", [None, [], "x"])
def test_index_ltp_non_dict_payload_gives_all_nan(install, payload):
    install(get_ltp=payload)
    assert all(math.isnan(v) for v in gmd.fetch_index_ltp(SETTINGS, ["^NSEI"]).values())


def test_index_ohlc_partial_and_unexpected_fields(install):
    install(get_ohlc={"NSE_NIFTY": {"open": 1, "close": 2, "extra": 99}, "NSE_BANKNIFTY": "bad"})
    out = gmd.fetch_index_ohlc(SETTINGS, ["^NSEI", "^NSEBANK"])
    assert out["^NSEI"]["open"] == 1.0 and out["^NSEI"]["close"] == 2.0
    assert math.isnan(out["^NSEI"]["high"]) and set(out["^NSEI"]) == {"open", "high", "low", "close"}
    assert all(math.isnan(v) for v in out["^NSEBANK"].values())


def test_index_functions_reject_empty_and_unknown_tickers(install):
    install(get_ltp={})
    with pytest.raises(ValueError):
        gmd.fetch_index_ltp(SETTINGS, [])
    with pytest.raises(ValueError):
        gmd.fetch_index_ohlc(SETTINGS, ["AAPL"])


# ------------------------------------------------------------------ options


@pytest.mark.parametrize(
    "expiry", ["2026-13-01", "2026-02-30", "06-10-2026", "20261006", "", "2026-10-6", None],
)
def test_bad_expiry_rejected_before_any_call(install, expiry):
    client = install(get_option_chain=CHAIN, get_greeks={})
    with pytest.raises(ValueError, match="expiry"):
        gmd.fetch_option_chain(SETTINGS, "NIFTY", expiry)
    with pytest.raises(ValueError, match="expiry"):
        gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", expiry)
    assert client.calls == []


@pytest.mark.parametrize("bad", ["", " ", "NIFTY 50", "NIFTY;", "../x", "a" * 70, None, 5])
def test_bad_symbols_rejected(install, bad):
    client = install(get_quote={}, get_greeks={}, get_option_chain=CHAIN, get_expiries={"expiries": []})
    for call in (
        lambda: gmd.fetch_option_quote(SETTINGS, bad),
        lambda: gmd.fetch_option_greeks(SETTINGS, "NIFTY", bad, "2026-10-06"),
        lambda: gmd.fetch_option_greeks(SETTINGS, bad, "X", "2026-10-06"),
        lambda: gmd.fetch_option_chain(SETTINGS, bad, "2026-10-06"),
        lambda: gmd.fetch_option_expiries(SETTINGS, bad),
    ):
        with pytest.raises(ValueError):
            call()
    assert client.calls == []


def test_bad_exchange_year_month_rejected(install):
    client = install(get_expiries={"expiries": []}, get_option_chain=CHAIN)
    with pytest.raises(ValueError):
        gmd.fetch_option_expiries(SETTINGS, "NIFTY", exchange="MCX")
    with pytest.raises(ValueError):
        gmd.fetch_option_expiries(SETTINGS, "NIFTY", year=1999)
    with pytest.raises(ValueError):
        gmd.fetch_option_expiries(SETTINGS, "NIFTY", month=13)
    with pytest.raises(ValueError):
        gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06", exchange="NYSE")
    assert client.calls == []


def test_expiries_parameters_and_malformed_responses(install):
    client = install(get_expiries={"expiries": ["2026-10-06"]})
    assert gmd.fetch_option_expiries(SETTINGS, "SENSEX", "BSE", year=2026, month=10) == ["2026-10-06"]
    kwargs = client.calls[0][1]
    assert (kwargs["exchange"], kwargs["underlying_symbol"], kwargs["year"], kwargs["month"]) == (
        "BSE", "SENSEX", 2026, 10)
    install(get_expiries={})
    assert gmd.fetch_option_expiries(SETTINGS, "NIFTY") == []  # genuinely none: empty, not an error
    install(get_expiries={"expiries": "2026-10-06"})
    with pytest.raises(TypeError):
        gmd.fetch_option_expiries(SETTINGS, "NIFTY")
    install(get_expiries=None)
    with pytest.raises(TypeError):
        gmd.fetch_option_expiries(SETTINGS, "NIFTY")


def test_chain_ignores_unexpected_and_bad_strikes_and_keeps_nulls_as_nan(install):
    install(get_option_chain={
        "underlying_ltp": None,
        "unexpected": {"x": 1},
        "strikes": {
            "abc": {"CE": {"ltp": 1}},          # non-numeric strike: skipped
            "25100": "not a dict",              # skipped
            "25000": {"CE": {"ltp": None, "last_price": 7, "volume": None, "oi": 0, "mystery": 1},
                      "XX": {"ltp": 1}},        # unknown right ignored
        },
    })
    chain = gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    assert math.isnan(chain.underlying_ltp)
    assert len(chain.rows) == 1
    row = chain.rows.iloc[0]
    assert row.ltp == 7 and math.isnan(row.volume) and row.open_interest == 0
    assert list(chain.rows.columns) == gmd.CHAIN_COLUMNS


def test_chain_with_only_one_side_and_no_greeks(install):
    install(get_option_chain={"strikes": {"25000": {"PE": {"ltp": 3}}}})
    rows = gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06").rows
    assert list(rows.right) == ["PE"]
    assert all(math.isnan(rows.iloc[0][c]) for c in ("iv", *gmd.GREEK_FIELDS))


def test_quote_ohlc_nested_and_flat_and_zero_is_kept(install):
    install(get_quote={"ltp": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 0})
    quote = gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")
    assert quote["ltp"] == 0.0 and quote["volume"] == 0.0 and quote["high"] == 2.0
    assert math.isnan(quote["open_interest"]) and quote["bid"] is not None


def test_greeks_nulls_stay_nan(install):
    install(get_greeks={"greeks": {"delta": None, "gamma": "x", "theta": -4.2, "iv": 0}})
    out = gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06")
    assert math.isnan(out["delta"]) and math.isnan(out["gamma"])
    assert out["theta"] == -4.2 and out["iv"] == 0.0
    assert set(out) == {*gmd.GREEK_FIELDS, "iv"}


def test_greeks_call_arguments_match_sdk_names(install):
    client = install(get_greeks={"greeks": {}})
    gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06", exchange="NSE")
    assert client.calls[0][1] == {
        "exchange": "NSE", "underlying": "NIFTY", "trading_symbol": "NIFTY2610625050CE",
        "expiry": "2026-10-06",
    }


# --------------------------------------------------------- errors / timeouts


@pytest.mark.parametrize(
    "name, call",
    [
        ("get_ltp", lambda: gmd.fetch_index_ltp(SETTINGS)),
        ("get_ohlc", lambda: gmd.fetch_index_ohlc(SETTINGS)),
        ("get_historical_candles", lambda: gmd.fetch_historical_candles(SETTINGS, "^NSEI")),
        ("get_expiries", lambda: gmd.fetch_option_expiries(SETTINGS, "NIFTY")),
        ("get_option_chain", lambda: gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")),
        ("get_quote", lambda: gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")),
        ("get_greeks", lambda: gmd.fetch_option_greeks(SETTINGS, "NIFTY", "X", "2026-10-06")),
    ],
)
@pytest.mark.parametrize("failure", [GrowwAPIException(code="500", msg="boom"), TimeoutError("t")])
def test_every_call_propagates_sdk_failures(install, name, call, failure):
    install(**{name: failure})
    with pytest.raises(type(failure)):
        call()


def test_every_call_except_greeks_passes_an_explicit_timeout(install):
    client = install(
        get_ltp={}, get_ohlc={}, get_historical_candles={"candles": GOOD_CANDLE},
        get_expiries={"expiries": []}, get_option_chain=CHAIN, get_quote={}, get_greeks={},
    )
    gmd.fetch_index_ltp(SETTINGS)
    gmd.fetch_index_ohlc(SETTINGS)
    gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    gmd.fetch_option_expiries(SETTINGS, "NIFTY")
    gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")
    gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06")
    timeouts = {name: kwargs.get("timeout") for name, kwargs in client.calls}
    assert timeouts.pop("get_greeks") is None
    assert timeouts["get_historical_candles"] == gmd.CANDLE_TIMEOUT_SECONDS == 30
    assert set(timeouts) - {"get_historical_candles"} == {
        "get_ltp", "get_ohlc", "get_expiries", "get_option_chain", "get_quote"}
    assert all(timeouts[n] == gmd.QUOTE_TIMEOUT_SECONDS == 10 for n in timeouts if n != "get_historical_candles")


def test_sdk_get_greeks_has_no_timeout_parameter_so_none_is_passed():
    """Documents the limitation. If a future SDK adds `timeout`, this fails and
    the call in fetch_option_greeks must start passing it."""
    assert "timeout" not in inspect.signature(GrowwAPI.get_greeks).parameters


# -------------------------------------------------- SDK signature conformance


def test_every_call_binds_to_the_real_sdk_signatures(install):
    """kwargs we send must be accepted by the installed growwapi methods
    (names, required parameters, no unknown arguments). No network."""
    client = install(
        get_ltp={}, get_ohlc={}, get_historical_candles={"candles": GOOD_CANDLE},
        get_expiries={"expiries": []}, get_option_chain=CHAIN, get_quote={}, get_greeks={},
    )
    gmd.fetch_index_ltp(SETTINGS)
    gmd.fetch_index_ohlc(SETTINGS)
    gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    gmd.fetch_option_candles(
        SETTINGS, exchange="NSE", groww_symbol="NSE-NIFTY-06Oct26-25000-CE",
        start=datetime(2026, 10, 1, 9, 15), end=datetime(2026, 10, 1, 15, 30),
    )
    gmd.fetch_option_expiries(SETTINGS, "NIFTY", year=2026, month=10)
    gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")
    gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06")
    assert len(client.calls) == 8
    for name, kwargs in client.calls:
        inspect.signature(getattr(GrowwAPI, name)).bind(None, **kwargs)  # TypeError if wrong


def test_sdk_interval_and_segment_constants_match_what_we_send():
    assert GrowwAPI.CANDLE_INTERVAL_MIN_5 == "5minute"
    assert GrowwAPI.CANDLE_INTERVAL_MIN_15 == "15minute"
    assert GrowwAPI.CANDLE_INTERVAL_DAY == "1day"
    assert GrowwAPI.CANDLE_INTERVAL_HOUR_1 == "1hour"
    assert GrowwAPI.CANDLE_INTERVAL_MIN_1 == "1minute"
    assert (GrowwAPI.SEGMENT_CASH, GrowwAPI.SEGMENT_FNO) == ("CASH", "FNO")
    assert (GrowwAPI.EXCHANGE_NSE, GrowwAPI.EXCHANGE_BSE) == ("NSE", "BSE")


# ----------------------------------------------------------------- read-only


def test_module_cannot_reach_any_order_api():
    source = Path(gmd.__file__).read_text()
    for forbidden in ("place_order", "modify_order", "cancel_order", "execute_market_order"):
        assert forbidden not in source.replace("Nothing here can place, modify or cancel an order", "")


def test_only_read_methods_are_ever_called(install):
    """The fake raises AttributeError for any method not configured, so an
    order call anywhere in these paths would fail this test."""
    install(
        get_ltp={}, get_ohlc={}, get_historical_candles={"candles": GOOD_CANDLE},
        get_expiries={"expiries": []}, get_option_chain=CHAIN, get_quote={}, get_greeks={},
    )
    gmd.fetch_index_ltp(SETTINGS)
    gmd.fetch_historical_candles(SETTINGS, "^NSEI")
    gmd.fetch_option_chain(SETTINGS, "NIFTY", "2026-10-06")
    gmd.fetch_option_quote(SETTINGS, "NIFTY2610625050CE")
    gmd.fetch_option_greeks(SETTINGS, "NIFTY", "NIFTY2610625050CE", "2026-10-06")


# ----------------------------------------------------- shared TokenService


def test_cli_live_session_is_the_one_market_data_uses(monkeypatch):
    """`fno_signals --live` builds a TokenService for orders; candle fetches in
    that process must reuse it instead of constructing a second one."""
    from fno_signals import broker

    service = FakeService(FakeClient())
    monkeypatch.setattr(broker, "TokenService", lambda settings: service)
    gmd.use_token_service(None)
    broker.generate_daily_session(SETTINGS)
    assert gmd._shared_service is service


def test_no_credentials_in_module_or_exceptions(install):
    source = Path(gmd.__file__).read_text().lower()
    assert "logging" not in source and "print(" not in source
    install(get_ltp=GrowwAPIException(code="401", msg="Unauthorised"))
    with pytest.raises(GrowwAPIException) as info:
        gmd.fetch_index_ltp(SETTINGS)
    assert "token" not in str(info.value).lower()
