"""Phase 8.0: the read-only SQL extractor and the environment preflight,
against a throwaway SQLite database (no SQL Server needed)."""

import json
import subprocess
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, insert, text

from algoedge.models import Base
from research.phase8.tools import extract, preflight
from research.phase8.tools.common import IST, sha256_file

START, END = datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 16, 0)
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=IST)


@pytest.fixture()
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'paper.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    tables = {name: model.__table__ for name, model in extract.MODELS.items()}
    with engine.begin() as connection:
        connection.execute(insert(tables["auto_trade_account_snapshots"]), [
            {"id": 1, "created_at": datetime(2026, 9, 30, 10, 0), "index_id": "nifty-50", "event": "ENTRY_CALL",
             "cash": 1.0, "quantity": 1, "side": "CALL", "average_price": 100.0,
             "last_event_at": datetime(2026, 9, 30, 9, 55)},
            {"id": 2, "created_at": datetime(2026, 10, 1, 10, 0), "index_id": "nifty-50", "event": "EXIT_TARGET",
             "cash": 1.0, "quantity": 0, "side": None, "average_price": None,
             "last_event_at": datetime(2026, 10, 1, 9, 55)},
        ])
        connection.execute(insert(tables["strategy_signals"]), [
            {"id": i, "created_at": datetime(2026, 10, 1, 10, i), "source": "algoedge.auto_trader",
             "index_id": "nifty-50", "action": "ENTRY_CALL", "reason": "x", "price": 1.5 * i} for i in (3, 1, 2)
        ] + [{"id": 9, "created_at": datetime(2026, 10, 2, 10, 0), "source": "algoedge.auto_trader",
              "index_id": "nifty-50", "action": "ENTRY_CALL", "reason": "out of range", "price": 1.0}])
        connection.execute(insert(tables["risk_state_events"]), [
            {"id": 1, "created_at": datetime(2026, 9, 30, 15, 0), "event": "TRADE_RECORDED", "scope": "paper",
             "auto_trading_enabled": True, "kill_switch": False, "trades_today": 2, "entries_today": 1,
             "realized_pnl_today": 3.0, "trade_day": "2026-09-30"},
        ])
    engine.dispose()
    return url


def run_extract(url, out, run_id="x1"):
    engine = extract.make_engine(url)
    try:
        return extract.extract(engine, url, START, END, out, run_id=run_id, clock=lambda: NOW)
    finally:
        engine.dispose()


def test_extraction_exports_every_table_deterministically(database, tmp_path) -> None:
    first = run_extract(database, tmp_path / "a")
    second = run_extract(database, tmp_path / "b")
    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["table_order"] == list(extract.MODELS) and set(manifest["tables"]) == set(extract.MODELS)
    assert {name: meta["rows"] for name, meta in manifest["tables"].items()} == {
        "strategy_signals": 3, "orders": 0, "risk_state_events": 0, "auto_trade_account_snapshots": 1,
        "paper_decision_events": 0, "alert_events": 0}
    signals = [json.loads(line) for line in (first / "strategy_signals.jsonl").read_text().splitlines()]
    assert [row["id"] for row in signals] == [1, 2, 3]  # ordered by id, out-of-range row excluded
    assert signals[0]["created_at"] == "2026-10-01T10:01:00"  # as written by the database, naive
    for name in [*extract.MODELS, "baseline"]:
        file = f"{name}.jsonl" if name != "baseline" else "baseline.json"
        assert sha256_file(first / file) == sha256_file(second / file)
    assert manifest["tables"]["orders"]["sha256"] == sha256_file(first / "orders.jsonl")
    assert manifest["range"]["start"] == "2026-10-01T09:00:00" and manifest["range"]["end_exclusive"] is True
    assert manifest["extracted_at"]["ist"] == "2026-10-01T17:00:00+05:30"
    assert all(meta["sql"].lstrip().upper().startswith("SELECT") for meta in manifest["tables"].values())
    assert all("2026" not in meta["sql"] for meta in manifest["tables"].values())  # values are bound, not inlined


