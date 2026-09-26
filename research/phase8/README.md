# Phase 8 — paper validation evidence tooling

Phase 8.0 provides the read-only tooling that the Phase 8.1 Extended Paper
Validation Protocol (`protocol/PHASE8_1_PROTOCOL.md`) needs. **It does not
change the paper engine.** Nothing under `src/algoedge/`, `src/fno_signals/`
or `research/phase6/` is modified, and there is no schema change or migration.
Everything is **paper/simulation only**: no tool calls a broker order API, and
none can change a risk setting.

| Tool (`PYTHONPATH=src:. python -m research.phase8.tools.<name>`) | Reads | Writes | Protocol |
|---|---|---|---|
| `preflight` | SQL Server (SELECT/inspection); dashboard GET `/status`, GET `/option-context/{index}`; `git diff` of `src/` | a report | 3.3, 3.2 A2 |
| `launch` | — | a new server log file | 3.3 (INFO logs) |
| `collector` | GET `/api/auto-trading/status`, GET `/api/alerts` | `status_*.jsonl` | 3.8, UI1–UI3, R1 |
| `bars` | yfinance via the engine's own `fetch_underlying_data` | `bars_*.jsonl` | D1–D4, A1, A4 |
| `extract` | SQL Server, SELECT only, bound parameters | `extract_*/` | 3.7 |
| `reconcile` | an extract directory plus optional bars/status evidence | `reconcile_*.jsonl` | R1–R5, C2, P1, A1, D1–D4, S1, UI1–UI3 |
| `drill` | POST `/api/auto-trading/run/{index}?interval=5m&quantity=1` **only** | `drill_*.jsonl` | drills C and D |

## Read-only guarantees

- **HTTP.** The collector and preflight issue GET only, to fixed paths. The
  drill can only POST the existing manual paper-cycle endpoint, for a paper
  index, with quantity fixed at 1. Base URLs must be http(s) with no
  credentials, and loopback unless explicitly allowed.
- **Database.** Only SQLAlchemy Core `SELECT`s with bound parameters are sent.
  Each statement is checked before execution and again at the cursor, and the
  connection is always rolled back. The tools never call `algoedge.db.init_db()`
  (which creates databases and tables) or `create_all()`.
- **Groww.** Preflight never touches Groww directly. It asks the running
  dashboard's existing read-only `option-context` endpoint, which performs the
  same `get_all_instruments` lookup that paper entries rely on. No tool holds
  credentials.
- **Evidence.** Files are exclusive-create and append-only, and each record is
  self-hashed. Missing or invalid data is recorded, never substituted (see
  `evidence/README.md`).

## Session workflow (Phase 8.1, once approved)

```
PYTHONPATH=src:. python -m research.phase8.tools.preflight --out E/preflight           # must print READY
PYTHONPATH=src:. python -m research.phase8.tools.launch --log-dir E/logs               # the dashboard, INFO logs
PYTHONPATH=src:. python -m research.phase8.tools.collector --out E/status              # 60 s samples
PYTHONPATH=src:. python -m research.phase8.tools.bars --out E/bars                     # 60 s bar captures
# after the close:
PYTHONPATH=src:. python -m research.phase8.tools.extract --start <day>T09:00 --end <day>T16:00 --out E/db
PYTHONPATH=src:. python -m research.phase8.tools.reconcile --db E/db/extract_... --bars E/bars --status E/status --out E/reconcile
```

`E` is `research/phase8/evidence/<campaign_id>`. The database connection comes
from the dashboard's own `ALGOEDGE_DB_*` settings, or from `--db-url-env NAME`.

## Reconciliation rules

A PLACED or FAILED paper order, and every paper decision other than
`ORDER_FAILED`, anchors one transaction group. The group's members are the rows
of the same index created within **1 s** of the anchor; `risk_state_events` has
no index column, so its rows match on time alone.

- **Missing member:** FAIL, P0 (a partial transaction).
- **Two candidates, or a row two groups could both claim:** UNRECONCILED. The
  tool never guesses and never repairs source data; the rows involved are listed.
- **Every check** ends PASS, FAIL, UNRECONCILED or UNVERIFIABLE (not enough
  evidence). A check with nothing to evaluate is UNVERIFIABLE, never PASS.
- **Any P0 FAIL** sets `stop_campaign`.

Risk-rule checks, with the protocol criteria they serve (each report entry
lists its `criteria` and `observations`):

