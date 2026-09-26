"""Phase 7 B7: paper decision audit trail and repeated-failure alerts.

- A strategy event that does not become a fill (blocked by a risk/control
  rule, expired as stale, a failed paper fill, entries held back by a
  missed square-off) writes a paper_decision_events row - in the same atomic
  transaction as the cycle's signal row. Fills and routine no-signal cycles
  write none.
- Consecutive paper cycle failures per index raise ONE alert when a streak
  reaches the threshold (3): CYCLE failures (exceptions, no valid market
  data) as PAPER_CYCLE_FAILURE, rolled-back persistence as DATABASE_FAILURE.
  A completed cycle ends the streak. Alerts are the app's existing in-app
  alert_events rows - nothing is sent externally.
"""

import asyncio
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from algoedge import alerts, auto_trader, db, web_server
from algoedge.auto_trader import AutoTradeCycleResult
from algoedge.failure_monitor import CYCLE, DATABASE, REPEATED_FAILURE_THRESHOLD, RepeatedFailureMonitor
from algoedge.models import Base, PaperDecisionEvent, StrategySignal
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, OrderResult, SimulatedAccount
from algoedge.risk_manager import IST, RiskDecision, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import TradeEvent
from fno_signals.strategy import run as run_strategy


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


_FULL = rising(40)
_ENTRY = next(e for e in run_strategy(_FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
              if e.kind == "ENTRY_CALL")
ENTRY_WINDOW = _FULL.loc[:_ENTRY.timestamp]
FRESH = (_ENTRY.timestamp + pd.Timedelta(minutes=7)).to_pydatetime()
STALE = (_ENTRY.timestamp + pd.Timedelta(minutes=30)).to_pydatetime()  # > 2 bars after the bar closed
YESTERDAY_BARS = pd.DataFrame(
    {c: [100.0] * 18 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 18},
    index=pd.date_range("2026-09-22 14:00", periods=18, freq="5min", tz="Asia/Kolkata"))
NO_VALID_DATA = pd.DataFrame({c: [np.nan] * 3 for c in ("Open", "High", "Low", "Close", "Volume")},
                             index=pd.date_range("2026-09-23 09:15", periods=3, freq="5min", tz="Asia/Kolkata"))


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY",
                          right=event.right, strike=event.strike, expiry=date(2026, 9, 30))


@pytest.fixture()
def database(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


@pytest.fixture()
def captured_alerts(monkeypatch) -> list[tuple[str, str]]:
    """A fresh monitor whose alerts are captured, not persisted/sent."""
    raised: list[tuple[str, str]] = []
    monkeypatch.setattr(web_server, "paper_failure_monitor", RepeatedFailureMonitor(
        alert=lambda category, message, *, source: raised.append((category, message))))
    return raised


@pytest.fixture()
def dashboard(monkeypatch):
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    account = SimulatedAccount()
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {"nifty-50": OrderManager(account)})
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: contract(event))

    def run(window, now: datetime) -> dict:
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(auto_trader, "datetime", FixedNow)
        fetch = window if callable(window) else (lambda *_a, **_k: window)
        monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
        return web_server._run_and_persist_cycle("nifty-50", "5m", 1)

    return risk_manager, account, run


# ================= audit: decisions that did not become a fill =================


@pytest.mark.parametrize("configure, reason_part", [
    (lambda rm: rm.trip_kill_switch("ops"), "kill switch"),
    (lambda rm: setattr(rm.state, "consecutive_loss_halt", True), "Trading halted"),
    (lambda rm: rm.disable_auto_trading(), "Auto trading is disabled"),
    (lambda rm: (setattr(rm.state, "trade_day", "2026-09-23"), setattr(rm.state, "realized_pnl_today", -5000.0)),
     "Daily loss limit reached"),
])
def test_a_risk_blocked_entry_is_audited_with_its_context(database, dashboard, configure, reason_part) -> None:
    risk_manager, account, run = dashboard
    configure(risk_manager)
    response = run(ENTRY_WINDOW, FRESH)

    assert response["order"] is None and response["persistence"]["status"] == db.PERSIST_OK
    (row,) = db.list_paper_decision_events(index_id="nifty-50")
    assert row["decision"] == "BLOCKED" and reason_part in row["reason"]
    assert row["reason"] == response["risk"]["reason"]  # the engine's reason, verbatim
    assert (row["eventKind"], row["price"], row["openQuantity"]) == ("ENTRY_CALL", _ENTRY.underlying_price, 0)
    assert row["killSwitch"] is risk_manager.state.kill_switch
    assert row["consecutiveLossHalt"] is risk_manager.state.consecutive_loss_halt
    assert row["autoTradingEnabled"] is risk_manager.state.auto_trading_enabled
    assert row["realizedPnlToday"] == risk_manager.state.realized_pnl_today


