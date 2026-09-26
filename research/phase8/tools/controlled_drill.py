"""Controlled Phase 8 drills: real engine evidence for the risk behaviours a
short live campaign may never exercise (HALT, EXIT_RULES, SQUARE_OFF).

Research only and paper only. Every fill is made by the unchanged engine:
the dashboard's own cycle path (algoedge.web_server._run_and_persist_cycle,
the same function the scheduler and "Run cycle now" call) runs
auto_trader.run_cycle(..., now=<controlled IST time>) against a
SimulatedAccount and a RiskManager, and persists through
db.record_paper_cycle() - so every row is written by the engine itself.
Nothing here calls a broker, an order endpoint or /api/manual-trading.

What is controlled (and therefore NOT live evidence):
- the clock: each cycle runs at a chosen IST time (run_cycle's `now`), and
  the database's created_at is pinned to that time (protocol assumption A3);
- the market data: deterministic synthetic 5-minute windows, shaped so the
  canonical strategy (unchanged parameters) produces the entry, the SL hit
  or the quiet afternoon each scenario needs;
- the option contract: resolved locally (no instrument-master call);
- operator actions: the real endpoint functions (kill switch, disable).

Isolation: each scenario writes to its own new SQLite file. The drill
refuses to run if algoedge.web_server is already connected to a non-SQLite
database, and imports it with ALGOEDGE_DB_SERVER and the Groww credential
variables blanked, so that import can neither connect to a configured
campaign database (e.g. AlgoEdge_Phase8) nor validate/mint a Groww token.

Each scenario directory holds: drill.db (the engine's rows), drill_*.jsonl
(every step: controlled time, the engine's own response, risk state, and the
scenario's checks), db/extract_*/ (extract.py of drill.db) and
reconcile/reconcile_*.jsonl (reconcile.py of that extract).

Note: with max_open_positions=1 the consecutive-loss halt can only trip on
an exit, which leaves no position open, so "an exit while halted" cannot be
produced by the engine. EXIT_RULES is exercised with the other two entry
restrictions R3 names: the kill switch and auto trading disabled.

    PYTHONPATH=src:. python -m research.phase8.tools.controlled_drill --out research/phase8/evidence/controlled_drills
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from algoedge import auto_trader
from algoedge.models import Base
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy
from research.phase8.tools import extract, reconcile
from research.phase8.tools.bars import INDEX_CHOICE
from research.phase8.tools.common import EvidenceFile, new_run_id, stamp, utc_now

SCHEMA = "phase8.controlled_drill.v1"
DEFAULT_DAY = date(2026, 9, 23)  # a Wednesday
FIVE = timedelta(minutes=5)
AFTER_CLOSE = timedelta(seconds=30)  # cycles run 30 s after the bar they act on has closed
COMMIT_DELAY = timedelta(milliseconds=250)  # database created_at = cycle time + this (a commit follows `now`)
SCENARIOS = ("HALT", "EXIT_RULES", "SQUARE_OFF")
TICKER_TO_INDEX = {INDEX_MAP[choice].ticker: index_id for index_id, choice in INDEX_CHOICE.items()}
MODELS = list(extract.MODELS.values())


# ---------------------------------------------------------------- windows


def rising(day: date, start: time, bars: int = 40) -> pd.DataFrame:
    index = pd.date_range(datetime.combine(day, start), periods=bars, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(bars)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * bars}, index=index)


def first_entry(frame: pd.DataFrame, index_id: str):
    index_config = INDEX_MAP[INDEX_CHOICE[index_id]]
    events = run_strategy(frame, strategy_config_for(index_config), underlying_label=index_config.name)[1]
    return next(e for e in events if e.kind in ("ENTRY_CALL", "ENTRY_PUT"))


@dataclass(frozen=True)
class Trade:
    """A synthetic window and the canonical events it produces."""

    index_id: str
    frame: pd.DataFrame
    entry: Any  # the canonical entry TradeEvent
    exit_bar: pd.Timestamp | None  # the bar whose low reaches the stop, if any

    @property
    def entry_cycle(self) -> datetime:
        return (self.entry.timestamp + FIVE + AFTER_CLOSE).to_pydatetime()

    @property
    def exit_cycle(self) -> datetime:
        return (self.exit_bar + FIVE + AFTER_CLOSE).to_pydatetime()


def stop_out_trade(index_id: str, day: date, start: time) -> Trade:
    """Rising bars up to the canonical entry, then one bar trading 5 points
    below the entry's stop: the strategy's own EXIT_SL at the stop level."""
    base = rising(day, start)
    entry = first_entry(base, index_id)
    close = float(base.loc[entry.timestamp, "Close"])
    crash_at = entry.timestamp + FIVE
    crash = pd.DataFrame({"Open": [close], "High": [close + 1.0], "Low": [entry.stop_loss - 5.0],
                          "Close": [entry.stop_loss - 3.0], "Volume": [0.0]}, index=pd.DatetimeIndex([crash_at]))
    return Trade(index_id, pd.concat([base.loc[:entry.timestamp], crash]), entry, crash_at)


def quiet_afternoon_trade(index_id: str, day: date, start: time) -> Trade:
    """Rising bars up to the canonical entry, then flat bars (inside SL and
    target) until the session's last bar: nothing but square-off closes it."""
    base = rising(day, start)
    entry = first_entry(base, index_id)
    close = float(base.loc[entry.timestamp, "Close"])
    flat_index = pd.date_range(entry.timestamp + FIVE, pd.Timestamp(datetime.combine(day, time(15, 25)), tz="Asia/Kolkata"),
                               freq="5min")
    flat = pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close, "Volume": 0.0},
                        index=flat_index)
    return Trade(index_id, pd.concat([base.loc[:entry.timestamp], flat]), entry, None)


