from datetime import datetime, timedelta

import pandas as pd
import pytest

from algoedge.backtest import (
    compute_backtest_metrics,
    compute_regime_labels,
    pair_trades,
    split_train_validation_test,
    walk_forward_windows,
)
from algoedge.risk_manager import IST
from fno_signals.strategy import TradeEvent

BASE = datetime(2026, 9, 24, 9, 15, tzinfo=IST)


def entry(kind, underlying_price, minute_offset, strike=24500, option_symbol="NIFTY 24500 CE"):
    right = "CE" if kind == "ENTRY_CALL" else "PE"
    return TradeEvent(
        timestamp=BASE + timedelta(minutes=minute_offset), kind=kind, underlying_price=underlying_price,
        option_symbol=option_symbol, stop_loss=underlying_price - 20, target=underlying_price + 40,
        exit_level=None, strike=strike, right=right,
    )


def exit_event(kind, exit_level, minute_offset):
    return TradeEvent(
        timestamp=BASE + timedelta(minutes=minute_offset), kind=kind, underlying_price=exit_level,
        option_symbol=None, stop_loss=None, target=None, exit_level=exit_level,
    )


# -- pair_trades ----------------------------------------------


def test_pairs_a_winning_call_round_trip() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]

    trades = pair_trades(events)

    assert len(trades) == 1
    assert trades[0].direction == "CALL"
    assert trades[0].points == pytest.approx(40.0)
    assert trades[0].exit_reason == "TARGET"


def test_pairs_a_losing_call_round_trip() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_SL", 24480, 5)]

    trades = pair_trades(events)

    assert trades[0].points == pytest.approx(-20.0)
    assert trades[0].exit_reason == "SL"


def test_put_direction_profits_when_price_falls() -> None:
    events = [entry("ENTRY_PUT", 24500, 0), exit_event("EXIT_TARGET", 24460, 5)]

    trades = pair_trades(events)

    assert trades[0].direction == "PUT"
    assert trades[0].points == pytest.approx(40.0)


def test_put_direction_loses_when_price_rises() -> None:
    events = [entry("ENTRY_PUT", 24500, 0), exit_event("EXIT_SL", 24520, 5)]

    trades = pair_trades(events)

    assert trades[0].points == pytest.approx(-20.0)


def test_pairs_multiple_sequential_round_trips() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5),
        entry("ENTRY_PUT", 24540, 10), exit_event("EXIT_SL", 24560, 15),
    ]

    trades = pair_trades(events)

    assert len(trades) == 2
    assert trades[0].points == pytest.approx(40.0)
    assert trades[1].points == pytest.approx(-20.0)


def test_an_unpaired_trailing_entry_is_not_counted_as_a_trade() -> None:
    events = [entry("ENTRY_CALL", 24500, 0)]  # never exits within the window

    trades = pair_trades(events)

    assert trades == []


def test_assumed_slippage_worsens_a_call_exit() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]

    trades = pair_trades(events, assumed_slippage_points=5.0)

    assert trades[0].points == pytest.approx(35.0)  # 40 - 5 slippage


def test_assumed_slippage_worsens_a_put_exit() -> None:
    events = [entry("ENTRY_PUT", 24500, 0), exit_event("EXIT_TARGET", 24460, 5)]

    trades = pair_trades(events, assumed_slippage_points=5.0)

    assert trades[0].points == pytest.approx(35.0)


# -- compute_backtest_metrics ----------------------------------------------


def test_empty_trades_returns_zeroed_metrics_not_a_crash() -> None:
    metrics = compute_backtest_metrics([])

    assert metrics.total_trades == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None


def test_basic_win_loss_metrics() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5),   # +40
        entry("ENTRY_CALL", 24540, 10), exit_event("EXIT_SL", 24520, 15),     # -20
    ]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.total_trades == 2
    assert metrics.wins == 1
    assert metrics.losses == 1
    assert metrics.win_rate == pytest.approx(50.0)
    assert metrics.net_points == pytest.approx(20.0)
    assert metrics.average_trade_points == pytest.approx(10.0)
    assert metrics.largest_win_points == pytest.approx(40.0)
    assert metrics.largest_loss_points == pytest.approx(-20.0)


