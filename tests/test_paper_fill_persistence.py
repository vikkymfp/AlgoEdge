"""Phase 7 B6: one paper cycle is persisted in ONE transaction.

A paper fill writes its signal, order, post-fill risk snapshot and account
snapshot through db.record_paper_cycle(): all rows commit together or none
do. A failure is rolled back completely, returned as PERSIST_FAILED and
surfaced by the dashboard cycle as DATABASE_FAILURE; the in-memory paper
fill is unaffected. Uses a real (in-memory SQLite) database; failures are
injected with SQLAlchemy before_insert hooks on the table under test.
"""

import logging
from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from algoedge import auto_trader, db, web_server
from algoedge.auto_trader import restore_account_state
from algoedge.models import AutoTradeAccountSnapshot, Base, OrderRecord, RiskStateEvent, StrategySignal
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager, restore_risk_state
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

TABLES = (StrategySignal, OrderRecord, RiskStateEvent, AutoTradeAccountSnapshot)


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


_FULL = rising(40)
_ENTRY = next(e for e in run_strategy(_FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
              if e.kind == "ENTRY_CALL")
ENTRY_WINDOW = _FULL.loc[:_ENTRY.timestamp]
ENTRY_NOW = (_ENTRY.timestamp + pd.Timedelta(minutes=7)).to_pydatetime()
FLAT = pd.DataFrame({c: [110.0] * 20 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 20},
                    index=pd.date_range("2026-09-23 14:00", periods=20, freq="5min", tz="Asia/Kolkata"))
SQUARE_OFF_NOW = datetime(2026, 9, 23, 15, 20, tzinfo=IST)


@pytest.fixture()
def database(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


def counts(engine) -> dict[str, int]:
    with sessionmaker(bind=engine)() as session:
        return {model.__tablename__: session.query(model).count() for model in TABLES}


def fail_inserts_into(model):
    """Makes every INSERT into `model` fail the way a dropped connection or a
    constraint violation does, mid-transaction; returns an undo function."""
    def boom(*_args):
        raise OperationalError("INSERT", {}, Exception(f"{model.__tablename__} write failed"))

    event.listen(model, "before_insert", boom)
    return lambda: event.remove(model, "before_insert", boom)


@pytest.fixture()
def dashboard(monkeypatch):
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    account = SimulatedAccount()
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {"nifty-50": OrderManager(account)})
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", lambda index_id, event: OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}", underlying="NIFTY", right=event.right,
        strike=event.strike, expiry=date(2026, 9, 30)))

    def run(window: pd.DataFrame, now: datetime) -> dict:
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(auto_trader, "datetime", FixedNow)
        monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: window)
        return web_server._run_and_persist_cycle("nifty-50", "5m", 1)

    return risk_manager, account, run


# ---------------- 1, 7, 9: a successful fill persists every record ----------------


