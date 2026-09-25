"""Tests for the Phase 6 research protocol (research.phase6.protocol and the
runner's --protocol / --holdout-from / --wf-design options) - SQLite only,
small synthetic data; never the real 10-year dataset.

    PYTHONPATH=src:. python -m pytest research/phase6/test_protocol.py -q
"""

import dataclasses
import json
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from algoedge.backtest import BacktestTrade
from research.phase6 import candle_source, historical_db, protocol
from research.phase6 import run as run_mod
from research.phase6.engine import walk_forward, window_bounds
from research.phase6.run import synthetic_frame

REPO = Path(__file__).resolve().parents[2]
IST = "Asia/Kolkata"
SEGMENT2_RESEARCH_DAYS = 2083  # trading days of 2015-11-16 .. 2024-04-25 in the imported dataset
SEGMENT1_DAYS = 109  # trading days of 2015-01-09 .. 2015-06-19


# ---------------- helpers ----------------


def candle_rows(days, *, seed, base=8000.0, bars=75, source="master_5min.csv", load_id=1):
    rng = np.random.default_rng(seed)
    rows, price = [], base
    for day in days:
        first = datetime.fromisoformat(f"{pd.Timestamp(day).date()} 09:15")
        for k in range(bars):
            start = first + timedelta(minutes=5 * k)
            close = price + rng.normal(0, 8)
            rows.append({"index_id": "nifty-50", "timeframe": "5m", "bar_start": start,
                         "bar_end": start + timedelta(minutes=5), "open": price,
                         "high": max(price, close) + abs(rng.normal(0, 4)),
                         "low": min(price, close) - abs(rng.normal(0, 4)), "close": close, "volume": None,
                         "source": source, "load_id": load_id})
            price = close
    return rows


RESEARCH_TAIL = pd.bdate_range("2024-04-08", "2024-04-25")  # 14 sessions, ends on the last research day
HOLDOUT_HEAD = pd.bdate_range("2024-04-26", "2024-05-10")


