"""Phase 6 research runner.

    PYTHONPATH=src:. python -m research.phase6.run --interval 5m
    PYTHONPATH=src:. python -m research.phase6.run --interval 5m --refresh   # re-download
    PYTHONPATH=src:. python -m research.phase6.run --synthetic               # pipeline check only
    PYTHONPATH=src:. python -m research.phase6.run --db --indices nifty-50   # SQL Server historical_candles

Writes research/phase6/results/<label>.md and .json. Never touches the
canonical strategy, the broker, or any order path.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from algoedge.backtest import compute_regime_labels, pair_trades
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import drop_invalid_bars
from fno_signals.strategy import run as canonical_run
from research.phase6 import candle_source, historical_db
from research.phase6 import data as data_mod
from research.phase6.engine import (
    Summary,
    continuous_splits,
    pair,
    production_splits,
    simulate,
    summarize,
    walk_forward,
)
from research.phase6.experiments import build_variants

RESULTS_DIR = Path(__file__).parent / "results"


def synthetic_frame(days: int = 60, seed: int = 7, interval_minutes: int = 5) -> pd.DataFrame:
    """Random-walk OHLCV on the NSE session grid - ONLY for checking the
    pipeline end to end. Results on it mean nothing about the strategy."""
    rng = np.random.default_rng(seed)
    bars_per_day = -(-375 // interval_minutes)
    sessions = pd.bdate_range("2026-06-01", periods=days)
    index = []
    for day in sessions:
        start = pd.Timestamp(f"{day.date()} 09:15", tz="Asia/Kolkata")
        index += [start + pd.Timedelta(minutes=interval_minutes * k) for k in range(bars_per_day)]
    n = len(index)
    steps = rng.normal(0, 12, n)
    steps[::bars_per_day] += rng.normal(0, 60, days)  # overnight gaps
    close = 24000 + np.cumsum(steps)
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 8, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 8, n))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": np.zeros(n)},
        index=pd.DatetimeIndex(index),
    )


def _fmt(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _row(name: str, s: Summary, splits: dict[str, Summary] | None = None) -> str:
    cells = [
        name, s.trades, _fmt(s.win_rate, 1), _fmt(s.profit_factor), _fmt(s.expectancy), _fmt(s.net_points, 1),
        _fmt(s.max_drawdown, 1), s.max_consecutive_losses, _fmt(s.avg_winner, 1), _fmt(s.avg_loser, 1),
        f"{s.call_trades} / {_fmt(s.call_net, 1)}", f"{s.put_trades} / {_fmt(s.put_net, 1)}",
    ]
    if splits is not None:
        cells += [
            f"{splits[k].trades} / {_fmt(splits[k].expectancy)} / {_fmt(splits[k].profit_factor)}"
            for k in ("train", "validation", "out_of_sample")
        ]
    return "| " + " | ".join(str(c) for c in cells) + " |"


HEADER = (
    "| variant | trades | win % | PF | expectancy | net pts | max DD | max consec L | avg win | avg loss "
    "| CALL n / net | PUT n / net | train n/exp/PF | valid n/exp/PF | OOS n/exp/PF |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def robustness_flags(results: dict, families: dict[str, list[str]]) -> dict[str, list[str]]:
    flags: dict[str, list[str]] = {}
    base = results["baseline"]["full"]
    base_exp = base.expectancy if base.expectancy is not None else 0.0
    for axis in families.values():
        for pos, name in enumerate(axis):
            if name == "baseline" or name not in results:
                continue
            s = results[name]["full"]
            notes = []
            # Nearest NON-baseline value on each side (the baseline itself is
            # trivially "no better than baseline", so it can't be the evidence).
            neighbours = []
            for step in (-1, 1):
                j = pos + step
                while 0 <= j < len(axis) and axis[j] == "baseline":
                    j += step
                if 0 <= j < len(axis):
                    neighbours.append(axis[j])
            neighbour_exp = [
                results[nb]["full"].expectancy for nb in neighbours
                if nb in results and results[nb]["full"].expectancy is not None
            ]
            if s.expectancy is not None and s.expectancy > base_exp and neighbour_exp:
                if all(e <= base_exp for e in neighbour_exp):
                    notes.append("isolated peak (neighbours no better than baseline) - possible overfit")
            splits = results[name]["continuous"]
            exps = [splits[k].expectancy for k in ("train", "validation", "out_of_sample")]
            if any(e is None for e in exps) or any(splits[k].trades < 10 for k in splits):
                notes.append("fewer than 10 trades in at least one split")
            elif s.expectancy and s.expectancy > 0 and any(e < 0 for e in exps):
                notes.append("positive overall but negative in at least one split")
            if s.trades < 30:
                notes.append(f"small sample ({s.trades} trades)")
            if notes:
                flags[name] = notes
    return flags


def run_index(label: str, df: pd.DataFrame, interval: str, index_key: int) -> dict:
    index_config = INDEX_MAP[index_key]
    base_config = strategy_config_for(index_config)
    quality = data_mod.quality_report(df, interval)
    # Same invalid-bar handling as the canonical run() (counted in the
    # data-quality report before it is applied).
    df = drop_invalid_bars(df)
    variants, families = build_variants(base_config)
    regimes = compute_regime_labels(df)

    # Parity: the research baseline must equal the production Backtest path.
    _results, canonical_events = canonical_run(df, base_config, underlying_label=index_config.name)
    production_trades = pair_trades(canonical_events)
    research_trades = pair(simulate(df, variants[0], index_config.name))
    assert [(t.entry_time, t.points) for t in production_trades] == [
        (t.entry_time, t.points) for t in research_trades
    ], "research baseline diverged from fno_signals.strategy.run()"

    results: dict[str, dict] = {}
    trades_by_variant = {}
    for v in variants:
        trades = pair(simulate(df, v, index_config.name))
        trades_by_variant[v.name] = trades
        results[v.name] = {
            "variant": v, "full": summarize(trades, regimes),
            "continuous": continuous_splits(trades, df, regimes),
        }
    results["baseline"]["production_splits"] = production_splits(df, variants[0], index_config.name)

    wf_pool = {k: t for k, t in trades_by_variant.items() if results[k]["variant"].family != "realism"}
    wf = walk_forward(df, wf_pool, "baseline") if interval in ("5m", "15m") else None
    return {
        "label": label, "quality": quality, "results": results,
        "families": families, "flags": robustness_flags(results, families), "walk_forward": wf,
    }


def render(run: dict, interval: str, synthetic: bool) -> str:
    lines = [f"## {run['label']} ({interval})", ""]
    if synthetic:
        lines += ["> SYNTHETIC random-walk data - pipeline check only, NOT a strategy result.", ""]
    q = run["quality"].as_dict()
    lines += ["### Data quality", "", "| check | value |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v, 3) if isinstance(v, float) else v} |" for k, v in q.items()]
    res = run["results"]
    base = res["baseline"]
    lines += ["", "### Baseline", "", HEADER, _row("baseline", base["full"], base["continuous"]), ""]
    ps = base["production_splits"]
    lines += [
        "Production split method (/api/backtest/run?split=true - strategy re-run per slice):", "",
        "| split | trades | win % | PF | expectancy | net pts | max DD |", "|---|---|---|---|---|---|---|",
    ]
    for k in ("train", "validation", "out_of_sample"):
        s = ps[k]
        lines.append(
            f"| {k} | {s.trades} | {_fmt(s.win_rate, 1)} | {_fmt(s.profit_factor)} | {_fmt(s.expectancy)} "
            f"| {_fmt(s.net_points, 1)} | {_fmt(s.max_drawdown, 1)} |"
        )
    b = base["full"]
    lines += ["", f"Exit reasons: {b.exit_reasons}; overnight holds: {b.overnight_trades}; "
              f"times paper's 3-consecutive-loss halt would trip: {b.halts_at_3_losses}", ""]
    lines += ["Time of day (entry hour):", "", "| hour | trades | win % | net pts |", "|---|---|---|---|"]
    lines += [f"| {x['label']} | {x['trades']} | {_fmt(x['win_rate'], 1)} | {_fmt(x['net_points'], 1)} |"
              for x in b.time_of_day]
    lines += ["", "Market regime (close vs SMA50 at entry):", "", "| regime | trades | win % | net pts |",
              "|---|---|---|---|"]
    lines += [f"| {x['label']} | {x['trades']} | {_fmt(x['win_rate'], 1)} | {_fmt(x['net_points'], 1)} |"
              for x in b.regime]

    lines += ["", "### Experiments (train/valid/OOS = one continuous run, trades assigned by entry time)", "",
              HEADER]
    for name, r in res.items():
        lines.append(_row(name, r["full"], r["continuous"]))
    if run["flags"]:
        lines += ["", "### Robustness flags", ""]
        lines += [f"- **{k}**: {'; '.join(v)}" for k, v in run["flags"].items()]
    wf = run["walk_forward"]
    if wf is not None:
        lines += ["", "### Walk-forward (20 train days -> 5 test days, pick = best train expectancy)", "",
                  "| train | test | picked | test net (picked) | test net (baseline) |", "|---|---|---|---|---|"]
        lines += [f"| {w['train']} | {w['test']} | {w['picked']} | {_fmt(w['picked_test_net'], 1)} "
                  f"| {_fmt(w['baseline_test_net'], 1)} |" for w in wf.windows]
        lines += ["", HEADER.split("\n")[0].rsplit("| train", 1)[0] + "|",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|",
                  _row("walk-forward picks (OOS)", wf.selected_oos), _row("baseline (same OOS days)", wf.baseline_oos)]
    return "\n".join(lines) + "\n"


def null_distribution(seeds: int, interval_minutes: int = 5) -> str:
    """Runs every variant on `seeds` independent random walks (no edge by
    construction). The spread of results is what pure chance produces over
    a 60-day window - the bar any real-data "improvement" has to clear."""
    base_config = strategy_config_for(INDEX_MAP[1])
    variants, _families = build_variants(base_config)
    by_variant: dict[str, list[Summary]] = {v.name: [] for v in variants}
    best_pf: list[float] = []
    for seed in range(seeds):
        df = synthetic_frame(seed=1000 + seed, interval_minutes=interval_minutes)
        pfs = []
        for v in variants:
            s = summarize(pair(simulate(df, v, "NULL")))
            by_variant[v.name].append(s)
            if v.family != "realism" and s.profit_factor is not None and s.trades >= 30:
                pfs.append(s.profit_factor)
        best_pf.append(max(pfs) if pfs else float("nan"))

    def pct(values, q):
        values = [x for x in values if x is not None and not math.isnan(x)]
        return float(np.percentile(values, q)) if values else None

    lines = [
        f"# Null benchmark - {seeds} random walks x 60 days of {interval_minutes}m bars", "",
        "No strategy has an edge on a random walk, so these ranges are pure noise.", "",
        "| variant | median trades | PF p10 | PF median | PF p90 | share PF>1 | expectancy p10 | expectancy p90 "
        "| median 3-loss halts |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, sums in by_variant.items():
        pfs = [s.profit_factor for s in sums]
        exps = [s.expectancy for s in sums]
        share = sum(1 for x in pfs if x is not None and x > 1) / len(sums)
        lines.append(
            f"| {name} | {int(np.median([s.trades for s in sums]))} | {_fmt(pct(pfs, 10))} | {_fmt(pct(pfs, 50))} "
            f"| {_fmt(pct(pfs, 90))} | {share:.0%} | {_fmt(pct(exps, 10))} | {_fmt(pct(exps, 90))} "
            f"| {int(np.median([s.halts_at_3_losses for s in sums]))} |"
        )
    lines += [
        "", "Best PF among all non-realism variants (>=30 trades) per random walk - what picking the "
        f"best of {len(variants)} variants yields by chance:", "",
        f"- p10 {_fmt(pct(best_pf, 10))}, median {_fmt(pct(best_pf, 50))}, p90 {_fmt(pct(best_pf, 90))}",
    ]
    return "\n".join(lines) + "\n"


def _jsonable(obj):
    if isinstance(obj, Summary):
        return asdict(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items() if k != "variant"}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return _jsonable(asdict(obj))
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_end(value: str) -> pd.Timestamp:
    """--end: a bare YYYY-MM-DD covers that entire IST day (bar_start <= 23:59:59 -
    whole seconds, because SQL Server DATETIME rounds 23:59:59.999... up to the
    next midnight); anything with a time component is kept exactly."""
    ts = pd.Timestamp(value)
    if _DATE_ONLY.fullmatch(value.strip()):
        return ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return ts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", default="5m", choices=list(data_mod.BACKTEST_TIMEFRAMES))
    parser.add_argument("--indices", nargs="+", default=list(data_mod.INDEX_CHOICE))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--csv", type=Path, default=None,
                        help="run on this CSV (e.g. data/phase6_nifty50_5m.csv from download_dhan) "
                             "instead of fetching; requires exactly one --indices value")
    parser.add_argument("--null", type=int, default=0, metavar="SEEDS",
                        help="only run the random-walk null benchmark with this many seeds")
    parser.add_argument("--db", action="store_true",
                        help="read candles from the research historical_candles table (SQL Server, "
                             "ALGOEDGE_DB_* settings); each contiguous segment is run separately")
    parser.add_argument("--db-source", default="master_5min.csv",
                        help="historical_candles.source to read with --db (default: master_5min.csv)")
    parser.add_argument("--start", type=pd.Timestamp, default=None,
                        help="with --db: first bar_start to read (inclusive, IST); YYYY-MM-DD = start of that day")
    parser.add_argument("--end", type=_parse_end, default=None,
                        help="with --db: last bar_start to read (inclusive, IST); YYYY-MM-DD = the whole day, "
                             "an explicit date-time is used exactly")
    args = parser.parse_args(argv)

    if args.db:
        conflicting = [flag for flag, used in (("--csv", args.csv is not None), ("--synthetic", args.synthetic),
                                               ("--null", bool(args.null))) if used]
        if conflicting:
            parser.error(f"--db cannot be combined with {', '.join(conflicting)}")
        if len(args.indices) != 1:
            parser.error("--db requires exactly one --indices value")
    elif args.start is not None or args.end is not None:
        parser.error("--start/--end are only supported with --db")

    if args.null:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"null_benchmark_{args.interval}.md"
        minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(args.interval, 5)
        path.write_text(null_distribution(args.null, minutes))
        print(f"wrote {path}")
        return 0

    if args.csv is not None and len(args.indices) != 1:
        parser.error("--csv requires exactly one --indices value")

    if args.db:
        return _run_db(args)

    runs = []
    for index_id in args.indices:
        if args.csv is not None:
            df = data_mod.load_csv(args.csv)
        elif args.synthetic:
            df = synthetic_frame(seed=list(data_mod.INDEX_CHOICE).index(index_id) + 1)
        else:
            df = data_mod.load(index_id, args.interval, refresh=args.refresh)
        runs.append(run_index(index_id, df, args.interval, data_mod.INDEX_CHOICE[index_id]))

    label = f"{'synthetic' if args.synthetic else 'phase6'}_{args.interval}"
    if args.csv is not None:
        label = f"{args.csv.stem}_{args.indices[0]}"
    _write_reports(args, label, runs)
    return 0


def _run_db(args) -> int:
    """--db: read historical_candles through the read-only candle source and
    run the unchanged run_index() on each contiguous segment on its own. The
    whole DB frame is never passed to run_index(): a data hole (e.g. the
    excluded 2015-06-22..2015-11-13 window) must not be bridged as if the bars
    on either side were adjacent. Segments are reported separately; no
    cross-segment aggregate is computed."""
    index_id = args.indices[0]
    engine = historical_db.research_engine()
    segments = candle_source.load_db_segments(
        engine, index_id=index_id, timeframe=args.interval, start=args.start, end=args.end,
        source=args.db_source,
    )
    if not segments:
        print(f"No candles in historical_candles for index_id={index_id!r}, timeframe={args.interval!r}, "
              f"source={args.db_source!r}, start={args.start}, end={args.end}", file=sys.stderr)
        return 1
    runs = []
    for number, segment in enumerate(segments, start=1):
        first, last = segment.index[0], segment.index[-1]
        label = f"{index_id} seg{number} {first:%Y-%m-%d %H:%M}..{last:%Y-%m-%d %H:%M}"
        print(f"{label}: {len(segment)} bars")
        runs.append(run_index(label, segment, args.interval, data_mod.INDEX_CHOICE[index_id]))
    _write_reports(args, f"db_{Path(args.db_source).stem}_{index_id}", runs)
    return 0


def _write_reports(args, label: str, runs: list[dict]) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    body = "".join(render(r, args.interval, args.synthetic) for r in runs)
    (args.out / f"{label}.md").write_text(f"# Phase 6 research results - {label}\n\n{body}")
    (args.out / f"{label}.json").write_text(json.dumps(
        [{"label": r["label"], "quality": _jsonable(r["quality"]),
          "results": {k: _jsonable(v) for k, v in r["results"].items()},
          "flags": r["flags"],
          "walk_forward": _jsonable(r["walk_forward"]) if r["walk_forward"] else None} for r in runs],
        indent=1, default=str,
    ))
    print(f"wrote {args.out / label}.md / .json")


if __name__ == "__main__":
    raise SystemExit(main())
