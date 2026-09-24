from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AUTO_TRADING_SOURCE = "algoedge.auto_trader"


@dataclass(frozen=True)
class EquityPoint:
    closed_at: Any
    realized_pnl: float
    cumulative_pnl: float


def compute_equity_curve(orders_newest_first: list[dict[str, Any]]) -> list[EquityPoint]:
    """Builds a running paper-account equity curve for Auto Trading from its
    recorded orders. `SimulatedAccount.fill()` already computes a per-fill
    realized P&L at order time, so this just needs to sum it in
    chronological order - no FIFO re-matching required, unlike the live
    order P&L path in pnl.py.
    """
    relevant = [
        order
        for order in orders_newest_first
        if order.get("source") == AUTO_TRADING_SOURCE
        and not order.get("live")
        and order.get("realizedPnl") is not None
    ]
    oldest_first = list(reversed(relevant))

    points: list[EquityPoint] = []
    cumulative = 0.0
    for order in oldest_first:
        realized_pnl = float(order["realizedPnl"])
        cumulative += realized_pnl
        points.append(EquityPoint(order.get("createdAt"), realized_pnl, cumulative))
    return points
