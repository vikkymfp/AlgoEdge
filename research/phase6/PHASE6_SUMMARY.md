# Phase 6 - final research summary (NIFTY 50, 5-minute)

**Final status: the canonical strategy is UNCHANGED.** Neither pre-registered
candidate is adopted. No production code, `strategy.py`, `config.py` or Pine
change was made or is proposed by this phase. No combined variant was created.
The final holdout was evaluated exactly once (Step E) and will not be rerun.

All figures are NIFTY 50 index points, quantity 1, zero cost (the current
`CostModel` defaults every rate to 0), before any option-premium effects. They
are research measurements, not trading recommendations.

## Provenance

| Item | Value |
|---|---|
| Dataset | `master_5min.csv` -> `historical_candles` (source `master_5min.csv`), **load_id 1** |
| Dataset SHA-256 | `8c3f954d8bc22a8a2a1f1d21d9eb8ff3f78a987f575edb13a6f0c5891d2227ce` |
| Grid (48 variants) hash | `23f6785610ea340d3c2e3943555d4876242eec2b1ce244179f6d8a07f8663afd` |
| Pre-registration (gate spec) hash | `8ecfe0cf9d11adecbc494b9ca665c62925b1b9373fb540a772de1792a7f76334` (`gate_version phase6-gate-1`) |
| Protocol version | `phase6-protocol-1` |
| Research period | segment 1: 2015-01-09 .. 2015-06-19 (8,169 bars, 109 days); segment 2: 2015-11-16 .. 2024-04-25 (156,095 bars, 2,083 days); cutoff 2024-04-25 23:59:59 IST |
| Final holdout | 2024-04-26 00:00:00 .. 2025-04-25 23:59:59 IST (18,313 bars, 245 days) |
| Canonical config | EMA 9/21, RSI 14 at 55/45, Supertrend 10/3.0, ATR 14, SL 1.5xATR, TP 4.5xATR, VWAP off, entries 09:15-15:40 IST |

The 2015-06-22 .. 2015-11-13 window is excluded from the data, so the two
segments are always run and reported separately; no metric combines them.
All evidence was read from a read-only SQLite replica of `historical_candles`
built by the same importer from the same file (its load record carries the SHA
above); the SQL Server instance was not reachable from the build container.

## Steps

| Step | What | Outcome | Commit |
|---|---|---|---|
| A | Leakage-safe walk-forward: rolling/anchored windows, warm-up, train trades must close inside the train block | Legacy output reproduced exactly with `train_exit_cutoff=False` | `39eca48` |
| B | Frozen research protocol: holdout boundary, Design 1 / Design 2, grid hash, 19 non-standard sessions (tagged, never removed), report metadata | `--protocol` runner; holdout bars cannot be read | `87f933f` |
| C | Research-period run (only up to 2024-04-25) | Baseline + 48-variant grid per segment; walk-forward on segment 2 | outputs only |
| D | Pre-registered candidate gate G1-G7 | Candidates: `rsi len 7`, `DI only` (`PREREGISTRATION.md`) | `c7e165e` |
| E | Final holdout, run once | Both candidates passed the pre-registered rule | outputs only |
| F | Technical review of the three strategies | Canonical kept pending implementation review | review only |
| G | Paper-session rules + gap-aware fills on research data | Neither candidate's advantage survives paper rules | outputs only |
| H | This summary | Canonical strategy unchanged | - |

Walk-forward designs (Step B): Design 1 rolling 250 train / 63 test / step 63;
Design 2 anchored 500 / 250 / 250; both with 10 warm-up days, train exit
cutoff, and at least 30 closed train trades. Segment 2 gives 28 and 6 windows;
segment 1 (109 days) is too short for either and is baseline/grid only.

## Research results (Step C, through 2024-04-25)

