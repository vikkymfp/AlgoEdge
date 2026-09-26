from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from algoedge.market_pulse import TIMEFRAMES
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, OrderResult, SimulatedAccount
from algoedge.risk_manager import IST, RiskDecision, RiskManager
from fno_signals.config import INDEX_MAP, StrategyConfig, strategy_config_for
from fno_signals.main import fetch_underlying_data
from fno_signals.strategy import (
    OpenPosition,
    TradeEvent,
    compute_indicators,
    drop_invalid_bars,
    risk_distances,
)
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

# yfinance interval string -> bar length. A strategy event is stamped with
# its bar's START time (yfinance's convention); it becomes actionable when
# that bar CLOSES, i.e. at timestamp + bar length.
_BAR_LENGTH = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1), "1d": timedelta(days=1),
}

# Freshness rule for paper fills: an event may only be acted on within this
# many bar lengths after its bar closed. For the dashboard's 5m timeframe
# polled every 300s (web_server.SCHEDULER_TICK_SECONDS) that is 10 minutes -
# the tick that first sees the closed bar, plus one missed/late tick or
# delayed data. Anything older is stale: its signal price no longer
# reflects the market, so it is never filled at that price.
SIGNAL_FRESHNESS_BARS = 2


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

    Missed square-off recovery (Phase 7): a position still open from an
    earlier trading day (its entry bar's IST date is before today) missed
    that day's square-off; it is closed first, on the first in-session
    cycle whose data has a bar from today, at today's latest close - see
    `_stale_position_day()`. No strategy event (so no new entry) is
    processed for that index until it is.

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

    Signal freshness: an event older than `SIGNAL_FRESHNESS_BARS` bar
    lengths after its bar closed is stale. A stale ENTRY (or a stale EXIT
    with no paper position to close) is expired - marked processed and
    never filled. A stale EXIT while a paper position is still open is a
    late exit: the position is still closed (never left orphaned), but at
    the current price, never the obsolete SL/target level. All stale events
    ahead of the first actionable one are expired in the same cycle, so a
    backlog (e.g. after a restart) never trickles through one per tick.

    Exit pricing: a fresh EXIT_SL/EXIT_TARGET fills at the event's
    `exit_level` - the SL/target price itself - exactly like the canonical
    backtest (algoedge.backtest.pair_trades), not at the exit bar's close.
    A forced square-off still fills at the latest close.

    Invalid OHLC bars (see fno_signals.strategy.drop_invalid_bars) are
    dropped before anything reads a price, so a NaN bar can never become
    a fill price.
    """
    if index_id not in _INDEX_CHOICE:
        raise ValueError(f"Unsupported index_id for Auto Trade: {index_id}")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe for Auto Trade: {timeframe}")

    now = now or datetime.now(IST)
    index_config = INDEX_MAP[_INDEX_CHOICE[index_id]]
    strategy_config = config or strategy_config_for(index_config)

    period, interval = TIMEFRAMES[timeframe]
    data = drop_invalid_bars(fetch_underlying_data(index_config.ticker, period=period, interval=interval))
    if data.empty:
        return AutoTradeCycleResult(None, RiskDecision(False, "No valid market data"), None)
    current_price = float(data["Close"].iloc[-1])

    account = order_manager.account
    own_open_position = 1 if account.quantity > 0 else 0
    open_positions = total_open_positions if total_open_positions is not None else own_open_position

    limits = risk_manager.limits
    today = now.date().isoformat()
    already_squared_off_today = account.square_off_date == today
    in_session = limits.trading_start <= now.time() <= limits.trading_end

    # Missed square-off recovery: paper never holds overnight, so a position
    # still open whose entry bar is from an EARLIER IST date than today
    # missed that day's 15:20-15:30 square-off (scheduler/data failures, a
    # restart). It is closed - before any strategy event, so no new entry can
    # happen first - on the first in-session cycle whose market data already
    # has a bar from today, at today's latest close; never at a stale
    # prior-day price, which would fabricate a fill.
    stale_since = _stale_position_day(account, now)
    if stale_since is not None:
        latest_bar_day = _as_ist(data.index[-1]).date()
        if not in_session or latest_bar_day != now.date():
            return AutoTradeCycleResult(None, RiskDecision(
                False, f"Open position from {stale_since} missed its square-off - "
                       "waiting for today's in-session market data to close it",
            ), None)
        return _force_close(
            index_id, data, current_price, now, risk_manager, order_manager,
            f"Missed square-off from {stale_since} - closed at today's latest price",
        )
    square_off_due = (
        in_session and not already_squared_off_today
        and now.time() >= limits.square_off_time
        and account.quantity > 0
    )

    if square_off_due:
        # A failed attempt (fetch error, FAILED fill) never marks
        # square_off_date, so every later cycle inside the window retries.
        return _force_close(
            index_id, data, current_price, now, risk_manager, order_manager,
            "Forced end-of-day square-off", square_off_date=today,
        )

    # Position sync: the canonical strategy is evaluated against THIS paper
    # account's real position, not the position it would reconstruct by
    # replaying the whole window. Replaying diverged whenever paper didn't
    # do what the replay assumed - a risk-blocked or expired entry, a forced
    # square-off, a restart - leaving the strategy "in" a phantom trade that
    # suppressed genuine new entries until the phantom exited. So once the
    # account has processed anything, the strategy restarts just after
    # account.last_event_at, seeded with the account's actual position;
    # indicators and setup edges still use the whole window (canonical
    # signals). account.last_event_at is the high-water mark of the last
    # event filled, expired or squared off; anything not newer than it has
    # already been processed. The OLDEST unprocessed event is handled first,
    # so two events landing between polls are taken in order, one per cycle.
    #
    # Signal freshness: an event older than SIGNAL_FRESHNESS_BARS bar
    # lengths after its bar closed is stale. A stale entry (or a stale exit
    # with nothing to close) is expired - marked processed, never filled -
    # and the strategy is re-evaluated from that point, so a genuine later
    # signal the expired one was masking is still found in the same cycle.
    bar_length = _BAR_LENGTH[interval]
    max_age = bar_length * SIGNAL_FRESHNESS_BARS
    event: TradeEvent | None = None
    last_expired: TradeEvent | None = None
    late_exit = False
    expired = 0
    for _ in range(len(data) + 1):  # each pass either returns an event or expires one
        events = _account_synced_events(data, strategy_config, index_config.name, account)
        unprocessed = (
            events if account.last_event_at is None
            else [e for e in events if e.timestamp > account.last_event_at]
        )
        if not unprocessed:
            break
        candidate = unprocessed[0]
        if now - (_as_ist(candidate.timestamp) + bar_length) <= max_age:
            event = candidate
            break
        if candidate.kind in _EXIT_KINDS and account.quantity > 0:
            event, late_exit = candidate, True
            break
        account.last_event_at = candidate.timestamp
        last_expired = candidate
        expired += 1
    if event is None:
        if expired:
            return AutoTradeCycleResult(
                last_expired,
                RiskDecision(False, f"Stale signal expired ({expired} event(s) older than {max_age} after bar close)"),
                None,
            )
        if account.last_event_at is not None:
            return AutoTradeCycleResult(
                None, RiskDecision(False, "Signal already processed (duplicate) - nothing new since the last event"),
                None,
            )
        return AutoTradeCycleResult(None, RiskDecision(False, "No actionable signal"), None)

    if event.kind in _EXIT_KINDS and account.quantity == 0:
        # The strategy replays its own position from the data; this exit
        # belongs to an entry this paper account never filled (blocked or
        # expired). Nothing to close - mark it processed so it can't block
        # every later event for this index.
        account.last_event_at = event.timestamp
        return AutoTradeCycleResult(
            event, RiskDecision(False, "No open paper position for this exit (its entry was never filled)"), None,
        )

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
    # An exit only closes the position this account already holds, so the
    # kill switch / auto-trading-disabled gates (which stop NEW risk) must
    # not block it - see RiskManager.check(risk_reducing=...).
    decision = risk_manager.check(
        action, quantity, open_positions, now=now, order_value=order_value, risk_reducing=action == "SELL",
    )
    if not decision.allowed:
        return AutoTradeCycleResult(event, decision, None)

    if event.kind in _EXIT_KINDS:
        fill_price = current_price if late_exit else float(event.exit_level)
        if late_exit:
            decision = RiskDecision(True, "Late exit - signal past its freshness window, filled at current price")
    else:
        fill_price = event.underlying_price
    order_result = order_manager.place_event(
        event.kind, fill_price, quantity, index_id=index_id, contract=contract
    )
    if order_result.status == "PLACED":
        is_exit = event.kind in _EXIT_KINDS
        if not is_exit:
            # The canonical levels this position will exit on (see
            # _account_synced_events); cleared by fill_event() when flat.
            account.stop_loss, account.target = event.stop_loss, event.target
        risk_manager.record_trade(realized_pnl=order_result.realized_pnl, now=now, is_exit=is_exit)
        # Deliberately only advanced on a successful fill, not merely on
        # having "seen" the event - a signal blocked by a risk gate this
        # cycle (e.g. auto trading briefly disabled) must still be
        # eligible to fire on a later cycle once the gate reopens.
        account.last_event_at = event.timestamp
    return AutoTradeCycleResult(event, decision, order_result)


def _force_close(
    index_id: str, data: pd.DataFrame, current_price: float, now: datetime,
    risk_manager: RiskManager, order_manager: OrderManager, reason: str, *, square_off_date: str | None = None,
) -> AutoTradeCycleResult:
    """Forced close of the whole open position at the latest close: the
    15:20 square-off, or the recovery of a missed one. Bypasses
    risk_manager.check() (a mandatory risk-reducing close), but still
    records the trade so P&L/trade-count/consecutive-loss accounting stays
    correct."""
    account = order_manager.account
    event = TradeEvent(
        timestamp=data.index[-1], kind="SQUARE_OFF", underlying_price=current_price,
        option_symbol=None, stop_loss=None, target=None, exit_level=current_price,
    )
    order_result = order_manager.place_event("SQUARE_OFF", current_price, account.quantity, index_id=index_id)
    if order_result.status != "PLACED":
        # A FAILED fill (e.g. a genuine race where quantity reached 0
        # between the check and the fill itself) must never mark
        # square_off_date - it has to remain eligible to retry.
        return AutoTradeCycleResult(event, RiskDecision(False, order_result.detail), order_result)
    risk_manager.record_trade(realized_pnl=order_result.realized_pnl, now=now, is_exit=True)
    # Advanced together, atomically with the fill above - a persistence
    # failure of the *durable* snapshot elsewhere (algoedge.db, always
    # fail-safe/never-raising) can never roll these back, so a restart always
    # resumes from a state that is at worst stale, never one that contradicts
    # what actually happened in this process.
    account.last_event_at = event.timestamp
    if square_off_date is not None:
        account.square_off_date = square_off_date
    return AutoTradeCycleResult(event, RiskDecision(True, reason), order_result)


def _stale_position_day(account: SimulatedAccount, now: datetime) -> Any:
    """The IST date of an open position's entry bar if it is before today
    (a missed square-off), else None. While a paper position is open,
    account.last_event_at IS its entry bar (only the entry fill advances it;
    exits, square-offs and expiries either flatten the account or only apply
    when flat). None as well when that date is unknown - never closes a
    position on a guess; the day's normal square-off still applies."""
    if account.quantity <= 0 or account.last_event_at is None:
        return None
    entry_day = _as_ist(account.last_event_at).tz_convert(IST).date()  # the IST date, whatever the stored zone
    return entry_day if entry_day < now.date() else None


def _account_synced_events(
    data: pd.DataFrame, config: StrategyConfig, label: str, account: SimulatedAccount,
) -> list[TradeEvent]:
    """Canonical strategy events, evaluated against the account's real
    position from just after account.last_event_at (see run_cycle). An
    account that has never processed anything gets the plain canonical
    replay of the window."""
    if account.last_event_at is None:
        return run_strategy(data, config, underlying_label=label)[1]
    held = None
    if account.quantity > 0 and account.side in ("CALL", "PUT"):
        stop_loss, target = account.stop_loss, account.target
        if stop_loss is None or target is None:
            stop_loss, target = _levels_at_entry_bar(data, config, account.last_event_at, account.side)
        held = OpenPosition(
            side=account.side, entry_price=float(account.average_price or np.nan),
            stop_loss=stop_loss, target=target,
        )
    return run_strategy(
        data, config, underlying_label=label, start_after=account.last_event_at, initial_position=held,
    )[1]


def _levels_at_entry_bar(
    data: pd.DataFrame, config: StrategyConfig, entry_at: Any, side: str,
) -> tuple[float, float]:
    """Re-derives an open position's canonical SL/target after a restart
    (they are not persisted): while a paper position is open, the account's
    last_event_at IS its entry bar, and paper never holds past the day's
    square-off, so that bar is inside the fetched window. Returns NaN levels
    if it isn't - the strategy then never exits that position on its own and
    the forced square-off closes it."""
    indicators = compute_indicators(data, config)
    if entry_at not in indicators.index:
        logger.warning("Entry bar %s not in window - open position has no strategy SL/target", entry_at)
        return float("nan"), float("nan")
    close = float(indicators.loc[entry_at, "Close"])
    sl_dist, tp_dist = risk_distances(float(indicators.loc[entry_at, "atr"]), config)
    direction = 1 if side == "CALL" else -1
    return close - direction * sl_dist, close + direction * tp_dist


def _as_ist(timestamp: Any) -> pd.Timestamp:
    ts = pd.Timestamp(timestamp)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts


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