def test_profit_factor_is_none_when_there_are_no_losses() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.profit_factor is None


def test_profit_factor_is_gross_win_over_gross_loss() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5),   # +40
        entry("ENTRY_CALL", 24540, 10), exit_event("EXIT_SL", 24520, 15),     # -20
    ]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.profit_factor == pytest.approx(2.0)  # 40 / 20


def test_max_consecutive_losses_counts_the_longest_streak() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_SL", 24480, 5),     # loss 1
        entry("ENTRY_CALL", 24480, 10), exit_event("EXIT_SL", 24460, 15),   # loss 2
        entry("ENTRY_CALL", 24460, 20), exit_event("EXIT_TARGET", 24500, 25),  # win, resets
        entry("ENTRY_CALL", 24500, 30), exit_event("EXIT_SL", 24480, 35),   # loss 1 again
    ]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.max_consecutive_losses == 2


def test_max_drawdown_is_the_largest_peak_to_trough_decline() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24600, 5),   # +100, peak
        entry("ENTRY_CALL", 24600, 10), exit_event("EXIT_SL", 24560, 15),     # -40
        entry("ENTRY_CALL", 24560, 20), exit_event("EXIT_SL", 24530, 25),     # -30 -> cumulative 30, dd from peak 100 = 70
    ]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.max_drawdown_points == pytest.approx(70.0)


def test_call_and_put_performance_are_tracked_independently() -> None:
    events = [
        entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5),
        entry("ENTRY_PUT", 24540, 10), exit_event("EXIT_SL", 24560, 15),
    ]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades)

    assert metrics.call_performance.trades == 1
    assert metrics.call_performance.net_points == pytest.approx(40.0)
    assert metrics.put_performance.trades == 1
    assert metrics.put_performance.net_points == pytest.approx(-20.0)


def test_time_of_day_buckets_by_entry_hour() -> None:
    morning_entry = TradeEvent(
        timestamp=datetime(2026, 9, 24, 9, 20, tzinfo=IST), kind="ENTRY_CALL", underlying_price=24500,
        option_symbol="x", stop_loss=24480, target=24540, exit_level=None, strike=24500, right="CE",
    )
    morning_exit = TradeEvent(
        timestamp=datetime(2026, 9, 24, 9, 25, tzinfo=IST), kind="EXIT_TARGET", underlying_price=24540,
        option_symbol=None, stop_loss=None, target=None, exit_level=24540,
    )
    afternoon_entry = TradeEvent(
        timestamp=datetime(2026, 9, 24, 14, 20, tzinfo=IST), kind="ENTRY_CALL", underlying_price=24500,
        option_symbol="x", stop_loss=24480, target=24540, exit_level=None, strike=24500, right="CE",
    )
    afternoon_exit = TradeEvent(
        timestamp=datetime(2026, 9, 24, 14, 25, tzinfo=IST), kind="EXIT_SL", underlying_price=24480,
        option_symbol=None, stop_loss=None, target=None, exit_level=24480,
    )
    trades = pair_trades([morning_entry, morning_exit, afternoon_entry, afternoon_exit])

    metrics = compute_backtest_metrics(trades)

    labels = {bucket.label: bucket for bucket in metrics.time_of_day_performance}
    assert "09:00-09:59" in labels
    assert "14:00-14:59" in labels
    assert labels["09:00-09:59"].net_points == pytest.approx(40.0)
    assert labels["14:00-14:59"].net_points == pytest.approx(-20.0)


def test_market_regime_breakdown_uses_the_supplied_labels() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]
    trades = pair_trades(events)
    regime_by_time = {trades[0].entry_time: "ABOVE_SMA"}

    metrics = compute_backtest_metrics(trades, regime_by_time)

    assert len(metrics.market_regime_performance) == 1
    assert metrics.market_regime_performance[0].label == "ABOVE_SMA"


