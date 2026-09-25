from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from algoedge.market_pulse import TIMEFRAMES
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, OrderResult, SimulatedAccount
from algoedge.risk_manager import IST, RiskDecision, RiskManager
from fno_signals.config import INDEX_MAP, StrategyConfig, strategy_config_for
from fno_signals.main import fetch_underlying_data
from fno_signals.strategy import TradeEvent
from fno_signals.strategy import run as run_strategy

logger = logging.getLogger("algoedge.auto_trader")

# Maps algoedge's kebab-case index ids (algoedge.market_pulse.INDEX_DEFINITIONS,
# used throughout the dashboard/web_server) to fno_signals' numeric IndexConfig
# choice (fno_signals.config.INDEX_MAP) - the same three underlyings, under
# two different existing key schemes that predate this module sharing a
# strategy engine with fno_signals.
_INDEX_CHOICE = {"nifty-50": 1, "bank-nifty": 2, "sensex": 3}

# fno_signals.strategy.TradeEvent.kind values that represent an entry
# (opening a paper CALL/PUT position) vs an exit (closing one).
_ENTRY_KINDS = {"ENTRY_CALL", "ENTRY_PUT"}
_EXIT_KINDS = {"EXIT_SL", "EXIT_TARGET"}


@dataclass(frozen=True)
class AutoTradeCycleResult:
    event: TradeEvent | None
    risk: RiskDecision
    order: OrderResult | None


