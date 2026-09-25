"""Tests for the leakage-safe, configurable research walk-forward
(research.phase6.engine.walk_forward / window_bounds).

    PYTHONPATH=src:. python -m pytest research/phase6/test_walk_forward.py -q
"""

import random

import pandas as pd
import pytest

from algoedge.backtest import BacktestTrade
from fno_signals.config import INDEX_MAP, strategy_config_for
from research.phase6 import engine
from research.phase6.engine import Variant, pair, simulate, summarize, walk_forward, window_bounds, with_signal
from research.phase6.run import synthetic_frame

IST = "Asia/Kolkata"
BASE = strategy_config_for(INDEX_MAP[1])


# ---------------- helpers ----------------


def legacy_walk_forward(df, trades_by_variant, baseline_name, train_days=20, test_days=5, min_train_trades=8):
    """Verbatim copy of the pre-Phase-6-Step-A implementation (the reference)."""
    local = df.index.tz_convert(IST) if df.index.tz is not None else df.index
    days = sorted(set(local.date))
    windows = []
    picked_oos = []
    base_oos = []
    start = 0

    def in_days(trades, day_set):
        return [t for t in trades if pd.Timestamp(t.entry_time).tz_convert(IST).date() in day_set]

    while start + train_days + test_days <= len(days):
        train_set = set(days[start:start + train_days])
        test_set = set(days[start + train_days:start + train_days + test_days])
        scores = {
            name: engine._score(summarize(in_days(trades, train_set)), min_train_trades)
            for name, trades in trades_by_variant.items()
        }
        best = max(scores, key=lambda k: (scores[k], k == baseline_name))
        if scores[best] == float("-inf"):
            best = baseline_name
        test_pick = in_days(trades_by_variant[best], test_set)
        test_base = in_days(trades_by_variant[baseline_name], test_set)
        picked_oos += test_pick
        base_oos += test_base
        windows.append({
            "train": f"{min(train_set)}..{max(train_set)}", "test": f"{min(test_set)}..{max(test_set)}",
            "picked": best, "picked_train_expectancy": scores[best],
            "picked_test_net": sum(t.points for t in test_pick),
            "baseline_test_net": sum(t.points for t in test_base),
        })
        start += test_days
    return engine.WalkForwardResult(windows, summarize(picked_oos), summarize(base_oos))


def brute_force(df, trades_by_variant, baseline_name, train_days, test_days, min_train_trades, *, step_days,
                anchored, warmup_days, train_exit_cutoff):
    """Independent, slow reference for the new options: scan every trade."""
    days = sorted(set(df.index.tz_convert(IST).date))
    windows, picked, base = [], [], []

    def d(ts):
        return pd.Timestamp(ts).tz_convert(IST).date()

    end = warmup_days + train_days
    while end + test_days <= len(days):
        start = warmup_days if anchored else end - train_days
        train, test = days[start:end], days[end:end + test_days]
        scores = {}
        for name, trades in trades_by_variant.items():
            chosen = [t for t in trades if d(t.entry_time) in train
                      and (not train_exit_cutoff or d(t.exit_time) <= train[-1])]
            scores[name] = engine._score(summarize(chosen), min_train_trades)
        best = max(scores, key=lambda k: (scores[k], k == baseline_name))
        if scores[best] == float("-inf"):
            best = baseline_name
        tp = [t for t in trades_by_variant[best] if d(t.entry_time) in test]
        tb = [t for t in trades_by_variant[baseline_name] if d(t.entry_time) in test]
        picked += tp
        base += tb
        windows.append({"train": f"{train[0]}..{train[-1]}", "test": f"{test[0]}..{test[-1]}", "picked": best,
                        "picked_train_expectancy": scores[best], "picked_test_net": sum(t.points for t in tp),
                        "baseline_test_net": sum(t.points for t in tb)})
        end += step_days
    return engine.WalkForwardResult(windows, summarize(picked), summarize(base))