def test_an_unresolved_option_contract_is_audited_as_blocked(database, dashboard, monkeypatch) -> None:
    _risk_manager, _account, run = dashboard
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: None)
    run(ENTRY_WINDOW, FRESH)
    (row,) = db.list_paper_decision_events()
    assert (row["decision"], row["reason"]) == ("BLOCKED", "No matching option contract found")


def test_a_stale_entry_is_audited_as_expired(database, dashboard) -> None:
    _risk_manager, account, run = dashboard
    response = run(ENTRY_WINDOW, STALE)
    assert response["risk"]["reason"].startswith("Stale signal expired")
    (row,) = db.list_paper_decision_events()
    assert row["decision"] == "EXPIRED" and row["eventKind"] == "ENTRY_CALL"
    assert account.quantity == 0


def test_a_failed_paper_fill_is_audited_with_the_fill_error(database, dashboard, monkeypatch) -> None:
    _risk_manager, _account, run = dashboard
    monkeypatch.setattr(OrderManager, "place_event", lambda self, *a, **k: OrderResult("FAILED", "fill rejected"))
    response = run(ENTRY_WINDOW, FRESH)
    assert response["order"]["status"] == "FAILED"
    (row,) = db.list_paper_decision_events()
    assert (row["decision"], row["reason"]) == ("ORDER_FAILED", "fill rejected")
    assert db.list_orders(source="algoedge.auto_trader", live=False)[0]["outcome"] == "FAILED"


def test_entries_held_back_by_a_missed_square_off_are_audited(database, dashboard) -> None:
    _risk_manager, account, run = dashboard
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.last_event_at = YESTERDAY_BARS.index[6]
    response = run(YESTERDAY_BARS, datetime(2026, 9, 23, 9, 26, tzinfo=IST))  # no bar from today yet
    assert response["persistence"] == {"status": db.PERSIST_OK, "error": None}
    (row,) = db.list_paper_decision_events()
    assert row["decision"] == "SQUARE_OFF_PENDING" and row["reason"].startswith("Open position from 2026-09-22")
    assert (row["eventKind"], row["eventAt"], row["openQuantity"]) == (None, None, 1)


def test_fills_and_routine_cycles_are_not_audited(database, dashboard) -> None:
    _risk_manager, _account, run = dashboard
    assert run(ENTRY_WINDOW, FRESH)["order"]["status"] == "PLACED"  # a fill: recorded as an order
    again = run(ENTRY_WINDOW, FRESH)  # "Signal already processed": routine
    assert again["risk"]["reason"].startswith("Signal already processed") and again["persistence"] is None
    assert db.list_paper_decision_events() == []


def test_the_decision_row_is_atomic_with_the_cycles_signal_row(database, dashboard) -> None:
    risk_manager, _account, run = dashboard
    risk_manager.trip_kill_switch("ops")

    def boom(*_args):
        raise OperationalError("INSERT", {}, Exception("decision write failed"))

    event.listen(PaperDecisionEvent, "before_insert", boom)
    try:
        response = run(ENTRY_WINDOW, FRESH)
    finally:
        event.remove(PaperDecisionEvent, "before_insert", boom)
    assert response["persistence"] == {"status": db.PERSIST_FAILED, "error": "DATABASE_FAILURE"}
    with sessionmaker(bind=database)() as session:
        assert session.query(StrategySignal).count() == session.query(PaperDecisionEvent).count() == 0


