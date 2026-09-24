import pandas as pd
import pytest

from algoedge.market_pulse import (
    TIMEFRAMES,
    _format_candles,
    _summarize,
    get_index_candles,
    get_index_summary,
)


def make_history(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([r[0] for r in rows], tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
        },
        index=index,
    )


def test_summarize_computes_change_against_previous_day_close() -> None:
    history = make_history(
        [
            ("2026-09-21 09:15", 100, 101, 99, 100),
            ("2026-09-21 15:25", 101, 102, 100, 102),
            ("2026-09-22 09:15", 103, 104, 102, 103),
            ("2026-09-22 09:20", 104, 105, 103, 105),
        ]
    )

    result = _summarize(history)

    assert result["price"] == 105.0
    assert result["change"] == pytest.approx(3.0)
    assert result["change_percent"] == pytest.approx(3.0 / 102 * 100)
    assert result["sparkline"] == [103.0, 105.0]


def test_summarize_uses_first_close_as_previous_when_only_one_day_present() -> None:
    history = make_history(
        [
            ("2026-09-22 09:15", 100, 101, 99, 100),
            ("2026-09-22 09:20", 101, 102, 100, 102),
        ]
    )

    result = _summarize(history)

    assert result["price"] == 102.0
    assert result["change"] == pytest.approx(2.0)


def test_summarize_returns_none_fields_for_empty_history() -> None:
    result = _summarize(pd.DataFrame({"Close": []}))

    assert result == {"price": None, "change": None, "change_percent": None, "sparkline": []}


def test_format_candles_maps_ohlc_rows() -> None:
    history = make_history([("2026-09-22 09:15", 100, 101, 99, 100.5)])

    candles = _format_candles(history)

    assert len(candles) == 1
    assert candles[0]["open"] == 100.0
    assert candles[0]["high"] == 101.0
    assert candles[0]["low"] == 99.0
    assert candles[0]["close"] == 100.5
    assert isinstance(candles[0]["time"], int)


def test_format_candles_drops_incomplete_rows() -> None:
    history = make_history([("2026-09-22 09:15", 100, 101, 99, 100)])
    history.loc[history.index[0], "Close"] = float("nan")

    assert _format_candles(history) == []


def test_get_index_candles_rejects_unsupported_timeframe() -> None:
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        get_index_candles("nifty-50", "3w")


def test_get_index_summary_handles_data_errors_gracefully(monkeypatch) -> None:
    from algoedge import market_pulse

    class BrokenTicker:
        def history(self, **_kwargs):
            raise OSError("network down")

    monkeypatch.setattr(market_pulse.yf, "Ticker", lambda _symbol: BrokenTicker())
    market_pulse._cache.clear()

    summary = get_index_summary("nifty-50")

    assert summary.price is None
    assert summary.sparkline == []


def test_all_timeframes_map_to_yfinance_period_and_interval() -> None:
    assert set(TIMEFRAMES) == {"1m", "5m", "15m", "1h", "1d"}
    for period, interval in TIMEFRAMES.values():
        assert period and interval