def day_frame(n_days: int, start: str = "2016-01-04") -> pd.DataFrame:
    """One bar per trading day - walk_forward only reads the trading days."""
    days = pd.bdate_range(start, periods=n_days)
    index = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:15") for d in days]).tz_localize(IST)
    return pd.DataFrame({c: 1.0 for c in ("Open", "High", "Low", "Close", "Volume")}, index=index)


def trade(day: pd.Timestamp, points: float, *, exit_day: pd.Timestamp | None = None, exit_hm: str = "15:00",
          entry_hm: str = "10:00") -> BacktestTrade:
    entry = pd.Timestamp(f"{day.date()} {entry_hm}", tz=IST)
    exit_ = pd.Timestamp(f"{(exit_day or day).date()} {exit_hm}", tz=IST)
    return BacktestTrade(entry_time=entry, exit_time=exit_, direction="CALL", entry_price=100.0,
                         exit_price=100.0 + points, exit_reason="TARGET" if points > 0 else "SL", points=points,
                         strike=100, option_symbol="X")


def random_pool(seed: int, n_days: int = 70, variants: int = 5) -> tuple[pd.DataFrame, dict]:
    rng = random.Random(seed)
    df = day_frame(n_days)
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    pool = {}
    for v in range(variants):
        trades = []
        for i, day in enumerate(days):
            for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
                hold = rng.choice([0, 0, 0, 1, 2, 3])  # some trades are held overnight / for days
                exit_day = days[min(i + hold, len(days) - 1)]
                trades.append(trade(day, round(rng.uniform(-40, 60), 2), exit_day=exit_day,
                                    entry_hm=f"{rng.randint(9, 14):02d}:{rng.choice(['15', '30', '45'])}"))
        trades.sort(key=lambda t: t.entry_time)  # pair() output is chronological
        pool["baseline" if v == 0 else f"v{v}"] = trades
    return df, pool


# ---------------- A: default behaviour unchanged ----------------


def test_default_configuration_reproduces_the_legacy_output_exactly() -> None:
    df = synthetic_frame(days=45, seed=11)
    pool = {
        "baseline": pair(simulate(df, Variant("baseline", BASE))),
        "st mult 2.0": pair(simulate(df, Variant("s", with_signal(BASE, supertrend_multiplier=2.0)))),
        "rsi 60/40": pair(simulate(df, Variant("r", with_signal(BASE, rsi_bull=60, rsi_bear=40)))),
    }
    compat = walk_forward(df, pool, "baseline", train_exit_cutoff=False)
    legacy = legacy_walk_forward(df, pool, "baseline")
    assert len(legacy.windows) == 5  # 45 trading days, 20/5 rolling: starts at days 0, 5, 10, 15, 20
    assert legacy.baseline_oos.trades > 0  # real simulate() trades, not an empty comparison
    assert compat.windows == legacy.windows  # every window: bounds, pick, train score, test nets
    assert compat.selected_oos == legacy.selected_oos
    assert compat.baseline_oos == legacy.baseline_oos
    assert compat == legacy


def test_defaults_keep_windows_and_test_blocks_while_train_scoring_changes() -> None:
    df, pool = random_pool(3)  # includes trades held overnight / for several days
    new, old = walk_forward(df, pool, "baseline"), legacy_walk_forward(df, pool, "baseline")
    assert len(new.windows) == len(old.windows) == 10  # 70 days, 20/5 rolling
    # Same windows and the same baseline test-block trades in every window.
    assert [(w["train"], w["test"]) for w in new.windows] == [(w["train"], w["test"]) for w in old.windows]
    assert [w["baseline_test_net"] for w in new.windows] == [w["baseline_test_net"] for w in old.windows]
    assert new.baseline_oos == old.baseline_oos
    # Where the same variant is picked, its test-block result is identical too.
    for n, o in zip(new.windows, old.windows, strict=True):
        if n["picked"] == o["picked"]:
            assert n["picked_test_net"] == o["picked_test_net"]
    # ...but train scoring really differs: trades still open at train end no longer count.
    assert any(n["picked_train_expectancy"] != o["picked_train_expectancy"]
               for n, o in zip(new.windows, old.windows, strict=True))


