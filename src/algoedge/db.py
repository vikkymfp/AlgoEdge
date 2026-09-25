from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from algoedge.config import Settings, get_settings
from algoedge.models import (
    AlertEvent,
    AutoTradeAccountSnapshot,
    Base,
    BrokerCredential,
    OrderRecord,
    ReconciliationEvent,
    RiskStateEvent,
    SignalRecord,
    SignalStateEvent,
    StrategySignal,
    TokenAuditEvent,
)
from algoedge.risk_manager import IST

logger = logging.getLogger("algoedge.db")

_engine: Any = None
_session_factory: sessionmaker | None = None


def _odbc_connection_url(settings: Settings, database: str) -> str:
    odbc_str = (
        f"DRIVER={{{settings.db_odbc_driver}}};"
        f"SERVER={settings.db_server};"
        f"DATABASE={database};"
        "TrustServerCertificate=yes;"
    )
    if settings.db_trusted_connection:
        odbc_str += "Trusted_Connection=yes;"
    return f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc_str)}"


def _ensure_database_exists(settings: Settings) -> None:
    master_engine = create_engine(_odbc_connection_url(settings, "master"), isolation_level="AUTOCOMMIT")
    try:
        with master_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM sys.databases WHERE name = :name"), {"name": settings.db_name}
            ).fetchone()
            if not exists:
                connection.execute(text(f"CREATE DATABASE [{settings.db_name}]"))
                logger.info("Created database %s", settings.db_name)
    finally:
        master_engine.dispose()


# Columns added to an EXISTING table after it may already have real rows.
# SQLAlchemy's Base.metadata.create_all() only creates missing TABLES, it
# never alters an existing one - this covers that gap with plain,
# idempotent SQL Server DDL rather than pulling in a full migration
# framework for a personal project's evolving schema. Keyed by table name
# so a later phase can add its own entry here without touching this
# function's logic.
_PENDING_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "risk_state_events": {
        "scope": "NVARCHAR(32) NOT NULL DEFAULT 'paper'",
        "kill_switch_reason": "NVARCHAR(255) NULL",
        "consecutive_losses": "INT NOT NULL DEFAULT 0",
        "consecutive_loss_halt": "BIT NOT NULL DEFAULT 0",
        "last_exit_at": "DATETIME2 NULL",
    },
    "orders": {
        "filled_quantity": "INT NULL",
        "remaining_quantity": "INT NULL",
        "expected_price": "FLOAT NULL",
        "slippage": "FLOAT NULL",
        "exit_reason": "NVARCHAR(24) NULL",
    },
}


def _apply_column_migrations(engine: Any) -> None:
    with engine.connect() as connection:
        for table_name, column_definitions in _PENDING_COLUMN_MIGRATIONS.items():
            existing = {
                row[0] for row in connection.execute(
                    text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :table"),
                    {"table": table_name},
                )
            }
            for column_name, type_clause in column_definitions.items():
                if column_name in existing:
                    continue
                connection.execute(text(f"ALTER TABLE [{table_name}] ADD [{column_name}] {type_clause}"))
                connection.commit()
                logger.info("Added column %s.%s", table_name, column_name)


def init_db(settings: Settings | None = None) -> bool:
    """Idempotent setup: creates the database and tables if missing.

    Returns True if persistence is now available, False otherwise. Never
    raises - a missing/unreachable database must not block the app from
    starting or trading, since this is an optional observability layer.
    """
    global _engine, _session_factory
    settings = settings or get_settings()
    if not settings.db_server:
        logger.info("ALGOEDGE_DB_SERVER not set - persistence disabled.")
        return False
    try:
        _ensure_database_exists(settings)
        engine = create_engine(_odbc_connection_url(settings, settings.db_name), pool_pre_ping=True)
        Base.metadata.create_all(engine)
        _apply_column_migrations(engine)
        _engine = engine
        _session_factory = sessionmaker(bind=engine)
        logger.info("Connected to %s / %s", settings.db_server, settings.db_name)
        return True
    except SQLAlchemyError as error:
        logger.warning("Database unavailable, persistence disabled: %s", error)
        _engine = None
        _session_factory = None
        return False


