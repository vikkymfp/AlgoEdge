"""Tests for the Phase 6 research harness (not part of the production suite).

    PYTHONPATH=src:. python -m pytest research/phase6 -q
"""

from dataclasses import replace
from datetime import time

import numpy as np
import pandas as pd
import pytest

from algoedge.backtest import compute_backtest_metrics, compute_regime_labels, pair_trades
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as canonical_run
from research.phase6 import data as data_mod
from research.phase6.engine import (
    AdxFilter,
    Variant,
    continuous_splits,
    pair,
    paper_session_variant,
    simulate,
    summarize,
    walk_forward,
    with_risk,
    with_signal,
)
from research.phase6.experiments import build_variants
from research.phase6.indicators_extra import dmi, pine_rma
from research.phase6.run import synthetic_frame

BASE = strategy_config_for(INDEX_MAP[1])
SEEDS = [1, 2, 3, 11, 42]


@pytest.fixture(scope="module")
def frames():
    return {seed: synthetic_frame(days=20, seed=seed) for seed in SEEDS}


def _event_tuples(events):
    return [
        (e.timestamp, e.kind, e.underlying_price, e.option_symbol, e.stop_loss, e.target, e.exit_level,
         e.strike, e.right)
        for e in events
    ]


# ---------- parity with production ----------


def test_research_constants_match_production() -> None:
    from algoedge import auto_trader, web_server

    assert data_mod.BACKTEST_TIMEFRAMES == web_server.BACKTEST_TIMEFRAMES
    assert data_mod.INDEX_CHOICE == auto_trader._INDEX_CHOICE


@pytest.mark.parametrize("guard", [False, True])
def test_simulate_reproduces_canonical_run_exactly(frames, guard) -> None:
    for df in frames.values():
        _results, expected = canonical_run(df, BASE, underlying_label="NIFTY 50")
        actual = simulate(df, Variant("baseline", BASE, guard_nan_risk=guard), "NIFTY 50")
        assert _event_tuples(actual) == _event_tuples(expected)


@pytest.mark.parametrize("config", [
    with_signal(BASE, ema_fast_length=12, ema_slow_length=26),
    with_signal(BASE, supertrend_multiplier=2.0, rsi_bull=60, rsi_bear=40),
    with_risk(BASE, sl_multiplier=2.0, tp_multiplier=4.0),
])
def test_simulate_matches_canonical_for_other_configs(frames, config) -> None:
    df = frames[SEEDS[0]]
    _results, expected = canonical_run(df, config, underlying_label="X")
    assert _event_tuples(simulate(df, Variant("v", config, guard_nan_risk=False), "X")) == _event_tuples(expected)


def test_pair_matches_production_pair_trades(frames) -> None:
    for df in frames.values():
        _results, events = canonical_run(df, BASE, underlying_label="X")
        for slippage in (0.0, 2.5):
            assert pair(events, slippage) == pair_trades(events, assumed_slippage_points=slippage)


def test_research_baseline_metrics_equal_the_backtest_endpoint_segment(frames) -> None:
    from algoedge import web_server

    df = frames[SEEDS[1]]
    segment = web_server._run_backtest_segment(df, BASE, INDEX_MAP[1], 0.0)
    trades = pair(simulate(df, Variant("baseline", BASE), INDEX_MAP[1].name))
    ours = web_server._backtest_metrics_payload(compute_backtest_metrics(trades, compute_regime_labels(df)))
    assert ours == segment["metrics"]


# ---------- NaN-risk guard ----------


def test_canonical_run_no_longer_freezes_during_the_atr_warm_up() -> None:
    # Before Phase 6 this config produced ONE event with a NaN stop and froze.
    # The canonical guard now matches the research guard exactly, and
    # guard_nan_risk=False still reproduces the old freeze for comparison.
    df = synthetic_frame(days=20, seed=1001)
    config = with_signal(BASE, rsi_length=7)
    _results, events = canonical_run(df, config, underlying_label="X")
    assert len(events) > 10
    assert _event_tuples(simulate(df, Variant("v", config), "X")) == _event_tuples(events)
    legacy = simulate(df, Variant("v", config, guard_nan_risk=False), "X")
    assert len(legacy) == 1 and np.isnan(legacy[0].stop_loss)


def test_simulate_drops_invalid_bars_like_the_canonical_run() -> None:
    df = synthetic_frame(days=20, seed=4)
    df.iloc[500, df.columns.get_loc("Close")] = np.nan
    df.iloc[900, df.columns.get_loc("High")] = np.nan
    _results, events = canonical_run(df, BASE, underlying_label="X")
    assert _event_tuples(simulate(df, Variant("b", BASE), "X")) == _event_tuples(events)


def test_guard_never_opens_a_position_without_finite_sl_tp() -> None:
    df = synthetic_frame(days=20, seed=1001)
    for config in (with_signal(BASE, rsi_length=7), with_risk(BASE, atr_length=21)):
        events = simulate(df, Variant("v", config), "X")
        entries = [e for e in events if e.kind.startswith("ENTRY")]
        assert len(entries) > 10
        assert all(np.isfinite(e.stop_loss) and np.isfinite(e.target) for e in entries)