def make_db(tmp_path, name, rows):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    historical_db.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(historical_db.HistoricalCandleLoad.__table__), [
            {"id": 1, "source": "master_5min.csv", "file_sha256": "a" * 64, "row_count": len(rows),
             "valid_rows": len(rows), "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0}])
        conn.execute(insert(historical_db.HistoricalCandle.__table__), rows)
    return engine


@pytest.fixture()
def spies(monkeypatch):
    """Records every frame handed to run_index and every reader call."""
    frames: list[pd.DataFrame] = []
    reader_calls: list[dict] = []
    real_run_index = run_mod.run_index

    def run_index_spy(label, df, interval, key, **kwargs):
        frames.append(df)
        return real_run_index(label, df, interval, key, **kwargs)
    monkeypatch.setattr(run_mod, "run_index", run_index_spy)
    real_reader = candle_source.load_db_segments

    def reader_spy(engine, **kwargs):
        reader_calls.append(kwargs)
        return real_reader(engine, **kwargs)
    monkeypatch.setattr(candle_source, "load_db_segments", reader_spy)
    return frames, reader_calls


def use_engine(monkeypatch, engine):
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: engine)


def run_protocol(out, *extra):
    return run_mod.main(["--db", "--indices", "nifty-50", "--out", str(out), "--protocol", *extra])


def day_frame(n_days, start="2016-01-04"):
    days = pd.bdate_range(start, periods=n_days)
    index = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:15") for d in days]).tz_localize(IST)
    return pd.DataFrame({c: 1.0 for c in ("Open", "High", "Low", "Close", "Volume")}, index=index)


def trade(day, points, *, exit_day=None):
    entry = pd.Timestamp(f"{pd.Timestamp(day).date()} 10:00", tz=IST)
    exit_ = pd.Timestamp(f"{pd.Timestamp(exit_day if exit_day is not None else day).date()} 15:00", tz=IST)
    return BacktestTrade(entry_time=entry, exit_time=exit_, direction="CALL", entry_price=100.0,
                         exit_price=100.0 + points, exit_reason="TARGET" if points > 0 else "SL", points=points,
                         strike=100, option_symbol="X")


def trading_days(df):
    return sorted(set(df.index.tz_convert(IST).date))


# ---------------- A: holdout boundary ----------------


def test_research_end_is_the_day_before_the_holdout_at_235959() -> None:
    assert protocol.HOLDOUT_START == date(2024, 4, 26)
    assert protocol.HOLDOUT_END == date(2025, 4, 25)
    assert protocol.RESEARCH_END_DATE == date(2024, 4, 25)
    assert protocol.research_end_for(protocol.HOLDOUT_START) == datetime(2024, 4, 25, 23, 59, 59)
    assert protocol.research_end_for(date(2024, 1, 1)) == datetime(2023, 12, 31, 23, 59, 59)


def test_protocol_reads_up_to_2024_04_25_and_never_the_holdout(tmp_path, monkeypatch, spies) -> None:
    rows = candle_rows(RESEARCH_TAIL, seed=1) + candle_rows(HOLDOUT_HEAD, seed=2)
    # A bar exactly at the holdout's first instant, and the last whole second before it.
    rows += [{**rows[-1], "bar_start": datetime(2024, 4, 26, 0, 0), "bar_end": datetime(2024, 4, 26, 0, 5)},
             {**rows[-1], "bar_start": datetime(2024, 4, 25, 23, 59, 59),
              "bar_end": datetime(2024, 4, 26, 0, 4, 59)}]
    use_engine(monkeypatch, make_db(tmp_path, "a.db", rows))
    frames, reader_calls = spies
    assert run_protocol(tmp_path / "out") == 0

    assert reader_calls == [{"index_id": "nifty-50", "timeframe": "5m", "start": None,
                             "end": pd.Timestamp("2024-04-25 23:59:59"), "source": "master_5min.csv"}]
    (frame,) = frames
    assert frame.index[0].isoformat() == "2024-04-08T09:15:00+05:30"
    assert frame.index[-1].isoformat() == "2024-04-25T23:59:59+05:30"  # inclusive research end
    assert (frame.index < pd.Timestamp("2024-04-26", tz=IST)).all()
    assert len(frame.loc["2024-04-25"]) == 75 + 1  # the whole last research day is included
    report = json.loads((tmp_path / "out" / "db_master_5min_nifty-50_protocol.json").read_text())
    assert report[0]["metadata"]["segments"][0]["last_bar"] == "2024-04-25T23:59:59+05:30"


def test_holdout_from_date_only_and_custom_boundary(tmp_path, monkeypatch, spies) -> None:
    use_engine(monkeypatch, make_db(tmp_path, "a.db", candle_rows(RESEARCH_TAIL, seed=1)))
    frames, reader_calls = spies
    assert run_mod.main(["--db", "--indices", "nifty-50", "--out", str(tmp_path / "out"),
                         "--holdout-from", "2024-04-15"]) == 0
    assert reader_calls[0]["end"] == pd.Timestamp("2024-04-14 23:59:59")
    assert frames[0].index[-1].isoformat() == "2024-04-12T15:25:00+05:30"


@pytest.mark.parametrize("extra, message", [
    (["--end", "2024-04-26"], "would read into the holdout"),
    (["--end", "2024-04-26 00:00"], "would read into the holdout"),
    (["--end", "2025-04-25"], "would read into the holdout"),
    (["--start", "2024-04-26"], "inside the holdout"),
    (["--holdout-from", "2024-04-26 09:15"], "YYYY-MM-DD"),
])
def test_holdout_violations_are_rejected_before_any_db_access(monkeypatch, capsys, extra, message) -> None:
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: pytest.fail("engine created"))
    with pytest.raises(SystemExit) as exit_info:
        run_mod.main(["--db", "--indices", "nifty-50", "--protocol", *extra])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_end_on_the_last_research_day_is_allowed(tmp_path, monkeypatch, spies) -> None:
    use_engine(monkeypatch, make_db(tmp_path, "a.db", candle_rows(RESEARCH_TAIL, seed=1)
                                    + candle_rows(HOLDOUT_HEAD, seed=2)))
    frames, reader_calls = spies
    assert run_protocol(tmp_path / "out", "--end", "2024-04-25") == 0  # date-only -> 23:59:59
    assert reader_calls[0]["end"] == pd.Timestamp("2024-04-25 23:59:59")
    assert frames[0].index[-1].isoformat() == "2024-04-25T15:25:00+05:30"


