# Phase 6 - Strategy Backtest & Indicator Analysis: findings

> **Final outcome:** see [`PHASE6_SUMMARY.md`](PHASE6_SUMMARY.md) - the historical research, the
> pre-registered holdout and the paper-rule validation are complete, and the canonical strategy is
> unchanged. Sections below marked PENDING DATA predate that run.

Status: **Steps 2-4 complete (code audit, verified by execution). The correctness and consistency
fixes from that audit are implemented - see "Phase 6 fixes" below. Steps 1, 5 and 6 are still
blocked on market data** - this environment's network policy denies Yahoo Finance
(`fc.yahoo.com`, `query1.finance.yahoo.com`; HTTP 403 at the egress proxy), the only data source
the production Backtest uses. No real-market performance number in this document exists yet, and
none has been estimated or carried over from earlier reports.

Base commit: `fdf7d98` (main). Research code lives in `research/phase6/` and is never imported by
`src/`. Production changes are limited to the correctness fixes listed below; no parameter,
indicator or entry/exit rule was changed.

## Phase 6 fixes (correctness & consistency only)

| Audit item | Before | After | Where |
|---|---|---|---|
| M5 ATR warm-up | An entry during the ATR warm-up got a NaN SL/TP and could never exit, freezing the run | The entry is refused on that bar (the setup edge is skipped, not deferred). No effect with the production defaults: RSI 14 and ATR 14 both first valid at bar 13, and the null benchmark is numerically identical | `fno_signals/strategy.py` `run()`; Pine mirror `riskValid` |
| M6 Invalid OHLC | A NaN Close crashed `run()`; a NaN High/Low made ATR NaN for the rest of the series | Bars with a missing or non-finite O/H/L/C, any price <= 0, or High < Low are dropped (never filled or interpolated). The run equals a run on data without that bar; indicators resume at the next valid bar; the count is logged | `fno_signals/strategy.py` `invalid_bar_mask` / `drop_invalid_bars`; also applied in `auto_trader.run_cycle` before any price is read |
| C2 Stale fills | A blocked entry was filled hours later at its original bar price | Freshness rule: an event is actionable for 2 bar lengths after its bar closes (10 min on 5m with the 300 s scheduler). A stale entry is expired (marked processed, never filled). A stale exit with an open position is a late exit, closed at the current price. A backlog of stale events is expired in one cycle | `auto_trader.run_cycle`, `SIGNAL_FRESHNESS_BARS` |
| C3 Exit price | Paper exits filled at the exit bar's close | A fresh EXIT_SL/EXIT_TARGET fills at its `exit_level` (the SL/target itself), exactly as `backtest.pair_trades` does, so paper P&L equals backtest points for the same round trip. Square-off still fills at the latest close. The recorded order price is the actual fill | `auto_trader.run_cycle`, `OrderResult.fill_price`, `web_server._run_and_persist_cycle` |
| C4 Orphan exit | An exit for an entry the paper account never filled could block every later event for that index | Marked processed and skipped ("No open paper position for this exit"). With position sync (below) this can no longer arise; the branch stays as a safety net | `auto_trader.run_cycle` |
| C4 Position divergence | Paper replayed the strategy over the whole window each cycle, so after a blocked/expired entry, a 15:20 square-off or a restart, the replay sat in a **phantom position** and suppressed genuine new setup edges until that phantom trade exited. Across 40 seeded random walks, 100 genuine entries were suppressed this way. A restored open position could also be doubled by a replayed entry | **Position sync:** once the account has processed anything, the strategy is re-run starting just after `account.last_event_at`, seeded with the account's real position (flat, or side/entry/SL/target). Indicators, setups and the edge trigger still use the whole window, so the signals are exactly the canonical ones. SL/target are stored on the account at fill; after a restart they are re-derived from the entry bar with the canonical rule (`risk_distances`). An expired entry re-seeds flat from its bar in the same cycle | `fno_signals/strategy.py` (`OpenPosition`, `start_after`/`initial_position`, `risk_distances`), `auto_trader._account_synced_events`, `SimulatedAccount.stop_loss/target` |

---

## 1. Current baseline - NOT YET REPRODUCED (data blocked)

The research harness reproduces the production Backtest path exactly (asserted on every run and
in `test_phase6.py`: identical events to `fno_signals.strategy.run()`, identical metrics payload to
`web_server._run_backtest_segment()`), so once data is available one command produces every
figure requested in Step 1:

```
PYTHONPATH=src:. python -m research.phase6.run --interval 5m          # fetch + cache + report
PYTHONPATH=src:. python -m research.phase6.run --interval 15m
PYTHONPATH=src:. python -m research.phase6.run --interval 1h          # 730 days - longest history
```

