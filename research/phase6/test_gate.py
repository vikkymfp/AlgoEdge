"""Tests for the frozen Phase 6 pre-registered gate (research.phase6.gate) -
synthetic data and SQLite only; never the real dataset, never the holdout.

    PYTHONPATH=src:. python -m pytest research/phase6/test_gate.py -q
"""

import dataclasses
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from algoedge.backtest import BacktestTrade
from research.phase6 import gate, historical_db, protocol
from research.phase6.engine import summarize

REPO = Path(__file__).resolve().parents[2]
IST = "Asia/Kolkata"
D1, D2 = (d.name for d in protocol.PROTOCOL_DESIGNS)


# ---------------- helpers ----------------


def trade(day, points, *, direction="CALL", exit_day=None):
    entry = pd.Timestamp(f"{pd.Timestamp(day).date()} 10:00", tz=IST)
    exit_ = pd.Timestamp(f"{pd.Timestamp(exit_day if exit_day is not None else day).date()} 15:00", tz=IST)
    return BacktestTrade(entry_time=entry, exit_time=exit_, direction=direction, entry_price=100.0,
                         exit_price=100.0 + points, exit_reason="TARGET" if points > 0 else "SL", points=points,
                         strike=100, option_symbol="X")


def comparison(design=D1, *, net=1200.0, exp=3.0, dd=1100.0, won=16, windows=28, base_net=1000.0, base_exp=2.5,
               base_dd=1000.0):
    return gate.DesignComparison(design, 400, exp, net, dd, 400, base_exp, base_net, base_dd, won, windows)


def evidence(name="v", family="fam", *, flags=(), seg1=1.0, d1=None, d2=None):
    return gate.VariantEvidence(
        name=name, family=family, segment2_flags=tuple(flags),
        segment2_split_expectancy={"train": 1.0, "validation": 1.0, "out_of_sample": 1.0},
        segment1_expectancy=seg1,
        comparisons={D1: d1 or comparison(D1), D2: d2 or comparison(D2, won=4, windows=6)},
    )


def with_baseline(*items):
    base = evidence(gate.BASELINE, "baseline", d1=comparison(D1, net=1000.0, exp=2.5, dd=1000.0, won=0),
                    d2=comparison(D2, net=1000.0, exp=2.5, dd=1000.0, won=0, windows=6))
    return {gate.BASELINE: base, **{e.name: e for e in items}}


# ---------------- frozen specification ----------------


def test_gate_spec_is_frozen() -> None:
    assert gate.GATE_SPEC == {
        "gate_version": "phase6-gate-1",
        "grid_hash": "23f6785610ea340d3c2e3943555d4876242eec2b1ce244179f6d8a07f8663afd",
        "research_cutoff": "2024-04-25",
        "designs": ["design1_rolling_250_63", "design2_anchored_500_250"],
        "criteria": ["G1", "G2", "G3", "G4", "G5", "G6", "G7"],
        "window_majority": "strict",
        "max_drawdown_ratio": 1.25,
        "min_segment1_expectancy": 0.0,
        "max_candidates": 2,
        "one_per_family": True,
        "candidates": ["rsi len 7", "DI only"],
        "holdout": {"start": "2024-04-26", "end": "2025-04-25",
                    "compare": "each candidate independently vs canonical baseline",
                    "pass_rule": "net_points > baseline AND expectancy > baseline",
                    "metrics": ["trades", "win_rate", "profit_factor", "expectancy", "net_points", "max_drawdown",
                                "call_trades", "call_net", "call_win_rate", "put_trades", "put_net", "put_win_rate",
                                "overnight_trades"],
                    "no_pass": "keep canonical strategy",
                    "pass": "report only; canonical strategy is not modified automatically"},
    }
    assert gate.gate_spec_hash() == "8ecfe0cf9d11adecbc494b9ca665c62925b1b9373fb540a772de1792a7f76334"


