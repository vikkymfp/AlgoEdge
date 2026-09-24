from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("algoedge.orders")


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
