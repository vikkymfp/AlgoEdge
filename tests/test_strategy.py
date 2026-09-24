import pandas as pd
import pytest

from algoedge.strategy import moving_average_signal


def test_moving_average_signal_returns_buy_when_short_average_is_higher() -> None:
    prices = pd.Series(list(range(1, 51)))

    signal = moving_average_signal(prices, short_window=5, long_window=20)

    assert signal.action == "buy"
    assert signal.price == 50.0


def test_moving_average_signal_rejects_invalid_windows() -> None:
    with pytest.raises(ValueError, match="short_window"):
        moving_average_signal(pd.Series(range(1, 51)), short_window=20, long_window=20)