def test_thresholds_and_candidates_are_frozen() -> None:
    assert gate.PREREGISTERED_CANDIDATES == ("rsi len 7", "DI only")
    assert (gate.MAX_DRAWDOWN_RATIO, gate.MIN_SEGMENT1_EXPECTANCY, gate.MAX_CANDIDATES, gate.ONE_PER_FAMILY) == (
        1.25, 0.0, 2, True)
    assert [code for code, _ in gate.GATE_CRITERIA] == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    assert (gate.RESEARCH_CUTOFF, gate.HOLDOUT_START, gate.HOLDOUT_END) == (
        date(2024, 4, 25), date(2024, 4, 26), date(2025, 4, 25))
    assert gate.research_end() == datetime(2024, 4, 25, 23, 59, 59)


def test_frozen_grid_hash_matches_the_unchanged_grid() -> None:
    assert protocol.grid_hash(1) == gate.FROZEN_GRID_HASH
    names = [v["name"] for v in protocol.grid_payload(1)["variants"]]
    assert len(names) == 48
    assert all(c in names for c in gate.PREREGISTERED_CANDIDATES)


def test_candidates_are_in_different_families_and_the_walk_forward_pool() -> None:
    payload = protocol.grid_payload(1)["variants"]
    family = {v["name"]: v["family"] for v in payload}
    assert family["rsi len 7"] == "rsi_length" and family["DI only"] == "adx_other"
    assert all(family[c] != "realism" for c in gate.PREREGISTERED_CANDIDATES)


# ---------------- G1-G6 ----------------


def test_a_variant_meeting_every_criterion_is_eligible() -> None:
    assert gate.check(evidence()) == []


@pytest.mark.parametrize("kwargs, code", [
    ({"flags": ["isolated peak (neighbours no better than baseline) - possible overfit"]}, "G1"),
    ({"d1": comparison(D1, net=1000.0)}, "G2"),  # equal is not "more"
    ({"d2": comparison(D2, net=900.0, won=4, windows=6)}, "G2"),
    ({"d1": comparison(D1, exp=2.49)}, "G3"),
    ({"d2": comparison(D2, exp=None, won=4, windows=6)}, "G3"),
    ({"d1": comparison(D1, won=14)}, "G4"),  # 14/28 is not a strict majority
    ({"d2": comparison(D2, won=3, windows=6)}, "G4"),
    ({"d1": comparison(D1, dd=1250.01)}, "G5"),
    ({"seg1": 0.0}, "G6"),
    ({"seg1": None}, "G6"),
])
def test_each_criterion_rejects_on_its_own(kwargs, code) -> None:
    fails = gate.check(evidence(**kwargs))
    assert [f[:2] for f in fails] == [code]


def test_boundaries_that_pass() -> None:
    assert gate.check(evidence(d1=comparison(D1, exp=2.5))) == []  # G3: equal expectancy is allowed
    assert gate.check(evidence(d1=comparison(D1, won=15))) == []  # G4: 15/28
    assert gate.check(evidence(d2=comparison(D2, won=4, windows=6))) == []  # G4: 4/6
    assert gate.check(evidence(d1=comparison(D1, dd=1250.0))) == []  # G5: exactly 1.25x
    assert gate.check(evidence(seg1=0.01)) == []  # G6


# ---------------- G7 ----------------


def test_ranking_uses_the_weaker_design_and_one_per_family_and_the_cap() -> None:
    items = with_baseline(
        evidence("a", "f1", d1=comparison(D1, net=5000.0), d2=comparison(D2, net=1100.0, won=4, windows=6)),  # 100
        evidence("b", "f1", d1=comparison(D1, net=1900.0), d2=comparison(D2, net=1900.0, won=4, windows=6)),  # 900
        evidence("c", "f2", d1=comparison(D1, net=1500.0), d2=comparison(D2, net=1500.0, won=4, windows=6)),  # 500
        evidence("d", "f3", d1=comparison(D1, net=1300.0), d2=comparison(D2, net=1300.0, won=4, windows=6)),  # 300
        evidence("e", "f4", flags=["x"]),
    )
    result = gate.evaluate(items)
    assert result.eligible == ["b", "c", "d", "a"]
    assert result.selected == ["b", "c"]
    assert result.rejected["d"] == ["G7 ranked below the 2-candidate cap"]
    assert result.rejected["a"] == ["G7 same parameter family 'f1' as higher-ranked 'b'"]
    assert result.rejected["e"][0].startswith("G1")
    assert gate.BASELINE not in result.rejected and gate.BASELINE not in result.eligible