| | Canonical | rsi len 7 | DI only |
|---|---|---|---|
| Seg 2 trades | 5,277 | 5,768 | 5,175 |
| Seg 2 expectancy | 2.44 | 2.69 | 2.62 |
| Seg 2 net points | 12,897.8 | 15,501.6 | 13,537.0 |
| Seg 2 max drawdown | 1,288.6 | 1,451.4 | 1,171.3 |
| Seg 2 CALL n / net | 2,629 / 10,920.4 | 2,865 / 11,950.3 | 2,524 / 11,028.3 |
| Seg 2 PUT n / net | 2,648 / 1,977.4 | 2,903 / 3,551.3 | 2,651 / 2,508.7 |
| Seg 2 overnight holds | 1,131 | 1,284 | 1,107 |
| Seg 2 split expectancy (train / valid / OOS) | 1.65 / 5.13 / 2.16 | 1.93 / 5.62 / 1.92 | 1.75 / 5.41 / 2.49 |
| Seg 1 trades / expectancy / net | 270 / 3.99 / 1,076.2 | 294 / 5.66 / 1,663.1 | 266 / 3.51 / 933.0 |
| Design 1 test days: expectancy / net / windows won | 2.81 / 12,634.9 / - | 3.11 / 15,286.7 / 16 of 28 | 2.99 / 13,164.7 / 17 of 28 |
| Design 2 test days: expectancy / net / windows won | 3.20 / 12,273.9 / - | 3.50 / 14,788.9 / 4 of 6 | 3.40 / 12,767.1 / 5 of 6 |
| Robustness flags | none | none | none |

The adaptive walk-forward selection itself underperformed the canonical
baseline on the same test days (Design 1: 10,832.6 vs 12,634.9 net points;
Design 2: 10,185.1 vs 12,273.9), so no "re-selecting" strategy was proposed.
Neither candidate was ever picked by the walk-forward in any window.

## Pre-registration (Step D)

Gate G1-G7 (thresholds chosen **after** seeing the Step C research results,
never the holdout): no segment-2 robustness flag; net points > baseline and
expectancy >= baseline on the same test days in both designs; a strict
majority of test windows won in each design; max drawdown <= 1.25x baseline;
segment-1 expectancy > 0; rank by the weaker design's net delta, one per
parameter family, at most two. Five variants were eligible; the two selected
were `rsi len 7` and `DI only`. Holdout rule: a candidate passes only if it
beats the canonical baseline on BOTH net points and expectancy; passing is
reported, never applied automatically. Full detail: `PREREGISTRATION.md`.

## Final holdout (Step E, evaluated once)

Only the three strategies ran, on holdout bars only (the implementation
self-warms its indicators, so no pre-holdout bar was read); the query was
bounded at 2025-04-25 23:59:59 and the dataset ends 2025-04-25 15:25.

| | Canonical | rsi len 7 | DI only |
|---|---|---|---|
| Trades | 545 | 576 | 542 |
| Win % / PF | 25.87 / 1.10 | 30.38 / 1.26 | 26.57 / 1.12 |
| Expectancy | 2.86 | 7.92 | 3.51 |
| Net points | 1,556.9 | 4,563.1 | 1,900.3 |
| Max drawdown | 2,177.5 | 1,157.9 | 2,024.4 |
| CALL n / net | 279 / 1,562.5 | 288 / 3,618.5 | 270 / 1,714.8 |
| PUT n / net | 266 / -5.6 | 288 / 944.6 | 272 / 185.6 |
| Overnight holds | 147 | 169 | 146 |
| Pre-registered rule | - | passed | passed |

The holdout used the research execution model (positions held overnight,
level fills), not paper-session rules.

## Paper-session validation (Step G, research data only)

Existing research implementation: `paper_session_variant()` (15:00 entry
cutoff and 15:20 square-off from `RiskLimits`), `exit_fill="gap_aware"`,
level fills otherwise, zero cost. Segment 2:

