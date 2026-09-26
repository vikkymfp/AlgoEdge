"""Phase 8.0 end to end: the REAL paper cycle path (web_server
_run_and_persist_cycle -> auto_trader.run_cycle -> db.record_paper_cycle)
writes a real (SQLite) database; extract.py exports it and reconcile.py
reconciles it. Proves the grouping rule on rows the engine actually writes.

The database clock is pinned to the simulated IST session time - protocol
assumption A3 (the SQL Server clock is IST) - since SQLite's own
CURRENT_TIMESTAMP is real-time UTC.
"""

import threading
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from algoedge import auto_trader, db, web_server
from algoedge.models import Base
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy
from research.phase8.tools import extract, reconcile

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


FULL = rising(60)
EVENTS = run_strategy(FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
ENTRY = next(e for e in EVENTS if e.kind == "ENTRY_CALL")
EXIT = next(e for e in EVENTS if e.kind in ("EXIT_TARGET", "EXIT_SL") and e.timestamp > ENTRY.timestamp)
FIVE = timedelta(minutes=5)


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"X{event.strike}{event.right}", underlying="NIFTY", right=event.right,
                          strike=event.strike, expiry=date(2026, 9, 30))


@pytest.fixture()
def paper(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'paper.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    clock = {"now": None, "tick": 0}

    def db_clock(_mapper, _connection, target):
        # One cycle's rows share a database timestamp to the millisecond, as on SQL Server.
        target.created_at = clock["now"].replace(tzinfo=None) + timedelta(milliseconds=clock["tick"])

    models = list(extract.MODELS.values())
    for model in models:
        event.listen(model, "before_insert", db_clock)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})
    monkeypatch.setattr(web_server, "_cycle_locks", {i: threading.Lock() for i in INDEX_IDS})
    monkeypatch.setattr(web_server, "_entry_guard", threading.Lock())
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: contract(event))

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(auto_trader, "datetime", FixedNow)
    windows = {}
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: windows["current"])
    yield url, engine, clock, windows, accounts
    for model in models:
        event.remove(model, "before_insert", db_clock)
    engine.dispose()


def test_the_engines_own_rows_reconcile(paper, tmp_path) -> None:
    url, _engine, clock, windows, accounts = paper
    entry_at = (ENTRY.timestamp + FIVE + timedelta(seconds=30)).to_pydatetime()
    clock["now"], windows["current"] = entry_at, FULL.loc[:ENTRY.timestamp]
    results = {}
    for offset, index_id in enumerate(INDEX_IDS):  # a scheduler tick: all indices, one after another
        clock["tick"] = offset * 2000
        results[index_id] = web_server._run_and_persist_cycle(index_id, "5m", 1)
    assert results["nifty-50"]["order"]["status"] == "PLACED"
    assert [results[i]["risk"]["reason"] for i in ("sensex", "bank-nifty")] == ["Max open positions reached"] * 2

    exit_at = (EXIT.timestamp + FIVE + timedelta(seconds=30)).to_pydatetime()
    clock["now"], clock["tick"], windows["current"] = exit_at, 0, FULL.loc[:EXIT.timestamp]
    exit_result = web_server._run_and_persist_cycle("nifty-50", "5m", 1)
    assert exit_result["order"]["status"] == "PLACED" and accounts["nifty-50"].quantity == 0

    extract_engine = extract.make_engine(url)
    directory = extract.extract(extract_engine, url, datetime(2026, 9, 23, 9, 0), datetime(2026, 9, 23, 16, 0),
                                tmp_path / "db", run_id="e2e")
    extract_engine.dispose()
    report = reconcile.reconcile(directory)

    exercised = ("LIVE_ORDERS", "P1_GROUPING", "TRANSITIONS", "R1_MAX_OPEN", "C2_DUPLICATES", "R2_COUNTERS",
                 "PNL", "AUDIT")
    assert {c: report["checks"][c]["status"] for c in exercised} == dict.fromkeys(exercised, "PASS"), \
        report["findings"]
    assert report["groups"]["by_status"] == {"RECONCILED": 4, "INCOMPLETE": 0, "UNRECONCILED": 0, "INCONSISTENT": 0}
    assert report["fills"] == 2 and report["stop_campaign"] is False


# ---------------- the tools against the real dashboard endpoints ----------------


def real_dashboard_transport(calls):
    """Routes the tools' requests to the real FastAPI endpoint functions
    (the bodies FastAPI would serialize), so a schema drift in the dashboard
    breaks these tests instead of a campaign."""
    import json
    from urllib.parse import parse_qs, urlsplit

    def transport(method, url, timeout):
        parts = urlsplit(url)
        calls.append((method, parts.path))
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if method == "GET" and parts.path == "/api/auto-trading/status":
            body = web_server.auto_trading_status()
        elif method == "GET" and parts.path == "/api/alerts":
            body = web_server.list_alerts(limit=int(query.get("limit", 50)))
        elif method == "POST" and parts.path.startswith("/api/auto-trading/run/"):
            body = web_server.auto_trading_run(parts.path.rsplit("/", 1)[1], query["interval"], int(query["quantity"]))
        else:
            raise AssertionError(f"unexpected {method} {parts.path}")
        return 200, json.dumps(body, default=str)

    return transport


def test_the_collector_reads_every_field_from_the_real_status_endpoint(paper) -> None:
    from research.phase8.tools import collector

    calls = []
    record = collector.collect_sample(real_dashboard_transport(calls), "http://127.0.0.1:5173", run_id="r", seq=0,
                                      timeout=5, alerts_limit=10, clock=lambda: datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc))
    assert record["ok"], record["problems"]
    assert record["status"]["missing_fields"] == []
    assert set(record["status"]["extracted"]["accounts"]) == set(INDEX_IDS)
    assert calls == [("GET", "/api/auto-trading/status"), ("GET", "/api/alerts")]


def test_the_preflight_limits_match_the_real_status_endpoint(paper) -> None:
    from research.phase8.tools import preflight

    result = preflight.dashboard_check(real_dashboard_transport([]), "http://127.0.0.1:5173", 5,
                                       lambda: datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc))
    assert result["status"] == "PASS", result


def test_the_drill_drives_the_real_paper_cycle_endpoint(paper, tmp_path) -> None:
    from research.phase8.tools import drill
    from research.phase8.tools.common import read_jsonl

    _url, _engine, clock, windows, accounts = paper
    clock["now"] = (ENTRY.timestamp + FIVE + timedelta(seconds=30)).to_pydatetime()
    clock["tick"], windows["current"] = 0, FULL.loc[:ENTRY.timestamp]
    calls = []
    run = drill.Drill(tmp_path, mode="repeat", transport=real_dashboard_transport(calls),
                      clock=lambda: clock["now"], run_id="real")
    drill.run_repeat(run, ["nifty-50", "sensex"], rounds=1, concurrency=2, pause=0)
    run.close()
    records = [r for r in read_jsonl(run.evidence.path) if "index_id" in r]
    placed = [r for r in records if (r["body_json"].get("order") or {}).get("status") == "PLACED"]
    assert len(records) == 4 and len(placed) == 1  # four concurrent paper cycles, exactly one fill
    assert sum(1 for a in accounts.values() if a.quantity > 0) == 1
    assert {method for method, _ in calls} == {"POST"}