@pytest.mark.parametrize("extra", [["--protocol"], ["--holdout-from", "2024-04-26"], ["--wf-design", "design1"]])
def test_protocol_options_require_db(capsys, extra) -> None:
    with pytest.raises(SystemExit):
        run_mod.main(["--synthetic", *extra])
    assert "only supported with --db" in capsys.readouterr().err


def test_a_leaked_holdout_bar_aborts_the_run(tmp_path, monkeypatch) -> None:
    """Defence in depth: even if the reader ignored its end bound, nothing runs."""
    use_engine(monkeypatch, make_db(tmp_path, "a.db", candle_rows(RESEARCH_TAIL, seed=1)))
    leaked = synthetic_frame(3)
    leaked.index = pd.DatetimeIndex([t.replace(year=2024, month=4, day=26 + i // 75)
                                     for i, t in enumerate(leaked.index)])
    monkeypatch.setattr(candle_source, "load_db_segments", lambda engine, **kw: [leaked])
    monkeypatch.setattr(run_mod, "run_index", lambda *a, **k: pytest.fail("run_index reached"))
    with pytest.raises(RuntimeError, match="holdout leak"):
        run_protocol(tmp_path / "out")
    assert not (tmp_path / "out").exists()


# ---------------- B: holdout isolation ----------------


def test_changing_only_holdout_data_does_not_change_the_research_report(tmp_path, monkeypatch) -> None:
    research = candle_rows(RESEARCH_TAIL, seed=1)
    reports = []
    for n, holdout in enumerate([candle_rows(HOLDOUT_HEAD, seed=2),
                                 candle_rows(HOLDOUT_HEAD, seed=99, base=15000.0)
                                 + candle_rows(pd.bdate_range("2024-05-13", "2025-04-25"), seed=5, bars=2),
                                 []]):
        use_engine(monkeypatch, make_db(tmp_path, f"b{n}.db", research + holdout))
        assert run_protocol(tmp_path / f"out{n}") == 0
        out = tmp_path / f"out{n}"
        reports.append(((out / "db_master_5min_nifty-50_protocol.json").read_text(),
                        (out / "db_master_5min_nifty-50_protocol.md").read_text()))
    # The loads row records row_count only; research outputs are byte-identical.
    assert reports[0] == reports[1] == reports[2]


# ---------------- C / D: exact design boundaries ----------------


def test_design_1_parameters_and_exact_windows() -> None:
    d = protocol.DESIGN_1_ROLLING
    assert dataclasses.astuple(d)[1:] == (250, 63, 63, False, 10, True, 30)
    assert d.required_trading_days == 323
    bounds = window_bounds(SEGMENT2_RESEARCH_DAYS, d.train_days, d.test_days, step_days=d.step_days,
                           anchored=d.anchored, warmup_days=d.warmup_days)
    assert len(bounds) == (SEGMENT2_RESEARCH_DAYS - 10 - 250 - 63) // 63 + 1 == 28
    assert bounds[0] == (10, 260, 260, 323)
    assert bounds[1] == (73, 323, 323, 386)
    assert bounds[-1] == (1711, 1961, 1961, 2024)
    for (a0, a1, t0, t1), nxt in zip(bounds, bounds[1:] + [None], strict=True):
        assert a1 - a0 == 250 and t0 == a1 and t1 - t0 == 63
        if nxt:
            assert nxt[2] == t1  # test blocks tile without gap or overlap
    assert bounds[-1][3] + 63 > SEGMENT2_RESEARCH_DAYS  # no partial last window


def test_design_2_parameters_and_exact_windows() -> None:
    d = protocol.DESIGN_2_ANCHORED
    assert dataclasses.astuple(d)[1:] == (500, 250, 250, True, 10, True, 30)
    assert d.required_trading_days == 760
    bounds = window_bounds(SEGMENT2_RESEARCH_DAYS, d.train_days, d.test_days, step_days=d.step_days,
                           anchored=d.anchored, warmup_days=d.warmup_days)
    assert bounds == [(10, 510 + 250 * k, 510 + 250 * k, 760 + 250 * k) for k in range(6)]
    assert bounds[-1] == (10, 1760, 1760, 2010)


def test_step_a_defaults_are_unchanged() -> None:
    import inspect
    params = inspect.signature(walk_forward).parameters
    assert {k: params[k].default for k in ("train_days", "test_days", "min_train_trades", "step_days",
                                           "anchored", "warmup_days", "train_exit_cutoff")} == {
        "train_days": 20, "test_days": 5, "min_train_trades": 8, "step_days": None, "anchored": False,
        "warmup_days": 0, "train_exit_cutoff": True}


@pytest.mark.parametrize("design, n_days, expected", [
    (protocol.DESIGN_1_ROLLING, 400, [((10, 259), (260, 322)), ((73, 322), (323, 385))]),
    (protocol.DESIGN_2_ANCHORED, 1100, [((10, 509), (510, 759)), ((10, 759), (760, 1009))]),
])
def test_walk_forward_with_design_kwargs_uses_those_dates(design, n_days, expected) -> None:
    df = day_frame(n_days)
    days = trading_days(df)
    pool = {"baseline": [trade(d, 1.0) for d in days], "v1": [trade(d, 2.0) for d in days]}
    wf = walk_forward(df, pool, "baseline", **design.kwargs())
    assert [(w["train"], w["test"]) for w in wf.windows] == [
        (f"{days[a]}..{days[b]}", f"{days[c]}..{days[e]}") for (a, b), (c, e) in expected]
    assert [w["picked"] for w in wf.windows] == ["v1", "v1"]


# ---------------- E: warm-up ----------------


@pytest.mark.parametrize("design", protocol.PROTOCOL_DESIGNS)
def test_warmup_days_are_in_no_window_and_never_scored(design) -> None:
    df = day_frame(design.required_trading_days)
    days = trading_days(df)
    warmup = days[:10]
    # v1 is superb, but only on warm-up days; the baseline is mediocre in the train block.
    pool = {"baseline": [trade(d, 1.0) for d in days[10:]],
            "v1": [trade(d, 1000.0) for d in warmup for _ in range(5)]}
    wf = walk_forward(df, pool, "baseline", **design.kwargs())
    assert len(wf.windows) == 1
    (window,) = wf.windows
    assert window["train"].startswith(str(days[10]))
    assert window["picked"] == "baseline"
    assert window["picked_train_expectancy"] == 1.0
    # Without the warm-up the same data would have picked v1: the exclusion matters.
    no_warmup = walk_forward(df, pool, "baseline", **{**design.kwargs(), "warmup_days": 0})
    assert no_warmup.windows[0]["picked"] == "v1"


# ---------------- F: minimum train trades ----------------


@pytest.mark.parametrize("design", protocol.PROTOCOL_DESIGNS)
def test_min_train_trades_is_30_closed_trades(design) -> None:
    assert design.min_train_trades == 30
    df = day_frame(design.required_trading_days)
    days = trading_days(df)
    train = days[10:10 + design.train_days]
    baseline = [trade(d, 1.0) for d in train]

    def pick(v1):
        return walk_forward(df, {"baseline": baseline, "v1": v1}, "baseline", **design.kwargs()).windows[0]

    assert pick([trade(d, 50.0) for d in train[:29]])["picked"] == "baseline"  # 29 < 30
    assert pick([trade(d, 50.0) for d in train[:30]])["picked"] == "v1"  # 30 qualifies
    # 30 entered, but the last one closes in the test block: only 29 count.
    held = [trade(d, 50.0) for d in train[-29:]] + [trade(train[-1], 50.0, exit_day=days[10 + design.train_days])]
    held.sort(key=lambda t: t.entry_time)
    assert pick(held)["picked"] == "baseline"


# ---------------- G: short segments ----------------


def test_segment_1_is_too_short_for_either_design() -> None:
    assert all(SEGMENT1_DAYS < d.required_trading_days for d in protocol.PROTOCOL_DESIGNS)
    assert all(SEGMENT2_RESEARCH_DAYS >= d.required_trading_days for d in protocol.PROTOCOL_DESIGNS)


def test_short_segment_skips_walk_forward_but_keeps_baseline_and_grid() -> None:
    run = run_mod.run_index("short", synthetic_frame(30), "5m", 1, wf_designs=protocol.PROTOCOL_DESIGNS)
    assert len(run["results"]) == 48 and "baseline" in run["results"]
    assert run["results"]["baseline"]["full"].trades > 0
    assert run["flags"] is not None
    assert run["walk_forward"] is None
    assert set(run["walk_forward_designs"]) == {"design1_rolling_250_63", "design2_anchored_500_250"}
    for entry in run["walk_forward_designs"].values():
        assert entry["result"] is None
    assert run["walk_forward_designs"]["design1_rolling_250_63"]["skipped"] == (
        "segment has 30 trading days < 323 required (10 warm-up + 250 train + 63 test)")
    assert run["walk_forward_designs"]["design2_anchored_500_250"]["skipped"] == (
        "segment has 30 trading days < 760 required (10 warm-up + 500 train + 250 test)")
    md = run_mod.render(run, "5m", False)
    assert md.count("Skipped: segment has 30 trading days") == 2


def test_design_runs_at_exactly_the_required_day_count_and_skips_one_below() -> None:
    small = protocol.WalkForwardDesign("small", train_days=20, test_days=5, step_days=5, anchored=False,
                                       warmup_days=10, train_exit_cutoff=True, min_train_trades=1)
    df = synthetic_frame(35)
    run = run_mod.run_index("exact", df, "5m", 1, wf_designs=(small,))
    assert run["walk_forward_designs"]["small"]["skipped"] is None
    assert len(run["walk_forward_designs"]["small"]["result"].windows) == 1
    run = run_mod.run_index("short", synthetic_frame(34), "5m", 1, wf_designs=(small,))
    assert run["walk_forward_designs"]["small"]["result"] is None


def test_without_designs_run_index_keeps_the_legacy_walk_forward() -> None:
    run = run_mod.run_index("legacy", synthetic_frame(30), "5m", 1)
    assert run["walk_forward_designs"] is None
    assert len(run["walk_forward"].windows) == (30 - 25) // 5 + 1


# ---------------- H: grid hash ----------------


def test_grid_hash_is_stable_and_process_independent() -> None:
    h = protocol.grid_hash(1)
    assert len(h) == 64 and int(h, 16) >= 0
    assert protocol.grid_hash(1) == h
    assert len(protocol.grid_payload(1)["variants"]) == 48
    code = "from research.phase6 import protocol; print(protocol.grid_hash(1))"
    for seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
                             env={"PYTHONPATH": f"{REPO / 'src'}:{REPO}", "PYTHONHASHSEED": seed,
                                  "PATH": "/usr/bin:/bin"})
        assert out.stdout.strip() == h