| Check | What it verifies |
|---|---|
| `ENTRY_RULES` (R5, R1) | Every PLACED entry: session open and 15:00 cutoff, cooldown after the last exit's engine time, the 10-entry daily cap (baseline included), and no entry while disabled, kill-switched or halted (read from the entry's own risk row) |
| `HALT` (R5) | The loss streak and halt, replayed against every risk and decision row: trips at 3, a win keeps the halt, only `CONSECUTIVE_LOSS_HALT_RESET` clears it, and it survives restarts |
| `EXIT_RULES` (R3) | No SL/target exit decision is BLOCKED by kill switch, disabled or halt; exits filled while a restriction is recorded are counted |
| `SQUARE_OFF` (R4) | Forced close between 15:20 and 15:30 with `square_off_date` recorded; later than the first scheduler tick is UNRECONCILED; missed square-offs; next-session recovery first and in session |
| `DECISION_STATE` (R3, R5) | Each BLOCKED reason agrees with its recorded state (kill switch, halt, disabled, cap, loss limit, cutoff/hours time, exact cooldown boundary) |
| `D1_EXIT_LEVEL` (D1) | An SL/target exit equals the level rebuilt from its entry's canonical replay on captured bars |

**Engine clock.** A cycle takes its engine time when it starts and commits
its rows at the end. Exits persist that time (`last_exit_at`); entries don't.
So entry time rules assume a cycle finishes within `ENGINE_CLOCK_TOLERANCE`
(120 s). A rule is FAIL only if violated for every engine time in
`[created_at − 120 s, created_at]`, PASS only if it holds for all of them,
and UNVERIFIABLE otherwise. Entries at the next scheduler tick after an exit
usually land inside that band for the cooldown.

**Exercise.** A behaviour that never occurred in range (halt trip, exit under a
restriction, forced close, exact exit level without bars) is UNVERIFIABLE,
never PASS. Criterion verdicts come from reconciling the whole campaign range.

`tests/test_end_to_end.py` runs the real engine cycle path into a real database,
extracts it and reconciles it, which proves the rule on rows the engine actually
writes.

## Known limitations (documented, not solved)

1. **Idle cycles cannot be proven from the database.** Routine cycles ("No
   actionable signal", "Signal already processed", one-off "No valid market
   data" or fetch failures) write no rows, and the engine logs none of them.
   Scheduler liveness is shown only indirectly: every canonical event in the
   captured bars must map to a fill or an audited decision (S1), plus INFO logs
   from `launch`, status samples and bar captures. The baseline is deliberately
   not changed to fix this.
2. **Engine time and capture time differ.** The engine's own fetch is not
   observable. Captures run on their own 60 s schedule, so a bar the engine
   used can differ from the captured one if yfinance revised it. Revisions are
   recorded, and a price not found in the evidence is UNRECONCILED, not FAIL.
3. **Grouping resolution is 1 s.** Two cycles of one index, or an entry and
   another index's exit, within 1 s of each other are UNRECONCILED by design.
4. **SL/target levels are not persisted.** An exact level check
   (`D1_EXIT_LEVEL`) needs bar evidence to rebuild them from the entry's
   canonical replay (the engine's own restart formula). Without bars, or for a
   late exit, it is UNVERIFIABLE. The bar-range check under `BARS` is
   supporting evidence only. Square-off prices are checked against captured
   closes (else UNVERIFIABLE).
5. **R-6 (database outage) is not reconciled automatically.** A fill that
   happened in memory but was never committed is lost at restart (existing B6
   behaviour). The reviewer applies the protocol's R-6 expectation to the
   status/database divergence inside that drill window.
6. **Runtime-only coverage.** max_open_positions > 1, the daily loss limit,
   malformed or missing candles, and a truly simultaneous two-index entry are
   covered by the frozen test suites, not by live evidence.
7. **P&L is in underlying index points**, not option premium; there are no
   costs.
8. **"First cycle" and the cooldown boundary.** Idle cycles leave no rows, so
   a square-off is proven only against the 15:20–15:30 window and the first
   scheduler tick. An entry right after a cooldown ends is usually
   UNVERIFIABLE, because its engine time isn't persisted.

## Tests

`python -m pytest research/phase8` runs mocked HTTP, synthetic bar frames,
throwaway SQLite databases and the real engine cycle. None of them needs SQL
Server, Groww or the network.