# ---------- ADX / DI ----------


def _reference_dmi(high, low, close, n):
    """Straightforward loop version of Pine's ta.dmi, written independently."""
    h, lo, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    size = len(h)
    tr = np.full(size, np.nan)
    pdm = np.full(size, np.nan)
    mdm = np.full(size, np.nan)
    tr[0] = h[0] - lo[0]
    for i in range(1, size):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm[i] = up if (up > down and up > 0) else 0.0
        mdm[i] = down if (down > up and down > 0) else 0.0

    def rma(x, start):
        out = np.full(size, np.nan)
        out[start + n - 1] = np.mean(x[start:start + n])
        for i in range(start + n, size):
            out[i] = (out[i - 1] * (n - 1) + x[i]) / n
        return out

    tr_s = rma(tr, 0)
    plus = 100 * rma(pdm, 1) / tr_s
    minus = 100 * rma(mdm, 1) / tr_s
    dx = np.abs(plus - minus) / np.where(plus + minus == 0, 1, plus + minus)
    first = int(np.argmax(~np.isnan(dx)))
    return plus, minus, 100 * rma(np.nan_to_num(dx), first)


def test_dmi_matches_an_independent_reference_implementation(frames) -> None:
    df = frames[SEEDS[2]].iloc[:600]
    di_plus, di_minus, adx = dmi(df["High"], df["Low"], df["Close"], 14, 14)
    ref_plus, ref_minus, ref_adx = _reference_dmi(df["High"], df["Low"], df["Close"], 14)
    np.testing.assert_allclose(di_plus.to_numpy()[20:], ref_plus[20:], rtol=1e-9)
    np.testing.assert_allclose(di_minus.to_numpy()[20:], ref_minus[20:], rtol=1e-9)
    np.testing.assert_allclose(adx.to_numpy()[60:], ref_adx[60:], rtol=1e-9)
    assert adx.dropna().between(0, 100).all()


def test_dmi_reads_a_clean_uptrend_as_strong_and_bullish() -> None:
    n = 120
    close = pd.Series(np.linspace(100, 220, n))
    di_plus, di_minus, adx = dmi(close + 1, close - 1, close, 14, 14)
    assert di_plus.iloc[-1] > di_minus.iloc[-1]
    assert adx.iloc[-1] > 50


def test_pine_rma_waits_for_a_full_valid_window() -> None:
    series = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0])
    result = pine_rma(series, 3)
    assert result.iloc[:4].isna().all()
    assert result.iloc[4] == pytest.approx(2.0)
    assert result.iloc[5] == pytest.approx((2.0 * 2 + 4.0) / 3)


def test_a_permissive_adx_gate_changes_nothing() -> None:
    # ADX is NaN during its own warm-up (~2x its length), which vetoes any
    # entry there, and one vetoed entry shifts every later trade. So use a
    # frame whose first canonical entry comes after ADX is valid.
    permissive = AdxFilter(threshold=-1.0, require_di=False)
    for seed in range(200):
        df = synthetic_frame(days=10, seed=seed)
        base_events = simulate(df, Variant("b", BASE), "X")
        _p, _m, adx = dmi(df["High"], df["Low"], df["Close"], 14, 14)
        if base_events and base_events[0].timestamp > adx.first_valid_index():
            break
    else:
        pytest.fail("no suitable seed")
    gated = simulate(df, Variant("p", BASE, adx=permissive), "X")
    assert _event_tuples(gated) == _event_tuples(base_events)


def test_adx_gate_only_ever_removes_entries(frames) -> None:
    df = frames[SEEDS[3]]
    base_entries = {e.timestamp for e in simulate(df, Variant("b", BASE), "X") if e.kind.startswith("ENTRY")}
    gated = simulate(df, Variant("g", BASE, adx=AdxFilter(threshold=25)), "X")
    # Gating can shift later entries (a vetoed trade frees the position), but
    # every gated entry must still be on a bar where the canonical setup edge fired.
    _results, _events = canonical_run(df, BASE, underlying_label="X")
    edges = set(_results.index[
        (_results["bull_setup"] & ~_results["bull_setup"].shift(1, fill_value=False))
        | (_results["bear_setup"] & ~_results["bear_setup"].shift(1, fill_value=False))
    ])
    gated_entries = [e.timestamp for e in gated if e.kind.startswith("ENTRY")]
    assert set(gated_entries) <= edges
    assert len(gated_entries) <= len(base_entries) + 5


# ---------- paper-session realism ----------