# ---------------------------------------------------------------- isolated engine


# Blanked while algoedge.web_server is imported: its import connects to the configured database
# (db.init_db) and builds a TokenService that validates or mints a Groww token when credentials
# are configured. Empty process variables also override any .env file.
ISOLATED_IMPORT_ENV = ("ALGOEDGE_DB_SERVER", "ALGOEDGE_GROWW_ACCESS_TOKEN", "ALGOEDGE_GROWW_API_KEY",
                       "ALGOEDGE_GROWW_API_SECRET")


def import_web_server_isolated():
    """Import algoedge.web_server with no database server and no Groww
    credentials (restoring every original value afterwards, also on failure),
    and refuse an already-imported, already-connected one."""
    if "algoedge.web_server" not in sys.modules:
        saved = {name: os.environ.get(name) for name in ISOLATED_IMPORT_ENV}
        os.environ.update(dict.fromkeys(ISOLATED_IMPORT_ENV, ""))
        try:
            import algoedge.web_server  # noqa: F401
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    from algoedge import db, web_server

    if db._engine is not None and db._engine.url.get_backend_name() != "sqlite":
        raise RuntimeError(f"refusing to run: algoedge.db is connected to {db._engine.url.get_backend_name()}; "
                           "run the drill in a process that has not connected to a campaign database")
    return web_server


def resolve_contract(index_id: str, trade_event) -> OptionContract:
    index_config = INDEX_MAP[INDEX_CHOICE[index_id]]
    return OptionContract(trading_symbol=f"{index_config.groww_underlying}DRILL{trade_event.strike}{trade_event.right}",
                          underlying=index_config.groww_underlying, right=trade_event.right,
                          strike=trade_event.strike, expiry=trade_event.timestamp.date() + timedelta(days=7))