def _hash_with(monkeypatch, mutate):
    real = protocol.build_variants

    def patched(base):
        variants, families = real(base)
        return mutate(list(variants), {k: list(v) for k, v in families.items()})
    monkeypatch.setattr(protocol, "build_variants", patched)
    try:
        return protocol.grid_hash(1)
    finally:
        monkeypatch.setattr(protocol, "build_variants", real)


@pytest.mark.parametrize("mutation", [
    "remove", "add", "reorder", "rename", "param", "adx", "session_rule", "family_axis"])
def test_any_grid_change_changes_the_hash(monkeypatch, mutation) -> None:
    original = protocol.grid_hash(1)

    def mutate(variants, families):
        v1 = variants[1]
        if mutation == "remove":
            variants.pop()
        elif mutation == "add":
            variants.append(dataclasses.replace(v1, name="extra"))
        elif mutation == "reorder":
            variants[1], variants[2] = variants[2], variants[1]
        elif mutation == "rename":
            variants[1] = dataclasses.replace(v1, name=v1.name + " ")
        elif mutation == "param":
            sig = dataclasses.replace(v1.config.signal, rsi_bull=v1.config.signal.rsi_bull + 1e-9)
            variants[1] = dataclasses.replace(v1, config=dataclasses.replace(v1.config, signal=sig))
        elif mutation == "adx":
            i = next(i for i, v in enumerate(variants) if v.adx is not None)
            variants[i] = dataclasses.replace(variants[i], adx=dataclasses.replace(
                variants[i].adx, threshold=variants[i].adx.threshold + 1))
        elif mutation == "session_rule":
            variants[1] = dataclasses.replace(v1, square_off=time(15, 19))
        elif mutation == "family_axis":
            families[next(iter(families))].reverse()
        return variants, families

    changed = _hash_with(monkeypatch, mutate)
    assert changed != original
    assert protocol.grid_hash(1) == original  # restored


