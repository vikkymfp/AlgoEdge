from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from algoedge.pnl import fifo_match

MANUAL_TRADING_SOURCE = "algoedge.manual_trading"


@dataclass(frozen=True)
class ManualTrade:
    trading_symbol: str
    side: str  # "LONG" | "SHORT"
    quantity: float
    entry_price: float
    entry_time: Any
    status: str  # "OPEN" | "CLOSED"
    exit_price: float | None = None
    exit_time: Any | None = None
    pnl: float | None = None  # None while OPEN - never guessed


def compute_manual_trades(orders_newest_first: list[dict[str, Any]]) -> list[ManualTrade]:
    """Pairs BUY/SELL fills placed via the Manual Trading page into discrete
    round-trip trades (FIFO), reusing the same matching core as realized
    P&L. Unlike the raw Trade Ledger (one row per order), this shows one row
    per trade - open or closed - which is what "how are my manual trades
    doing" actually needs.

    Only orders from the Manual Trading source are considered - fno_signals
    and Auto Trading have their own tracking (the F&O scanner's own console
    log, and the paper account respectively).
    """
    manual_orders_newest_first = [
        order for order in orders_newest_first if order.get("source") == MANUAL_TRADING_SOURCE
    ]
    oldest_first = list(reversed(manual_orders_newest_first))
    closed_trades, open_lots_by_symbol = fifo_match(oldest_first)

    trades = [
        ManualTrade(
            trading_symbol=trade.trading_symbol,
            side=trade.side,  # the Manual Trading form allows SELL-to-open
            # (shorting) just as freely as BUY-to-open, so this must reflect
            # the actual entry side, never assumed to be LONG.
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            entry_time=trade.entry_time,
            status="CLOSED",
            exit_price=trade.exit_price,
            exit_time=trade.closed_at,
            pnl=trade.pnl,
        )
        for trade in closed_trades
    ]

    for symbol, lots in open_lots_by_symbol.items():
        for lot in lots:
            trades.append(ManualTrade(
                trading_symbol=symbol,
                side="LONG" if lot.side == "BUY" else "SHORT",
                quantity=lot.quantity,
                entry_price=lot.price,
                entry_time=lot.opened_at,
                status="OPEN",
            ))

    return trades
