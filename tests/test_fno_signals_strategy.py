import pandas as pd
import pytest

from fno_signals import strategy as strategy_module
from fno_signals.config import DEFAULT_CONFIG
from fno_signals.strategy import run


def make_df(n: int, close=100.0, high=None, low=None) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = close if isinstance(close, list) else [close] * n
    highs = high if high is not None else closes
    lows = low if low is not None else closes
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": [0.0] * n},
        index=index,
    )


def patch_engine(monkeypatch, bull, bear, in_session=None, atr_value=10.0):
    n = len(bull)

    def fake_compute_indicators(df, config):
        result = df.copy()
        result["atr"] = atr_value
        result["ema_fast"] = df["Close"]
        result["ema_slow"] = df["Close"]
        result["rsi"] = 50.0
        result["supertrend"] = df["Close"]
        result["supertrend_dir"] = 0.0
        result["vwap"] = df["Close"]
        return result

    def fake_compute_setups(indicators, config):
        return pd.Series(bull, index=indicators.index), pd.Series(bear, index=indicators.index)

    def fake_session_filter(index, config):
        values = in_session if in_session is not None else [True] * n
        return pd.Series(values, index=index)

    monkeypatch.setattr(strategy_module, "compute_indicators", fake_compute_indicators)
    monkeypatch.setattr(strategy_module, "compute_setups", fake_compute_setups)
    monkeypatch.setattr(strategy_module, "compute_session_filter", fake_session_filter)


def test_entry_triggers_only_on_first_bar_setup_turns_true(monkeypatch) -> None:
    bull = [False, True, True, True]
    bear = [False, False, False, False]
    patch_engine(monkeypatch, bull, bear)
    df = make_df(4)

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    assert results["call_signal"].tolist() == [False, True, False, False]
    assert len(events) == 1
    assert events[0].kind == "ENTRY_CALL"


def test_option_symbol_and_risk_levels_use_atr_and_strike_step(monkeypatch) -> None:
    bull = [False, True]
    bear = [False, False]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    df = make_df(2, close=[100.0, 24523.0])

    _results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    entry = events[0]
    assert entry.option_symbol == "NIFTY 24500 CE"
    assert entry.underlying_price == pytest.approx(24523.0)
    assert entry.stop_loss == pytest.approx(24523.0 - 10.0 * 1.5)
    assert entry.target == pytest.approx(24523.0 + 10.0 * 1.5 * (4.5 / 1.5))


def test_no_entry_while_a_position_is_already_open(monkeypatch) -> None:
    bull = [False, True, True, True]
    bear = [False, False, False, False]
    patch_engine(monkeypatch, bull, bear)
    df = make_df(4)

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    assert results["pos"].tolist() == [0, 1, 1, 1]
    assert sum(1 for e in events if e.kind == "ENTRY_CALL") == 1


def test_exit_on_stop_loss(monkeypatch) -> None:
    bull = [False, True, True]
    bear = [False, False, False]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    # entry at bar1 close=100 -> sl = 100 - 15 = 85; bar2 low breaches it.
    df = make_df(3, close=[100.0, 100.0, 100.0], low=[100.0, 100.0, 80.0], high=[100.0, 100.0, 100.0])

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    assert results["pos"].tolist() == [0, 1, 0]
    exit_events = [e for e in events if e.kind == "EXIT_SL"]
    assert len(exit_events) == 1
    assert exit_events[0].exit_level == pytest.approx(85.0)


def test_exit_on_target(monkeypatch) -> None:
    bull = [False, True, True]
    bear = [False, False, False]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    # entry at bar1 close=100 -> tp = 100 + 45 = 145; bar2 high reaches it.
    df = make_df(3, close=[100.0, 100.0, 100.0], high=[100.0, 100.0, 150.0], low=[100.0, 100.0, 100.0])

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    assert results["pos"].tolist() == [0, 1, 0]
    exit_events = [e for e in events if e.kind == "EXIT_TARGET"]
    assert len(exit_events) == 1
    assert exit_events[0].exit_level == pytest.approx(145.0)


def test_put_entry_and_stop_loss_direction(monkeypatch) -> None:
    bull = [False, False, False]
    bear = [False, True, True]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    # short entry at bar1 close=100 -> sl = 100 + 15 = 115 (above entry); bar2 high breaches it.
    df = make_df(3, close=[100.0, 100.0, 100.0], high=[100.0, 100.0, 120.0], low=[100.0, 100.0, 100.0])

    _results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    entry = next(e for e in events if e.kind == "ENTRY_PUT")
    assert entry.option_symbol == "NIFTY 100 PE"
    assert entry.stop_loss == pytest.approx(115.0)
    exit_event = next(e for e in events if e.kind == "EXIT_SL")
    assert exit_event.exit_level == pytest.approx(115.0)


def test_no_reentry_on_same_bar_as_exit_even_if_setup_still_true(monkeypatch) -> None:
    # setup stays continuously true across the whole window - only ONE entry
    # should ever fire, even after the position is stopped out, because the
    # edge condition (setup and not setup[1]) never re-fires.
    bull = [False, True, True, True, True]
    bear = [False, False, False, False, False]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    df = make_df(
        5,
        close=[100.0] * 5,
        low=[100.0, 100.0, 80.0, 100.0, 100.0],  # bar2 stops out the bar1 entry
        high=[100.0] * 5,
    )

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    entries = [e for e in events if e.kind == "ENTRY_CALL"]
    assert len(entries) == 1
    assert results["pos"].tolist() == [0, 1, 0, 0, 0]


def test_reentry_after_exit_when_setup_toggles_off_then_on_again(monkeypatch) -> None:
    bull = [False, True, True, False, True]
    bear = [False, False, False, False, False]
    patch_engine(monkeypatch, bull, bear, atr_value=10.0)
    df = make_df(
        5,
        close=[100.0] * 5,
        low=[100.0, 100.0, 80.0, 100.0, 100.0],
        high=[100.0] * 5,
    )

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    entries = [e for e in events if e.kind == "ENTRY_CALL"]
    assert len(entries) == 2
    assert results["pos"].tolist() == [0, 1, 0, 0, 1]


def test_session_filter_blocks_entries_outside_the_window(monkeypatch) -> None:
    bull = [False, True, True]
    bear = [False, False, False]
    patch_engine(monkeypatch, bull, bear, in_session=[True, False, True])
    df = make_df(3)

    results, events = run(df, DEFAULT_CONFIG, "NIFTY")

    assert not any(e.kind == "ENTRY_CALL" for e in events)
    assert results["pos"].tolist() == [0, 0, 0]
