import numpy as np
import pandas as pd
import pytest

from fno_signals.indicators import atr, ema, rma, round_to_strike, rsi, session_vwap, supertrend


def test_ema_of_constant_series_equals_the_constant() -> None:
    closes = pd.Series([50.0] * 10)

    assert ema(closes, length=5).tolist() == pytest.approx([50.0] * 10)


def test_rma_seeds_with_sma_of_first_length_values_then_recurses() -> None:
    series = pd.Series([float(v) for v in range(1, 21)])

    result = rma(series, length=5)

    assert result.iloc[:4].isna().all()
    assert result.iloc[4] == pytest.approx(sum(range(1, 6)) / 5)  # SMA seed
    assert result.iloc[5] == pytest.approx((result.iloc[4] * 4 + 6) / 5)  # Wilder recursion


def test_rma_differs_from_plain_ewm_seed() -> None:
    series = pd.Series([10.0, 20.0, 10.0, 20.0, 10.0, 20.0])
    plain_ewm = series.ewm(alpha=1 / 3, adjust=False).mean()

    result = rma(series, length=3)

    assert result.iloc[2] != pytest.approx(plain_ewm.iloc[2])


def test_rsi_is_100_for_a_pure_uptrend_avg_loss_zero() -> None:
    closes = pd.Series([float(v) for v in range(1, 30)])

    assert rsi(closes, length=14).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_for_a_pure_downtrend_avg_gain_zero() -> None:
    closes = pd.Series([float(v) for v in range(30, 1, -1)])

    assert rsi(closes, length=14).iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_series_favors_avg_loss_zero_branch() -> None:
    # Pine: down == 0 ? 100 : ... — takes precedence even when up is also 0.
    closes = pd.Series([100.0] * 20)

    assert rsi(closes, length=14).iloc[-1] == pytest.approx(100.0)


def test_atr_uses_rma_not_plain_ema() -> None:
    high = pd.Series([12.0, 13.0, 11.0, 14.0, 10.0, 15.0, 9.0, 16.0])
    low = pd.Series([8.0, 9.0, 7.0, 10.0, 6.0, 11.0, 5.0, 12.0])
    close = pd.Series([10.0, 11.0, 9.0, 12.0, 8.0, 13.0, 7.0, 14.0])

    result = atr(high, low, close, length=4)

    assert result.iloc[:2].isna().all()
    assert not np.isnan(result.iloc[3])


def test_supertrend_direction_is_negative_in_a_strong_uptrend() -> None:
    n = 40
    close = pd.Series([100.0 + i * 2 for i in range(n)])
    high = close + 1
    low = close - 1

    _line, direction = supertrend(high, low, close, length=10, multiplier=3.0)

    assert direction.iloc[-1] == -1.0  # uptrend


def test_supertrend_direction_is_positive_in_a_strong_downtrend() -> None:
    n = 40
    close = pd.Series([500.0 - i * 2 for i in range(n)])
    high = close + 1
    low = close - 1

    _line, direction = supertrend(high, low, close, length=10, multiplier=3.0)

    assert direction.iloc[-1] == 1.0  # downtrend


def test_supertrend_seeds_direction_up_before_atr_is_valid() -> None:
    close = pd.Series([100.0, 101.0, 102.0])
    high = close + 1
    low = close - 1

    _line, direction = supertrend(high, low, close, length=10, multiplier=3.0)

    assert direction.iloc[0] == 1.0


def test_session_vwap_hand_computed_two_bars() -> None:
    index = pd.date_range("2026-09-23 09:15", periods=2, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "High": [10.0, 12.0],
            "Low": [8.0, 10.0],
            "Close": [9.0, 11.0],
            "Volume": [100.0, 50.0],
        },
        index=index,
    )

    result = session_vwap(df)

    assert result.iloc[0] == pytest.approx(9.0)
    assert result.iloc[1] == pytest.approx(((9.0 * 100) + (11.0 * 50)) / 150)


def test_session_vwap_resets_at_a_new_calendar_day() -> None:
    index = pd.DatetimeIndex(
        ["2026-09-22 15:25", "2026-09-23 09:15"], tz="Asia/Kolkata"
    )
    df = pd.DataFrame(
        {"High": [10.0, 20.0], "Low": [8.0, 18.0], "Close": [9.0, 19.0], "Volume": [100.0, 100.0]},
        index=index,
    )

    result = session_vwap(df)

    assert result.iloc[1] == pytest.approx(19.0)  # fresh session, not blended with day 1


def test_session_vwap_is_nan_for_zero_volume_index_data() -> None:
    index = pd.date_range("2026-09-23 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {"High": [10.0, 11.0, 12.0], "Low": [9.0, 10.0, 11.0], "Close": [9.5, 10.5, 11.5], "Volume": [0.0, 0.0, 0.0]},
        index=index,
    )

    result = session_vwap(df)

    assert result.isna().all()


@pytest.mark.parametrize(
    "price,step,expected",
    [
        (24523, 50, 24500),
        (24525, 50, 24550),  # tie rounds up (away from zero), like Pine's math.round
        (24549, 50, 24550),
        (100, 100, 100),
    ],
)
def test_round_to_strike(price: float, step: int, expected: int) -> None:
    assert round_to_strike(price, step) == expected
