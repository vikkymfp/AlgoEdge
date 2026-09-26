# AlgoEdge Phase 8.1 — Extended Paper Validation Protocol

| | |
|---|---|
| Protocol version | **8.1-v1 (DRAFT, not signed off)** |
| Baseline (engine under test) | `06eba8cb6cedfbdc41585f362dd6c07424aed120` (`main`, "Merge pull request #6"). `src/` must be byte-identical to this commit; preflight check G enforces this. |
| Evidence tooling | Phase 8.0, `research/phase8/tools/` (see `../README.md`) |
| Scope | **Paper/simulation only.** Validation of the frozen engine, not strategy research. |

Once signed off, the criteria below cannot change for this version. Any
amendment is a new version. Results collected under a version are reported
against that version.

## 1. Objective

Determine whether the complete frozen paper-trading engine behaves consistently
and reliably over an extended paper period. That covers:

- signal generation and completed-candle entry handling;
- risk controls, simulated fills and exits;
- persistence and restart restore;
- concurrency;
- square-off and missed-square-off recovery;
- audit logging.

Phase 8.1 does **not** prove profitability, optimise parameters, select a
strategy, or establish live-trading readiness. P&L is reconciled, never judged.

## 2. Frozen configuration (never changed during the campaign)

**Strategy** (`fno_signals.config.strategy_config_for`):

| Setting | Value |
|---|---|
| EMA | 9 / 21 |
| RSI | 14, thresholds 55 / 45 |
| Supertrend | 10 / 3.0 |
| ATR | 14 |
| Stop loss | 1.5 × ATR |
| Target | 4.5 × ATR |
| VWAP | off |
| Strategy session | 09:15–15:40 IST |
| Overnight behaviour | canonical, unchanged |

**Paper controls** (`RiskLimits`; engine controls, not strategy rules):

| Control | Value |
|---|---|
| Trading hours | 09:15–15:30 |
| Entry cutoff | 15:00 |
| Square-off | 15:20 |
| Cooldown | 5 min |
| Daily entry cap | 10 new entries/day |
| Daily loss limit | 5,000 underlying points |
| Max open positions | 1 |
| Consecutive-loss halt | 3 |
| Max quantity | 50 |

## 3. Environment

**A. Existing, already-validated behaviour**

| Item | Value |
|---|---|
| Data | yfinance, `fetch_underlying_data`, period 5d, 5m bars |
| Tickers | `^NSEI` (nifty-50), `^BSESN` (sensex), `^NSEBANK` (bank-nifty); all three run in the scheduler loop |
| Timezone | IST |
| Scheduler | Tick every 300 s, indices run one after another; manual "Run cycle" also available |
| Quantity | 1 |
| Database | SQL Server (`mssql+pyodbc`), a **dedicated, new, empty** database for the campaign |
| Starting state | Flat on all indices |
| Candle rule | Flat account evaluates completed bars only; exits, square-off and recovery use the latest bar |
| Freshness | 2 bars (10 min) after bar close |
| Stale positions (B4) | A prior-day position closes at today's price on the first in-session cycle with a bar from today; entries are held back (SQUARE_OFF_PENDING) until then |
| Contract lookup | Every entry needs the Groww read-only instrument master; if it's unavailable the entry is BLOCKED |
| Engine safeguards | B5 per-index locks plus the global entry guard; B6 one transaction per cycle; B7 audit and failure alerts; B9 `entries_today` |
| P&L unit | Underlying points × quantity |

**B. Assumptions still needing validation (measured, not assumed)**

- **A1:** each completed bar arrives within the freshness window. Measured
  from the bar evidence, `newly_completed.delay_seconds`.
- **A2:** the Groww instrument master stays available. Checked by preflight E
  at every session start.
- **A3:** the SQL Server clock and the host clock are both IST and synchronised.
- **A4:** yfinance does not revise completed bars the engine acted on. Revisions
  are recorded in the bar evidence.
- **A5:** the host stays up apart from planned drills.
- **A6:** non-trading days produce no fills.

## 4. Operating rules

1. Before each session:
   - preflight must print **READY**, and the report is archived;
   - start the dashboard only through `tools.launch` (INFO logs);
   - start `tools.collector` and `tools.bars`, both at 60 s.
2. The operator never uses the kill switch, disable or manual runs, except
   where a drill says so. Every operator action is written to
   `operator/operator_log.md` with its IST time.
3. A consecutive-loss halt is reset only once, before 09:15 on the next
   trading day, and the reset is logged.
4. After each session, run `tools.extract` for the session and then
   `tools.reconcile` with bars and status. Any `stop_campaign` means §9 applies.
5. Pre-campaign gate on the baseline, archived:
   - `pytest tests`: 877 passed;
   - `pytest research/phase6`: 293 passed;
   - `pytest research/phase8`: all passed.

