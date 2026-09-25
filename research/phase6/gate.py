"""Phase 6 pre-registered candidate gate (Step D) - FROZEN.

RESEARCH ONLY. Nothing in src/ imports this module; it never changes the
canonical strategy.

The gate below (G1-G7) and the two candidates it selected were fixed on
research data only (segments up to 2024-04-25 23:59:59 IST). The thresholds
were chosen AFTER the Step C research results had been seen (never the
holdout). Do not edit GATE_SPEC, the thresholds or PREREGISTERED_CANDIDATES:
test_gate.py pins them by value and by hash.

This module also defines - but never runs - the pre-registered holdout
evaluation (2024-04-26 .. 2025-04-25): each candidate is compared on its own
against the canonical baseline, and passes only if it beats the baseline on
BOTH net points and expectancy. Nothing here reads holdout data: the only
reader (load_research_segments) is bounded at the research end, and the
evidence builder refuses any bar at/after the holdout start.

    PYTHONPATH=src:. python -m research.phase6.gate --db --out research/phase6/PREREGISTRATION.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from algoedge.backtest import BacktestTrade, compute_regime_labels
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import drop_invalid_bars
from research.phase6 import candle_source, historical_db, protocol
from research.phase6.engine import Summary, continuous_splits, pair, simulate, summarize, window_bounds
from research.phase6.experiments import build_variants

IST = "Asia/Kolkata"
BASELINE = "baseline"
INDEX_KEY = 1  # NIFTY 50
INDEX_ID = "nifty-50"

# ---------------------------------------------------------------- frozen specification

GATE_VERSION = "phase6-gate-1"
FROZEN_GRID_HASH = "23f6785610ea340d3c2e3943555d4876242eec2b1ce244179f6d8a07f8663afd"
RESEARCH_CUTOFF = protocol.RESEARCH_END_DATE  # 2024-04-25 (bars up to 23:59:59 IST)

# G4: a variant must win a strict majority of test windows in EACH design,
# i.e. >= 15 of 28 (Design 1) and >= 4 of 6 (Design 2).
WINDOW_MAJORITY = "strict"  # windows_won * 2 > windows
# G5: max drawdown on the same OOS days at most 25% worse than the baseline's.
MAX_DRAWDOWN_RATIO = 1.25
# G6: segment 1 (baseline/grid only, too short for walk-forward) sanity check.
MIN_SEGMENT1_EXPECTANCY = 0.0  # strictly greater than
# G7: ranking and cap.
MAX_CANDIDATES = 2
ONE_PER_FAMILY = True

PREREGISTERED_CANDIDATES = ("rsi len 7", "DI only")

HOLDOUT_START = protocol.HOLDOUT_START  # 2024-04-26
HOLDOUT_END = protocol.HOLDOUT_END  # 2025-04-25
HOLDOUT_METRICS = (
    "trades", "win_rate", "profit_factor", "expectancy", "net_points", "max_drawdown",
    "call_trades", "call_net", "call_win_rate", "put_trades", "put_net", "put_win_rate", "overnight_trades",
)

GATE_CRITERIA = (
    ("G1", "No robustness flag in segment 2 (isolated peak, negative split, <10 trades in a split, "
           "<30 trades) - existing run.robustness_flags."),
    ("G2", "Net points > canonical baseline on the SAME walk-forward test days, in BOTH Design 1 and Design 2."),
    ("G3", "Expectancy >= canonical baseline on those same test days, in BOTH designs."),
    ("G4", "Beats the baseline (test-window net points, strictly) in a strict majority of test windows in "
           "EACH design: >= 15/28 (Design 1), >= 4/6 (Design 2)."),
    ("G5", "Max drawdown on the same test days <= 1.25 x the baseline's (at most 25% worse)."),
    ("G6", "Segment 1 (2015-01-09..2015-06-19, baseline/grid only) expectancy > 0."),
    ("G7", "Rank eligible variants by the smaller of their two design net-point deltas (descending, ties by "
           "name); take at most one per parameter family; at most 2 candidates."),
)

GATE_SPEC: dict[str, Any] = {
    "gate_version": GATE_VERSION,
    "grid_hash": FROZEN_GRID_HASH,
    "research_cutoff": RESEARCH_CUTOFF.isoformat(),
    "designs": [d.name for d in protocol.PROTOCOL_DESIGNS],
    "criteria": [code for code, _ in GATE_CRITERIA],
    "window_majority": WINDOW_MAJORITY,
    "max_drawdown_ratio": MAX_DRAWDOWN_RATIO,
    "min_segment1_expectancy": MIN_SEGMENT1_EXPECTANCY,
    "max_candidates": MAX_CANDIDATES,
    "one_per_family": ONE_PER_FAMILY,
    "candidates": list(PREREGISTERED_CANDIDATES),
    "holdout": {"start": HOLDOUT_START.isoformat(), "end": HOLDOUT_END.isoformat(),
                "compare": "each candidate independently vs canonical baseline",
                "pass_rule": "net_points > baseline AND expectancy > baseline",
                "metrics": list(HOLDOUT_METRICS),
                "no_pass": "keep canonical strategy",
                "pass": "report only; canonical strategy is not modified automatically"},
}


def gate_spec_hash(spec: dict[str, Any] = GATE_SPEC) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- evidence


def _ist_date(ts):
    return pd.Timestamp(ts).tz_convert(IST).date()


@dataclass(frozen=True)
class DesignComparison:
    """One variant vs the baseline on exactly the test days of one design."""
    design: str
    trades: int
    expectancy: float | None
    net_points: float
    max_drawdown: float
    baseline_trades: int
    baseline_expectancy: float | None
    baseline_net_points: float
    baseline_max_drawdown: float
    windows_won: int
    windows: int

    @property
    def net_delta(self) -> float:
        return self.net_points - self.baseline_net_points


def same_oos_comparison(days: list, trades_by_variant: dict[str, list[BacktestTrade]],
                        design: protocol.WalkForwardDesign, baseline: str = BASELINE) -> dict[str, DesignComparison]:
    """Each variant, held FIXED, scored on the test days of every window of
    `design` (trades attributed by IST entry day, as in walk_forward), next to
    the baseline on the same days. A window is won when the variant's test
    net points are strictly above the baseline's."""
    bounds = window_bounds(len(days), design.train_days, design.test_days, step_days=design.step_days,
                           anchored=design.anchored, warmup_days=design.warmup_days)
    tests = [set(days[t0:t1]) for _, _, t0, t1 in bounds]
    by_window = {name: [[t for t in trades if _ist_date(t.entry_time) in test] for test in tests]
                 for name, trades in trades_by_variant.items()}
    base_windows = by_window[baseline]
    base = summarize([t for w in base_windows for t in w])
    out = {}
    for name, windows in by_window.items():
        s = summarize([t for w in windows for t in w])
        won = sum(sum(t.points for t in w) > sum(t.points for t in b) for w, b in zip(windows, base_windows,
                                                                                         strict=True))
        out[name] = DesignComparison(design.name, s.trades, s.expectancy, s.net_points, s.max_drawdown,
                                     base.trades, base.expectancy, base.net_points, base.max_drawdown,
                                     won, len(tests))
    return out