def test_the_baseline_is_the_state_in_force_at_the_range_start(database, tmp_path) -> None:
    baseline = json.loads((run_extract(database, tmp_path) / "baseline.json").read_text())
    assert baseline["account_snapshot:nifty-50"]["quantity"] == 1  # the 10:00 exit is in range, not baseline
    assert baseline["account_snapshot:sensex"] is None
    assert baseline["risk_state"]["entries_today"] == 1 and baseline["risk_state"]["trade_day"] == "2026-09-30"


def test_the_extractor_only_ever_sends_select_statements(database, tmp_path) -> None:
    engine = extract.make_engine(database)
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *rest: statements.append(statement))
    extract.extract(engine, database, START, END, tmp_path, run_id="x2", clock=lambda: NOW)
    engine.dispose()
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    after = create_engine(database)
    with after.connect() as connection:  # nothing was written
        assert connection.execute(text("SELECT COUNT(*) FROM strategy_signals")).scalar_one() == 4
    after.dispose()


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT 1; DROP TABLE orders", "select * into x from orders",
                                 "UPDATE orders SET live = 1", "EXEC sp_who", "  insert into orders values (1)"])
def test_mutating_sql_is_rejected(sql) -> None:
    with pytest.raises(extract.ReadOnlyViolation):
        extract.assert_select_only(sql)


def test_the_cursor_guard_blocks_a_tagged_mutation(database) -> None:
    engine = extract.make_engine(database)
    with engine.connect() as connection, pytest.raises(extract.ReadOnlyViolation):
        connection.exec_driver_sql("DELETE FROM orders", execution_options={"phase8_read_only": True})
    engine.dispose()


def test_no_credentials_reach_the_manifest(database, tmp_path) -> None:
    odbc = ("mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+18%7D%3BSERVER%3Dsqlhost%3B"
            "DATABASE%3DAlgoEdge%3BUID%3Dsa%3BPWD%3Dhunter2%3B")
    assert extract.safe_db_identifier(odbc) == {"backend": "mssql", "server": "sqlhost", "database": "AlgoEdge"}
    assert extract.safe_db_identifier("mssql+pyodbc://sa:hunter2@sqlhost/AlgoEdge?driver=x") == {
        "backend": "mssql", "server": "sqlhost", "database": "AlgoEdge"}
    engine = extract.make_engine(database)
    target = extract.extract(engine, "mssql+pyodbc://sa:hunter2@sqlhost/AlgoEdge?driver=x", START, END, tmp_path,
                             run_id="x3", clock=lambda: NOW)
    engine.dispose()
    text_out = (target / "manifest.json").read_text()
    assert "hunter2" not in text_out and '"sa"' not in text_out


def test_the_url_comes_from_the_named_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("PHASE8_TEST_DB", "sqlite://")
    assert extract.resolve_db_url("PHASE8_TEST_DB") == "sqlite://"
    monkeypatch.delenv("PHASE8_TEST_DB")
    with pytest.raises(RuntimeError, match="is not set"):
        extract.resolve_db_url("PHASE8_TEST_DB")


def test_no_database_configured_is_an_explicit_error(monkeypatch) -> None:
    monkeypatch.setenv("ALGOEDGE_DB_SERVER", "")
    with pytest.raises(RuntimeError, match="no database configured"):
        extract.resolve_db_url(None)


def test_an_existing_extraction_directory_is_never_overwritten(database, tmp_path) -> None:
    run_extract(database, tmp_path, run_id="same")
    with pytest.raises(FileExistsError):
        run_extract(database, tmp_path, run_id="same")


# ---------------- preflight ----------------