@dataclass
class Engine:
    web_server: Any
    risk_manager: RiskManager
    accounts: dict[str, SimulatedAccount]
    clock: dict[str, datetime]
    windows: dict[str, pd.DataFrame]
    db_path: Path
    evidence: EvidenceFile
    scenario: str
    run_id: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def at(self, moment: datetime) -> None:
        self.clock["now"] = moment

    def cycle(self, step: str, index_id: str, moment: datetime) -> dict[str, Any]:
        """One real paper cycle, exactly as the scheduler/"Run cycle now" run it."""
        self.at(moment)
        response = self.web_server._run_and_persist_cycle(index_id, "5m", 1)
        self.record(step, index_id=index_id, response=response)
        return response

    def operator(self, step: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        response = action()
        self.record(step, response=response)
        return response

    def record(self, step: str, **payload: Any) -> None:
        self.evidence.append({
            "schema": SCHEMA, "run_id": self.run_id, "scenario": self.scenario, "kind": "step", "step": step,
            "controlled_now": stamp(self.clock["now"]), "risk_state": dataclasses.asdict(self.risk_manager.state),
            **payload,
        })

    def check(self, name: str, observed: Any, expected: Any) -> bool:
        passed = observed == expected
        self.checks.append({"check": name, "expected": expected, "observed": observed,
                            "result": "PASS" if passed else "FAIL"})
        return passed


@contextmanager
def isolated_engine(scenario_dir: Path, scenario: str, run_id: str,
                    windows: dict[str, pd.DataFrame]) -> Iterator[Engine]:
    web_server = import_web_server_isolated()
    from algoedge import db

    scenario_dir.mkdir(parents=True, exist_ok=False)
    db_path = scenario_dir / "drill.db"
    sql_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(sql_engine)
    clock: dict[str, datetime] = {"now": datetime.combine(DEFAULT_DAY, time(9, 0), tzinfo=IST)}

    def pin_created_at(_mapper, _connection, target) -> None:
        target.created_at = (clock["now"] + COMMIT_DELAY).astimezone(IST).replace(tzinfo=None)

    def fetch(ticker: str, **_kwargs) -> pd.DataFrame:
        frame = windows.get(TICKER_TO_INDEX[ticker])
        if frame is None:
            return pd.DataFrame()  # an index this scenario does not drive has no data (never faked)
        return frame.loc[frame.index <= clock["now"]]  # bars that have started by now; the last may be forming

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    accounts = {index_id: SimulatedAccount() for index_id in web_server.INDEX_DEFINITIONS}
    for model in MODELS:
        event.listen(model, "before_insert", pin_created_at)
    try:
        with ExitStack() as stack, EvidenceFile(scenario_dir, "drill", run_id, utc_now()) as evidence:
            for target, name, value in (
                (db, "_session_factory", sessionmaker(bind=sql_engine)),
                (web_server, "risk_manager", risk_manager),
                (web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()}),
                (web_server, "_cycle_locks", {i: threading.Lock() for i in accounts}),
                (web_server, "_entry_guard", threading.Lock()),
                (web_server, "_resolve_auto_trade_contract", resolve_contract),
                # The dashboard's cycle path, with run_cycle given the controlled time explicitly.
                (web_server, "run_cycle", lambda *a, **k: auto_trader.run_cycle(*a, now=clock["now"], **k)),
                (auto_trader, "fetch_underlying_data", fetch),
            ):
                stack.enter_context(patch.object(target, name, value))
            yield Engine(web_server, risk_manager, accounts, clock, windows, db_path, evidence, scenario, run_id)
    finally:
        for model in MODELS:
            event.remove(model, "before_insert", pin_created_at)
        sql_engine.dispose()


# ---------------------------------------------------------------- scenarios


def scenario_halt(engine: Engine, day: date) -> None:
    """Three consecutive stop-outs on three indices trip the halt on the third."""
    from algoedge import db

    trades = [stop_out_trade("nifty-50", day, time(9, 15)), stop_out_trade("sensex", day, time(9, 35)),
              stop_out_trade("bank-nifty", day, time(9, 55))]
    for trade in trades:
        engine.windows[trade.index_id] = trade.frame
    for number, trade in enumerate(trades, start=1):
        entry = engine.cycle(f"entry {number}", trade.index_id, trade.entry_cycle)
        engine.check(f"entry {number} filled", ((entry["signal"] or {}).get("kind"), (entry["order"] or {}).get("status")),
                     (trade.entry.kind, "PLACED"))
        exit_ = engine.cycle(f"stop-out {number}", trade.index_id, trade.exit_cycle)
        engine.check(f"exit {number} is the strategy's EXIT_SL, filled",
                     ((exit_["signal"] or {}).get("kind"), (exit_["order"] or {}).get("status")), ("EXIT_SL", "PLACED"))
        engine.check(f"exit {number} is a loss", (exit_["order"] or {}).get("realizedPnl", 0.0) < 0, True)
        state = engine.risk_manager.state
        engine.check(f"after loss {number}: (consecutive_losses, halt)",
                     (state.consecutive_losses, state.consecutive_loss_halt), (number, number >= 3))
    persisted = db.load_latest_risk_state(scope="paper") or {}
    engine.check("persisted risk state after the third loss: (consecutive_losses, halt)",
                 (persisted.get("consecutive_losses"), persisted.get("consecutive_loss_halt")), (3, True))
    categories = [a["category"] for a in db.list_alert_events()]
    engine.check("TRADING_HALTED alert persisted", "TRADING_HALTED" in categories, True)


