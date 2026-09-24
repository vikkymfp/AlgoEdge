import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from algoedge import db as db_module
from algoedge.config import Settings
from algoedge.models import Base, OrderRecord, StrategySignal
from algoedge.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def reset_db_module_state():
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


def test_init_db_disabled_when_no_server_configured() -> None:
    settings = Settings(db_server="")

    result = db_module.init_db(settings)

    assert result is False
    assert db_module.is_available() is False


def test_record_signal_does_not_raise_when_db_unavailable() -> None:
    db_module.record_signal(source="test", index_id="nifty-50", action="BUY", reason="x", price=100.0)


def test_record_order_does_not_raise_when_db_unavailable() -> None:
    db_module.record_order(source="test", live=False)


def test_record_risk_snapshot_does_not_raise_when_db_unavailable() -> None:
    db_module.record_risk_snapshot(RiskManager(), event="SNAPSHOT")


def test_load_latest_risk_state_returns_none_when_unavailable() -> None:
    assert db_module.load_latest_risk_state() is None


@pytest.fixture
def sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db_module._session_factory = factory
    yield factory
    engine.dispose()


def test_record_signal_persists_a_row(sqlite_session_factory) -> None:
    db_module.record_signal(
        source="algoedge.auto_trader", index_id="nifty-50", action="BUY",
        reason="RSI above 55", price=24500.0, rsi=60.1, ema=24400.0,
    )

    session = sqlite_session_factory()
    rows = session.query(StrategySignal).all()
    session.close()
    assert len(rows) == 1
    assert rows[0].action == "BUY"
    assert rows[0].index_id == "nifty-50"
    assert rows[0].rsi == pytest.approx(60.1)


def test_record_order_persists_a_row(sqlite_session_factory) -> None:
    db_module.record_order(
        source="fno_signals", live=True, index_id="nifty-50", trading_symbol="NIFTY26SEP24500CE",
        exchange="NSE", strike=24500, right="CE", side="BUY", order_type="MARKET", product="NRML",
        quantity=65, groww_order_id="gid-1", outcome="SUCCESS", order_status="EXECUTED", attempts=1,
    )

    session = sqlite_session_factory()
    rows = session.query(OrderRecord).all()
    session.close()
    assert len(rows) == 1
    assert rows[0].live is True
    assert rows[0].groww_order_id == "gid-1"
    assert rows[0].outcome == "SUCCESS"
    assert rows[0].quantity == 65


def test_list_orders_returns_newest_first(sqlite_session_factory) -> None:
    db_module.record_order(source="fno_signals", live=True, trading_symbol="A", outcome="SUCCESS")
    db_module.record_order(source="algoedge.auto_trader", live=False, trading_symbol="B", outcome="SUCCESS")

    rows = db_module.list_orders()

    assert [row["tradingSymbol"] for row in rows] == ["B", "A"]


def test_list_orders_filters_by_live_flag(sqlite_session_factory) -> None:
    db_module.record_order(source="fno_signals", live=True, trading_symbol="A", outcome="SUCCESS")
    db_module.record_order(source="algoedge.auto_trader", live=False, trading_symbol="B", outcome="SUCCESS")

    rows = db_module.list_orders(live=True)

    assert [row["tradingSymbol"] for row in rows] == ["A"]


def test_list_orders_filters_by_source(sqlite_session_factory) -> None:
    db_module.record_order(source="fno_signals", live=True, trading_symbol="A", outcome="SUCCESS")
    db_module.record_order(source="algoedge.auto_trader", live=False, trading_symbol="B", outcome="SUCCESS")

    rows = db_module.list_orders(source="fno_signals")

    assert [row["tradingSymbol"] for row in rows] == ["A"]


def test_list_orders_respects_limit(sqlite_session_factory) -> None:
    for symbol in ("A", "B", "C"):
        db_module.record_order(source="fno_signals", live=True, trading_symbol=symbol, outcome="SUCCESS")

    rows = db_module.list_orders(limit=2)

    assert len(rows) == 2
    assert [row["tradingSymbol"] for row in rows] == ["C", "B"]


def test_list_orders_returns_empty_list_when_db_unavailable() -> None:
    assert db_module.list_orders() == []


def test_record_risk_snapshot_persists_current_state(sqlite_session_factory) -> None:
    manager = RiskManager()
    manager.enable_auto_trading()
    manager.record_trade(realized_pnl=150.0)

    db_module.record_risk_snapshot(manager, event="SNAPSHOT")

    state = db_module.load_latest_risk_state()
    assert state["auto_trading_enabled"] is True
    assert state["trades_today"] == 1
    assert state["realized_pnl_today"] == pytest.approx(150.0)


def test_load_latest_risk_state_returns_the_most_recent_event(sqlite_session_factory) -> None:
    manager = RiskManager()
    db_module.record_risk_snapshot(manager, event="DISABLE")
    manager.enable_auto_trading()
    db_module.record_risk_snapshot(manager, event="ENABLE")

    state = db_module.load_latest_risk_state()

    assert state["auto_trading_enabled"] is True


def test_record_signal_swallows_a_dropped_connection_on_session_creation(monkeypatch) -> None:
    # A connection that drops between requests raises when a *new* session
    # is opened, not just on commit - this must never escape and interrupt
    # whatever trading operation triggered the write.
    def broken_factory():
        raise OperationalError("connect", {}, Exception("connection dropped"))

    monkeypatch.setattr(db_module, "_session_factory", broken_factory)

    db_module.record_signal(source="test", index_id="nifty-50", action="BUY", reason="x", price=100.0)


def test_load_latest_risk_state_returns_none_on_a_dropped_connection(monkeypatch) -> None:
    def broken_factory():
        raise OperationalError("connect", {}, Exception("connection dropped"))

    monkeypatch.setattr(db_module, "_session_factory", broken_factory)

    assert db_module.load_latest_risk_state() is None
