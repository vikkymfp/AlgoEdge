"""Read-only Phase 8.1 preflight: is the environment the campaign needs
actually available? Never fakes success - every check must PASS for READY.

Checks:
  A sql_connectivity      the configured SQL Server answers `SELECT 1`
  B required_tables       the six evidence tables exist
  C required_columns      every column the tooling reads exists
  D read_only_query       a parameterized SELECT on each table succeeds
  E groww_instrument_master  the running dashboard resolves an ATM CE and PE
                          contract for every paper index through its existing
                          read-only GET /api/auto-trading/option-context/{index}
                          (the same Groww get_all_instruments lookup paper
                          entries depend on; this tool holds no credentials and
                          calls no order endpoint)
  F dashboard_status      GET /api/auto-trading/status answers and its limits
                          equal the frozen RiskLimits
  G engine_code_frozen    src/ is unchanged since the Phase 8.1 baseline commit

    PYTHONPATH=src:. python -m research.phase8.tools.preflight --out research/phase8/evidence/<campaign>/preflight
"""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine

from algoedge.risk_manager import RiskLimits
from research.phase8.tools.bars import INDEX_TICKERS
from research.phase8.tools.common import (
    Clock,
    EvidenceFile,
    Transport,
    describe_error,
    exchange,
    new_run_id,
    redact,
    stamp,
    urllib_transport,
    utc_now,
    validate_base_url,
)
from research.phase8.tools.extract import MODELS, assert_select_only, columns_of, make_engine, resolve_db_url

SCHEMA = "phase8.preflight.v1"
BASELINE_COMMIT = "06eba8cb6cedfbdc41585f362dd6c07424aed120"
READY, NOT_READY = "READY", "NOT READY"


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": redact(detail)}


def database_checks(engine: Engine | None, setup_error: str | None) -> list[dict[str, str]]:
    names = ("A_sql_connectivity", "B_required_tables", "C_required_columns", "D_read_only_query")
    if engine is None:
        return [_check(names[0], "FAIL", setup_error or "no database configured")] + \
            [_check(name, "SKIPPED", "database not reachable") for name in names[1:]]
    results: list[dict[str, str]] = []
    try:
        with engine.connect() as connection:
            try:
                statement = "SELECT 1"
                assert_select_only(statement)
                connection.execute(text(statement).execution_options(phase8_read_only=True)).scalar_one()
                results.append(_check(names[0], "PASS", "SELECT 1 answered"))

                inspector = inspect(connection)
                present = set(inspector.get_table_names())
                missing_tables = [table for table in MODELS if table not in present]
                results.append(_check(names[1], "FAIL" if missing_tables else "PASS",
                                      f"missing: {missing_tables}" if missing_tables else "all six tables present"))

                missing_columns: dict[str, list[str]] = {}
                for table in MODELS:
                    if table in present:
                        have = {column["name"] for column in inspector.get_columns(table)}
                        lacking = [name for name in columns_of(table) if name not in have]
                        if lacking:
                            missing_columns[table] = lacking
                if missing_tables:
                    results.append(_check(names[2], "SKIPPED", "tables missing"))
                else:
                    results.append(_check(names[2], "FAIL" if missing_columns else "PASS",
                                          f"missing: {missing_columns}" if missing_columns else "all columns present"))

                counts = {}
                for table in MODELS:
                    if table not in present:
                        continue
                    model_table = MODELS[table].__table__
                    query = select(func.count()).select_from(model_table).where(
                        model_table.c.id >= 0)
                    assert_select_only(str(query.compile(dialect=connection.dialect)))
                    counts[table] = connection.execute(query.execution_options(phase8_read_only=True)).scalar_one()
                results.append(_check(names[3], "FAIL" if missing_tables else "PASS",
                                      f"row counts: {counts}"))
            finally:
                connection.rollback()
    except Exception as error:  # noqa: BLE001 - any database failure means NOT READY, reported safely
        done = {result["check"] for result in results}
        detail = f"{describe_error(error)['type']}: {describe_error(error)['message']}"
        remaining = [name for name in names if name not in done]
        results.append(_check(remaining[0], "FAIL", detail))  # the check that was running
        results += [_check(name, "SKIPPED", f"not run after: {detail}") for name in remaining[1:]]
    return results