## 5. Validation window (selection rule, fixed before any result)

- **Start:** the first NSE session after sign-off and after the gate passes.
- **Length:** at least 20 consecutive NSE sessions, at most 30. Extend past 20
  only until the coverage quotas are met.
- **Classes:** computed from NIFTY daily OHLC using data before that day only:

  | Class | Rule |
  |---|---|
  | Volatile | range ≥ 1.5 × median of the prior 20 sessions |
  | Quiet | range ≤ 0.6 × that median |
  | Gap | \|open − previous close\| ≥ 0.5 % |

- **Quotas:** at least 3 volatile, 3 quiet and 2 gap sessions, and all drills
  completed.
- Unmet quotas at session 30 make the affected criteria **INCONCLUSIVE**.
- A special or short session is observed if one occurs; no entries are
  expected outside 09:15–15:30. Non-trading days are expected to have no fills.

## 6. Pass/fail criteria

Each criterion ends PASS, FAIL or INCONCLUSIVE. The overall result is PASS only
if there is zero P0, no open P1, every criterion is PASS, and the minimum
activity below is met:

- at least 15 entry fills;
- at least 1 each of EXIT_SL, EXIT_TARGET and SQUARE_OFF;
- at least 1 BLOCKED("Max open positions reached");
- at least 1 EXPIRED;
- all drills completed.

Otherwise the result is INCONCLUSIVE.

| ID | Criterion | Evidence / tool check |
|---|---|---|
| D1 | Every fill price is a real bar value: an entry at its bar's close, an exit at its SL/target level, a square-off/late exit/recovery at the latest close | reconcile `BARS` |
| D2 | No entry from a still-forming bar (`event_at + 5 min ≤ fill time`) | `BARS` FORMING_BAR_ENTRY |
| D3 | No entry from a bar older than the freshness window; stale events are EXPIRED | `S1_REPLAY`, `AUDIT` |
| D4 | No fill from an invalid-OHLC bar | `BARS` INVALID_BAR_FILL |
| S1 | Every paper entry equals the canonical replay on captured bars, with the account-synchronised high-water mark | `S1_REPLAY` |
| S2 | Strategy configuration unchanged | preflight G, `src/` frozen |
| S3 | Every fill and audited decision has its signal row in the same transaction | `P1_GROUPING` |
| R1 | Global open positions never exceed 1 | `R1_MAX_OPEN`; status `MAX_OPEN_OBSERVED` |
| R2 | `entries_today` rises only on PLACED entries; blocked, failed, expired, exit and square-off events never raise it; it resets each IST day (lazily, as designed) | `R2_COUNTERS` |
| R3 | Exits, square-off and recovery are never blocked by the kill switch, disabled state or halt | drills, `AUDIT` |
| R4 | Square-off at the first cycle between 15:20 and 15:30; a missed one is recovered the next session before any entry | drills R-4a/R-4b, `TRANSITIONS` |
| R5 | No entry after 15:00, inside the cooldown or past the cap; the halt trips at 3 losses and holds | `AUDIT`, `R2_COUNTERS` |
| C1 | R1 holds during the concurrency drills | `R1_MAX_OPEN` on drill sessions |
| C2 | No strategy event is filled twice | `C2_DUPLICATES` |
| C3 | No deadlock; every drill request returns; any 409 is explained | drill evidence |
| C4 | No unexplained cycle exceptions | server log, alerts |
| P1 | Every PLACED fill has its signal, order, risk and snapshot rows together | `P1_GROUPING` |
| P2 | After each restart the restored state equals the last committed rows (R-6 excepted per §7) | status vs `extract` baseline |
| P3 | A database failure leaves no partial group; the failure is surfaced | `P1_GROUPING`, log, alerts |
| A1 | Each classified non-fill decision has its audit row (re-audit after a restart is expected) | `AUDIT`, `P1_GROUPING` |
| A2 | Audit reasons are the engine's own text; alerts carry no raw error text | `AUDIT`, alert rows |
| A3 | 3 consecutive failures per index and kind give exactly one alert | alert rows, drill R-7 |
| UI1 | Dashboard quantity/side equal the persisted state (poll-lag tolerant) | `DASHBOARD` |
| UI2 | `entriesToday` equals the persisted counter (lazy day reset allowed) | `DASHBOARD` |
| UI3 | Dashboard limits equal the frozen `RiskLimits` | `DASHBOARD` LIMITS_CHANGED, preflight F |

## 7. Drills (first eligible opportunity on or after the listed session)