def app_transport(*, groww_ok=True, limits=None, status=200):
    frozen = {"maxOpenPositions": 1, "maxTradesPerDay": 10, "dailyLossLimit": 5000.0, "entryCutoff": "15:00:00",
              "squareOffTime": "15:20:00", "cooldownMinutes": 5, "maxConsecutiveLosses": 3}
    calls = []

    def transport(method, url, timeout):
        calls.append((method, url))
        assert method == "GET"
        if url.endswith("/api/auto-trading/status"):
            return status, json.dumps({"limits": {**frozen, **(limits or {})}})
        if "/api/auto-trading/option-context/" in url:
            leg = {"available": True, "tradingSymbol": "NIFTY26OCT25000CE"} if groww_ok else \
                {"available": False, "reason": "Groww is not connected"}
            return 200, json.dumps({"call": leg, "put": leg})
        raise AssertionError(url)

    transport.calls = calls
    return transport


def git_ok(_args):
    return subprocess.CompletedProcess([], 0)


def run(database, **kwargs):
    options = {"base_url": "http://127.0.0.1:5173", "db_url_env": None, "transport": app_transport(),
               "url_resolver": lambda _env: database, "run_git": git_ok}
    options.update(kwargs)
    return preflight.run_preflight(**options)


def test_preflight_is_ready_only_when_every_check_passes(database) -> None:
    report = run(database)
    assert [c["status"] for c in report["checks"]] == ["PASS"] * 7
    assert report["result"] == "READY"


def test_no_database_means_not_ready(database) -> None:
    def unconfigured(_env):
        raise RuntimeError("ALGOEDGE_DB_SERVER is not set - no database configured")

    report = run(database, url_resolver=unconfigured)
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["A_sql_connectivity"] == "FAIL"
    assert {statuses[k] for k in ("B_required_tables", "C_required_columns", "D_read_only_query")} == {"SKIPPED"}
    assert report["result"] == "NOT READY"


def test_an_unreachable_server_is_not_ready(database) -> None:
    report = run(database, url_resolver=lambda _env: "sqlite:////nonexistent/dir/x.db")
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["A_sql_connectivity"] == "FAIL" and report["result"] == "NOT READY"


def test_a_connection_error_leaks_no_password(database) -> None:
    def failing(_env):
        raise RuntimeError("login failed for mssql+pyodbc://sa:hunter2@sqlhost/AlgoEdge PWD=hunter2;")

    report = run(database, url_resolver=failing)
    assert report["result"] == "NOT READY" and "hunter2" not in json.dumps(report)


def test_missing_tables_or_columns_fail(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'partial.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE strategy_signals (id INTEGER PRIMARY KEY, created_at DATETIME)"))
    engine.dispose()
    report = run(url)
    statuses = {c["check"]: c for c in report["checks"]}
    assert statuses["B_required_tables"]["status"] == "FAIL" and "orders" in statuses["B_required_tables"]["detail"]
    assert report["result"] == "NOT READY"


def test_groww_unavailable_is_not_ready(database) -> None:
    transport = app_transport(groww_ok=False)
    report = run(database, transport=transport)
    groww = next(c for c in report["checks"] if c["check"] == "E_groww_instrument_master")
    assert groww["status"] == "FAIL" and "Groww is not connected" in groww["detail"]
    assert report["result"] == "NOT READY"
    assert all(method == "GET" for method, _ in transport.calls)
    assert not any("/run/" in url or "order" in url for _, url in transport.calls)


def test_changed_limits_or_a_down_dashboard_are_not_ready(database) -> None:
    assert run(database, transport=app_transport(limits={"maxOpenPositions": 2}))["result"] == "NOT READY"
    assert run(database, transport=app_transport(status=503))["result"] == "NOT READY"


def test_changed_engine_code_is_not_ready(database) -> None:
    report = run(database, run_git=lambda args: subprocess.CompletedProcess(args, 1))
    code = next(c for c in report["checks"] if c["check"] == "G_engine_code_frozen")
    assert code["status"] == "FAIL" and report["result"] == "NOT READY"
