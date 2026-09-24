import pandas as pd
import pytest

from algoedge.indicators import ema, macd, rsi, vwap


def test_ema_of_constant_series_equals_the_constant() -> None:
    closes = pd.Series([50.0] * 10)

    result = ema(closes, length=5)

    assert result.tolist() == pytest.approx([50.0] * 10)


def test_ema_reacts_to_a_price_jump() -> None:
    closes = pd.Series([10.0] * 5 + [20.0] * 5)

    result = ema(closes, length=3)

    assert result.iloc[4] == pytest.approx(10.0)
    assert result.iloc[-1] > 15.0
    assert result.iloc[-1] < 20.0


def test_rsi_is_100_for_a_pure_uptrend() -> None:
    closes = pd.Series([float(value) for value in range(1, 21)])

    result = rsi(closes, length=14)

    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_for_a_pure_downtrend() -> None:
    closes = pd.Series([float(value) for value in range(20, 0, -1)])

    result = rsi(closes, length=14)

    assert result.iloc[-1] == pytest.approx(0.0)


def test_macd_is_zero_for_a_constant_series() -> None:
    closes = pd.Series([100.0] * 40)

    result = macd(closes, fast_length=12, slow_length=26, signal_length=9)

    assert result["macd"].iloc[-1] == pytest.approx(0.0)
    assert result["signal"].iloc[-1] == pytest.approx(0.0)
    assert result["histogram"].iloc[-1] == pytest.approx(0.0)


def test_macd_is_positive_when_price_is_rising() -> None:
    closes = pd.Series([float(value) for value in range(1, 61)])

    result = macd(closes, fast_length=12, slow_length=26, signal_length=9)

    assert result["macd"].iloc[-1] > 0


def test_vwap_matches_hand_computed_weighted_average() -> None:
    candles = pd.DataFrame(
        {
            "High": [10.0, 12.0],
            "Low": [8.0, 10.0],
            "Close": [9.0, 11.0],
            "Volume": [100.0, 50.0],
        }
    )

    result = vwap(candles)

    assert result.iloc[0] == pytest.approx(9.0)
    expected_second = ((9.0 * 100) + (11.0 * 50)) / 150
    assert result.iloc[1] == pytest.approx(expected_second)


def test_vwap_is_nan_when_no_volume_traded() -> None:
    candles = pd.DataFrame({"High": [10.0], "Low": [8.0], "Close": [9.0], "Volume": [0.0]})

    result = vwap(candles)

    assert pd.isna(result.iloc[0])
