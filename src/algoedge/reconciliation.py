from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _number(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PositionComparison:
    trading_symbol: str
    expected_quantity: float
    actual_quantity: float
    matches: bool


@dataclass(frozen=True)
class ReconciliationReport:
    comparisons: list[PositionComparison] = field(default_factory=list)
    unconfirmed_orders: list[dict[str, Any]] = field(default_factory=list)


def compute_expected_positions(orders: list[dict[str, Any]]) -> dict[str, float]:
    """Replays confirmed-filled real orders to compute what each symbol's
    net quantity should be.

    `outcome == "SUCCESS"` counts the full requested quantity.
    `outcome == "PARTIAL"` counts only `filledQuantity` - a partial fill
    still creates a real (partial) position on Groww's side, so ignoring
    it entirely (as an earlier version of this function did) would make a
    correctly-filled portion look like a phantom mismatch instead of
    reconciling. The unfilled remainder's fate is still unresolved, so
    PARTIAL orders are ALSO surfaced by find_unconfirmed_orders() below -
    both are true at once: part of it is a real, countable position, and
    part of it still needs manual attention.

    FAILED/CANCELLED never became a position at all. TIMEOUT/UNKNOWN are
    genuinely unconfirmed (Groww never told us whether they filled), so
    they're excluded here and surfaced separately rather than guessed
    either way.
    """
    expected: dict[str, float] = {}
    for order in orders:
        outcome = order.get("outcome")
        if not order.get("live") or outcome not in ("SUCCESS", "PARTIAL"):
            continue
        symbol = order.get("tradingSymbol")
        if not symbol:
            continue
        if outcome == "PARTIAL":
            quantity = _number(order.get("filledQuantity"), 0.0) or 0.0
        else:
            quantity = _number(order.get("quantity"), 0.0) or 0.0
        side = str(order.get("side") or "").upper()
        signed = quantity if side == "BUY" else -quantity if side == "SELL" else 0.0
        expected[symbol] = expected.get(symbol, 0.0) + signed
    return {symbol: quantity for symbol, quantity in expected.items() if quantity != 0}


def find_unconfirmed_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Real orders whose fill status was never confirmed one way or the
    other, or was only partially resolved - these can't safely be treated
    as fully settled, so they're called out for manual review alongside
    (not instead of) being counted toward the expected position above."""
    return [
        order for order in orders
        if order.get("live") and order.get("outcome") in ("TIMEOUT", "UNKNOWN", "PARTIAL")
    ]


def _sum_live_quantities(live_positions: list[dict[str, Any]]) -> dict[str, float]:
    actual: dict[str, float] = {}
    for position in live_positions:
        symbol = str(position.get("trading_symbol", "")).upper()
        if not symbol:
            continue
        actual[symbol] = actual.get(symbol, 0.0) + (_number(position.get("quantity"), 0.0) or 0.0)
    return {symbol: quantity for symbol, quantity in actual.items() if quantity != 0}


def reconcile(orders: list[dict[str, Any]], live_positions: list[dict[str, Any]]) -> ReconciliationReport:
    """Pure comparison: what our own order records say the position should
    be (`orders`, from the DB) vs what Groww actually reports (`live_positions`,
    from get_positions_for_user). Kept side-effect free and dependency-free
    for testability - fetching both lists is the caller's job.
    """
    expected = compute_expected_positions(orders)
    actual = _sum_live_quantities(live_positions)

    symbols = sorted(set(expected) | set(actual))
    comparisons = [
        PositionComparison(
            trading_symbol=symbol,
            expected_quantity=expected.get(symbol, 0.0),
            actual_quantity=actual.get(symbol, 0.0),
            matches=expected.get(symbol, 0.0) == actual.get(symbol, 0.0),
        )
        for symbol in symbols
    ]
    return ReconciliationReport(comparisons=comparisons, unconfirmed_orders=find_unconfirmed_orders(orders))
