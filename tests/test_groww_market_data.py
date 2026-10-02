"""Groww paid market-data layer: parsing, shapes, timeouts, shared connection.

A fake client stands in for growwapi; no test touches the network or Groww.
Response shapes are assumptions about Groww's payloads, so every parser is
checked for the missing-value case too (NaN, never a default).
"""

from __future__ import annotations

import math
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

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
    from algoedge import web_server

    assert gmd._shared_service is web_server.token_service