| # | Session ≥ | Action | Expected |
|---|---|---|---|
| R-1 | 2 | Restart while flat | Flat; counters equal the last risk row; no fill |
| R-2 | 3 | Restart within 60 s of an entry | Position, contract and `last_event_at` restored; no re-fill (C2) |
| R-3 | 4 | Restart after holding ≥ 15 min | Restored; exit later as normal |
| R-4a | 6 | Stop 15:12, start 15:18 | Square-off at the first cycle ≥ 15:20 |
| R-4b | 8 | Stop 15:12, start next day before 09:15 | SQUARE_OFF_PENDING, then recovery at today's price, no entry first |
| R-5 | 5 | Restart within 5 min of a BLOCKED entry | Not marked processed; may fill later only if fresh and allowed; re-audited if blocked again |
| R-6 | 10 | Stop SQL Server for about 15 min while flat, then restart the app | Persistence FAILED and logged; no partial groups; after restart, state = last committed rows (an in-memory-only fill is lost and may re-fill once if still fresh; that is one committed fill, not C2); alerts are lost while the database is down |
| R-7 | 12 | Block Yahoo for 20 min, restore, later restart | One PAPER_CYCLE_FAILURE per index after 3 failures (while the database is up); no fills; counts reset at restart |
| C | 14 | `tools.drill overlap` (offset 2 s) for 1 session | C1–C4, R2, A1 |
| D | 15 | `tools.drill repeat` for 1 session | C1–C4, R2, A1 |

Data-failure cases are **verified against existing behaviour only**:

| Case | How it's covered |
|---|---|
| No data / fetch failure | R-7 and natural occurrences |
| Stale data | Weekends and holidays |
| Forming bar | Every cycle (D2) |
| Delayed bar | Natural occurrences (A1) |
| Malformed or missing bar | Existing tests re-run at the gate, plus natural occurrences checked against D4 |

Concurrency cases not coverable live are covered by the frozen suites
`test_paper_entry_concurrency` and `test_auto_trade_cycle_lock`:

- a simultaneous two-index entry;
- max_open_positions > 1, which isn't configurable at runtime.

## 8. Severity

| Level | Meaning | Examples |
|---|---|---|
| P0 | Safety or correctness blocker | More than 1 open position (always P0, never a statistic); duplicate fill; fill from a forming, invalid or fabricated bar; blocked exit or square-off; wrong restore; partial transaction; any live order; tampered evidence |
| P1 | Major reliability failure | Missed square-off not recovered; deadlock; counter drift; missing or orphan audit row; wrong P&L; changed limits; unexplained repeated exceptions; missing or storming alerts |
| P2 | Non-blocking defect | Ambiguous evidence (UNRECONCILED) resolved by review; dashboard lag beyond one poll; frequent EXPIRED rows from A1 latency; justified 409s |
| P3 | Cosmetic or observability | Labels, log noise, missing diagnostic-only data |

## 9. Stop conditions (halt the campaign, investigate, never retry to hide)

- any P0 (the reconciler's `stop_campaign`), including a max_open_positions
  violation or a duplicate fill;
- an unexplained position mismatch between the database, memory and the replay;
- corrupted persistence or a partial group;
- incorrect restored risk or account state;
- acceptance of fabricated or invalid data;
- a concurrency failure that repeats and can't be explained;
- any UNRECONCILED item not resolved within 1 trading day.

A stopped campaign resumes only as a new protocol version and baseline, and the
stopped run is reported as FAIL.

## 10. Evidence

Layout and retention are in `../evidence/README.md`.

**Correctness evidence:**
- database extracts (six tables plus baseline);
- bar captures;
- status samples;
- the INFO server log;
- drill responses;
- the operator log;
- preflight reports;
- reconciliation reports.

**Diagnostic evidence:**
- bar latency;
- instrument-master outages;
- cycle duration;
- 409 counts;
- clock drift;
- yfinance revisions.

**Accepted limitation (Phase 8.0 decision).** Idle cycles that write no row
cannot be proven from database evidence. Liveness is shown by S1 (every
canonical event is handled), the INFO log, status samples and bar captures.
The baseline is not changed to address this.

## 11. Report format

Sections A–N:

| | Section |
|---|---|
| A | Environment |
| B | Validation window |
| C | Sessions by class |
| D | Data integrity |
| E | Signal integrity |
| F | Risk controls |
| G | Concurrency |
| H | Persistence and restart |
| I | Data failures |
| J | Audit |
| K | Dashboard reconciliation |
| L | Failures by severity |
| M | Known limitations |
| N | Each criterion PASS / FAIL / INCONCLUSIVE against **this version** |

## 12. Out of scope (not solved by Phase 8.1)

- real broker orders and exits;
- option-premium P&L, brokerage, taxes and slippage;
- live option quotes;
- external monitoring;
- deployment, backup and database disaster recovery;
- a holiday calendar;
- the pre-existing shared daily-counter race (an exit's `record_trade` runs
  outside the entry guard);
- live-readiness validation.