@pytest.mark.parametrize("result, expected", [
    (AutoTradeCycleResult(None, RiskDecision(False, "No actionable signal"), None), None),
    (AutoTradeCycleResult(None, RiskDecision(False, "No valid market data"), None), None),
    (AutoTradeCycleResult(_ENTRY, RiskDecision(True, "Risk checks passed"), OrderResult("PLACED", "ok")), None),
    (AutoTradeCycleResult(_ENTRY, RiskDecision(False, "Past entry cutoff - no new positions may be opened"),
                          None), "BLOCKED"),
    (AutoTradeCycleResult(TradeEvent(timestamp=_ENTRY.timestamp, kind="EXIT_SL", underlying_price=1.0,
                                     option_symbol=None, stop_loss=None, target=None, exit_level=1.0),
                          RiskDecision(False, auto_trader.REASON_EXIT_WITHOUT_POSITION), None), "SKIPPED"),
])
def test_decision_classification(result, expected) -> None:
    assert web_server._paper_decision(result) == expected


def test_audit_rows_hold_no_credentials_or_broker_data() -> None:
    columns = {c.name for c in PaperDecisionEvent.__table__.columns}
    assert not {c for c in columns if any(k in c for k in ("token", "secret", "password", "key", "credential"))}


# ================= repeated-failure monitor =================


def test_the_threshold_is_three_consecutive_failures() -> None:
    raised = []
    monitor = RepeatedFailureMonitor(alert=lambda category, message, *, source: raised.append((category, message)))
    assert REPEATED_FAILURE_THRESHOLD == monitor.threshold == 3
    assert [monitor.record_failure("nifty-50", CYCLE, "RuntimeError") for _ in range(3)] == [False, False, True]
    assert raised == [(alerts.PAPER_CYCLE_FAILURE,
                       "nifty-50: 3 consecutive paper cycle failures (CYCLE) - last: RuntimeError")]


def test_no_alert_storm_one_alert_per_streak_and_success_resets() -> None:
    raised = []
    monitor = RepeatedFailureMonitor(alert=lambda category, message, *, source: raised.append(category))
    for _ in range(10):
        monitor.record_failure("nifty-50", CYCLE, "RuntimeError")
    assert raised == [alerts.PAPER_CYCLE_FAILURE]  # 10 failures, one alert
    monitor.record_success("nifty-50")
    assert monitor.consecutive_failures("nifty-50", CYCLE) == 0
    for _ in range(3):
        monitor.record_failure("nifty-50", CYCLE, "RuntimeError")
    assert raised == [alerts.PAPER_CYCLE_FAILURE] * 2  # a NEW streak alerts again


def test_streaks_are_per_index_and_per_kind() -> None:
    raised = []
    monitor = RepeatedFailureMonitor(alert=lambda category, message, *, source: raised.append(message))
    for index_id in ("nifty-50", "sensex", "nifty-50", "sensex", "bank-nifty"):
        monitor.record_failure(index_id, CYCLE, "E")
    monitor.record_failure("nifty-50", DATABASE, "D")
    assert raised == []  # 2 + 2 + 1 CYCLE and 1 DATABASE: no streak reached 3
    monitor.record_failure("nifty-50", CYCLE, "E")
    assert raised == ["nifty-50: 3 consecutive paper cycle failures (CYCLE) - last: E"]


def test_unknown_kinds_and_bad_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError):
        RepeatedFailureMonitor(threshold=0)
    with pytest.raises(ValueError):
        RepeatedFailureMonitor(alert=lambda *a, **k: None).record_failure("nifty-50", "NETWORK", "x")


# ---------------- through the dashboard's real cycle path ----------------


def failing_fetch(*_a, **_k):
    raise RuntimeError("yfinance: No data found, symbol may be delisted; session=abc123secret")