def scenario_exit_rules(engine: Engine, day: date) -> None:
    """An open position's stop-out fills with the kill switch engaged and auto
    trading disabled; a new entry at the same time is blocked."""
    from algoedge import db

    held, other = stop_out_trade("nifty-50", day, time(9, 15)), stop_out_trade("sensex", day, time(9, 35))
    engine.windows.update({held.index_id: held.frame, other.index_id: other.frame})
    entry = engine.cycle("entry", held.index_id, held.entry_cycle)
    engine.check("entry filled", (entry["order"] or {}).get("status"), "PLACED")
    engine.at(held.entry_cycle + timedelta(minutes=1))
    engine.operator("operator: kill switch", lambda: engine.web_server.auto_trading_kill_switch(
        reason="Phase 8 controlled drill"))
    engine.operator("operator: disable auto trading", engine.web_server.auto_trading_disable)
    state = engine.risk_manager.state
    engine.check("restrictions active: (kill_switch, auto_trading_enabled)",
                 (state.kill_switch, state.auto_trading_enabled), (True, False))
    exit_ = engine.cycle("stop-out while restricted", held.index_id, held.exit_cycle)
    engine.check("risk-reducing EXIT_SL filled while restricted",
                 ((exit_["signal"] or {}).get("kind"), (exit_["order"] or {}).get("status")), ("EXIT_SL", "PLACED"))
    engine.check("exit filled at the stop level", (exit_["order"] or {}).get("detail", "").endswith(
        f"@ {held.entry.stop_loss:.2f}"), True)
    engine.check("position closed", engine.accounts[held.index_id].quantity, 0)
    persisted = db.load_latest_risk_state(scope="paper") or {}
    engine.check("persisted risk row after the exit: (kill_switch, auto_trading_enabled)",
                 (persisted.get("kill_switch"), persisted.get("auto_trading_enabled")), (True, False))
    account = db.load_latest_auto_trade_account_state(held.index_id) or {}
    engine.check("persisted account snapshot after the exit: quantity", account.get("quantity"), 0)
    blocked = engine.cycle("new entry while restricted", other.index_id, other.entry_cycle)
    engine.check("new entry blocked by the kill switch",
                 (blocked["order"], blocked["risk"]["reason"].startswith("Emergency kill switch is engaged")),
                 (None, True))
    decisions = db.list_paper_decision_events(index_id=other.index_id)
    engine.check("blocked entry audited", [d["decision"] for d in decisions], ["BLOCKED"])


def scenario_square_off(engine: Engine, day: date) -> None:
    """An open position with no SL/target hit is force-closed at 15:21."""
    from algoedge import db

    trade = quiet_afternoon_trade("nifty-50", day, time(12, 0))
    engine.windows[trade.index_id] = trade.frame
    entry = engine.cycle("entry", trade.index_id, trade.entry_cycle)
    engine.check("entry filled", (entry["order"] or {}).get("status"), "PLACED")
    before = engine.cycle("15:10 cycle (before square-off time)", trade.index_id,
                          datetime.combine(day, time(15, 10), tzinfo=IST))
    engine.check("still holding at 15:10", (before["order"], engine.accounts[trade.index_id].quantity), (None, 1))
    square = engine.cycle("15:21 cycle", trade.index_id, datetime.combine(day, time(15, 21), tzinfo=IST))
    engine.check("engine generated SQUARE_OFF, filled",
                 ((square["signal"] or {}).get("kind"), (square["order"] or {}).get("status")),
                 ("SQUARE_OFF", "PLACED"))
    engine.check("position closed, square_off_date recorded in memory",
                 (engine.accounts[trade.index_id].quantity, engine.accounts[trade.index_id].square_off_date),
                 (0, day.isoformat()))
    account = db.load_latest_auto_trade_account_state(trade.index_id) or {}
    engine.check("persisted account snapshot: (quantity, square_off_date)",
                 (account.get("quantity"), account.get("square_off_date")), (0, day.isoformat()))


