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
    db_module._last_successful_check_at = None
    yield
    db_module._engine = None
    db_module._session_factory = None
    db_module._last_successful_check_at = None


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


# -- check_connection() - the System Health page's real DB health check ----------------------------------------------


def test_check_connection_reports_not_configured_when_no_engine(monkeypatch) -> None:
    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(db_server=""))

    result = db_module.check_connection()

    assert result["connected"] is False
    assert result["databaseName"] is None
    assert "not configured" in result["error"].lower()
    assert result["lastSuccessfulCheckAt"] is None


def test_check_connection_runs_a_real_query_and_succeeds(monkeypatch) -> None:
    # A real SQLite engine, not a mock - check_connection() must actually
    # execute SELECT 1 against it, not merely observe that _engine is set
    # (that distinction is the entire point of this function existing
    # instead of reusing is_available()).
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(db_server="localhost", db_name="AlgoEdge"))

    result = db_module.check_connection()

    assert result["connected"] is True
    assert result["databaseName"] == "AlgoEdge"
    assert result["error"] is None
    assert result["lastSuccessfulCheckAt"] is not None
    engine.dispose()


def test_check_connection_returns_a_generic_error_never_the_raw_exception(monkeypatch) -> None:
    class BrokenEngine:
        def connect(self):
            # A realistic driver error can contain connection-string
            # fragments - this must never reach the returned result.
            raise OperationalError(
                "connect", {}, Exception("Login failed for user 'sa': password='hunter2' host=10.0.0.5")
            )

    monkeypatch.setattr(db_module, "_engine", BrokenEngine())
    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(db_server="10.0.0.5", db_name="AlgoEdge"))

    result = db_module.check_connection()

    assert result["connected"] is False
    assert result["error"] == "Unable to connect to database"
    assert "hunter2" not in result["error"]
    assert "10.0.0.5" not in result["error"]


def test_check_connection_keeps_last_successful_time_after_a_later_failure(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(db_server="localhost", db_name="AlgoEdge"))
    first = db_module.check_connection()
    assert first["connected"] is True
    engine.dispose()

    class BrokenEngine:
        def connect(self):
            raise OperationalError("connect", {}, Exception("connection dropped"))

    monkeypatch.setattr(db_module, "_engine", BrokenEngine())
    second = db_module.check_connection()

    assert second["connected"] is False
    assert second["lastSuccessfulCheckAt"] == first["lastSuccessfulCheckAt"]