def run_cycle(
    index_id: str,
    timeframe: str,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    config: StrategyConfig | None = None,
    quantity: int = 1,
    now: datetime | None = None,
    total_open_positions: int | None = None,
    resolve_contract_fn: Callable[[TradeEvent], OptionContract | None] | None = None,
) -> AutoTradeCycleResult:
    """One full pass of Auto Trading's paper flow, using the exact same
    signal engine as Backtest and `fno_signals --live`:

    fno_signals.strategy.run() -> Risk Manager -> Order Manager (paper only).

    Auto Trading never places a real Groww order - `OrderManager` here is
    always wired to a `SimulatedAccount`. Using the canonical strategy
    means Auto Trade's paper fills are directly comparable to Backtest's
    results for the same OHLCV window (see the signal-parity test in
    tests/test_auto_trader.py) - previously Auto Trade ran a separate,
    simpler RSI+EMA long-only strategy (`algoedge.strategy_engine`) that
    shared no code with Backtest/Pine and couldn't represent PUT signals
    at all.

    `total_open_positions` lets a caller running several indices against
    independent `SimulatedAccount`s (one per index) enforce a single,
    global `max_open_positions` limit across all of them. Defaults to just
    this cycle's own account when not given, matching the original
    single-account behaviour.

    Forced end-of-day square-off (Phase 4): once `risk_manager.limits.
    square_off_time` is reached within the trading session, any open
    position is closed unconditionally - this check runs before any
    strategy event is even looked at, so it fires even on a cycle with no
    new ENTRY/EXIT signal, and it takes precedence over a pending event on
    a cycle where both would otherwise apply. It deliberately bypasses
    `risk_manager.check()`'s gates (kill switch, auto-trading-enabled,
    daily loss limit, etc.) - those gate taking on NEW risk, not this
    mandatory risk-reducing close, the same way a real broker's SEBI-
    mandated square-off isn't something a trading bot's own kill switch
    can suppress. It still calls `risk_manager.record_trade()` so P&L/
    trade-count/consecutive-loss accounting stays correct.

    Option contract resolution (Phase 5): `resolve_contract_fn`, if given,
    is called for a genuinely new ENTRY event only (never for an exit or
    square-off, which only ever close whatever contract the position was
    already opened against) and must return a validated
    `algoedge.option_contract.OptionContract`, or `None` if nothing could
    be resolved. `None` (or `resolve_contract_fn` itself being `None`, the
    default) refuses the entry outright - `OrderManager.place_event()` is
    never called for an unresolved/ambiguous contract. This module never
    performs the resolution itself (and has no growwapi/TokenService
    dependency to do so) - the actual instrument-master lookup lives in
    whatever the caller passes in (see web_server.py), keeping Auto Trade
    exactly as broker-import-free as it was before this phase.
    """
    if index_id not in _INDEX_CHOICE:
        raise ValueError(f"Unsupported index_id for Auto Trade: {index_id}")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe for Auto Trade: {timeframe}")

    now = now or datetime.now(IST)
    index_config = INDEX_MAP[_INDEX_CHOICE[index_id]]
    strategy_config = config or strategy_config_for(index_config)

    period, interval = TIMEFRAMES[timeframe]
    data = fetch_underlying_data(index_config.ticker, period=period, interval=interval)
    _results, events = run_strategy(data, strategy_config, underlying_label=index_config.name)

    account = order_manager.account
    own_open_position = 1 if account.quantity > 0 else 0
    open_positions = total_open_positions if total_open_positions is not None else own_open_position

    limits = risk_manager.limits
    today = now.date().isoformat()
    already_squared_off_today = account.square_off_date == today
    in_session = limits.trading_start <= now.time() <= limits.trading_end
    square_off_due = (
        in_session and not already_squared_off_today
        and now.time() >= limits.square_off_time
        and account.quantity > 0
    )

    if square_off_due:
        current_price = float(data["Close"].iloc[-1])
        square_off_event = TradeEvent(
            timestamp=data.index[-1], kind="SQUARE_OFF", underlying_price=current_price,
            option_symbol=None, stop_loss=None, target=None, exit_level=current_price,
        )
        close_quantity = account.quantity
        order_result = order_manager.place_event(
            "SQUARE_OFF", current_price, close_quantity, index_id=index_id
        )
        if order_result.status == "PLACED":
            risk_manager.record_trade(realized_pnl=order_result.realized_pnl, now=now, is_exit=True)
            # Both advanced together, atomically with the fill above - a
            # persistence failure of the *durable* snapshot elsewhere
            # (algoedge.db, always fail-safe/never-raising) can never roll
            # these back, so a restart always resumes from a state that is
            # at worst stale, never one that contradicts what actually
            # happened in this process.
            account.last_event_at = square_off_event.timestamp
            account.square_off_date = today
            return AutoTradeCycleResult(
                square_off_event, RiskDecision(True, "Forced end-of-day square-off"), order_result
            )
        # A FAILED fill (e.g. a genuine race where quantity reached 0
        # between the check above and the fill itself) must never mark
        # square_off_date - it has to remain eligible to retry.
        return AutoTradeCycleResult(square_off_event, RiskDecision(False, order_result.detail), order_result)

    if not events:
        return AutoTradeCycleResult(None, RiskDecision(False, "No actionable signal"), None)

    # fno_signals.strategy.run() recomputes the ENTIRE window from scratch
    # every call - it has no memory of what a previous cycle already acted
    # on. account.last_event_at is the high-water mark of the last event
    # actually PLACED (see order_manager.py); anything not newer than it
    # has already been processed. Picking the OLDEST unprocessed event
    # (not simply the newest one in the window) is what makes this
    # chronological and loss-free: if two real events land between polls
    # (e.g. an exit immediately followed by a reversal entry), this cycle
    # processes only the first of them and a later cycle picks up the
    # second - never silently skipping the intermediate one the way always
    # jumping straight to events[-1] would.
    unprocessed = (
        events if account.last_event_at is None
        else [e for e in events if e.timestamp > account.last_event_at]
    )
    if not unprocessed:
        return AutoTradeCycleResult(events[-1], RiskDecision(False, "Signal already processed (duplicate)"), None)
    event = unprocessed[0]

    if event.kind in _ENTRY_KINDS and already_squared_off_today:
        # Today's forced square-off already happened - a stale ENTRY event
        # from the same recomputed window must never reopen the position
        # that was deliberately closed for the day.
        return AutoTradeCycleResult(
            event, RiskDecision(False, "Square-off already occurred for today - no new entries"), None
        )

    contract: OptionContract | None = None
    if event.kind in _ENTRY_KINDS:
        # A new position may only ever open against a validated contract -
        # never OrderManager first, resolution second. A soft failure here
        # (resolver unavailable, no match this cycle) behaves exactly like
        # any other blocked signal: last_event_at is not advanced, so it
        # remains eligible to retry on a later cycle.
        if resolve_contract_fn is None:
            return AutoTradeCycleResult(
                event, RiskDecision(False, "Option contract resolution unavailable"), None
            )
        contract = resolve_contract_fn(event)
        if contract is None:
            return AutoTradeCycleResult(event, RiskDecision(False, "No matching option contract found"), None)

    # RiskManager.check()/record_trade() only understand BUY/SELL (kept
    # unchanged, per the canonical-strategy unification's own scope) - an
    # entry (CALL or PUT) always "opens"/uses capital like a BUY, an exit
    # (SL or target) always "closes"/realizes P&L like a SELL. The actual
    # CALL/PUT accounting happens in OrderManager.place_event() below,
    # driven by event.kind directly, not this simplification.
    action = "BUY" if event.kind in _ENTRY_KINDS else "SELL"
    order_value = quantity * event.underlying_price
    decision = risk_manager.check(action, quantity, open_positions, now=now, order_value=order_value)
    if not decision.allowed:
        return AutoTradeCycleResult(event, decision, None)

    order_result = order_manager.place_event(
        event.kind, event.underlying_price, quantity, index_id=index_id, contract=contract
    )
    if order_result.status == "PLACED":
        is_exit = event.kind in _EXIT_KINDS
        risk_manager.record_trade(realized_pnl=order_result.realized_pnl, now=now, is_exit=is_exit)
        # Deliberately only advanced on a successful fill, not merely on
        # having "seen" the event - a signal blocked by a risk gate this
        # cycle (e.g. auto trading briefly disabled) must still be
        # eligible to fire on a later cycle once the gate reopens.
        account.last_event_at = event.timestamp
    return AutoTradeCycleResult(event, decision, order_result)


