from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from algoedge.option_contract import OptionContract

logger = logging.getLogger("algoedge.orders")

_ENTRY_EVENT_KINDS = {"ENTRY_CALL": "CALL", "ENTRY_PUT": "PUT"}
# SQUARE_OFF (Phase 4's forced end-of-day close) is a closing fill exactly
# like a strategy-driven EXIT_SL/EXIT_TARGET - same CALL/PUT P&L direction
# math in fill_event() below, just triggered by wall-clock time instead of
# the canonical strategy's own SL/TP levels.
_EXIT_EVENT_KINDS = {"EXIT_SL", "EXIT_TARGET", "SQUARE_OFF"}


@dataclass
class SimulatedAccount:
    """A quantity-aware paper trading account for Auto Trading.

    Distinct from `PaperBroker` (a single toy share position for the basic
    demo strategy in main.py) — this tracks quantity and realized P&L per
    fill, which the Risk Manager and Auto Trading loop need.
    """

    cash: float = 1_000_000.0
    quantity: int = 0
    average_price: float | None = None
    index_id: str | None = None  # which index the open position is in, for
    # unrealized P&L (needs to know which current price to check against)
    # Set only by fill_event() (the CALL/PUT path) — stays None for the
    # legacy fill() BUY/SELL path below, which is always long-only.
    side: str | None = None  # "CALL" | "PUT" | None
    # A monotonically-increasing high-water mark: the timestamp of the last
    # fno_signals.strategy TradeEvent actually placed against this account.
    # A canonical-strategy walk-forward window is fully recomputed from
    # scratch on every call (fno_signals.strategy.run() has no memory
    # across calls), so a caller that re-fetches an overlapping window
    # every cycle would otherwise see the exact same already-acted-upon
    # event as "the latest event" over and over. Comparing against this
    # field is what lets algoedge.auto_trader.run_cycle() tell "the same
    # signal I already filled" apart from "a genuinely new one" - see
    # tests/test_auto_trader.py's duplicate-signal tests. Any orderable
    # timestamp works here; kept untyped to avoid a pandas dependency in
    # this otherwise strategy-agnostic module.
    last_event_at: Any | None = None
    # ISO date (YYYY-MM-DD, IST) of the last forced end-of-day square-off
    # performed against this account - None until the first one ever
    # happens. Compared against "today" each cycle (algoedge.auto_trader.
    # run_cycle()): a fresh date each real trading day naturally means "not
    # yet squared off today" without needing an explicit daily reset, the
    # same pattern algoedge.risk_manager.RiskState.trade_day already uses.
    # Also what blocks a stale ENTRY event from reopening a position after
    # today's square-off has already happened.
    square_off_date: str | None = None
    # The instrument-master-validated contract this open position was
    # actually resolved against (Phase 5) - set only by fill_event()'s
    # entry branch when a `contract` is supplied, cleared when the
    # position fully closes. Restoring this on startup (see
    # auto_trader.restore_account_state()) is what lets an already-open
    # position survive a restart WITHOUT re-resolving a contract.
    contract: OptionContract | None = None

    def fill(self, action: str, price: float, quantity: int, index_id: str | None = None) -> float:
        """Executes a simulated fill and returns realized P&L (0.0 for entries)."""
        side = action.upper()
        if side == "BUY":
            total_cost = quantity * price
            existing_value = self.quantity * (self.average_price or price)
            self.average_price = (existing_value + total_cost) / (self.quantity + quantity)
            if self.quantity == 0:
                self.index_id = index_id
            self.quantity += quantity
            self.cash -= total_cost
            return 0.0
        if side == "SELL":
            close_quantity = min(quantity, self.quantity)
            realized_pnl = (price - (self.average_price or price)) * close_quantity
            self.quantity -= close_quantity
            self.cash += close_quantity * price
            if self.quantity == 0:
                self.average_price = None
                self.index_id = None
            return realized_pnl
        raise ValueError(f"Unsupported action: {action}")

    def fill_event(
        self, kind: str, price: float, quantity: int, index_id: str | None = None,
        contract: OptionContract | None = None,
    ) -> float:
        """Paper-fills a CALL/PUT entry or exit from a canonical
        `fno_signals.strategy.TradeEvent.kind`, and returns realized P&L
        (0.0 for entries).

        A CALL position behaves like the legacy long-only `fill()` path
        (profits when price rises); a PUT position mirrors it (profits when
        price falls) — the strategy itself only ever computes SL/TP on the
        underlying's price, never an option premium, so that's what this
        paper-fills too. Cash bookkeeping treats both sides the same way
        `fill()` does (subtract on entry, add back on exit) — an
        approximation appropriate for a paper account with no real
        short-margin mechanics, not a claim about real broker margin.

        `contract` (Phase 5) is the instrument-master-validated
        `OptionContract` this entry is opening against - required for a
        genuinely new entry (an already-open position adding to itself
        keeps its original contract, see below), never used or required
        for an exit/square-off, which only ever closes what's already open.
        """
        if kind in _ENTRY_EVENT_KINDS:
            side = _ENTRY_EVENT_KINDS[kind]
            if self.quantity > 0 and self.side != side:
                raise ValueError(f"Cannot open {side} while a {self.side} position is open")
            total_cost = quantity * price
            existing_value = self.quantity * (self.average_price or price)
            self.average_price = (existing_value + total_cost) / (self.quantity + quantity)
            self.side = side
            if self.quantity == 0:
                self.index_id = index_id
                self.contract = contract
            self.quantity += quantity
            self.cash -= total_cost
            return 0.0
        if kind in _EXIT_EVENT_KINDS:
            if self.quantity == 0 or self.side is None:
                raise ValueError("No open position to exit")
            close_quantity = min(quantity, self.quantity)
            direction = 1 if self.side == "CALL" else -1
            realized_pnl = (price - (self.average_price or price)) * close_quantity * direction
            self.quantity -= close_quantity
            self.cash += close_quantity * price
            if self.quantity == 0:
                self.average_price = None
                self.side = None
                self.index_id = None
                self.contract = None
            return realized_pnl
        raise ValueError(f"Unsupported event kind: {kind}")