def test_canonical_config_change_changes_the_hash(monkeypatch) -> None:
    original = protocol.grid_hash(1)
    real = protocol.strategy_config_for

    def shifted(index_config):
        cfg = real(index_config)
        return dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, tp_multiplier=4.6))
    monkeypatch.setattr(protocol, "strategy_config_for", shifted)
    assert protocol.grid_hash(1) != original
    assert protocol.canonical_config(1)["risk"]["tp_multiplier"] == 4.6  # same single source


def test_canonical_config_comes_from_strategy_config_for() -> None:
    cfg = protocol.canonical_config(1)
    assert cfg["signal"] == {"ema_fast_length": 9, "ema_slow_length": 21, "rsi_length": 14, "rsi_bull": 55.0,
                             "rsi_bear": 45.0, "supertrend_length": 10, "supertrend_multiplier": 3.0,
                             "use_vwap": False}
    assert cfg["risk"]["atr_length"] == 14
    assert (cfg["risk"]["sl_multiplier"], cfg["risk"]["tp_multiplier"]) == (1.5, 4.5)
    assert (cfg["session"]["start"], cfg["session"]["end"], cfg["session"]["timezone"]) == (
        "09:15:00", "15:40:00", IST)
    assert protocol.grid_payload(1)["variants"][0]["name"] == "baseline"
    assert protocol.grid_payload(1)["variants"][0]["config"] == cfg