def test_default_parameter_values() -> None:
    import inspect

    params = inspect.signature(walk_forward).parameters
    assert (params["train_days"].default, params["test_days"].default, params["min_train_trades"].default) == (
        20, 5, 8)
    assert params["step_days"].default is None and params["anchored"].default is False
    assert params["warmup_days"].default == 0 and params["train_exit_cutoff"].default is True
    assert all(params[p].kind is inspect.Parameter.KEYWORD_ONLY
               for p in ("step_days", "anchored", "warmup_days", "train_exit_cutoff"))


def test_the_default_is_behaviourally_leakage_safe() -> None:
    df, pool = _leak_setup(exit_offset_days=1)
    default = walk_forward(df, pool, "baseline", 3, 1, 1)
    assert default == walk_forward(df, pool, "baseline", 3, 1, 1, train_exit_cutoff=True)
    assert default != walk_forward(df, pool, "baseline", 3, 1, 1, train_exit_cutoff=False)
    assert default.windows[0]["picked"] == "baseline"  # the test-block-dependent trade did not count
    df, pool = random_pool(3)
    assert walk_forward(df, pool, "baseline") == walk_forward(df, pool, "baseline", step_days=5, anchored=False,
                                                              warmup_days=0, train_exit_cutoff=True)


# ---------------- B/C: train-exit leakage cutoff ----------------


def _leak_setup(exit_offset_days: int):
    """Train = days 0-2, test = day 3. Variant x's only big winner is entered
    on the last train day; `exit_offset_days` later it closes."""
    df = day_frame(4)
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    pool = {
        "baseline": [trade(days[0], 5.0), trade(days[1], 5.0)],
        "x": [trade(days[0], -1.0), trade(days[2], 100.0, exit_day=days[2 + exit_offset_days])],
    }
    return df, pool


def test_train_trade_exiting_after_train_end_is_excluded_from_scoring() -> None:
    df, pool = _leak_setup(exit_offset_days=1)  # exits in the TEST block
    leaky = walk_forward(df, pool, "baseline", 3, 1, 1, train_exit_cutoff=False)
    safe = walk_forward(df, pool, "baseline", 3, 1, 1)
    assert leaky.windows[0]["picked"] == "x"  # the future-dependent trade decided the selection
    assert leaky.windows[0]["picked_train_expectancy"] == pytest.approx(49.5)  # (-1 + 100) / 2
    assert safe.windows[0]["picked"] == "baseline"  # x's train score is now just its -1 trade
    assert safe.windows[0]["picked_train_expectancy"] == 5.0
    assert leaky.baseline_oos == safe.baseline_oos  # test block untouched


def test_train_trade_exiting_on_the_last_train_day_is_included() -> None:
    df, pool = _leak_setup(exit_offset_days=0)  # closes the same (last train) day, 15:00
    result = walk_forward(df, pool, "baseline", 3, 1, 1)
    assert result.windows[0]["picked"] == "x"
    assert result.windows[0]["picked_train_expectancy"] == pytest.approx(49.5)


def test_test_trades_are_attributed_by_entry_even_if_they_exit_later() -> None:
    df = day_frame(5)
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    pool = {"baseline": [trade(days[0], 1.0), trade(days[3], 7.0, exit_day=days[4])]}
    result = walk_forward(df, pool, "baseline", 3, 1, 1)
    assert result.windows[0]["test"].startswith(str(days[3].date()))
    assert result.windows[0]["baseline_test_net"] == 7.0


# ---------------- D/E/F: window boundaries ----------------


def test_rolling_window_bounds() -> None:
    assert window_bounds(10, 3, 2, step_days=2, anchored=False, warmup_days=0) == [
        (0, 3, 3, 5), (2, 5, 5, 7), (4, 7, 7, 9)]


def test_anchored_window_bounds() -> None:
    assert window_bounds(10, 3, 2, step_days=2, anchored=True, warmup_days=0) == [
        (0, 3, 3, 5), (0, 5, 5, 7), (0, 7, 7, 9)]