def test_ties_are_broken_by_name_and_zero_candidates_is_valid() -> None:
    tied = gate.evaluate(with_baseline(evidence("y", "f1"), evidence("x", "f2")))
    assert tied.selected == ["x", "y"]
    none = gate.evaluate(with_baseline(evidence("x", "f1", seg1=-1.0)))
    assert none.selected == [] and none.eligible == []


# ---------------- same-OOS comparison ----------------


def test_same_oos_comparison_uses_only_the_design_test_days() -> None:
    design = protocol.WalkForwardDesign("t", train_days=3, test_days=2, step_days=2, anchored=False,
                                        warmup_days=1, train_exit_cutoff=True, min_train_trades=1)
    days = [d.date() for d in pd.bdate_range("2020-01-06", periods=10)]
    # windows: test days idx 4-5, 6-7, 8-9; days 0-3 are warm-up/train only.
    base = [trade(d, 1.0) for d in days]
    variant = [trade(days[0], 1000.0), trade(days[3], 1000.0),  # not on any test day: ignored
               trade(days[4], 5.0), trade(days[6], -5.0), trade(days[8], 2.0), trade(days[9], 0.5)]
    out = gate.same_oos_comparison(days, {"baseline": base, "v": variant}, design)
    v, b = out["v"], out["baseline"]
    assert (v.windows, v.windows_won) == (3, 2)  # 5>2, -5<2, 2.5>2
    assert (v.trades, v.net_points) == (4, 2.5)
    assert (b.trades, b.net_points, b.windows_won) == (6, 6.0, 0)
    assert (v.baseline_net_points, v.baseline_trades) == (6.0, 6)
    assert v.net_delta == -3.5
    assert v.expectancy == pytest.approx(2.5 / 4)


def test_same_oos_comparison_matches_protocol_window_counts() -> None:
    days = [d.date() for d in pd.bdate_range("2015-11-16", periods=2083)]
    out = {d.name: gate.same_oos_comparison(days, {"baseline": []}, d)["baseline"]
           for d in protocol.PROTOCOL_DESIGNS}
    assert (out[D1].windows, out[D2].windows) == (28, 6)


# ---------------- research-only input ----------------


def candle_rows(days):
    rows = []
    for day in days:
        first = datetime.fromisoformat(f"{pd.Timestamp(day).date()} 09:15")
        for k in range(75):
            start = first + timedelta(minutes=5 * k)
            rows.append({"index_id": "nifty-50", "timeframe": "5m", "bar_start": start,
                         "bar_end": start + timedelta(minutes=5), "open": 100.0 + k, "high": 102.0 + k,
                         "low": 99.0 + k, "close": 101.0 + k, "volume": None, "source": "master_5min.csv",
                         "load_id": 1})
    return rows