Report per index: trade count, win rate, PF, expectancy, net points, max drawdown, max consecutive
losses, average winner/loser, CALL vs PUT, time-of-day, regime, train/validation/OOS (both the
production split method and a continuous-run method), walk-forward, and data quality.

To unblock, either allow `fc.yahoo.com`, `query1.finance.yahoo.com` and `query2.finance.yahoo.com`
in the environment's network settings, or drop CSV snapshots at
`data/phase6_<nifty-50|bank-nifty|sensex>_<interval>.csv` (columns
`Datetime,Open,High,Low,Close,Volume`, tz-aware timestamps) - the runner uses a cached CSV when
present.

## 2. Exact indicator configuration (canonical strategy)

Source: `src/fno_signals/config.py` (defaults), `src/fno_signals/indicators.py` (math),
`src/fno_signals/strategy.py` (rules). Identical for all three indices except `strike_step`
(NIFTY 50, BANK NIFTY/SENSEX 100), which only affects the option label, never the signal.

| Indicator | Implementation | Parameters | Role | Type |
|---|---|---|---|---|
| EMA fast / slow | `close.ewm(span=n, adjust=False)` - seeded from the first close | 9 / 21 on Close | `ema_fast > ema_slow` for CALL, `<` for PUT | Filter (part of setup) |
| RSI | Wilder: RMA of gains / losses; `down==0 -> 100`, `up==0 -> 0` | length 14; CALL > 55, PUT < 45 | Momentum confirmation | Filter (part of setup) |
| Supertrend | Pine `ta.supertrend`: hl2 +/- mult x ATR(len), ratcheting final bands, direction starts at +1 | ATR length 10, multiplier 3.0 | direction < 0 (up) for CALL, > 0 (down) for PUT | Filter (part of setup) |
| ATR (risk) | Wilder RMA of True Range | length 14 (separate from Supertrend's 10) | SL = 1.5 x ATR, TP = 4.5 x ATR (1:3), `min_sl_points` 0 = off | Risk component |
| VWAP | session-anchored, hlc3 x Volume, resets on each date | `use_vwap=False` in production | would require close > / < VWAP | Filter - **disabled** |
| ADX / DI+ / DI- | **Not present anywhere in `src/`** (grep-verified) | - | - | - |

**Trigger:** there is no separate trigger indicator. Setup = EMA AND RSI AND Supertrend (AND VWAP
if on); an entry fires only on the bar where the combined setup turns true (`setup[i] and not
setup[i-1]`), with no open position, inside the 09:15-15:40 IST session. The entry fills at that
bar's close. A setup that is still true after an exit never re-fires.

**Exit:** only SL or TP, checked from the bar after entry on bar High/Low; if one bar touches both,
SL is assumed (conservative). Positions are never force-closed - they carry overnight until SL/TP.

## 3. Backtest methodology findings

Verified by reading the code and, where marked **[executed]**, by running it.

| # | Area | Finding | Severity |
|---|---|---|---|
| M1 | Look-ahead | None found. All indicators are causal (EWM, RMA, rolling, Supertrend uses `close[i-1]`); SL/TP evaluated from the next bar; regime SMA is trailing. | OK |
| M2 | Fill timing | Entry fills at the signal bar's own close - zero latency. Paper polls every 5 min after the fact, so real fills are later. | Low (optimistic) |
| M3 | SL/TP sequencing | Same-bar SL+TP resolved as SL (conservative). Entry bar is never checked for SL/TP (correct for a close fill). | OK |
| M4 | Gap-through fills | An exit always fills at the exact SL/TP level even if the bar opens beyond it. With overnight carry this is systematically optimistic on SL. The research `gap_aware` variant measures the size. | Medium |
| M5 | **Warm-up / NaN risk [executed] - FIXED** | `run()` opens a position even when ATR is still NaN, giving NaN SL/TP that can **never exit** - the backtest (and paper replay) freezes for the rest of the window. Production defaults avoid it only by coincidence (RSI 14 and ATR 14 both first valid at bar 13). Any change with RSI < 14 or ATR > 14 triggers it (confirmed: RSI 7 -> 1 event on a 60-day series). | **High (latent)** |
| M6 | **NaN bars [executed] - FIXED** | One NaN Close makes `run()` raise `ValueError` (`round_to_strike`), so `/api/backtest/run` returns an error and every paper cycle fails (logged by the scheduler) until the bar leaves the 5-day window. One NaN High/Low makes ATR NaN for the rest of the series (224 events fell to 2 in a test). No NaN check exists anywhere in the pipeline. | **High** |
| M7 | Warm-up bias | Signals can fire from bar 13, before EMA21 converges. The production split method re-runs the strategy on each slice, so validation/OOS each restart warm-up and lose any trade spanning a boundary. The research report shows both methods. | Low-Medium |
| M8 | Unclosed trade | A position still open at the end of the data is silently dropped from all metrics. | Low |
| M9 | Session boundaries | Indicators run continuously across the overnight gap; trades carry overnight. Paper Auto Trade forces a 15:20 square-off and a 15:00 entry cutoff - **not modelled by the backtest** (see C1). | Medium |
| M10 | Timezone | yfinance NSE intraday is tz-aware IST; the session filter converts correctly and assumes IST for naive data. Bars are labelled by start time, so the "09:15" bar entry is really at 09:20. OK. | OK |
| M11 | Duplicate signals | The backtest's edge trigger and single-position state machine prevent duplicates. The paper dedup has a separate flaw (C2). | OK (backtest) |
| M12 | Data gaps | No check for missing bars, duplicate timestamps, bad OHLC or partial last bar. The research `quality_report()` adds these checks. | Medium |
| M13 | Train/val/OOS | Chronological 60/20/20 by bar count, no shuffling. But no parameter was ever *selected* on train - defaults come from the Pine script - so "train" trains nothing. On 5m (period `60d` ~ 40 trading days) validation and OOS are only about 8 trading days each. | Medium (sample size) |
| M14 | Reproducibility | Every Backtest run fetches the *current* rolling yfinance window, so a reported number can never be reproduced later. The research harness caches CSV snapshots. | Medium |
| M15 | P&L unit | Points on the underlying, not option premium (documented in `backtest.py`). Theta, IV, delta and spreads are ignored, so a positive points edge may not survive in options. | Inherent limitation |
| M16 | Pine parity (minor) | `fno_signals.indicators.rma` seeds from `nanmean` of the first `length` values; Pine's `ta.rma` waits for `length` non-na values. For RSI this seeds one bar earlier than TradingView (bar 13 vs 14). Negligible after warm-up. | Low |
| M17 | Partial last bar (not verifiable here) | yfinance intraday normally includes the still-forming bar during market hours, so both the backtest's last bar and every paper cycle can act on an unclosed candle. Must be verified once data access exists. | Medium (unverified) |

## 4. Strategy consistency (Backtest vs Paper Auto Trade)

Both call the same `fno_signals.strategy.run()` with the same `strategy_config_for()` config, so
**signal generation is identical**. Execution is not:

| # | Difference | Effect | Evidence |
|---|---|---|---|
| C1 | Paper enforces 15:00 entry cutoff, 15:20 square-off, 5-min cooldown, max 10 trades/day, max 1 open position **across all three indices**, 3-consecutive-loss halt; the backtest enforces none | Paper takes different and far fewer trades; no overnight holds | `RiskLimits` defaults; research `paper session` variant measures the cutoff and square-off part |
| C2 | **Stale fills [executed] - FIXED**: a blocked event stays "unprocessed" and is filled later at its *original* bar price | An entry blocked at 10:35 (auto trading off) was filled 3.5 h later at 132 while the market was at 218, then exited on the historical exit - fictitious paper P&L | `auto_trader.run_cycle` picks the oldest event newer than `last_event_at`; `last_event_at` only advances on a fill |
| C3 | **Exit fill price [executed] - FIXED**: paper exits at the exit bar's close; the backtest exits at the SL/TP level | Same trade: backtest +18.0 pts, paper +16.0 | `run_cycle` passes `event.underlying_price` (bar close) to `place_event` |
| C4 | Strategy replay vs paper state - **FIXED (position sync)** | The strategy's internal position comes from replaying the window, not from the paper account, so after a blocked entry the strategy is "in a trade" the paper account doesn't hold; its later EXIT is refused, and new entries are suppressed until the phantom trade exits | Follows from C2 / `run()` design |
| C5 | 3-consecutive-loss halt vs a 1:3, ~25-30% win-rate strategy | On no-edge data the baseline trips the halt about 25 times in 136 trades (about once every 5 trades). The halt needs a manual reset, so paper would sit halted most of the time. | Null benchmark, "median 3-loss halts" |
| C6 | Data window | Paper uses `period=5d`, Backtest `60d`. Indicators are recursive, so values in the first part of the paper window differ slightly (converged within ~100 bars). | `market_pulse.TIMEFRAMES` vs `BACKTEST_TIMEFRAMES` |
| C7 | Dashboard "Strategy" panel | `/api/strategy/signal` (rendered by `app.js` `loadStrategySignal`) still evaluates the **legacy** `algoedge.strategy_engine` (RSI+EMA, % SL/TP), not the canonical strategy, so the user can see BUY/SELL that neither Backtest nor Auto Trade would act on | `web_server.py:243-290` |
| C8 | Option-chain panel | Shows "BUY CALL/PUT" whenever the setup *state* is true; the canonical strategy only enters on the setup *edge* | `web_server.py:553` |

## 5. Backtest vs paper: strategy rules vs execution/risk controls

**Canonical position/session semantics (unchanged, now documented in `SessionConfig`):** the
strategy's 09:15-15:40 window gates **new entries only**. `run()` has no session exit, so a
position **may be held overnight and across days** and exits only on its own SL or target. The
Backtest measures exactly that. Paper Auto Trade never holds overnight because of its own 15:20
square-off, which is an execution/risk control, not a strategy rule. Since the position sync, the
strategy is re-seeded flat after a square-off, so paper no longer carries a phantom overnight
position either. The research "paper session" variant models the same thing (flat after
square-off).

**3-consecutive-loss halt:** a paper Auto Trade **risk control**, not an indicator or filter. It
never changes which signals the strategy generates, only whether paper may act on them. It is not
tuned or removed. Its effect is reported separately from strategy performance: the research
reports' "times paper's 3-consecutive-loss halt would trip" line and the null benchmark's
"median 3-loss halts" column never feed into any strategy metric (win rate, PF, expectancy,
drawdown).

| Control | Where | Class | Backtest models it? | Decision |
|---|---|---|---|---|
| Setup (EMA/RSI/Supertrend), edge trigger, ATR SL/TP, 09:15-15:40 entry window, overnight carry | `fno_signals` | **Strategy rule** | Yes | Unchanged |
| Entry cutoff 15:00 | `RiskLimits.entry_cutoff` | Execution/risk control (no new positions near the close) | No | Keep in paper only; measurable with the research "paper session" variant |
| Forced square-off 15:20 | `RiskLimits.square_off_time`, `run_cycle` | Execution/risk control (intraday option product, no overnight risk) | No | Keep, unchanged. This is the largest divergence: the strategy is designed to carry overnight, paper never does. **Owner decision** whether the strategy should become intraday |
| Cooldown 5 min | `RiskLimits.cooldown_minutes` | Risk control (no blind re-entry) | No | Keep. A blocked entry now retries only within the freshness window, then expires |
| Daily trade cap 10 | `RiskLimits.max_trades_per_day` | Risk control (entries only since the pre-Phase-6 fix) | No | Keep |
| Daily loss limit | `RiskLimits.daily_loss_limit` | Risk control (entries only) | No | Keep |
| Max 1 open position across all indices | `RiskLimits.max_open_positions` | Portfolio control | No (each index is backtested alone) | Keep. Paper trades a portfolio of three signals, not three independent backtests |
| 3-consecutive-loss halt | `RiskLimits.max_consecutive_losses` | Risk control (manual reset) | No | Keep, unchanged (a parameter decision, not a correctness bug). With ~25-30% winners at 1:3 it trips about once every 5 trades on no-edge data. **Owner decision** |

None of these needed a behavior change for correctness. The correctness problems were in how a
blocked signal was later executed (C2-C4), which is now fixed. The distinction is documented in the
`RiskLimits` and `SessionConfig` docstrings.

## 6. Dashboard Strategy panel (verified, not replaced)

- `/api/strategy/signal/{index_id}`, rendered by `web/app.js` `loadStrategySignal()` as the
  dashboard's "Strategy" BUY/SELL, evaluates the **legacy** `algoedge.strategy_engine` (RSI+EMA,
  percent SL/TP). Its only side effect is a `db.record_signal(source="algoedge.strategy_engine")`
  log row. Nothing in Auto Trade, Backtest, the order paths or risk reads it (grep-verified), so it
  has no trading effect.
