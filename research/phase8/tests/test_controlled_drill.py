"""Phase 8 controlled drills: the real engine produces HALT, EXIT_RULES and
SQUARE_OFF evidence in an isolated SQLite database, and the campaign
reconciler reads it."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from algoedge import db
from research.phase8.tools import controlled_drill
from research.phase8.tools.common import read_jsonl, verify_record

REPO = Path(__file__).resolve().parents[3]


def summary_of(result):
    records = read_jsonl(Path(result["evidence_file"]))
    assert all(verify_record(r) for r in records)
    (summary,) = [r for r in records if r["kind"] == "summary"]
    return summary, [r for r in records if r["kind"] == "step"]


def rows(result, sql):
    connection = sqlite3.connect(Path(result["scenario_dir"]) / "drill.db")
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


@pytest.fixture()
def untouched_globals():
    """The drill must leave the process exactly as it found it."""
    from algoedge import auto_trader, web_server

    before = (db._session_factory, web_server.risk_manager, web_server.order_managers, web_server.run_cycle,
              auto_trader.fetch_underlying_data)
    yield
    after = (db._session_factory, web_server.risk_manager, web_server.order_managers, web_server.run_cycle,
             auto_trader.fetch_underlying_data)
    assert after == before


def test_three_consecutive_stop_outs_trip_the_halt(tmp_path, untouched_globals):
    result = controlled_drill.run_scenario(tmp_path, "HALT")
    summary, steps = summary_of(result)
    assert summary["engine_result"] == "PASS", summary["engine_checks"]
    assert summary["reconcile_target"]["check"] == "HALT" and summary["reconcile_target"]["status"] == "PASS"
    assert summary["reconcile_target"]["observations"]["halt_trips_verified"] == 1
    assert [s["step"] for s in steps] == ["entry 1", "stop-out 1", "entry 2", "stop-out 2", "entry 3", "stop-out 3"]
    # The engine's own rows: three losing SELLs, and the halt set only by the third.
    sells = rows(result, "select realized_pnl from orders where side = 'SELL' and outcome = 'PLACED' order by id")
    assert len(sells) == 3 and all(pnl < 0 for (pnl,) in sells)
    streak = rows(result, "select consecutive_losses, consecutive_loss_halt from risk_state_events "
                          "where event = 'TRADE_RECORDED' order by id")
    assert streak == [(0, 0), (1, 0), (1, 0), (2, 0), (2, 0), (3, 1)]
    # Regression: the exit that trips the halt persists the halt in its own (post-trade) risk
    # row, but nothing was restricted before it - it must not count as an exit under the halt.
    assert summary["reconcile_checks"]["EXIT_RULES"] == "UNVERIFIABLE"
    report = read_jsonl(next((Path(result["scenario_dir"]) / "reconcile").glob("reconcile_*.jsonl")))[0]
    exit_rules = report["checks"]["EXIT_RULES"]
    assert (exit_rules["evaluated"], exit_rules["observations"]["exits_filled_while_restricted"]) == (0, 0)


def test_a_stop_out_fills_under_kill_switch_and_disabled_trading(tmp_path, untouched_globals):
    result = controlled_drill.run_scenario(tmp_path, "EXIT_RULES")
    summary, _steps = summary_of(result)
    assert summary["engine_result"] == "PASS", summary["engine_checks"]
    target = summary["reconcile_target"]
    assert (target["check"], target["status"]) == ("EXIT_RULES", "PASS")
    assert target["observations"]["exits_filled_while_restricted"] == 1
    assert summary["reconcile_checks"]["DECISION_STATE"] == "PASS"  # the blocked entry agrees with its state
    events = [e for (e,) in rows(result, "select event from risk_state_events order by id")]
    assert events == ["TRADE_RECORDED", "KILL_SWITCH_ON", "DISABLE", "TRADE_RECORDED"]
    assert rows(result, "select decision, reason from paper_decision_events") == [
        ("BLOCKED", "Emergency kill switch is engaged (Phase 8 controlled drill)")]
    assert target["evaluated"] == 1  # counted from the state BEFORE the exit (kill switch on, trading disabled)


def test_an_open_position_is_squared_off_at_15_21(tmp_path, untouched_globals):
    result = controlled_drill.run_scenario(tmp_path, "SQUARE_OFF")
    summary, _steps = summary_of(result)
    assert summary["engine_result"] == "PASS", summary["engine_checks"]
    target = summary["reconcile_target"]
    assert (target["check"], target["status"]) == ("SQUARE_OFF", "PASS")
    assert target["observations"]["same_day_square_offs"] == 1
    (snapshot,) = rows(result, "select quantity, square_off_date, created_at from auto_trade_account_snapshots "
                               "where event = 'SQUARE_OFF'")
    assert snapshot[:2] == (0, "2026-09-23") and snapshot[2].startswith("2026-09-23 15:21:00")


def test_the_drill_writes_only_paper_rows_and_immutable_evidence(tmp_path):
    result = controlled_drill.run_scenario(tmp_path, "HALT")
    assert rows(result, "select count(*) from orders where live = 1") == [(0,)]
    scenario_dir = Path(result["scenario_dir"])
    reconcile_files = list((scenario_dir / "reconcile").glob("reconcile_*.jsonl"))
    assert len(reconcile_files) == 1 and verify_record(read_jsonl(reconcile_files[0])[0])
    with pytest.raises(FileExistsError):
        controlled_drill.run_scenario(tmp_path, "HALT")  # an existing scenario directory is never reused


def test_an_engine_surprise_is_a_fail_not_an_exception(tmp_path, monkeypatch):
    def broken(engine, day):
        raise AssertionError("simulated engine surprise")

    monkeypatch.setitem(controlled_drill.SCENARIO_FUNCTIONS, "HALT", broken)
    summary, _ = summary_of(controlled_drill.run_scenario(tmp_path, "HALT"))
    assert summary["engine_result"] == "FAIL" and "simulated engine surprise" in summary["error"]


def test_it_refuses_a_process_connected_to_a_campaign_database(monkeypatch):
    class FakeUrl:
        def get_backend_name(self):
            return "mssql"

    monkeypatch.setattr(db, "_engine", type("FakeEngine", (), {"url": FakeUrl()})())
    with pytest.raises(RuntimeError, match="refusing to run"):
        controlled_drill.import_web_server_isolated()


# Run in a fresh process: every network attempt is recorded and refused, so no real request is made.
NETWORK_SPY = """
import json, socket, sys
attempts = []
def _deny_getaddrinfo(host, *a, **k):
    attempts.append(["getaddrinfo", str(host)]); raise OSError("network disabled by test spy")