# ---------------- I: metadata ----------------


def test_protocol_report_metadata(tmp_path, monkeypatch) -> None:
    use_engine(monkeypatch, make_db(tmp_path, "i.db", candle_rows(pd.bdate_range("2015-06-08", "2015-06-19"), seed=1)
                                    + candle_rows(RESEARCH_TAIL, seed=2)))
    assert run_protocol(tmp_path / "out") == 0
    report = json.loads((tmp_path / "out" / "db_master_5min_nifty-50_protocol.json").read_text())
    assert len(report) == 2
    meta = report[1]["metadata"]
    assert meta["segment_number"] == 2 and report[0]["metadata"]["segment_number"] == 1
    assert {k: v for k, v in report[0]["metadata"].items() if k != "segment_number"} == {
        k: v for k, v in meta.items() if k != "segment_number"}
    assert meta["protocol_version"] == protocol.PROTOCOL_VERSION
    assert meta["dataset_source"] == "historical_candles (source=master_5min.csv)"
    assert meta["db_loads"] == [{"load_id": 1, "file_sha256": "a" * 64}]
    assert (meta["index_id"], meta["timeframe"]) == ("nifty-50", "5m")
    assert meta["segments"] == [
        {"first_bar": "2015-06-08T09:15:00+05:30", "last_bar": "2015-06-19T15:25:00+05:30", "bars": 750,
         "trading_days": 10},
        {"first_bar": "2024-04-08T09:15:00+05:30", "last_bar": "2024-04-25T15:25:00+05:30", "bars": 1050,
         "trading_days": 14}]
    assert (meta["research_end"], meta["holdout_start"], meta["holdout_end"]) == (
        "2024-04-25T23:59:59", "2024-04-26", "2025-04-25")
    assert meta["grid_hash"] == protocol.grid_hash(1) and meta["grid_variants"] == 48
    assert meta["canonical_config"] == protocol.canonical_config(1)
    assert meta["walk_forward_designs"] == [
        {"name": "design1_rolling_250_63", "train_days": 250, "test_days": 63, "step_days": 63, "anchored": False,
         "warmup_days": 10, "train_exit_cutoff": True, "min_train_trades": 30, "required_trading_days": 323},
        {"name": "design2_anchored_500_250", "train_days": 500, "test_days": 250, "step_days": 250,
         "anchored": True, "warmup_days": 10, "train_exit_cutoff": True, "min_train_trades": 30,
         "required_trading_days": 760}]
    assert meta["non_standard_sessions_known"] == 19
    assert meta["non_standard_sessions_in_data"] == []
    from algoedge import __version__
    assert meta["algoedge_version"] == __version__
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    assert meta["git_commit"] in (head, None)  # real commit, never invented
    assert isinstance(meta["git_worktree_dirty"], bool | None)
    md = (tmp_path / "out" / "db_master_5min_nifty-50_protocol.md").read_text()
    assert meta["grid_hash"] in md and md.count("Skipped: segment has") == 4