- **Determination:** it should display the canonical `fno_signals.strategy` state. A panel labelled
  "Strategy" showing signals neither Backtest nor Auto Trade would act on is misleading.
- **Not changed in this pass:** replacing it changes the endpoint's response contract, its query
  parameters (RSI/EMA/% knobs with no canonical equivalent) and the panel's rendering in
  `web/app.js`. That is a UI/API change to approve separately. Proposed change: return the
  canonical last-bar state (bull/bear setup, whether this bar is an entry edge, RSI, EMA fast/slow,
  Supertrend direction) using the same `run()` call the option-chain endpoint already makes, and
  retire `strategy_engine`.
- Related: the option-chain panel shows "BUY CALL/PUT" whenever the setup *state* is true, while
  the strategy only enters on the setup *edge*. Also display-only.

## 7-9. Experiments, ADX/DI, VWAP, robustness, train/val/OOS - PENDING DATA

The full grid (`experiments.py`) is ready: 48 variants, all starting from the production config and
changing one family at a time, plus a few justified combinations:

- EMA 5/13, 8/21, 12/26, 9/30, 20/50
- RSI length 7/10/21; RSI thresholds 50/50 to 60/40
- Supertrend multiplier 2.0-4.0 and length 7-20
- SL 1.0-2.5 x ATR at 1:3; reward:risk 1R-4R; ATR length 7-21
- VWAP ON
- ADX > 15/20/25/30 with DI alignment, ADX length 10/14/20, ADX without DI, DI only, and ADX in "setup" mode vs "gate" mode
- Realism variants: paper session rules, paper close-fill, gap-aware fills