@dataclass(frozen=True)
class VariantEvidence:
    name: str
    family: str
    segment2_flags: tuple[str, ...]
    segment2_split_expectancy: dict[str, float | None]
    segment1_expectancy: float | None
    comparisons: dict[str, DesignComparison] = field(default_factory=dict)  # by design name


def check(e: VariantEvidence) -> list[str]:
    """G1-G6 failures for one variant (empty = eligible)."""
    fails = []
    if e.segment2_flags:
        fails.append(f"G1 robustness flag: {'; '.join(e.segment2_flags)}")
    for design in protocol.PROTOCOL_DESIGNS:
        c = e.comparisons[design.name]
        label = design.name
        if not c.net_points > c.baseline_net_points:
            fails.append(f"G2 {label}: net {c.net_points:.1f} <= baseline {c.baseline_net_points:.1f}")
        if c.expectancy is None or c.baseline_expectancy is None or c.expectancy < c.baseline_expectancy:
            fails.append(f"G3 {label}: expectancy {_f(c.expectancy)} < baseline {_f(c.baseline_expectancy)}")
        if not c.windows_won * 2 > c.windows:
            fails.append(f"G4 {label}: won {c.windows_won}/{c.windows} windows (needs a strict majority)")
        if c.max_drawdown > MAX_DRAWDOWN_RATIO * c.baseline_max_drawdown:
            fails.append(f"G5 {label}: max DD {c.max_drawdown:.1f} > {MAX_DRAWDOWN_RATIO} x baseline "
                         f"{c.baseline_max_drawdown:.1f}")
    if e.segment1_expectancy is None or not e.segment1_expectancy > MIN_SEGMENT1_EXPECTANCY:
        fails.append(f"G6 segment 1 expectancy {_f(e.segment1_expectancy)} <= {MIN_SEGMENT1_EXPECTANCY}")
    return fails