def test_a_successful_entry_fill_persists_all_records_together(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    response = run(ENTRY_WINDOW, ENTRY_NOW)

    assert response["order"]["status"] == "PLACED"
    assert response["persistence"] == {"status": db.PERSIST_OK, "error": None}
    assert counts(database) == {"strategy_signals": 1, "orders": 1, "risk_state_events": 1,
                                "auto_trade_account_snapshots": 1}
    with sessionmaker(bind=database)() as session:
        order = session.query(OrderRecord).one()
        assert (order.side, order.outcome, order.live) == ("BUY", "PLACED", False)
        assert order.price == pytest.approx(_ENTRY.underlying_price)
        assert order.trading_symbol == f"NIFTY26SEP{_ENTRY.strike}CE"
        assert session.query(RiskStateEvent).one().trades_today == 1
        assert session.query(AutoTradeAccountSnapshot).one().quantity == 1


def test_restart_restores_exactly_what_the_atomic_write_committed(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    run(ENTRY_WINDOW, ENTRY_NOW)

    restored_account = SimulatedAccount()
    restore_account_state(restored_account, db.load_latest_auto_trade_account_state("nifty-50"))
    assert (restored_account.quantity, restored_account.side) == (1, "CALL")
    assert restored_account.average_price == pytest.approx(account.average_price)
    assert restored_account.last_event_at == account.last_event_at
    assert restored_account.contract.trading_symbol == account.contract.trading_symbol

    restored_risk = RiskManager()
    restore_risk_state(restored_risk, db.load_latest_risk_state(scope="paper"))
    assert restored_risk.state.trades_today == 1


def test_a_blocked_signal_still_persists_just_its_signal(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    risk_manager.trip_kill_switch("test")
    response = run(ENTRY_WINDOW, ENTRY_NOW)
    assert response["order"] is None and response["persistence"]["status"] == db.PERSIST_OK
    assert counts(database) == {"strategy_signals": 1, "orders": 0, "risk_state_events": 0,
                                "auto_trade_account_snapshots": 0}


# ---------------- 2-6: any failing write rolls the whole cycle back ----------------


@pytest.mark.parametrize("failing", [StrategySignal, OrderRecord, RiskStateEvent, AutoTradeAccountSnapshot])
def test_any_failed_write_rolls_back_the_whole_fill(database, dashboard, caplog, failing) -> None:
    risk_manager, account, run = dashboard
    undo = fail_inserts_into(failing)
    try:
        with caplog.at_level(logging.ERROR):
            response = run(ENTRY_WINDOW, ENTRY_NOW)
    finally:
        undo()

    assert counts(database) == dict.fromkeys(counts(database), 0)  # no partial records at all
    assert response["persistence"] == {"status": db.PERSIST_FAILED, "error": "DATABASE_FAILURE"}
    assert "DATABASE_FAILURE" in caplog.text
    assert response["order"]["status"] == "PLACED"  # the in-memory paper fill is unaffected
    assert account.quantity == 1 and risk_manager.state.trades_today == 1


def test_after_a_rolled_back_fill_a_restart_sees_the_previous_consistent_state(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    undo = fail_inserts_into(AutoTradeAccountSnapshot)
    try:
        run(ENTRY_WINDOW, ENTRY_NOW)
    finally:
        undo()
    # Neither an orphan order nor a risk snapshot claiming a trade the account never recorded.
    assert db.load_latest_auto_trade_account_state("nifty-50") is None
    assert db.load_latest_risk_state(scope="paper") is None
    assert db.list_orders(source="algoedge.auto_trader", live=False) == []


def test_a_connection_that_cannot_even_open_fails_cleanly(monkeypatch, dashboard) -> None:
    def broken_factory():
        raise OperationalError("connect", {}, Exception("connection dropped"))

    monkeypatch.setattr(db, "_session_factory", broken_factory)
    risk_manager, account, run = dashboard
    response = run(ENTRY_WINDOW, ENTRY_NOW)
    assert response["persistence"] == {"status": db.PERSIST_FAILED, "error": "DATABASE_FAILURE"}
    assert account.quantity == 1


def test_an_invalid_record_writes_nothing(database) -> None:
    status = db.record_paper_cycle(signal={"source": "x", "index_id": "nifty-50", "action": "A", "reason": "r",
                                           "price": 1.0, "not_a_column": 1})
    assert status == db.PERSIST_FAILED
    assert counts(database) == dict.fromkeys(counts(database), 0)


def test_persistence_disabled_is_not_a_failure(monkeypatch, dashboard) -> None:
    monkeypatch.setattr(db, "_session_factory", None)  # ALGOEDGE_DB_SERVER not configured
    risk_manager, account, run = dashboard
    response = run(ENTRY_WINDOW, ENTRY_NOW)
    assert response["persistence"] == {"status": db.PERSIST_DISABLED, "error": None}
    assert response["order"]["status"] == "PLACED"


# ---------------- 8: square-off is atomic too ----------------


def test_square_off_persists_atomically(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.last_event_at = FLAT.index[3]
    response = run(FLAT, SQUARE_OFF_NOW)
    assert response["signal"]["kind"] == "SQUARE_OFF" and response["persistence"]["status"] == db.PERSIST_OK
    assert counts(database) == {"strategy_signals": 1, "orders": 1, "risk_state_events": 1,
                                "auto_trade_account_snapshots": 1}
    snapshot = db.load_latest_auto_trade_account_state("nifty-50")
    assert snapshot["quantity"] == 0 and snapshot["square_off_date"] == "2026-09-23"
    order = db.list_orders(source="algoedge.auto_trader", live=False)[0]
    assert order["exitReason"] is not None and order["realizedPnl"] == pytest.approx(10.0)


def test_a_failed_square_off_write_rolls_back_completely(database, dashboard) -> None:
    risk_manager, account, run = dashboard
    account.quantity, account.average_price, account.side = 1, 100.0, "CALL"
    account.last_event_at = FLAT.index[3]
    undo = fail_inserts_into(OrderRecord)
    try:
        response = run(FLAT, SQUARE_OFF_NOW)
    finally:
        undo()
    assert response["persistence"]["error"] == "DATABASE_FAILURE"
    assert counts(database) == dict.fromkeys(counts(database), 0)
    assert account.quantity == 0  # the in-memory square-off itself happened


# ---------------- rows match the existing per-record writers exactly ----------------


def test_atomic_rows_carry_the_same_fields_as_the_per_record_writers(database) -> None:
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    risk_manager.record_trade(realized_pnl=-4.0, now=datetime(2026, 9, 23, 10, 0, tzinfo=IST), is_exit=True)
    account = SimulatedAccount(quantity=1, average_price=101.5, side="PUT", square_off_date="2026-09-22",
                               last_event_at=datetime(2026, 9, 23, 9, 55),
                               contract=OptionContract(trading_symbol="NIFTY26SEP24500PE", underlying="NIFTY",
                                                       right="PE", strike=24500, expiry=date(2026, 9, 30)))
    db.record_risk_snapshot(risk_manager, event="TRADE_RECORDED")
    db.record_auto_trade_account_snapshot("nifty-50", account, event="ENTRY_PUT")
    signal = {"source": "s", "index_id": "nifty-50", "action": "ENTRY_PUT", "reason": "r", "price": 1.0}
    assert db.record_paper_cycle(signal=signal, risk_manager=risk_manager,
                                 account_snapshot=("nifty-50", account, "ENTRY_PUT")) == db.PERSIST_OK

    def columns(row):
        return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in ("id", "created_at")}

    with sessionmaker(bind=database)() as session:
        legacy_risk, atomic_risk = session.query(RiskStateEvent).order_by(RiskStateEvent.id).all()
        legacy_account, atomic_account = session.query(AutoTradeAccountSnapshot).order_by(
            AutoTradeAccountSnapshot.id).all()
        assert columns(legacy_risk) == columns(atomic_risk)
        assert columns(legacy_account) == columns(atomic_account)
