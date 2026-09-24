from __future__ import annotations

import logging

logger = logging.getLogger("algoedge.signal_decisions")


def log_signal_decision(
    *,
    signal_id: str,
    underlying_symbol: str,
    underlying_price: float,
    direction: str,
    strike: int | None = None,
    option_type: str | None = None,
    risk_status: str,
    position: str,
    option_symbol: str | None = None,
    order_side: str | None = None,
    requested_quantity: int | None = None,
    broker_order_id: str | None = None,
    fill_quantity: int | None = None,
    fill_price: float | None = None,
    status: str,
) -> None:
    """One structured, greppable log line per signal decision - spec §30's
    "every important action must be explainable" requirement, matching the
    shape of its worked example (signal/underlying/direction/strike/risk/
    position/option/order/qty/broker order/fill/status).

    Deliberately omits a "signal score" field: the spec's own example
    includes one, but no strategy in this codebase computes such a number
    (fno_signals' EMA/RSI/Supertrend setup is a boolean entry condition,
    not a scored one) - inventing a fake score to match the example's
    shape would be exactly the kind of fabrication this session's
    "never invent, never guess" discipline exists to prevent.
    """
    logger.info(
        "signal_decision signal_id=%s underlying=%s spot=%s direction=%s strike=%s option_type=%s "
        "risk=%s position=%s option=%s order_side=%s requested_qty=%s broker_order=%s "
        "fill_qty=%s fill_price=%s status=%s",
        signal_id, underlying_symbol, underlying_price, direction, strike, option_type,
        risk_status, position, option_symbol, order_side, requested_quantity,
        broker_order_id, fill_quantity, fill_price, status,
    )