def test_loader_is_bounded_at_the_research_end(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    historical_db.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(historical_db.HistoricalCandleLoad.__table__), [
            {"id": 1, "source": "master_5min.csv", "file_sha256": "a" * 64, "row_count": 0, "valid_rows": 0,
             "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0}])
        conn.execute(insert(historical_db.HistoricalCandle.__table__),
                     candle_rows(pd.bdate_range("2024-04-22", "2024-05-03")))
    segments = gate.load_research_segments(engine)
    assert len(segments) == 1
    assert segments[0].index[-1].isoformat() == "2024-04-25T15:25:00+05:30"
    assert (segments[0].index < pd.Timestamp("2024-04-26", tz=IST)).all()


def test_evidence_builder_refuses_holdout_bars() -> None:
    idx = pd.DatetimeIndex([pd.Timestamp("2024-04-25 15:25"), pd.Timestamp("2024-04-26 09:15")]).tz_localize(IST)
    frame = pd.DataFrame({c: [1.0, 1.0] for c in ("Open", "High", "Low", "Close", "Volume")}, index=idx)
    with pytest.raises(RuntimeError, match="holdout data in gate input"):
        gate.build_evidence([frame])
    with pytest.raises(RuntimeError, match="holdout data"):
        gate._assert_research_only([frame.iloc[1:]])
    gate._assert_research_only([frame.iloc[:1]])  # the last research bar is fine


def test_evidence_builder_needs_the_two_protocol_segments() -> None:
    idx = pd.DatetimeIndex([pd.Timestamp("2020-01-06 09:15")]).tz_localize(IST)
    frame = pd.DataFrame({c: [1.0] for c in ("Open", "High", "Low", "Close", "Volume")}, index=idx)
    with pytest.raises(ValueError, match="expected 2 research segments"):
        gate.build_evidence([frame])


def test_gate_module_has_no_holdout_run_path() -> None:
    source = (REPO / "research/phase6/gate.py").read_text()
    # The only candle read is the bounded research loader; nothing reads from the holdout start.
    assert source.count("load_db_segments(") == 1
    assert "start=" not in source.split("def load_research_segments")[1].split("def ")[0]


# ---------------- pre-registered holdout rule (not run) ----------------


def summary(points, directions=None, overnight=0):
    days = pd.bdate_range("2024-05-06", periods=len(points))
    directions = directions or ["CALL"] * len(points)
    trades = [trade(d, p, direction=dr, exit_day=days[min(i + 1, len(days) - 1)] if i < overnight else None)
              for i, (d, p, dr) in enumerate(zip(days, points, directions, strict=True))]
    return summarize(trades)


def test_holdout_decision_needs_both_net_and_expectancy() -> None:
    base = summary([10.0, -5.0, 5.0, -2.0])  # net 8, 4 trades, exp 2
    assert gate.holdout_decision(summary([20.0, -5.0]), base)["passed"]  # net 15, exp 7.5
    assert gate.holdout_decision(summary([3.0] * 5), base)["passed"]  # net 15, exp 3
    # More net points but lower expectancy (many trades): fails.
    low_exp = summary([1.5] * 6)  # net 9 > 8, exp 1.5 < 2
    d = gate.holdout_decision(low_exp, base)
    assert (d["beats_net_points"], d["beats_expectancy"], d["passed"]) == (True, False, False)
    # Higher expectancy but lower net: fails.
    d = gate.holdout_decision(summary([7.0]), base)
    assert (d["beats_net_points"], d["beats_expectancy"], d["passed"]) == (False, True, False)
    # Ties do not beat the baseline.
    d = gate.holdout_decision(summary([10.0, -5.0, 5.0, -2.0]), base)
    assert d == {"beats_net_points": False, "beats_expectancy": False, "passed": False}
    assert not gate.holdout_decision(summary([]), base)["passed"]  # no trades -> no expectancy


def test_holdout_verdict_keeps_canonical_unless_reported() -> None:
    fail = {"passed": False, "beats_net_points": True, "beats_expectancy": False}
    ok = {"passed": True, "beats_net_points": True, "beats_expectancy": True}
    assert gate.holdout_verdict({"rsi len 7": fail, "DI only": fail}) == (
        "No candidate passed: keep the canonical strategy.")
    verdict = gate.holdout_verdict({"rsi len 7": ok, "DI only": fail})
    assert verdict.startswith("Passed: rsi len 7.") and "NOT modified automatically" in verdict


def test_holdout_metrics_include_the_required_fields() -> None:
    s = summary([10.0, -4.0, 6.0], directions=["CALL", "PUT", "PUT"], overnight=1)
    m = gate.holdout_metrics(s)
    assert list(m) == list(gate.HOLDOUT_METRICS)
    assert (m["trades"], m["net_points"], m["expectancy"]) == (3, 12.0, 4.0)
    assert (m["call_trades"], m["call_net"], m["put_trades"], m["put_net"]) == (1, 10.0, 2, 2.0)
    assert m["overnight_trades"] == 1
    assert m["win_rate"] == pytest.approx(200 / 3) and m["profit_factor"] == pytest.approx(4.0)
    assert m["max_drawdown"] == 4.0


# ---------------- report ----------------


def test_preregistration_report_content() -> None:
    items = with_baseline(evidence("rsi len 7", "rsi_length"), evidence("DI only", "adx_other"),
                          evidence("tp 4R", "reward_risk", flags=["isolated peak"]))
    result = gate.evaluate(items)
    meta = {"dataset_source": "historical_candles (source=master_5min.csv)",
            "db_loads": [{"load_id": 1, "file_sha256": "b" * 64}], "grid_variants": 48,
            "grid_hash": gate.FROZEN_GRID_HASH, "protocol_version": protocol.PROTOCOL_VERSION,
            "segments": [{"first_bar": "2015-01-09T09:15", "last_bar": "2015-06-19T15:25", "bars": 8169,
                          "trading_days": 109}], "git_commit": "abc", "git_worktree_dirty": False}
    md = gate.render_preregistration(items, result, meta)
    for code, _ in gate.GATE_CRITERIA:
        assert f"**{code}**" in md
    assert "1. **DI only**" in md and "2. **rsi len 7**" in md  # tie -> by name
    assert gate.FROZEN_GRID_HASH in md and "load_id 1 SHA-256 `" + "b" * 64 in md
    assert "Research cutoff: **2024-04-25**" in md
    assert "thresholds were chosen AFTER seeing the Step C research results" in md
    assert "The holdout was not used" in md
    assert "2024-04-26 through 2025-04-25" in md
    assert "BOTH (1) net points and (2) expectancy" in md
    assert "keep the canonical strategy" in md and "not modified automatically" in md
    assert "- tp 4R: G1" in md
    assert gate.gate_spec_hash() in md


def test_committed_preregistration_matches_the_frozen_gate() -> None:
    md = (REPO / "research/phase6/PREREGISTRATION.md").read_text()
    assert gate.gate_spec_hash() in md and gate.FROZEN_GRID_HASH in md
    assert re.search(r"1\. \*\*rsi len 7\*\*.*\n2\. \*\*DI only\*\*", md)
    assert "8c3f954d8bc22a8a2a1f1d21d9eb8ff3f78a987f575edb13a6f0c5891d2227ce" in md
    assert "load_id 1" in md


def test_nothing_under_src_imports_the_gate() -> None:
    pattern = re.compile(r"^\s*(from|import)\s+research\b", re.MULTILINE)
    assert [p for p in (REPO / "src").rglob("*.py") if pattern.search(p.read_text())] == []


def test_evidence_dataclasses_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        comparison().net_points = 0.0


def test_final_summary_matches_the_frozen_artifacts() -> None:
    md = (REPO / "research/phase6/PHASE6_SUMMARY.md").read_text()
    assert gate.FROZEN_GRID_HASH in md and protocol.grid_hash(1) in md
    assert gate.gate_spec_hash() in md
    assert "8c3f954d8bc22a8a2a1f1d21d9eb8ff3f78a987f575edb13a6f0c5891d2227ce" in md and "load_id 1" in md
    assert "**Final status: the canonical strategy is UNCHANGED.**" in md
    assert "**`rsi len 7` is not adopted.**" in md and "**`DI only` is not adopted.**" in md
    assert all(f"`{c}`" in md for c in gate.PREREGISTERED_CANDIDATES)
    assert "2024-04-26 00:00:00 .. 2025-04-25 23:59:59" in md and "evaluated exactly once" in md
    for step in "ABCDEFGH":
        assert re.search(rf"^\| {step} \|", md, re.MULTILINE), step