def _deny_connect(self, address):
    attempts.append(["connect", str(address)]); raise OSError("network disabled by test spy")
socket.getaddrinfo = _deny_getaddrinfo
socket.socket.connect = _deny_connect
"""
CONFIGURED = {"ALGOEDGE_DB_SERVER": "campaign-sql-host.invalid", "ALGOEDGE_GROWW_ACCESS_TOKEN": "dummy-token",
              "ALGOEDGE_GROWW_API_KEY": "dummy-key", "ALGOEDGE_GROWW_API_SECRET": "dummy-secret"}


def run_isolated(code, extra_env):
    """extra_env values of None remove the variable from the child's environment."""
    env = {k: v for k, v in {**os.environ, **extra_env}.items() if v is not None}
    env["PYTHONPATH"] = f"src{os.pathsep}."
    out = subprocess.run([sys.executable, "-c", NETWORK_SPY + code], cwd=REPO, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_isolated_import_never_connects_to_a_database_or_groww():
    # A campaign database and Groww credentials are configured: importing through the drill must
    # leave persistence disabled, give the token service no credentials, attempt no network
    # call at all, and restore every variable afterwards.
    result = run_isolated(
        "from research.phase8.tools import controlled_drill as c\n"
        "ws = c.import_web_server_isolated()\n"
        "from algoedge import db; import os\n"
        "print(json.dumps({'engine': db._engine is None, 'attempts': attempts,\n"
        "  'token_credentials': [ws.token_service._api_key, ws.token_service._api_secret,\n"
        "                        ws.token_service._access_token],\n"
        "  'env': {n: os.environ.get(n) for n in c.ISOLATED_IMPORT_ENV}}))\n", CONFIGURED)
    assert result["engine"] is True
    assert result["attempts"] == []  # no Groww token validation/minting, no database connection
    assert result["token_credentials"] == [None, None, None]
    assert result["env"] == CONFIGURED  # all original values restored


def test_the_network_spy_detects_the_groww_token_path_without_isolation():
    # Control: a plain import with Groww credentials configured does try the network (the token
    # service validates/mints a token) - so the empty attempt list above is meaningful. Every
    # attempt is refused by the spy; nothing reaches a broker.
    groww_only = {k: v for k, v in CONFIGURED.items() if k != "ALGOEDGE_DB_SERVER"}
    result = run_isolated("import algoedge.web_server as ws\n"
                          "print(json.dumps({'attempts': attempts, 'key': ws.token_service._api_key}))\n",
                          {**groww_only, "ALGOEDGE_DB_SERVER": ""})
    assert result["attempts"] and result["key"] == "dummy-key"


def test_the_environment_is_restored_when_the_import_fails():
    unset = "ALGOEDGE_GROWW_API_SECRET"  # one variable absent beforehand must stay absent
    configured = {k: v for k, v in CONFIGURED.items() if k != unset}
    result = run_isolated(
        "import importlib.abc\n"
        "class Fail(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'algoedge.web_server':\n"
        "            raise ImportError('simulated import failure')\n"
        "sys.meta_path.insert(0, Fail())\n"
        "from research.phase8.tools import controlled_drill as c; import os\n"
        "try:\n"
        "    c.import_web_server_isolated(); failed = False\n"
        "except ImportError:\n"
        "    failed = True\n"
        "print(json.dumps({'failed': failed, 'env': {n: os.environ.get(n) for n in c.ISOLATED_IMPORT_ENV}}))\n",
        {**configured, unset: None})
    assert result["failed"] is True
    assert result["env"] == {**configured, unset: None}  # values restored, the absent one still absent