| Config | Strategy | Trades | Expectancy | Net pts | Max DD | CALL n / net | PUT n / net | Overnight |
|---|---|---|---|---|---|---|---|---|
| As researched | Canonical | 5,277 | 2.44 | 12,897.8 | 1,288.6 | 2,629 / 10,920.4 | 2,648 / 1,977.4 | 1,131 |
| | rsi len 7 | 5,768 | 2.69 | 15,501.6 | 1,451.4 | 2,865 / 11,950.3 | 2,903 / 3,551.3 | 1,284 |
| | DI only | 5,175 | 2.62 | 13,537.0 | 1,171.3 | 2,524 / 11,028.3 | 2,651 / 2,508.7 | 1,107 |
| Paper session, level fill | Canonical | 5,316 | 1.24 | 6,569.6 | 2,308.7 | 2,701 / 2,849.0 | 2,615 / 3,720.5 | 2 |
| | rsi len 7 | 5,847 | 0.70 | 4,064.4 | 2,946.2 | 2,953 / 1,361.8 | 2,894 / 2,702.6 | 2 |
| | DI only | 5,208 | 1.27 | 6,591.0 | 1,933.8 | 2,597 / 2,953.7 | 2,611 / 3,637.2 | 2 |
| Paper session, gap-aware fill | Canonical | 5,316 | 1.28 | 6,786.0 | 2,308.7 | 2,701 / 3,070.3 | 2,615 / 3,715.7 | 2 |
| | rsi len 7 | 5,847 | 0.73 | 4,272.7 | 2,946.0 | 2,953 / 1,572.9 | 2,894 / 2,699.8 | 2 |
| | DI only | 5,208 | 1.31 | 6,807.3 | 1,933.8 | 2,597 / 3,175.0 | 2,611 / 3,632.4 | 2 |

Walk-forward test days under paper session, level fill (net points vs
canonical, windows won): `rsi len 7` -2,581.8 (9 of 28) and -2,641.7 (1 of 6);
`DI only` -208.0 (12 of 28) and -210.4 (3 of 6). Segment 1 under paper
session, level fill: canonical 281 / 1.87 / 525.6; `rsi len 7` 301 / 2.34 /
704.1; `DI only` 275 / 1.37 / 375.9 (trades / expectancy / net). The 2
overnight holds are sessions missing their 15:20 bar, closed at the next open.

## Conclusion

- **`rsi len 7` is not adopted.** It passed the pre-registered holdout rule,
  but its advantage disappears under paper-session rules: segment-2
  expectancy 0.70 vs 1.24, net 4,064.4 vs 6,569.6, max drawdown 2,946.2 vs
  2,308.7, and it trails the canonical strategy on both walk-forward designs.
- **`DI only` is not adopted.** Under paper-session rules its advantage is
  negligible (+21.4 net points, expectancy 1.27 vs 1.24; lower drawdown
  1,933.8 vs 2,308.7) and it is behind the canonical strategy on the
  walk-forward test days (-208.0 and -210.4 net points).
- **The final holdout remains closed.** It was evaluated once in Step E and is
  not rerun for any purpose, including paper-rule evaluation.
- **No combined variant** (RSI 7 + DI) is created or evaluated.
- **No production or Pine change.** The canonical strategy is unchanged.

## Known limitations

- Results are index points at quantity 1 with zero cost; option premium,
  delta/theta, brokerage, taxes and slippage are not modelled.
- Evidence was computed on a read-only SQLite replica of `historical_candles`
  (same importer, same file SHA), not re-run against the SQL Server instance.
- The gate thresholds were chosen after the research results had been seen;
  selecting from 45 variants means in-sample edges are expected to shrink.
- The holdout is one year (about 545 trades) and was run without paper-session
  rules, so there is no holdout evidence under those rules.
- Paper risk controls not modelled by the simulator: the daily trade cap
  (would have applied on 43 / 84 / 35 segment-2 days for canonical /
  `rsi len 7` / `DI only`), the 3-consecutive-loss halt (needs a manual reset;
  would trip 790 / 875 / 773 times), signal freshness and cycle latency. The
  daily loss limit and the 5-minute cooldown never applied.
- Positions still open at the end of a period are not counted as trades.
- The VWAP variant produces 0 trades (the dataset has no volume).
- One index (NIFTY 50) and one timeframe (5m). Known missing days:
  2015-01-16, 2025-03-20, 2025-03-21. The 19 non-standard sessions are kept
  and tagged; the with/without-sessions sensitivity run was not performed.
- Research, holdout and Step G outputs were written outside the repository
  (`research/phase6/results/` is not git-ignored); this document records the
  numbers they produced.