def test_trades_missing_a_regime_label_are_not_guessed_into_a_bucket() -> None:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]
    trades = pair_trades(events)

    metrics = compute_backtest_metrics(trades, regime_by_time={})

    assert metrics.market_regime_performance == []


# -- compute_regime_labels ----------------------------------------------


def test_compute_regime_labels_classifies_above_and_below_sma() -> None:
    index = pd.date_range("2026-09-01", periods=5, freq="D")
    df = pd.DataFrame({"Close": [100.0, 100.0, 100.0, 200.0, 50.0]}, index=index)

    labels = compute_regime_labels(df, sma_length=3)

    # bar 3 (index 3): sma of [100,100,200]=133.3, close=200 -> ABOVE
    # bar 4 (index 4): sma of [100,200,50]=116.7, close=50 -> BELOW
    assert labels[index[3]] == "ABOVE_SMA"
    assert labels[index[4]] == "BELOW_SMA"


def test_compute_regime_labels_skips_bars_before_the_lookback_fills() -> None:
    index = pd.date_range("2026-09-01", periods=3, freq="D")
    df = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=index)

    labels = compute_regime_labels(df, sma_length=3)

    assert index[0] not in labels
    assert index[1] not in labels


# -- split_train_validation_test ----------------------------------------------


def test_split_train_validation_test_is_chronological_and_non_overlapping() -> None:
    df = pd.DataFrame({"Close": range(100)})

    splits = split_train_validation_test(df, train_fraction=0.6, validation_fraction=0.2)

    assert len(splits["train"]) == 60
    assert len(splits["validation"]) == 20
    assert len(splits["out_of_sample"]) == 20
    assert splits["train"].index[-1] < splits["validation"].index[0]
    assert splits["validation"].index[-1] < splits["out_of_sample"].index[0]


def test_split_rejects_fractions_that_leave_no_out_of_sample_room() -> None:
    df = pd.DataFrame({"Close": range(100)})

    with pytest.raises(ValueError, match="out-of-sample"):
        split_train_validation_test(df, train_fraction=0.7, validation_fraction=0.4)


def test_split_rejects_a_fraction_outside_zero_to_one() -> None:
    df = pd.DataFrame({"Close": range(100)})

    with pytest.raises(ValueError):
        split_train_validation_test(df, train_fraction=1.5, validation_fraction=0.2)


# -- walk_forward_windows ----------------------------------------------


def test_walk_forward_windows_tile_the_series_without_overlap_by_default() -> None:
    df = pd.DataFrame({"Close": range(100)})

    windows = walk_forward_windows(df, train_size=30, test_size=10)

    assert len(windows) == 7  # (100 - 30) // 10 = 7 full windows
    assert len(windows[0].train) == 30
    assert len(windows[0].test) == 10
    assert windows[0].train.index[-1] < windows[0].test.index[0]


def test_walk_forward_windows_advance_by_step() -> None:
    df = pd.DataFrame({"Close": range(50)})

    windows = walk_forward_windows(df, train_size=20, test_size=5, step=5)

    assert windows[0].train.index[0] == 0
    assert windows[1].train.index[0] == 5  # advanced by step, not train_size + test_size


def test_walk_forward_windows_each_test_window_never_reused_as_training_data() -> None:
    df = pd.DataFrame({"Close": range(60)})

    windows = walk_forward_windows(df, train_size=20, test_size=10, step=10)

    for window in windows:
        test_indices = set(window.test.index)
        train_indices = set(window.train.index)
        assert test_indices.isdisjoint(train_indices)


def test_walk_forward_windows_rejects_nonpositive_sizes() -> None:
    df = pd.DataFrame({"Close": range(10)})

    with pytest.raises(ValueError):
        walk_forward_windows(df, train_size=0, test_size=5)