@dataclass(frozen=True)
class OrderResult:
    status: str  # "PLACED" | "FAILED"
    detail: str
    realized_pnl: float = 0.0


class OrderManager:
    """Places orders that already passed the Risk Manager.

    Auto Trading currently routes exclusively through a simulated account.
    Strategy signals are generated from NIFTY/BANK NIFTY/SENSEX index data,
    which has no direct tradable instrument on Groww's cash segment — real
    execution needs option/future contract resolution (expiry, strike,
    CE/PE) that the Manual Trading module hasn't built yet. Routing to the
    live Groww broker here would place an order for whatever
    ALGOEDGE_SYMBOL happens to be configured, unrelated to the signal's
    instrument, so it is intentionally not wired up yet.
    """

    def __init__(self, account: SimulatedAccount | None = None) -> None:
        self.account = account or SimulatedAccount()

    def place(self, action: str, price: float, quantity: int, index_id: str | None = None) -> OrderResult:
        logger.info("Auto order decision: %s qty=%s price=%.2f", action, quantity, price)
        try:
            realized_pnl = self.account.fill(action, price, quantity, index_id=index_id)
        except ValueError as error:
            logger.warning("Order failed: %s", error)
            return OrderResult("FAILED", str(error))
        logger.info(
            "Paper order filled: %s qty=%s price=%.2f realized_pnl=%.2f",
            action, quantity, price, realized_pnl,
        )
        return OrderResult(
            "PLACED", f"Paper order filled: {action} {quantity} @ {price:.2f}", realized_pnl
        )

    def place_event(
        self, kind: str, price: float, quantity: int, index_id: str | None = None,
        contract: OptionContract | None = None,
    ) -> OrderResult:
        """Same as `place()`, but for a canonical strategy's CALL/PUT
        `TradeEvent.kind` rather than a plain BUY/SELL action. `contract`
        is the instrument-master-validated `OptionContract` a new entry is
        opening against (Phase 5) - see `SimulatedAccount.fill_event()`."""
        logger.info("Auto order decision: %s qty=%s price=%.2f", kind, quantity, price)
        try:
            realized_pnl = self.account.fill_event(kind, price, quantity, index_id=index_id, contract=contract)
        except ValueError as error:
            logger.warning("Order failed: %s", error)
            return OrderResult("FAILED", str(error))
        logger.info(
            "Paper order filled: %s qty=%s price=%.2f realized_pnl=%.2f",
            kind, quantity, price, realized_pnl,
        )
        return OrderResult(
            "PLACED", f"Paper order filled: {kind} {quantity} @ {price:.2f}", realized_pnl
        )