def test_step_days_controls_the_advance() -> None:
    assert window_bounds(8, 3, 2, step_days=1, anchored=False, warmup_days=0) == [
        (0, 3, 3, 5), (1, 4, 4, 6), (2, 5, 5, 7), (3, 6, 6, 8)]
    assert window_bounds(12, 3, 2, step_days=4, anchored=False, warmup_days=0) == [
        (0, 3, 3, 5), (4, 7, 7, 9)]  # the next (8, 11, 11, 13) would run past day 12


def test_walk_forward_uses_those_bounds_for_its_windows() -> None:
    df, pool = random_pool(5, n_days=12)
    days = sorted(set(df.index.date))
    result = walk_forward(df, pool, "baseline", 3, 2, 1, step_days=3, anchored=True, warmup_days=1)
    expected = window_bounds(12, 3, 2, step_days=3, anchored=True, warmup_days=1)
    assert [(w["train"], w["test"]) for w in result.windows] == [
        (f"{days[a]}..{days[b - 1]}", f"{days[c]}..{days[e - 1]}") for a, b, c, e in expected]


# ---------------- G/H: no overlap, chronological ----------------


@pytest.mark.parametrize("anchored", [False, True])
@pytest.mark.parametrize("n, train, test, step, warmup", [
    (60, 10, 5, 5, 0), (60, 7, 3, 4, 2), (100, 20, 5, 5, 3), (47, 5, 5, 9, 1), (30, 1, 1, 1, 0)])
def test_train_and_test_never_overlap_and_tests_are_chronological(anchored, n, train, test, step, warmup) -> None:
    bounds = window_bounds(n, train, test, step_days=step, anchored=anchored, warmup_days=warmup)
    expected_windows = (n - warmup - train - test) // step + 1  # independent count of fitting windows
    assert expected_windows > 0 and len(bounds) == expected_windows
    for a, b, c, e in bounds:
        assert warmup <= a < b == c < e <= n  # train strictly before test, inside the data
        assert e - c == test
        assert b - a == (b - warmup if anchored else train)
    tests = [(c, e) for _a, _b, c, e in bounds]
    assert tests == sorted(tests)
    for (c1, e1), (c2, _e2) in zip(tests, tests[1:], strict=False):
        assert c2 == c1 + step
        if step >= test:
            assert c2 >= e1  # non-overlapping test blocks


# ---------------- I: warm-up ----------------


def test_warmup_days_never_appear_in_any_window() -> None:
    df, pool = random_pool(7, n_days=30)
    days = sorted(set(df.index.date))
    result = walk_forward(df, pool, "baseline", 5, 3, 1, warmup_days=4, anchored=True)
    assert len(result.windows) == 7  # (30 - 4 warm-up - 5 train - 3 test) // 3 + 1
    for w in result.windows:  # every window starts after the 4 warm-up days
        assert w["train"].split("..")[0] >= str(days[4]) and w["test"].split("..")[0] > str(days[4])
    assert result.windows[0]["train"].startswith(str(days[4]))
    reference = brute_force(df, pool, "baseline", 5, 3, 1, step_days=3, anchored=True, warmup_days=4,
                            train_exit_cutoff=True)
    assert result == reference


# ---------------- J/K: selection rules ----------------


def test_min_train_trades_is_respected() -> None:
    df = day_frame(4)
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    pool = {"baseline": [trade(days[0], 1.0), trade(days[1], 1.0)],
            "great_but_sparse": [trade(days[1], 90.0)]}
    assert walk_forward(df, pool, "baseline", 3, 1, 2).windows[0]["picked"] == "baseline"
    assert walk_forward(df, pool, "baseline", 3, 1, 1).windows[0]["picked"] == "great_but_sparse"


def test_ties_go_to_the_baseline_and_it_is_the_fallback() -> None:
    df = day_frame(4)
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    tie = {"a_first": [trade(days[0], 3.0)], "baseline": [trade(days[1], 3.0)]}
    assert walk_forward(df, tie, "baseline", 3, 1, 1).windows[0]["picked"] == "baseline"
    nothing = {"x": [trade(days[0], 50.0)], "baseline": [trade(days[1], 1.0)]}
    result = walk_forward(df, nothing, "baseline", 3, 1, 5)  # nobody has 5 trades
    assert result.windows[0]["picked"] == "baseline"
    assert result.windows[0]["picked_train_expectancy"] == float("-inf")


