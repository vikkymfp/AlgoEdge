from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from algoedge.pnl import fifo_match


@dataclass(frozen=True)
class StrategyPerformance:
    source: str
    trades_closed: int
    wins: int
    losses: int
    total_pnl: float
    average_pnl: float
    best_trade: float | None
    worst_trade: float | None
    win_rate: float | None  # percentage, None if no closed trades yet


def _summarize(source: str, trade_pnls: list[float]) -> StrategyPerformance:
    trades_closed = len(trade_pnls)
    if trades_closed == 0:
        return StrategyPerformance(source, 0, 0, 0, 0.0, 0.0, None, None, None)
    wins = sum(1 for pnl in trade_pnls if pnl > 0)
    losses = sum(1 for pnl in trade_pnls if pnl < 0)
    total = sum(trade_pnls)
    return StrategyPerformance(
        source=source,
        trades_closed=trades_closed,
        wins=wins,
        losses=losses,
        total_pnl=total,
        average_pnl=total / trades_closed,
        best_trade=max(trade_pnls),
        worst_trade=min(trade_pnls),
        win_rate=(wins / trades_closed) * 100,
    )


def compute_strategy_performance(orders_newest_first: list[dict[str, Any]]) -> list[StrategyPerformance]:
    """Groups CLOSED round-trip trades by their source (e.g.
    "algoedge.auto_trader", "fno_signals") and computes win-rate/P&L stats
    per strategy.

    Live orders are FIFO-matched independently PER SOURCE - if two
    different strategies both traded the same symbol, their fills must
    never be matched against each other's, or the resulting "trades" would
    be fictional. Paper orders (Auto Trading) use the already-computed
    realized_pnl per order rather than re-deriving it.

    Signal volume (how many signals a strategy generated) is deliberately
    NOT included here - see the Daily Summary report for that. This is
    about trade OUTCOMES only.
    """
    sources = sorted({order.get("source") for order in orders_newest_first if order.get("source")})
    results = []

    for source in sources:
        source_orders = [order for order in orders_newest_first if order.get("source") == source]
        live_orders = [order for order in source_orders if order.get("live")]
        paper_orders = [order for order in source_orders if not order.get("live")]

        trade_pnls: list[float] = []

        if live_orders:
            closed_trades, _open_lots = fifo_match(list(reversed(live_orders)))
            trade_pnls.extend(trade.pnl for trade in closed_trades)

        trade_pnls.extend(
            float(order["realizedPnl"]) for order in paper_orders if order.get("realizedPnl") is not None
        )

        results.append(_summarize(source, trade_pnls))

    return results
