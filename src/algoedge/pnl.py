from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from algoedge.cost_model import CostModel, compute_trade_costs


@dataclass
class _Lot:
    side: str  # BUY | SELL
    quantity: float
    price: float
    opened_at: Any = None


@dataclass(frozen=True)
class RealizedTrade:
    trading_symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float  # gross P&L - unchanged meaning from before cost tracking existed
    side: str = "LONG"  # "LONG" (opened with BUY) | "SHORT" (opened with SELL)
    entry_time: Any = None
    closed_at: Any = None  # the closing order's createdAt - P&L "realizes"
    # on the day a position is closed, not the day it was opened
    costs: float = 0.0  # brokerage+STT+exchange+GST+stamp duty; 0.0 when no CostModel was supplied
    net_pnl: float | None = None  # pnl - costs; set in __post_init__ when not given explicitly

    def __post_init__(self) -> None:
        if self.net_pnl is None:
            object.__setattr__(self, "net_pnl", self.pnl - self.costs)


@dataclass(frozen=True)
class RealizedPnlReport:
    live_total: float  # gross
    paper_total: float  # paper trades never incur real costs - gross == net
    live_trades: list[RealizedTrade] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.live_total + self.paper_total

    @property
    def live_costs(self) -> float:
        return sum(trade.costs for trade in self.live_trades)

    @property
    def live_net_total(self) -> float:
        return sum(trade.net_pnl for trade in self.live_trades)

    @property
    def net_total(self) -> float:
        return self.live_net_total + self.paper_total


def fifo_match(
    orders_oldest_first: list[dict[str, Any]],
    cost_model: CostModel | None = None,
) -> tuple[list[RealizedTrade], dict[str, list[_Lot]]]:
    """Core FIFO matching of BUY/SELL fills per symbol among confirmed-filled
    real orders. Returns (closed round-trips, remaining open lots by symbol) -
    shared by realized-P&L computation (which only needs the closed trades)
    and per-trade views like Manual Trade Tracking (which also need to show
    what's still open).

    `orders_oldest_first` must be in chronological order (oldest first) -
    db.list_orders() returns newest-first, so callers must reverse before
    calling this. Orders with no known fill price (e.g. a MARKET order
    Groww never returned an average_fill_price for) are skipped rather than
    guessed into the P&L, since a wrong price would silently corrupt every
    later match for that symbol too.

    `cost_model`, if given, computes brokerage/STT/exchange/GST/stamp-duty
    costs for each closed trade (RealizedTrade.costs/net_pnl) - omitted
    (or an all-zero CostModel) leaves net_pnl == pnl, same as before cost
    tracking existed.
    """
    lots_by_symbol: dict[str, list[_Lot]] = {}
    trades: list[RealizedTrade] = []

    for order in orders_oldest_first:
        if not order.get("live") or order.get("outcome") != "SUCCESS":
            continue
        symbol = order.get("tradingSymbol")
        side = str(order.get("side") or "").upper()
        quantity = float(order.get("quantity") or 0)
        price = order.get("price")
        if not symbol or side not in ("BUY", "SELL") or quantity <= 0 or price is None:
            continue
        price = float(price)

        lots = lots_by_symbol.setdefault(symbol, [])
        remaining = quantity

        while remaining > 1e-9 and lots and lots[0].side != side:
            lot = lots[0]
            matched = min(lot.quantity, remaining)
            pnl = (price - lot.price) * matched if lot.side == "BUY" else (lot.price - price) * matched
            # STT/stamp duty are transaction-side-specific (sell/buy), not
            # entry/exit-specific - map correctly regardless of whether the
            # opening leg was a BUY (LONG) or a SELL (SHORT).
            buy_price, sell_price = (lot.price, price) if lot.side == "BUY" else (price, lot.price)
            costs = compute_trade_costs(buy_price, sell_price, matched, cost_model) if cost_model else 0.0
            trades.append(RealizedTrade(
                symbol, matched, lot.price, price, pnl,
                side="LONG" if lot.side == "BUY" else "SHORT",
                entry_time=lot.opened_at, closed_at=order.get("createdAt"),
                costs=costs,
            ))
            lot.quantity -= matched
            remaining -= matched
            if lot.quantity <= 1e-9:
                lots.pop(0)

        if remaining > 1e-9:
            lots.append(_Lot(side=side, quantity=remaining, price=price, opened_at=order.get("createdAt")))

    return trades, lots_by_symbol


def compute_live_realized_pnl(
    orders_oldest_first: list[dict[str, Any]], cost_model: CostModel | None = None,
) -> tuple[float, list[RealizedTrade]]:
    """FIFO-matches BUY/SELL fills per symbol to compute realized (gross)
    P&L for closed round trips only. See fifo_match() for the shared
    matching core and net_pnl (cost-adjusted) per trade."""
    trades, _open_lots = fifo_match(orders_oldest_first, cost_model)
    total = sum(trade.pnl for trade in trades)
    return total, trades


def compute_paper_realized_pnl(orders: list[dict[str, Any]]) -> float:
    """Paper (Auto Trading) orders already carry a correctly-computed
    realized_pnl per trade from SimulatedAccount.fill() at write time - this
    just sums it, rather than re-deriving via FIFO matching."""
    return sum(
        float(order.get("realizedPnl") or 0.0)
        for order in orders
        if not order.get("live") and order.get("realizedPnl") is not None
    )


def compute_realized_pnl(
    orders_newest_first: list[dict[str, Any]], cost_model: CostModel | None = None,
) -> RealizedPnlReport:
    """Takes exactly what db.list_orders() returns (newest first, live +
    paper mixed) and splits/computes both sides. `cost_model` only applies
    to live trades - paper (Auto Trading) fills never incur real
    brokerage/taxes, so their net P&L always equals their gross P&L."""
    live_orders_oldest_first = list(reversed([order for order in orders_newest_first if order.get("live")]))
    paper_orders = [order for order in orders_newest_first if not order.get("live")]

    live_total, live_trades = compute_live_realized_pnl(live_orders_oldest_first, cost_model)
    paper_total = compute_paper_realized_pnl(paper_orders)

    return RealizedPnlReport(live_total=live_total, paper_total=paper_total, live_trades=live_trades)


def compute_paper_unrealized_pnl(account: Any, current_price: float | None) -> float | None:
    """Auto Trading's paper account holds a single simulated long position
    at index-spot-price levels (not option premiums), so unrealized P&L can
    be computed directly against the current index price from yfinance.
    Returns None if there's no open position or no current price available -
    never a guessed/zero value standing in for genuinely unknown data.
    """
    if account.quantity == 0 or account.average_price is None or current_price is None:
        return None
    return (current_price - account.average_price) * account.quantity
