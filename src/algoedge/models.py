from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, LargeBinary, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StrategySignal(Base):
    """Every BUY/SELL/HOLD signal the strategy engines generate, whether or
    not it ever became an order - kept for later backtesting/analysis."""

    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    source: Mapped[str] = mapped_column(String(64))  # e.g. "algoedge.auto_trader", "fno_signals"
    index_id: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str] = mapped_column(String(16))  # BUY | SELL | HOLD
    reason: Mapped[str] = mapped_column(String(255))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema: Mapped[float | None] = mapped_column(Float, nullable=True)


class OrderRecord(Base):
    """Every order placed - paper (algoedge Auto Trading) or real
    (fno_signals --live, or dashboard Manual Trading)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    source: Mapped[str] = mapped_column(String(64))
    live: Mapped[bool] = mapped_column(Boolean, default=False)  # real Groww order vs paper simulation
    index_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trading_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(8), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    strike: Mapped[int | None] = mapped_column(Integer, nullable=True)
    right: Mapped[str | None] = mapped_column(String(4), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    product: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    groww_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # SUCCESS|PARTIAL|FAILED|CANCELLED|TIMEOUT|UNKNOWN|PLACED
    order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # `quantity` above is the REQUESTED quantity - these two make a
    # partial fill impossible to silently miss (spec §17-18: "never treat
    # a partially-filled order as fully filled").
    filled_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Slippage tracking (§15): only ever populated when a real pre-trade
    # reference price was known (a LIMIT/SL/SL_M order's own submitted
    # price) - null for MARKET orders, where no such price exists on this
    # account's Groww tier. Never guessed.
    expected_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Formal exit categorization (§19) - only set on the order that closes
    # a position. STOP_LOSS|TARGET|REVERSAL|TIME_BASED|MANUAL|RISK|
    # KILL_SWITCH|END_OF_SESSION. Distinct from `reason` above, which
    # explains the ORDER PIPELINE's outcome (e.g. a rejection), not why a
    # position was strategically closed.
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)


class RiskStateEvent(Base):
    """Append-only audit log of Risk Manager state changes. The most recent
    row is also used to restore RiskManager's in-memory state on startup, so
    the daily-loss/trades-today counters and kill switch survive a restart
    instead of silently resetting."""

    __tablename__ = "risk_state_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    event: Mapped[str] = mapped_column(String(32))  # ENABLE|DISABLE|KILL_SWITCH_ON|KILL_SWITCH_OFF|SNAPSHOT
    # "paper" = the dashboard's Auto Trading simulated account; a distinct
    # value per real-money source (e.g. "live_fno_signals") keeps real and
    # paper daily-loss/trades-per-day/consecutive-loss budgets from ever
    # being conflated - a paper loss must never block real trading capacity
    # and vice versa.
    scope: Mapped[str] = mapped_column(String(32), default="paper")
    auto_trading_enabled: Mapped[bool] = mapped_column(Boolean)
    kill_switch: Mapped[bool] = mapped_column(Boolean)
    kill_switch_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trades_today: Mapped[int] = mapped_column(Integer)  # every fill (entries, exits, square-offs)
    # Paper Auto Trade's successful NEW ENTRY fills today (its trade cap's
    # unit). NULL in rows saved before this column existed.
    entries_today: Mapped[int | None] = mapped_column(Integer, nullable=True)
    realized_pnl_today: Mapped[float] = mapped_column(Float)
    trade_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_loss_halt: Mapped[bool] = mapped_column(Boolean, default=False)
    last_exit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AutoTradeAccountSnapshot(Base):
    """Append-only audit log of a paper Auto Trade SimulatedAccount's state
    (algoedge.order_manager) - one row per change. The most recent row per
    index_id also restores an OrderManager's SimulatedAccount on startup,
    so an open paper position and the event-dedup high-water mark
    (last_event_at) survive a process restart instead of silently
    resetting to flat - see algoedge.auto_trader.run_cycle()'s duplicate-
    signal handling, which depends on last_event_at surviving a restart to
    stay correct."""

    __tablename__ = "auto_trade_account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    index_id: Mapped[str] = mapped_column(String(32))  # which OrderManager this snapshot belongs to
    event: Mapped[str] = mapped_column(String(32))  # ENTRY_CALL|ENTRY_PUT|EXIT_SL|EXIT_TARGET|SQUARE_OFF|SNAPSHOT
    cash: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)  # CALL | PUT | None
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ISO date (YYYY-MM-DD) of the last forced end-of-day square-off, if
    # any - restoring this on startup is what stops a restart from either
    # re-squaring-off an already-closed day or letting a stale ENTRY
    # reopen a position that was deliberately closed for the day.
    square_off_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The instrument-master-validated OptionContract (Phase 5,
    # algoedge.option_contract) this open position was resolved against -
    # null while flat. Restoring these on startup is what lets an
    # already-open position survive a restart WITHOUT re-resolving a
    # contract (a re-resolution could legitimately pick a different one if
    # the instrument master has since rolled to a new expiry).
    contract_trading_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contract_right: Mapped[str | None] = mapped_column(String(4), nullable=True)
    contract_strike: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_instrument_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PaperDecisionEvent(Base):
    """Append-only audit of paper Auto Trade decisions that did NOT become a
    fill: a strategy event blocked by a risk/control rule, expired as stale,
    an exit skipped for lack of a position, a failed paper fill, or entries
    held back while a missed square-off waits for today's data. Fills are
    already recorded as orders; routine "no new signal" cycles are not
    audited. Holds only non-sensitive paper context - never credentials,
    tokens or broker data."""

    __tablename__ = "paper_decision_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    index_id: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32))  # BLOCKED|EXPIRED|SKIPPED|ORDER_FAILED|SQUARE_OFF_PENDING
    reason: Mapped[str] = mapped_column(String(255))  # the engine's own reason text, verbatim
    event_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ENTRY_CALL|... or None
    event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # the event's bar
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Risk/control context at decision time (RiskManager state + this index's position).
    auto_trading_enabled: Mapped[bool] = mapped_column(Boolean)
    kill_switch: Mapped[bool] = mapped_column(Boolean)
    consecutive_loss_halt: Mapped[bool] = mapped_column(Boolean)
    trades_today: Mapped[int] = mapped_column(Integer)  # every fill today
    entries_today: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the paper entry cap's count
    realized_pnl_today: Mapped[float] = mapped_column(Float)
    open_quantity: Mapped[int] = mapped_column(Integer)


class BrokerCredential(Base):
    """One row per broker (currently just "groww"), upserted in place -
    holds encrypted secrets and token metadata for the API Management page.

    Raw API keys/secrets/tokens are NEVER stored here in plaintext - only
    Fernet-encrypted bytes (see credential_manager.py). If
    ALGOEDGE_CREDENTIAL_ENCRYPTION_KEY isn't configured, this table simply
    isn't written to and credentials stay .env-only/in-memory for that
    process's lifetime - matches the rest of this app's "DB is an optional
    layer, never a requirement" philosophy."""

    __tablename__ = "broker_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker: Mapped[str] = mapped_column(String(32), unique=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)  # e.g. "gw...ABCD", for display only
    encrypted_api_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encrypted_api_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_expiry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connection_status: Mapped[str] = mapped_column(String(24), default="MISSING")
    # MISSING | CONNECTED | TOKEN_EXPIRED | DISCONNECTED | ERROR
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TokenAuditEvent(Base):
    """Append-only audit log for credential/token changes and validation
    attempts - the "Token History" section. Never holds a raw secret/token,
    only a short trailing-character reference (see
    credential_manager.reference_hint) so history stays useful without
    being a second place a leaked secret could be recovered from."""

    __tablename__ = "token_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    broker: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(32))
    # CREDENTIALS_UPDATED | TOKEN_UPDATED | VALIDATION_SUCCESS | VALIDATION_FAILED |
    # AUTO_REFRESH_SUCCESS | AUTO_REFRESH_FAILED
    token_reference: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SignalRecord(Base):
    """One row per distinct trading signal (deterministic signal_id, not a
    random UUID - see signal_pipeline.py) - the spec's demand for
    idempotency only means something if the same underlying signal always
    produces the same signal_id. Tracks the signal's current lifecycle
    state; the full transition history lives in SignalStateEvent below.

    Deliberately separate from the existing StrategySignal table:
    StrategySignal is a lightweight "what did the strategy think" log used
    for backtesting/analysis; SignalRecord is the load-bearing entity that
    duplicate-detection, expiry, and order placement are gated on."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(128), unique=True)
    source: Mapped[str] = mapped_column(String(64))  # e.g. "fno_signals", "algoedge.auto_trader"
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    underlying_symbol: Mapped[str] = mapped_column(String(32))
    underlying_price: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(16))  # CALL | PUT | FLAT
    option_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    option_strike: Mapped[int | None] = mapped_column(Integer, nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(4), nullable=True)  # CE | PE
    option_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    risk_model: Mapped[str] = mapped_column(String(24), default="OPTION_PREMIUM_BASED")
    # UNDERLYING_BASED | OPTION_PREMIUM_BASED
    state: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SignalStateEvent(Base):
    """Append-only audit trail of every state transition a signal made -
    "never skip a transition" is enforced in signal_state.py; this table is
    what makes that enforcement inspectable after the fact, and is the
    SignalId-traceable chain the spec's SQL audit trail section asks for."""

    __tablename__ = "signal_state_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    from_state: Mapped[str] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ReconciliationEvent(Base):
    """Append-only audit log of every reconciliation check the
    ReconciliationGate ran - the spec's "create a reconciliation event"
    requirement whenever a mismatch (or an unconfirmed order) blocks new
    orders. Also records the clean checks (status="OK"), so the history
    shows the gate was actually run, not just when it found a problem."""

    __tablename__ = "reconciliation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16))  # UNKNOWN | OK | MISMATCH | UNCONFIRMED
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mismatch_count: Mapped[int] = mapped_column(Integer, default=0)
    unconfirmed_count: Mapped[int] = mapped_column(Integer, default=0)


class AlertEvent(Base):
    """The spec's Alerts section (§29), in-app only per explicit user
    choice - no email/SMS/messaging integration exists in this app, so an
    "alert" is a persisted row here plus a dashboard banner for anything
    unacknowledged, not an external notification."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    severity: Mapped[str] = mapped_column(String(16))  # INFO | WARNING | CRITICAL
    category: Mapped[str] = mapped_column(String(32))
    # ORDER_REJECTED | ORDER_FAILED | BROKER_DISCONNECTED | POSITION_MISMATCH |
    # DAILY_LOSS_LIMIT_REACHED | KILL_SWITCH_ACTIVATED | UNEXPECTED_POSITION |
    # UNEXPECTED_ORDER | WEBHOOK_AUTH_FAILURE | DATABASE_FAILURE |
    # TRADING_HALTED | SYSTEM_RESTART | PAPER_CYCLE_FAILURE
    message: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64))  # which process/module raised it
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