def worst_design_delta(e: VariantEvidence) -> float:
    return min(e.comparisons[d.name].net_delta for d in protocol.PROTOCOL_DESIGNS)


@dataclass(frozen=True)
class GateResult:
    eligible: list[str]  # passed G1-G6, in G7 rank order
    selected: list[str]  # the candidates (G7)
    rejected: dict[str, list[str]]  # variant -> reasons (G1-G7)


def evaluate(evidence: dict[str, VariantEvidence]) -> GateResult:
    rejected: dict[str, list[str]] = {}
    eligible = []
    for name, e in evidence.items():
        if name == BASELINE:
            continue
        fails = check(e)
        if fails:
            rejected[name] = fails
        else:
            eligible.append(name)
    eligible.sort(key=lambda n: (-worst_design_delta(evidence[n]), n))
    selected: list[str] = []
    for name in eligible:
        family = evidence[name].family
        taken = next((s for s in selected if evidence[s].family == family), None)
        if ONE_PER_FAMILY and taken:
            rejected[name] = [f"G7 same parameter family '{family}' as higher-ranked {taken!r}"]
        elif len(selected) >= MAX_CANDIDATES:
            rejected[name] = [f"G7 ranked below the {MAX_CANDIDATES}-candidate cap"]
        else:
            selected.append(name)
    return GateResult(eligible, selected, rejected)


# ---------------------------------------------------------------- research-only data path


def research_end() -> datetime:
    return protocol.research_end_for(HOLDOUT_START)


def load_research_segments(engine, source: str = "master_5min.csv") -> list[pd.DataFrame]:
    """The ONLY reader here: bounded at 2024-04-25 23:59:59 IST."""
    segments = candle_source.load_db_segments(engine, index_id=INDEX_ID, timeframe="5m",
                                              end=pd.Timestamp(research_end()), source=source)
    _assert_research_only(segments)
    return segments


def _assert_research_only(segments: list[pd.DataFrame]) -> None:
    limit = pd.Timestamp(HOLDOUT_START).tz_localize(IST)
    for seg in segments:
        if len(seg) and seg.index[-1] >= limit:
            raise RuntimeError(f"holdout data in gate input: bar at {seg.index[-1]} (holdout starts {limit})")


def build_evidence(segments: list[pd.DataFrame], index_key: int = INDEX_KEY) -> dict[str, VariantEvidence]:
    """Evidence for every walk-forward-pool variant (the 48-variant grid
    minus the realism family), from research segments only: segment 1 is the
    short 2015 segment (grid only), segment 2 the walk-forward segment."""
    from research.phase6.run import robustness_flags

    _assert_research_only(segments)
    if len(segments) != 2:
        raise ValueError(f"expected 2 research segments (2015 short + main), got {len(segments)}")
    variants, families = build_variants(strategy_config_for(INDEX_MAP[index_key]))
    pool = [v for v in variants if v.family != "realism"]
    label = INDEX_MAP[index_key].name
    per_segment = []
    for seg in segments:
        df = drop_invalid_bars(seg)
        regimes = compute_regime_labels(df)
        trades = {v.name: pair(simulate(df, v, label)) for v in pool}
        results = {n: {"full": summarize(t, regimes), "continuous": continuous_splits(t, df, regimes)}
                   for n, t in trades.items()}
        per_segment.append((df, trades, results))
    (_, _, res1), (df2, trades2, res2) = per_segment
    days = sorted(set(df2.index.tz_convert(IST).date))
    short = [d.name for d in protocol.PROTOCOL_DESIGNS if len(days) < d.required_trading_days]
    if short:
        raise ValueError(f"segment 2 too short for {short}")
    flags = robustness_flags(res2, families)
    comparisons = {d.name: same_oos_comparison(days, trades2, d) for d in protocol.PROTOCOL_DESIGNS}
    return {
        v.name: VariantEvidence(
            name=v.name, family=v.family, segment2_flags=tuple(flags.get(v.name, ())),
            segment2_split_expectancy={k: s.expectancy for k, s in res2[v.name]["continuous"].items()},
            segment1_expectancy=res1[v.name]["full"].expectancy,
            comparisons={d: comparisons[d][v.name] for d in comparisons},
        )
        for v in pool
    }