def test_git_state_never_invents_a_commit(tmp_path) -> None:
    assert protocol.git_state(tmp_path) == {"git_commit": None, "git_worktree_dirty": None}


def test_non_default_holdout_has_no_protocol_holdout_end(tmp_path, monkeypatch) -> None:
    use_engine(monkeypatch, make_db(tmp_path, "i.db", candle_rows(RESEARCH_TAIL, seed=2)))
    assert run_mod.main(["--db", "--indices", "nifty-50", "--out", str(tmp_path / "out"),
                         "--holdout-from", "2024-04-20", "--wf-design", "design2"]) == 0
    meta = json.loads((tmp_path / "out" / "db_master_5min_nifty-50.json").read_text())[0]["metadata"]
    assert (meta["research_end"], meta["holdout_start"], meta["holdout_end"]) == (
        "2024-04-19T23:59:59", "2024-04-20", None)
    assert [d["name"] for d in meta["walk_forward_designs"]] == ["design2_anchored_500_250"]


# ---------------- J: non-standard sessions ----------------


def test_exactly_19_known_non_standard_sessions() -> None:
    sessions = protocol.NON_STANDARD_SESSIONS
    assert len(sessions) == len(set(sessions)) == 19
    assert list(sessions) == sorted(sessions)
    assert sum(d <= protocol.RESEARCH_END_DATE for d in sessions) == 9
    assert sum(protocol.HOLDOUT_START <= d <= protocol.HOLDOUT_END for d in sessions) == 10
    assert date(2024, 3, 2) in sessions and date(2024, 5, 18) in sessions  # the two DR-drill Saturdays