def restore_account_state(account: SimulatedAccount, snapshot: dict[str, Any]) -> None:
    """Applies a previously-persisted paper account snapshot (see
    `algoedge.db.load_latest_auto_trade_account_state()`) onto a fresh
    `SimulatedAccount`, restoring an open position and the event-dedup
    high-water mark (`last_event_at`) across a process restart - without
    this, `run_cycle()`'s duplicate-signal check would have no memory of
    what was already filled before the restart, and could re-fill it.

    Fails safe on malformed/unexpected data: never raises, and leaves
    `account` at its safe, already-flat defaults if the snapshot can't be
    applied cleanly - a corrupted persistence row must never crash startup
    or silently open a position from garbage data. Deliberately does NOT
    force-flatten a genuinely open position just because the process
    restarted - only `run_cycle()`'s own square-off check does that, on
    its own schedule, exactly as if the process had never restarted. If a
    position was already squared off before the restart, `square_off_date`
    restoring correctly prevents `run_cycle()` from doing it again today.

    `contract` (Phase 5), if present in `snapshot`, is restored as-is -
    NEVER re-resolved against the instrument master. The position was
    already validated once, when it was opened; re-resolving on every
    restart would be both unnecessary (per this phase's own requirement)
    and would risk a *different* contract being picked if the instrument
    master has since rolled to a new expiry.
    """
    try:
        cash = float(snapshot["cash"])
        quantity = int(snapshot["quantity"])
        average_price = snapshot["average_price"]
        side = snapshot["side"]
        last_event_at = snapshot["last_event_at"]
        square_off_date = snapshot.get("square_off_date")
        contract_data = snapshot.get("contract")
        if average_price is not None:
            average_price = float(average_price)
        if side is not None and side not in ("CALL", "PUT"):
            raise ValueError(f"unexpected side {side!r}")
        if square_off_date is not None and not isinstance(square_off_date, str):
            raise ValueError(f"unexpected square_off_date {square_off_date!r}")
        if last_event_at is not None and getattr(last_event_at, "tzinfo", "missing") is None:
            # SQL Server's DATETIME2 carries no UTC offset - every other
            # persisted/restored timestamp in this app assumes IST
            # (algoedge.risk_manager.IST) when it comes back naive.
            last_event_at = last_event_at.replace(tzinfo=IST)
        contract: OptionContract | None = None
        if contract_data is not None:
            contract = OptionContract(
                trading_symbol=str(contract_data["trading_symbol"]),
                underlying=str(contract_data["underlying"]),
                right=str(contract_data["right"]),
                strike=int(contract_data["strike"]),
                expiry=contract_data["expiry"],
                instrument_id=contract_data.get("instrument_id"),
            )
    except (KeyError, TypeError, ValueError) as error:
        logger.warning("Could not restore auto trade account state, starting flat instead: %s", error)
        return

    account.cash = cash
    account.quantity = quantity
    account.average_price = average_price
    account.side = side
    account.last_event_at = last_event_at
    account.square_off_date = square_off_date
    account.contract = contract