# ---------------------------------------------------------------- pre-registered holdout rule (NOT run here)


def holdout_metrics(summary: Summary) -> dict[str, Any]:
    """The minimum metrics the holdout report must show for each strategy."""
    return {k: getattr(summary, k) for k in HOLDOUT_METRICS}


def holdout_decision(candidate: Summary, baseline: Summary) -> dict[str, Any]:
    """A candidate passes only if it beats the canonical baseline on BOTH net
    points and expectancy (strictly). Informational only - it never changes
    the canonical strategy."""
    beats_net = candidate.net_points > baseline.net_points
    beats_exp = (candidate.expectancy is not None and baseline.expectancy is not None
                 and candidate.expectancy > baseline.expectancy)
    return {"beats_net_points": beats_net, "beats_expectancy": beats_exp, "passed": beats_net and beats_exp}


def holdout_verdict(decisions: dict[str, dict[str, Any]]) -> str:
    passed = [name for name, d in decisions.items() if d["passed"]]
    if not passed:
        return "No candidate passed: keep the canonical strategy."
    return (f"Passed: {', '.join(passed)}. Report only - the canonical strategy is NOT modified "
            "automatically; any change needs a separate, explicit decision.")


# ---------------------------------------------------------------- report


def _f(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_preregistration(evidence: dict[str, VariantEvidence], result: GateResult,
                           metadata: dict[str, Any]) -> str:
    d1, d2 = (d.name for d in protocol.PROTOCOL_DESIGNS)
    base = evidence[BASELINE]
    lines = [
        "# Phase 6 - pre-registered candidates and holdout evaluation", "",
        f"Gate version `{GATE_VERSION}`, gate spec SHA-256 `{gate_spec_hash()}`. "
        "Frozen: the thresholds and candidates below must not change after this point.", "",
        "## Provenance", "",
        f"- Research cutoff: **{RESEARCH_CUTOFF}** (bars up to {research_end().isoformat()} IST). "
        f"Holdout: {HOLDOUT_START} .. {HOLDOUT_END}.",
        f"- Dataset: {metadata['dataset_source']}; "
        + ", ".join(f"load_id {x['load_id']} SHA-256 `{x['file_sha256']}`" for x in metadata["db_loads"]),
        f"- Grid: {metadata['grid_variants']} variants, hash `{metadata['grid_hash']}` "
        f"(45 in the walk-forward pool; the realism family is excluded, as in walk_forward).",
        "- Segments: " + "; ".join(f"{s['first_bar'][:10]}..{s['last_bar'][:10]} ({s['bars']} bars, "
                                     f"{s['trading_days']} days)" for s in metadata["segments"]),
        f"- Code: git {metadata.get('git_commit')} (worktree dirty: {metadata.get('git_worktree_dirty')}), "
        f"protocol {metadata['protocol_version']}.",
        "- **The thresholds were chosen AFTER seeing the Step C research results** (never the holdout). The "
        "holdout is therefore still an untouched test, but selecting from 45 variants means any in-sample "
        "edge should be expected to shrink out of sample.",
        "- **The holdout was not used**: every number here comes from research data read with an upper bound "
        f"of {research_end().isoformat()}; the gate code refuses any bar on or after {HOLDOUT_START}.", "",
        "## Gate criteria", "",
    ]
    lines += [f"- **{code}** - {text}" for code, text in GATE_CRITERIA]
    lines += ["", "## Pre-registered candidates", ""]
    lines += [f"{i}. **{name}** (family `{evidence[name].family}`)" for i, name in enumerate(result.selected, 1)]
    lines += ["", "## Research-only evidence (segment 2 walk-forward test days; baseline on the same days)", "",
              "| variant | D1 trades | D1 exp | D1 net | D1 dNet | D1 maxDD | D1 won | D2 trades | D2 exp | D2 net "
              "| D2 dNet | D2 maxDD | D2 won | seg2 exp train/valid/OOS | seg1 exp |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    shown = [BASELINE, *result.selected] + [n for n in result.eligible if n not in result.selected]
    for name in shown:
        e = evidence[name]
        a, b = e.comparisons[d1], e.comparisons[d2]
        sp = " / ".join(_f(e.segment2_split_expectancy[k]) for k in ("train", "validation", "out_of_sample"))
        lines.append(f"| {name} | {a.trades} | {_f(a.expectancy)} | {a.net_points:.1f} | {a.net_delta:+.1f} "
                     f"| {a.max_drawdown:.1f} | {a.windows_won}/{a.windows} | {b.trades} | {_f(b.expectancy)} "
                     f"| {b.net_points:.1f} | {b.net_delta:+.1f} | {b.max_drawdown:.1f} | {b.windows_won}/{b.windows} "
                     f"| {sp} | {_f(e.segment1_expectancy)} |")
    lines += ["", f"Baseline on the same test days: D1 {base.comparisons[d1].net_points:.1f} pts "
              f"(exp {_f(base.comparisons[d1].expectancy)}), D2 {base.comparisons[d2].net_points:.1f} pts "
              f"(exp {_f(base.comparisons[d2].expectancy)}). Points are index points before costs.", "",
              "## Rejected variants", ""]
    lines += [f"- {name}: {' | '.join(reasons)}" for name, reasons in sorted(result.rejected.items())]
    lines += [
        "", "## Pre-registered holdout evaluation (NOT yet run)", "",
        f"- Period: {HOLDOUT_START} through {HOLDOUT_END} (IST), never used before this document.",
        "- Each candidate is compared **independently** against the canonical baseline over that same period.",
        "- Reported for each: trades, win rate, profit factor, expectancy, net points, max drawdown, "
        "CALL/PUT breakdown (trades, net, win rate), overnight holds.",
        "- Decision rule: a candidate passes the holdout only if it beats the canonical baseline on BOTH "
        "(1) net points and (2) expectancy.",
        "- If neither candidate passes, keep the canonical strategy. If one or both pass, report the results; "
        "the canonical strategy is not modified automatically.", "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", action="store_true", required=True,
                        help="read research segments from historical_candles (bounded at the research end)")
    parser.add_argument("--db-source", default="master_5min.csv")
    parser.add_argument("--out", type=Path, required=True, help="pre-registration markdown to write")
    parser.add_argument("--dataset-label", default=None,
                        help="dataset description for the report (default: historical_candles (source=...))")
    args = parser.parse_args(argv)

    engine = historical_db.research_engine()
    segments = load_research_segments(engine, args.db_source)
    evidence = build_evidence(segments)
    result = evaluate(evidence)
    metadata = protocol.research_metadata(
        index_id=INDEX_ID, index_key=INDEX_KEY, timeframe="5m",
        dataset_source=args.dataset_label or f"historical_candles (source={args.db_source})", segments=segments,
        designs=protocol.PROTOCOL_DESIGNS, holdout_start=HOLDOUT_START, research_end=research_end(),
        loads=protocol.db_loads(engine, args.db_source),
    )
    if metadata["grid_hash"] != FROZEN_GRID_HASH:
        print(f"grid hash {metadata['grid_hash']} != frozen {FROZEN_GRID_HASH}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_preregistration(evidence, result, metadata))
    print(f"selected {result.selected}; wrote {args.out}")
    if tuple(result.selected) != PREREGISTERED_CANDIDATES:
        print(f"gate selection {result.selected} != pre-registered {list(PREREGISTERED_CANDIDATES)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