def is_available() -> bool:
    return _session_factory is not None


_last_successful_check_at: datetime | None = None


def check_connection() -> dict:
    """A real, lightweight DB health check - runs an actual `SELECT 1`
    against the live engine right now. Never infers CONNECTED from whether
    the engine object merely exists (that's is_available(), which only
    reflects startup-time configuration and says nothing about whether the
    connection is still good this moment) - System Health needs the real
    thing, not application state standing in for it.

    Never raises. Never returns the raw driver exception (which can echo
    connection-string fragments) - only a fixed, generic message, with the
    real exception logged server-side for whoever is actually debugging it.
    """
    global _last_successful_check_at
    settings = get_settings()
    database_name = settings.db_name if settings.db_server else None

    if _engine is None:
        return {
            "connected": False, "databaseName": database_name,
            "error": "Database not configured or unavailable" if settings.db_server else "Database not configured",
            "lastSuccessfulCheckAt": _last_successful_check_at,
        }
    try:
        with _engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        _last_successful_check_at = datetime.now(IST)
        return {
            "connected": True, "databaseName": database_name, "error": None,
            "lastSuccessfulCheckAt": _last_successful_check_at,
        }
    except SQLAlchemyError as error:
        logger.warning("Database health check failed: %s", error)
        return {
            "connected": False, "databaseName": database_name, "error": "Unable to connect to database",
            "lastSuccessfulCheckAt": _last_successful_check_at,
        }


@contextmanager
def _session_scope():
    """Yields a session for a write, or None if the DB isn't configured/
    reachable. Callers must check for None and just skip persistence rather
    than let a database problem interrupt trading.

    Session creation itself is inside the guarded block (not just
    commit/flush) - a connection that drops between requests must not
    propagate out and interrupt whatever trading operation triggered this
    write.
    """
    if _session_factory is None:
        yield None
        return
    try:
        session = _session_factory()
    except SQLAlchemyError as error:
        logger.warning("Could not open a database session, continuing without persistence: %s", error)
        yield None
        return
    try:
        yield session
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        logger.warning("Database write failed, continuing without persistence: %s", error)
    finally:
        session.close()


def record_signal(
    *,
    source: str,
    index_id: str,
    action: str,
    reason: str,
    price: float | None,
    rsi: float | None = None,
    ema: float | None = None,
    timeframe: str | None = None,
) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(StrategySignal(
            source=source, index_id=index_id, timeframe=timeframe,
            action=action, reason=reason, price=price, rsi=rsi, ema=ema,
        ))