def test_non_standard_sessions_are_tagged_not_removed(tmp_path, monkeypatch, spies) -> None:
    frames, _ = spies
    days = list(pd.bdate_range("2024-02-26", "2024-03-01")) + [pd.Timestamp("2024-03-02")] + list(
        pd.bdate_range("2024-03-04", "2024-03-08"))
    rows = [r for day in days for r in candle_rows([day], seed=day.day, bars=22 if day.weekday() == 5 else 75)]
    use_engine(monkeypatch, make_db(tmp_path, "j.db", rows))
    assert run_protocol(tmp_path / "out") == 0
    (frame,) = frames
    assert len(frame) == 10 * 75 + 22  # the 22-bar Saturday is still in the data
    assert len(frame.loc["2024-03-02"]) == 22
    report = json.loads((tmp_path / "out" / "db_master_5min_nifty-50_protocol.json").read_text())[0]
    assert report["quality"]["rows"] == 10 * 75 + 22
    assert report["non_standard_sessions"]["dates_in_data"] == ["2024-03-02"]
    assert report["metadata"]["non_standard_sessions_in_data"] == ["2024-03-02"]
    assert "Non-standard sessions" in (tmp_path / "out" / "db_master_5min_nifty-50_protocol.md").read_text()


def test_session_trade_tagging_supports_a_later_sensitivity_run() -> None:
    trades = [trade(pd.Timestamp("2024-03-01"), 5.0), trade(pd.Timestamp("2024-03-02"), -3.0),
              trade(pd.Timestamp("2024-03-04"), 7.0)]
    assert protocol.trades_entered_on(trades) == [trades[1]]
    assert protocol.trades_entered_on(trades, sessions=()) == []
    frame = day_frame(5, start="2024-02-27")
    assert protocol.sessions_in(frame) == []
    assert protocol.sessions_in(day_frame(1, start="2023-06-14")) == [date(2023, 6, 14)]


# ---------------- K: segment isolation ----------------


def test_segments_across_the_2015_gap_are_never_joined(tmp_path, monkeypatch, spies) -> None:
    small = protocol.WalkForwardDesign("small", train_days=3, test_days=2, step_days=2, anchored=False,
                                       warmup_days=1, train_exit_cutoff=True, min_train_trades=1)
    monkeypatch.setitem(protocol.DESIGNS_BY_NAME, "design1", small)
    seg1 = pd.bdate_range("2015-06-08", "2015-06-19")
    seg2 = pd.bdate_range("2015-11-16", "2015-11-27")
    use_engine(monkeypatch, make_db(tmp_path, "k.db", candle_rows(seg1, seed=1) + candle_rows(seg2, seed=2)))
    frames, _ = spies
    assert run_protocol(tmp_path / "out", "--wf-design", "design1") == 0
    assert len(frames) == 2
    boundary = pd.Timestamp("2015-06-22", tz=IST)
    assert (frames[0].index < boundary).all() and (frames[1].index >= boundary).all()
    report = json.loads((tmp_path / "out" / "db_master_5min_nifty-50_protocol.json").read_text())
    for run, seg in zip(report, (seg1, seg2), strict=True):
        wf = run["walk_forward_designs"]["small"]["result"]
        assert len(wf["windows"]) == (10 - 1 - 3 - 2) // 2 + 1 == 3
        allowed = {str(d.date()) for d in seg}
        for w in wf["windows"]:
            for span in (w["train"], w["test"]):
                first, last = span.split("..")
                assert first in allowed and last in allowed
        assert run["walk_forward_designs"]["small"]["result"]["windows"][0]["train"].startswith(str(seg[1].date()))
    assert [s["trading_days"] for s in report[0]["metadata"]["segments"]] == [10, 10]
    # No cross-segment aggregate anywhere in the report.
    assert len(report) == 2 and all("combined" not in json.dumps(r).lower() for r in report)