def test_repeated_scheduler_cycle_failures_alert_once_at_the_threshold(dashboard, captured_alerts, monkeypatch) -> None:
    risk_manager, _account, _run = dashboard
    monkeypatch.setattr(web_server._scheduler, "_index_ids", ["nifty-50"])
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", failing_fetch)
    for _ in range(2):
        asyncio.run(web_server._scheduler._tick())
    assert captured_alerts == []  # below the threshold
    for _ in range(3):
        asyncio.run(web_server._scheduler._tick())
    assert captured_alerts == [(alerts.PAPER_CYCLE_FAILURE,
                                "nifty-50: 3 consecutive paper cycle failures (CYCLE) - last: RuntimeError")]
    assert "abc123secret" not in captured_alerts[0][1]  # only the exception type, never its message


def test_failures_below_the_threshold_are_reset_by_a_successful_cycle(dashboard, captured_alerts) -> None:
    _risk_manager, _account, run = dashboard
    for fetch in (failing_fetch, failing_fetch):
        with pytest.raises(RuntimeError):
            run(fetch, FRESH)
    run(ENTRY_WINDOW, FRESH)  # a completed cycle ends the streak
    for fetch in (failing_fetch, failing_fetch):
        with pytest.raises(RuntimeError):
            run(fetch, FRESH)
    assert captured_alerts == []
    assert web_server.paper_failure_monitor.consecutive_failures("nifty-50", CYCLE) == 2


def test_no_valid_market_data_counts_as_a_cycle_failure(dashboard, captured_alerts) -> None:
    _risk_manager, _account, run = dashboard
    for _ in range(3):
        assert run(NO_VALID_DATA, FRESH)["risk"]["reason"] == "No valid market data"
    assert captured_alerts == [(alerts.PAPER_CYCLE_FAILURE,
                                "nifty-50: 3 consecutive paper cycle failures (CYCLE) - last: no valid market data")]


def test_repeated_database_failures_raise_database_failure(dashboard, captured_alerts, monkeypatch) -> None:
    risk_manager, _account, run = dashboard
    risk_manager.trip_kill_switch("ops")  # each manual run re-audits the blocked entry -> a write every cycle

    def broken_factory():
        raise OperationalError("connect", {}, Exception("server=10.0.0.5;uid=algoedge"))

    monkeypatch.setattr(db, "_session_factory", broken_factory)
    for _ in range(3):
        assert run(ENTRY_WINDOW, FRESH)["persistence"]["error"] == "DATABASE_FAILURE"
    assert captured_alerts == [(alerts.DATABASE_FAILURE, "nifty-50: 3 consecutive paper cycle failures (DATABASE)"
                                " - last: paper cycle persistence rolled back")]
    assert "10.0.0.5" not in captured_alerts[0][1]
    assert web_server.paper_failure_monitor.consecutive_failures("nifty-50", CYCLE) == 0  # the cycles ran


def test_a_busy_cycle_counts_as_a_cycle_failure(dashboard, captured_alerts, monkeypatch) -> None:
    _risk_manager, _account, run = dashboard
    monkeypatch.setattr(web_server, "_CYCLE_LOCK_TIMEOUT_SECONDS", 0.01)
    lock = web_server._cycle_locks["nifty-50"]
    assert lock.acquire()
    try:
        for _ in range(3):
            with pytest.raises(web_server.CycleBusyError):
                run(ENTRY_WINDOW, FRESH)
    finally:
        lock.release()
    assert captured_alerts == [(alerts.PAPER_CYCLE_FAILURE,
                                "nifty-50: 3 consecutive paper cycle failures (CYCLE) - last: CycleBusyError")]


def test_repeated_failure_alerts_are_persisted_as_in_app_alerts(database, dashboard, monkeypatch) -> None:
    # The real alert path (alerts.raise_alert -> alert_events), nothing external.
    monkeypatch.setattr(web_server, "paper_failure_monitor", RepeatedFailureMonitor())
    _risk_manager, _account, run = dashboard
    for _ in range(4):
        with pytest.raises(RuntimeError):
            run(failing_fetch, FRESH)
    (alert,) = db.list_alert_events()
    assert (alert["category"], alert["severity"], alert["source"]) == (
        "PAPER_CYCLE_FAILURE", "CRITICAL", "algoedge.auto_trader")
    assert alert["acknowledged"] is False