Walk-forward: 20 train days -> 5 test days, rolling. The pick is the best *train expectancy* with at
least 8 trades (never win rate), evaluated on unseen test days against the baseline on the same
days. Robustness flags: isolated peaks (the nearest non-baseline neighbours are no better than
baseline), positive overall but negative in a split, fewer than 10 trades in a split, and fewer
than 30 trades overall.

**VWAP (known without data):** yfinance index tickers report Volume = 0, so VWAP is NaN for every
bar and `use_vwap=True` blocks every signal (the synthetic zero-volume check produced 0 trades).
VWAP cannot be evaluated on spot index data at all; it needs a volume-bearing proxy (index futures).

**ADX/DI (known without data):** not in the canonical strategy. The research implementation matches
Pine's `ta.dmi` and is verified against an independent reference implementation.

### Null benchmark - what chance alone produces (executed)

`results/null_benchmark_5m.md`: every variant run on 40 independent random walks (60 days of 5m
bars; no edge exists by construction):

- Baseline PF ranges **0.75-1.37** (p10-p90) with expectancy -7.1 to +9.0 points per trade. **A
  single 60-day 5m backtest with PF around 1.3 is statistically indistinguishable from no edge.**
- Picking the best of the 48 variants gives a median PF of **1.34** (p90 1.91) purely by chance.
  Any real-data "improvement" must clearly beat this, hold in the walk-forward and across all three
  indices, and not be an isolated peak before it counts as evidence.