# ---------------- L: optimized assignment == old / brute force ----------------


@pytest.mark.parametrize("seed", range(8))
def test_indexed_assignment_matches_the_legacy_implementation(seed) -> None:
    df, pool = random_pool(seed)
    for train, test, min_trades in ((20, 5, 8), (10, 3, 4), (7, 7, 1)):
        assert walk_forward(df, pool, "baseline", train, test, min_trades, train_exit_cutoff=False) == \
            legacy_walk_forward(df, pool, "baseline", train, test, min_trades)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("options", [
    {"step_days": 5, "anchored": False, "warmup_days": 0, "train_exit_cutoff": True},
    {"step_days": 3, "anchored": True, "warmup_days": 4, "train_exit_cutoff": True},
    {"step_days": 7, "anchored": False, "warmup_days": 2, "train_exit_cutoff": False},
    {"step_days": 2, "anchored": True, "warmup_days": 0, "train_exit_cutoff": False},
])
def test_indexed_assignment_matches_brute_force_for_all_options(seed, options) -> None:
    df, pool = random_pool(100 + seed)
    assert walk_forward(df, pool, "baseline", 12, 5, 3, **options) == brute_force(
        df, pool, "baseline", 12, 5, 3, **options)


def test_unsorted_input_keeps_original_order_for_sums() -> None:
    df, pool = random_pool(42)
    shuffled = {k: random.Random(1).sample(v, len(v)) for k, v in pool.items()}
    assert walk_forward(df, shuffled, "baseline", 10, 5, 3, train_exit_cutoff=False) == legacy_walk_forward(
        df, shuffled, "baseline", 10, 5, 3)


# ---------------- M: short data ----------------


def test_short_dataset_produces_no_windows() -> None:
    df, pool = random_pool(9, n_days=24)
    result = walk_forward(df, pool, "baseline")  # needs 25 days
    assert result.windows == [] and result.selected_oos.trades == 0 and result.baseline_oos.trades == 0
    fits = walk_forward(df, pool, "baseline", 20, 4).windows  # 20 + 4 == 24 days: exactly one window
    assert len(fits) == 1 and fits[0]["test"].endswith(str(sorted(set(df.index.date))[-1]))
    assert walk_forward(df, pool, "baseline", 10, 5, warmup_days=10).windows == []  # 14 days left < 15
    assert window_bounds(0, 20, 5, step_days=5, anchored=False, warmup_days=0) == []


@pytest.mark.parametrize("train, test, step, warmup", [(0, 2, 1, 0), (3, 0, 1, 0), (3, 2, 0, 0), (3, 2, 1, -1)])
def test_invalid_window_parameters_are_rejected(train, test, step, warmup) -> None:
    with pytest.raises(ValueError):
        window_bounds(10, train, test, step_days=step, anchored=False, warmup_days=warmup)


def test_trade_on_a_day_outside_the_data_is_ignored_like_before() -> None:
    df = day_frame(6)  # Mon 2016-01-04 .. Mon 2016-01-11
    days = list(pd.DatetimeIndex(df.index).normalize().tz_localize(None))
    saturday = pd.Timestamp("2016-01-09")  # inside the test block's date range, but not a trading day
    assert saturday.date() not in set(df.index.date)
    pool = {"baseline": [trade(days[0], 2.0), trade(saturday, 99.0), trade(days[5], 4.0)]}
    new = walk_forward(df, pool, "baseline", 3, 3, 1, train_exit_cutoff=False)  # test = Thu, Fri, Mon
    assert new.windows[0]["test"] == f"{days[3].date()}..{days[5].date()}"
    assert new == legacy_walk_forward(df, pool, "baseline", 3, 3, 1)
    assert new.baseline_oos.net_points == 4.0  # a naive date-range filter would also count the 99