SCENARIO_FUNCTIONS = {"HALT": scenario_halt, "EXIT_RULES": scenario_exit_rules, "SQUARE_OFF": scenario_square_off}
TARGET_CHECK = {"HALT": "HALT", "EXIT_RULES": "EXIT_RULES", "SQUARE_OFF": "SQUARE_OFF"}


def run_scenario(out_dir: Path, scenario: str, *, day: date = DEFAULT_DAY, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    scenario_dir = out_dir / scenario.lower()
    with isolated_engine(scenario_dir, scenario, run_id, {}) as engine:
        engine.at(datetime.combine(day, time(9, 0), tzinfo=IST))
        try:
            SCENARIO_FUNCTIONS[scenario](engine, day)
            error = None
        except Exception as failure:  # noqa: BLE001 - an engine surprise is a FAIL, recorded as evidence
            error = f"{type(failure).__name__}: {failure}"
            engine.checks.append({"check": "scenario ran to completion", "expected": None, "observed": error,
                                  "result": "FAIL"})
        engine_result = "PASS" if engine.checks and all(c["result"] == "PASS" for c in engine.checks) else "FAIL"
        db_path, evidence = engine.db_path, engine.evidence
        # Reconcile the engine's own rows with the campaign tooling (extract.py + reconcile.py).
        url = f"sqlite:///{db_path}"
        sql = extract.make_engine(url)
        try:
            extract_dir = extract.extract(sql, url, datetime.combine(day, time(9, 0)),
                                          datetime.combine(day, time(16, 0)), scenario_dir / "db", run_id=run_id)
        finally:
            sql.dispose()
        report = reconcile.reconcile(extract_dir)
        with EvidenceFile(scenario_dir / "reconcile", "reconcile", run_id, utc_now()) as reconcile_evidence:
            reconcile_evidence.append(report)
        target = TARGET_CHECK[scenario]
        summary = {
            "schema": SCHEMA, "run_id": run_id, "scenario": scenario, "kind": "summary",
            "controlled": "clock, market data, contract resolution and operator actions; fills by the real engine",
            "engine_checks": engine.checks, "engine_result": engine_result, "error": error,
            "reconcile_target": {"check": target, **report["checks"][target]},
            "reconcile_checks": {name: result["status"] for name, result in report["checks"].items()},
            "files": {"db": db_path.name, "extract": extract_dir.name},
        }
        evidence.append(summary)
    return {**summary, "scenario_dir": str(scenario_dir), "evidence_file": str(evidence.path)}


def run(out_dir: Path, scenarios: tuple[str, ...] = SCENARIOS, *, day: date = DEFAULT_DAY) -> dict[str, Any]:
    run_id = new_run_id()
    run_dir = out_dir / f"controlled_{utc_now().astimezone(IST):%Y%m%dT%H%M%S%z}_{run_id}"
    return {scenario: run_scenario(run_dir, scenario, day=day, run_id=run_id) for scenario in scenarios}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    args = parser.parse_args(argv)
    results = run(args.out, tuple(args.scenarios))
    for scenario, result in results.items():
        target = result["reconcile_target"]
        print(f"{scenario:11} engine={result['engine_result']:4}  reconcile {target['check']}={target['status']} "
              f"(evaluated={target['evaluated']})  {result['scenario_dir']}")
    return 0 if all(r["engine_result"] == "PASS" and r["reconcile_target"]["status"] == "PASS"
                    for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