def groww_check(transport: Transport, base_url: str, timeout: float, clock: Clock) -> dict[str, str]:
    problems, resolved = [], []
    for index_id in sorted(INDEX_TICKERS):
        result = exchange(transport, "GET", base_url, f"/api/auto-trading/option-context/{index_id}", timeout, clock)
        if result.error is not None or result.http_status != 200 or not isinstance(result.body_json, dict):
            reason = result.error["type"] if result.error else f"HTTP {result.http_status}"
            problems.append(f"{index_id}: {reason}")
            continue
        for leg in ("call", "put"):
            info = result.body_json.get(leg)
            if isinstance(info, dict) and info.get("available") is True:
                resolved.append(f"{index_id}:{leg}={info.get('tradingSymbol')}")
            else:
                problems.append(f"{index_id} {leg}: {info.get('reason') if isinstance(info, dict) else 'absent'}")
    if problems:
        return _check("E_groww_instrument_master", "FAIL", "; ".join(problems))
    return _check("E_groww_instrument_master", "PASS", "resolved " + ", ".join(resolved))


def dashboard_check(transport: Transport, base_url: str, timeout: float, clock: Clock) -> dict[str, str]:
    result = exchange(transport, "GET", base_url, "/api/auto-trading/status", timeout, clock)
    if result.error is not None or result.http_status != 200 or not isinstance(result.body_json, dict):
        reason = result.error["type"] if result.error else f"HTTP {result.http_status}"
        return _check("F_dashboard_status", "FAIL", f"status endpoint unavailable: {reason}")
    frozen = RiskLimits()
    expected = {"maxOpenPositions": frozen.max_open_positions, "maxTradesPerDay": frozen.max_trades_per_day,
                "dailyLossLimit": frozen.daily_loss_limit, "entryCutoff": frozen.entry_cutoff.isoformat(),
                "squareOffTime": frozen.square_off_time.isoformat(), "cooldownMinutes": frozen.cooldown_minutes,
                "maxConsecutiveLosses": frozen.max_consecutive_losses}
    limits = result.body_json.get("limits") or {}
    diff = {key: (limits.get(key), value) for key, value in expected.items() if limits.get(key) != value}
    if diff:
        return _check("F_dashboard_status", "FAIL", f"limits differ from frozen values: {diff}")
    return _check("F_dashboard_status", "PASS", "status answered; limits equal the frozen RiskLimits")


def code_check(run_git: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> dict[str, str]:
    def default(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=30,
                              cwd=Path(__file__).resolve().parent)

    git = run_git or default
    try:
        committed = git(["diff", "--quiet", BASELINE_COMMIT, "HEAD", "--", "src"])
        working = git(["diff", "--quiet", "HEAD", "--", "src"])
    except (OSError, subprocess.SubprocessError) as error:
        return _check("G_engine_code_frozen", "FAIL", f"git unavailable: {describe_error(error)['type']}")
    if committed.returncode == 0 and working.returncode == 0:
        return _check("G_engine_code_frozen", "PASS", f"src/ identical to baseline {BASELINE_COMMIT[:7]}")
    if committed.returncode > 1 or working.returncode > 1:
        return _check("G_engine_code_frozen", "FAIL", "baseline commit not available to git")
    return _check("G_engine_code_frozen", "FAIL", "src/ differs from the baseline commit or has local changes")


def run_preflight(*, base_url: str, db_url_env: str | None, allow_non_loopback: bool = False,
                  timeout: float = 30.0, transport: Transport = urllib_transport, clock: Clock = utc_now,
                  engine_factory: Callable[[str], Engine] = make_engine,
                  url_resolver: Callable[[str | None], str] = resolve_db_url,
                  run_git: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> dict[str, Any]:
    started = clock()
    engine, setup_error = None, None
    try:
        engine = engine_factory(url_resolver(db_url_env))
    except Exception as error:  # noqa: BLE001 - reported as NOT READY without the connection string
        setup_error = f"{describe_error(error)['type']}: {describe_error(error)['message']}"
    try:
        checks = database_checks(engine, setup_error)
    finally:
        if engine is not None:
            engine.dispose()
    base = validate_base_url(base_url, allow_non_loopback=allow_non_loopback)
    checks.append(groww_check(transport, base, timeout, clock))
    checks.append(dashboard_check(transport, base, timeout, clock))
    checks.append(code_check(run_git))
    return {"schema": SCHEMA, "started_at": stamp(started), "finished_at": stamp(clock()),
            "baseline_commit": BASELINE_COMMIT, "checks": checks,
            "result": READY if all(check["status"] == "PASS" for check in checks) else NOT_READY}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--db-url-env", default=None)
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="also write the report as evidence")
    args = parser.parse_args(argv)
    report = run_preflight(base_url=args.base_url, db_url_env=args.db_url_env,
                           allow_non_loopback=args.allow_non_loopback)
    for check in report["checks"]:
        print(f"{check['check']:28} {check['status']:8} {check['detail']}")
    print(f"PREFLIGHT: {report['result']}")
    if args.out is not None:
        with EvidenceFile(args.out, "preflight", new_run_id(), utc_now()) as evidence:
            evidence.append(report)
    return 0 if report["result"] == READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