def list_signals(*, source: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Returns signal records as plain dicts (safe after the session closes),
    newest first. Returns an empty list (never raises) if the DB isn't
    configured/reachable."""
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = session.query(StrategySignal)
        if source is not None:
            query = query.filter(StrategySignal.source == source)
        query = query.order_by(StrategySignal.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "createdAt": row.created_at,
                "source": row.source,
                "indexId": row.index_id,
                "timeframe": row.timeframe,
                "action": row.action,
                "reason": row.reason,
                "price": row.price,
                "rsi": row.rsi,
                "ema": row.ema,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load signal history: %s", error)
        return []
    finally:
        if session is not None:
            session.close()


def record_order(
    *,
    source: str,
    live: bool,
    index_id: str | None = None,
    trading_symbol: str | None = None,
    exchange: str | None = None,
    expiry_date: date | None = None,
    strike: int | None = None,
    right: str | None = None,
    side: str | None = None,
    order_type: str | None = None,
    product: str | None = None,
    quantity: int | None = None,
    price: float | None = None,
    trigger_price: float | None = None,
    groww_order_id: str | None = None,
    outcome: str | None = None,
    order_status: str | None = None,
    reason: str | None = None,
    attempts: int | None = None,
    realized_pnl: float | None = None,
    filled_quantity: int | None = None,
    remaining_quantity: int | None = None,
    expected_price: float | None = None,
    slippage: float | None = None,
    exit_reason: str | None = None,
) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(OrderRecord(
            source=source, live=live, index_id=index_id, trading_symbol=trading_symbol,
            exchange=exchange, expiry_date=expiry_date, strike=strike, right=right, side=side,
            order_type=order_type, product=product, quantity=quantity, price=price,
            trigger_price=trigger_price, groww_order_id=groww_order_id, outcome=outcome,
            order_status=order_status, reason=reason, attempts=attempts, realized_pnl=realized_pnl,
            filled_quantity=filled_quantity, remaining_quantity=remaining_quantity,
            expected_price=expected_price, slippage=slippage, exit_reason=exit_reason,
        ))


def record_risk_snapshot(risk_manager: Any, event: str, *, scope: str = "paper") -> None:
    """`scope` keeps real-money and paper-simulation risk budgets from ever
    being conflated - "paper" is the dashboard's Auto Trading account;
    fno_signals' live CLI path uses a distinct scope (e.g.
    "live_fno_signals") so a bad paper day can never block real trading
    capacity, and vice versa."""
    state = risk_manager.state
    with _session_scope() as session:
        if session is None:
            return
        session.add(RiskStateEvent(
            event=event, scope=scope,
            auto_trading_enabled=state.auto_trading_enabled,
            kill_switch=state.kill_switch,
            kill_switch_reason=state.kill_switch_reason,
            trades_today=state.trades_today,
            realized_pnl_today=state.realized_pnl_today,
            trade_day=state.trade_day,
            consecutive_losses=state.consecutive_losses,
            consecutive_loss_halt=state.consecutive_loss_halt,
            last_exit_at=state.last_exit_at,
        ))


def load_latest_risk_state(*, scope: str = "paper") -> dict[str, Any] | None:
    """Returns the most recent risk-state snapshot for this scope as a
    plain dict (safe to use after the session closes), or None if
    unavailable/none recorded yet for that scope."""
    if _session_factory is None:
        return None
    session: Session | None = None
    try:
        session = _session_factory()
        row = (
            session.query(RiskStateEvent)
            .filter(RiskStateEvent.scope == scope)
            .order_by(RiskStateEvent.id.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "auto_trading_enabled": row.auto_trading_enabled,
            "kill_switch": row.kill_switch,
            "kill_switch_reason": row.kill_switch_reason,
            "trades_today": row.trades_today,
            "realized_pnl_today": row.realized_pnl_today,
            "trade_day": row.trade_day,
            "consecutive_losses": row.consecutive_losses,
            "consecutive_loss_halt": row.consecutive_loss_halt,
            "last_exit_at": row.last_exit_at,
        }
    except SQLAlchemyError as error:
        logger.warning("Could not load prior risk state: %s", error)
        return None
    finally:
        if session is not None:
            session.close()


def record_auto_trade_account_snapshot(index_id: str, account: Any, *, event: str) -> None:
    """Persists a paper Auto Trade SimulatedAccount's current state
    (algoedge.order_manager.SimulatedAccount) - callers pass the account
    object itself (duck-typed: cash/quantity/average_price/side/
    last_event_at) rather than importing the class here, to avoid a
    fno_signals/algoedge.order_manager dependency in this module."""
    with _session_scope() as session:
        if session is None:
            return
        session.add(AutoTradeAccountSnapshot(
            index_id=index_id, event=event, cash=account.cash, quantity=account.quantity,
            average_price=account.average_price, side=account.side,
            last_event_at=account.last_event_at,
        ))


def load_latest_auto_trade_account_state(index_id: str) -> dict[str, Any] | None:
    """Returns the most recent paper Auto Trade account snapshot for this
    index as a plain dict (safe to use after the session closes), or None
    if unavailable/none recorded yet - callers must treat None exactly like
    "no prior state" (a fresh flat account), never as an error to surface."""
    if _session_factory is None:
        return None
    session: Session | None = None
    try:
        session = _session_factory()
        row = (
            session.query(AutoTradeAccountSnapshot)
            .filter(AutoTradeAccountSnapshot.index_id == index_id)
            .order_by(AutoTradeAccountSnapshot.id.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "cash": row.cash, "quantity": row.quantity, "average_price": row.average_price,
            "side": row.side, "last_event_at": row.last_event_at,
        }
    except SQLAlchemyError as error:
        logger.warning("Could not load prior auto trade account state for %s: %s", index_id, error)
        return None
    finally:
        if session is not None:
            session.close()


def list_orders(
    *, live: bool | None = None, source: str | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Returns order records as plain dicts (safe after the session closes),
    newest first. Returns an empty list (never raises) if the DB isn't
    configured/reachable - callers should treat that the same as "no orders
    on record" rather than a hard error."""
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = session.query(OrderRecord)
        if live is not None:
            query = query.filter(OrderRecord.live == live)
        if source is not None:
            query = query.filter(OrderRecord.source == source)
        query = query.order_by(OrderRecord.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "createdAt": row.created_at,
                "source": row.source,
                "live": row.live,
                "indexId": row.index_id,
                "tradingSymbol": row.trading_symbol,
                "exchange": row.exchange,
                "expiryDate": row.expiry_date,
                "strike": row.strike,
                "right": row.right,
                "side": row.side,
                "orderType": row.order_type,
                "product": row.product,
                "quantity": row.quantity,
                "price": row.price,
                "triggerPrice": row.trigger_price,
                "growwOrderId": row.groww_order_id,
                "outcome": row.outcome,
                "orderStatus": row.order_status,
                "reason": row.reason,
                "attempts": row.attempts,
                "realizedPnl": row.realized_pnl,
                "filledQuantity": row.filled_quantity,
                "remainingQuantity": row.remaining_quantity,
                "expectedPrice": row.expected_price,
                "slippage": row.slippage,
                "exitReason": row.exit_reason,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load order history: %s", error)
        return []
    finally:
        if session is not None:
            session.close()


def load_broker_credential(broker: str) -> dict[str, Any] | None:
    """Returns the stored credential row as a plain dict (encrypted blobs
    included, still encrypted) or None if unavailable/not yet configured.
    Never raises - callers should treat a missing row the same as
    "nothing stored yet"."""
    if _session_factory is None:
        return None
    session: Session | None = None
    try:
        session = _session_factory()
        row = session.query(BrokerCredential).filter(BrokerCredential.broker == broker).first()
        if row is None:
            return None
        return {
            "broker": row.broker,
            "apiKeyHint": row.api_key_hint,
            "encryptedApiKey": row.encrypted_api_key,
            "encryptedApiSecret": row.encrypted_api_secret,
            "encryptedAccessToken": row.encrypted_access_token,
            "tokenCreatedAt": row.token_created_at,
            "tokenExpiryAt": row.token_expiry_at,
            "lastValidatedAt": row.last_validated_at,
            "lastSuccessfulRequestAt": row.last_successful_request_at,
            "connectionStatus": row.connection_status,
            "lastError": row.last_error,
            "updatedAt": row.updated_at,
        }
    except SQLAlchemyError as error:
        logger.warning("Could not load broker credential: %s", error)
        return None
    finally:
        if session is not None:
            session.close()


def save_broker_credential(broker: str, **fields: Any) -> bool:
    """Upserts the single credential row for this broker. `fields` uses the
    same snake_case names as the BrokerCredential model; only fields
    actually passed are updated, so a partial update (e.g. just the access
    token) never clobbers unrelated columns. Returns whether the write
    succeeded - callers use this to know whether an update actually
    persisted or is in-memory-only for this process."""
    with _session_scope() as session:
        if session is None:
            return False
        row = session.query(BrokerCredential).filter(BrokerCredential.broker == broker).first()
        if row is None:
            row = BrokerCredential(broker=broker)
            session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        return True


def record_token_audit_event(
    *,
    broker: str,
    event: str,
    status: str,
    token_reference: str | None = None,
    error_message: str | None = None,
) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(TokenAuditEvent(
            broker=broker, event=event, status=status,
            token_reference=token_reference, error_message=error_message,
        ))


def list_token_audit_events(*, broker: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Returns token audit history as plain dicts, newest first. Never
    raises - returns an empty list if the DB isn't configured/reachable."""
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = session.query(TokenAuditEvent)
        if broker is not None:
            query = query.filter(TokenAuditEvent.broker == broker)
        query = query.order_by(TokenAuditEvent.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "createdAt": row.created_at,
                "broker": row.broker,
                "event": row.event,
                "tokenReference": row.token_reference,
                "status": row.status,
                "errorMessage": row.error_message,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load token audit history: %s", error)
        return []
    finally:
        if session is not None:
            session.close()


def _signal_record_to_dict(row: SignalRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "signalId": row.signal_id,
        "source": row.source,
        "createdAt": row.created_at,
        "expiresAt": row.expires_at,
        "underlyingSymbol": row.underlying_symbol,
        "underlyingPrice": row.underlying_price,
        "direction": row.direction,
        "optionSymbol": row.option_symbol,
        "optionStrike": row.option_strike,
        "optionType": row.option_type,
        "optionExpiry": row.option_expiry,
        "riskModel": row.risk_model,
        "state": row.state,
        "reason": row.reason,
    }


def create_signal_record(
    *,
    signal_id: str,
    source: str,
    expires_at: datetime,
    underlying_symbol: str,
    underlying_price: float,
    direction: str,
    state: str,
    option_symbol: str | None = None,
    option_strike: int | None = None,
    option_type: str | None = None,
    option_expiry: date | None = None,
    risk_model: str = "OPTION_PREMIUM_BASED",
    reason: str | None = None,
) -> bool:
    """Inserts a new signal row. Returns whether it persisted - False just
    means "no DB configured/reachable right now", never raises. Relies on
    the DB's unique constraint on signal_id as the authoritative duplicate
    guard when a database IS configured; SignalService also checks first so
    duplicate detection still works (in-memory, for that process's
    lifetime) when it isn't."""
    with _session_scope() as session:
        if session is None:
            return False
        session.add(SignalRecord(
            signal_id=signal_id, source=source, expires_at=expires_at,
            underlying_symbol=underlying_symbol, underlying_price=underlying_price,
            direction=direction, option_symbol=option_symbol, option_strike=option_strike,
            option_type=option_type, option_expiry=option_expiry, risk_model=risk_model,
            state=state, reason=reason,
        ))
        return True


def get_signal_by_id(signal_id: str) -> dict[str, Any] | None:
    if _session_factory is None:
        return None
    session: Session | None = None
    try:
        session = _session_factory()
        row = session.query(SignalRecord).filter(SignalRecord.signal_id == signal_id).first()
        return None if row is None else _signal_record_to_dict(row)
    except SQLAlchemyError as error:
        logger.warning("Could not load signal %s: %s", signal_id, error)
        return None
    finally:
        if session is not None:
            session.close()


def update_signal_state(signal_id: str, state: str, *, reason: str | None = None) -> bool:
    with _session_scope() as session:
        if session is None:
            return False
        row = session.query(SignalRecord).filter(SignalRecord.signal_id == signal_id).first()
        if row is None:
            return False
        row.state = state
        if reason is not None:
            row.reason = reason
        return True


def record_signal_state_event(
    *, signal_id: str, from_state: str, to_state: str, reason: str | None = None,
) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(SignalStateEvent(
            signal_id=signal_id, from_state=from_state, to_state=to_state, reason=reason,
        ))


def list_signal_state_events(signal_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = (
            session.query(SignalStateEvent)
            .filter(SignalStateEvent.signal_id == signal_id)
            .order_by(SignalStateEvent.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "signalId": row.signal_id,
                "createdAt": row.created_at,
                "fromState": row.from_state,
                "toState": row.to_state,
                "reason": row.reason,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load signal state history for %s: %s", signal_id, error)
        return []
    finally:
        if session is not None:
            session.close()


def find_open_signal_for(
    *, source: str, underlying_symbol: str, direction: str, in_flight_states: list[str],
) -> dict[str, Any] | None:
    """Returns the most recent signal for this source+symbol+direction that
    is currently in one of `in_flight_states` (ORDER_PENDING through
    EXIT_PENDING) - the duplicate-order check from the spec: a second
    signal for something already being worked must never place a second
    order. Returns None (not an error) when the DB isn't configured -
    callers must decide how to behave when duplicate-order protection
    itself is unavailable."""
    if _session_factory is None:
        return None
    session: Session | None = None
    try:
        session = _session_factory()
        row = (
            session.query(SignalRecord)
            .filter(
                SignalRecord.source == source,
                SignalRecord.underlying_symbol == underlying_symbol,
                SignalRecord.direction == direction,
                SignalRecord.state.in_(in_flight_states),
            )
            .order_by(SignalRecord.id.desc())
            .first()
        )
        return None if row is None else _signal_record_to_dict(row)
    except SQLAlchemyError as error:
        logger.warning("Could not check for an open signal: %s", error)
        return None
    finally:
        if session is not None:
            session.close()


def record_reconciliation_event(
    *, status: str, reason: str | None = None, mismatch_count: int = 0, unconfirmed_count: int = 0,
) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(ReconciliationEvent(
            status=status, reason=reason, mismatch_count=mismatch_count, unconfirmed_count=unconfirmed_count,
        ))


def list_reconciliation_events(*, limit: int | None = None) -> list[dict[str, Any]]:
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = session.query(ReconciliationEvent).order_by(ReconciliationEvent.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "createdAt": row.created_at,
                "status": row.status,
                "reason": row.reason,
                "mismatchCount": row.mismatch_count,
                "unconfirmedCount": row.unconfirmed_count,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load reconciliation history: %s", error)
        return []
    finally:
        if session is not None:
            session.close()


def record_alert_event(*, severity: str, category: str, message: str, source: str) -> None:
    with _session_scope() as session:
        if session is None:
            return
        session.add(AlertEvent(severity=severity, category=category, message=message, source=source))


def list_alert_events(
    *, unacknowledged_only: bool = False, limit: int | None = None,
) -> list[dict[str, Any]]:
    if _session_factory is None:
        return []
    session: Session | None = None
    try:
        session = _session_factory()
        query = session.query(AlertEvent)
        if unacknowledged_only:
            # .is_(False) generates "IS 0", which SQL Server rejects as
            # invalid syntax (found via live testing against the real
            # database, not SQLite - SQLite accepts it, masking the bug in
            # tests). == False generates a portable "= 0"/"= 1" comparison.
            query = query.filter(AlertEvent.acknowledged == False)
        query = query.order_by(AlertEvent.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": row.id,
                "createdAt": row.created_at,
                "severity": row.severity,
                "category": row.category,
                "message": row.message,
                "source": row.source,
                "acknowledged": row.acknowledged,
                "acknowledgedAt": row.acknowledged_at,
            }
            for row in rows
        ]
    except SQLAlchemyError as error:
        logger.warning("Could not load alert history: %s", error)
        return []
    finally:
        if session is not None:
            session.close()


def acknowledge_alert_event(alert_id: int) -> bool:
    with _session_scope() as session:
        if session is None:
            return False
        row = session.query(AlertEvent).filter(AlertEvent.id == alert_id).first()
        if row is None:
            return False
        row.acknowledged = True
        row.acknowledged_at = datetime.now(IST)
        return True


def acknowledge_all_alert_events() -> int:
    with _session_scope() as session:
        if session is None:
            return 0
        count = (
            session.query(AlertEvent)
            .filter(AlertEvent.acknowledged == False)
            .update({"acknowledged": True, "acknowledged_at": datetime.now(IST)}, synchronize_session=False)
        )
        return count