- Filters that cut trade count (ADX > 30: ~24 trades) widen the chance range (PF p90 1.94), so a
  high PF from a heavily filtered variant is weak evidence.

## 10. Potential strategy improvements (hypotheses to test, not recommendations)

1. ~~Guard entries on finite ATR; drop NaN bars (M5, M6)~~ - done.
2. ~~Fix paper's stale-event replay and SL/TP-level fills (C2-C4)~~ - done.
3. Decide whether the strategy is intraday (like paper) or carry-forward (like the backtest) (C1 / section 5).
4. Revisit `max_consecutive_losses=3` for a low-win-rate 1:3 strategy (C5) - owner decision.
5. Only after data: ADX/DI as a gate, and wider SL/TP. These can only be judged against the null benchmark and walk-forward.

## 11. Risks / weaknesses that remain

- No real-data baseline yet (network policy).
- Small samples: 5m is limited to about 40 trading days by yfinance; 1h gives 2 years but is a different timeframe from the one paper trades.
- Points-based P&L on spot, not option premium (M15).
- Paper still differs from the backtest by design (the session and risk controls in section 5), so
  paper results will not match backtest results unless those controls are modelled or the strategy
  is made intraday.
- Open-position SL/target are not persisted. After a restart they are re-derived from the entry
  bar, which is exact while that bar is inside the fetched 5-day window (always true given the
  daily square-off). If it isn't, the position has no strategy exit and is closed by the 15:20
  square-off.
- Gap-through fills at the SL level remain optimistic in both backtest and paper (M4).
- The legacy dashboard Strategy panel (section 6).

## 12. Recommendation

**Keep the canonical strategy's rules and parameters unchanged** pending real-data results (the
Phase 6 fixes changed no rule or parameter). No experiment has run on real
data, and the null benchmark shows that a single 60-day backtest cannot separate a small edge from
noise. With the correctness fixes done, the next steps are the two owner decisions in section 5
(intraday vs carry-forward, and the consecutive-loss halt) and the Strategy panel change in
section 6. Then re-run this harness on real data (ideally 1h/730d plus 5m snapshots accumulated
over time) before considering any parameter change.