def test_paper_session_variant_never_enters_late_or_holds_overnight(frames) -> None:
    df = frames[SEEDS[4]]
    variant = paper_session_variant(Variant("b", BASE))
    events = simulate(df, variant, "X")
    for e in events:
        end = (e.timestamp + pd.Timedelta(minutes=5)).time()
        if e.kind.startswith("ENTRY"):
            assert end <= time(15, 0)
    trades = pair(events)
    assert trades and all(t.entry_time.date() == t.exit_time.date() for t in trades)
    assert any(t.exit_reason == "SQUARE_OFF" for t in trades)
    for t in trades:
        if t.exit_reason == "SQUARE_OFF":
            assert (t.exit_time + pd.Timedelta(minutes=5)).time() >= time(15, 20)


def test_exit_fill_modes(frames) -> None:
    df = frames[SEEDS[1]]
    level = pair(simulate(df, Variant("l", BASE), "X"))
    gap = pair(simulate(df, Variant("g", BASE, exit_fill="gap_aware"), "X"))
    close = pair(simulate(df, Variant("c", BASE, exit_fill="bar_close"), "X"))
    assert [t.entry_time for t in level] == [t.entry_time for t in gap] == [t.entry_time for t in close]
    for lv, gp, cl in zip(level, gap, close, strict=True):
        if lv.exit_reason == "SL":
            assert gp.points <= lv.points + 1e-9
        else:
            assert gp.points >= lv.points - 1e-9
        assert cl.exit_price == pytest.approx(df.loc[cl.exit_time, "Close"])


# ---------- splits, walk-forward, experiments, data quality ----------


def test_continuous_splits_partition_every_trade(frames) -> None:
    df = frames[SEEDS[0]]
    trades = pair(simulate(df, Variant("b", BASE), "X"))
    splits = continuous_splits(trades, df, {})
    assert sum(s.trades for s in splits.values()) == len(trades)


def test_walk_forward_only_scores_train_days_and_reports_test_days() -> None:
    df = synthetic_frame(days=35, seed=5)
    pool = {
        "baseline": pair(simulate(df, Variant("baseline", BASE), "X")),
        "st mult 2.0": pair(simulate(df, Variant("s", with_signal(BASE, supertrend_multiplier=2.0)), "X")),
    }
    wf = walk_forward(df, pool, "baseline", train_days=20, test_days=5)
    assert len(wf.windows) == 3
    for w in wf.windows:
        assert w["train"].split("..")[1] < w["test"].split("..")[0]
        assert w["picked"] in pool
    assert wf.baseline_oos.trades == sum(
        1 for t in pool["baseline"]
        if t.entry_time.date() >= pd.Timestamp(wf.windows[0]["test"].split("..")[0]).date()
    )


def test_experiment_grid_starts_from_the_production_config() -> None:
    variants, families = build_variants(BASE)
    assert variants[0].name == "baseline" and variants[0].config == BASE
    names = [v.name for v in variants]
    assert len(names) == len(set(names))
    for axis in families.values():
        assert all(n == "baseline" or n in names for n in axis)
    assert all(v.config.option == BASE.option and v.config.session == BASE.session for v in variants)


def test_quality_report_flags_gaps_duplicates_and_nans() -> None:
    df = synthetic_frame(days=3, seed=9)
    clean = data_mod.quality_report(df, "5m")
    assert clean.missing_bars_total == 0 and clean.duplicate_timestamps == 0 and clean.nan_rows == 0
    assert clean.expected_bars_per_day == 75 and clean.zero_volume_fraction == 1.0

    broken = pd.concat([df.iloc[:10], df.iloc[15:], df.iloc[[20]]]).copy()
    broken.iloc[30, broken.columns.get_loc("Close")] = np.nan
    report = data_mod.quality_report(broken, "5m")
    assert report.duplicate_timestamps == 1
    assert report.nan_rows == 1
    assert report.missing_bars_total == 5 and report.days_with_missing_bars == 1
    assert report.max_intraday_gap_minutes == 30.0


def test_csv_round_trip_preserves_ist_timestamps(tmp_path) -> None:
    df = synthetic_frame(days=2, seed=3)
    path = tmp_path / "x.csv"
    data_mod.save_csv(df, path)
    loaded = data_mod.load_csv(path)
    assert str(loaded.index.tz) == "Asia/Kolkata"
    pd.testing.assert_index_equal(loaded.index, df.index.rename("Datetime"), check_exact=True)
    np.testing.assert_allclose(loaded["Close"].to_numpy(), df["Close"].to_numpy())


def test_summary_extras(frames) -> None:
    trades = pair(simulate(frames[SEEDS[0]], Variant("b", BASE), "X"))
    s = summarize(trades)
    assert s.trades == len(trades)
    assert s.call_trades + s.put_trades == s.trades
    assert sum(s.exit_reasons.values()) == s.trades
    assert s.avg_winner > 0 > s.avg_loser
    streak = longest_prefix = 0
    for t in sorted(trades, key=lambda t: t.entry_time):
        streak = streak + 1 if t.points < 0 else 0
        longest_prefix = max(longest_prefix, streak)
    assert (s.halts_at_3_losses > 0) == (longest_prefix >= 3)
    assert replace(Variant("b", BASE)).guard_nan_risk is True
