from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from growwapi.groww.exceptions import GrowwAPIException

# Groww's real order-status vocabulary, taken directly from the
# StocksOrderStatus enum embedded in growwapi's own protobuf schema
# (growwapi/groww/proto/stock_orders_socket_response_pb2.py) rather than
# assumed — the REST get_order_status()/get_order_detail() responses use
# these same strings as plain JSON. This is a superset of the four
# terminal + four transitional states typically expected: it also includes
# APPROVED, DELIVERY_AWAITED, COMPLETED, CANCELLATION_REQUESTED and
# MODIFICATION_REQUESTED, which are real states Groww can return.
SUCCESS_STATUSES = frozenset({"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"})
FAILURE_STATUSES = frozenset({"REJECTED", "FAILED"})
CANCELLED_STATUSES = frozenset({"CANCELLED"})
TRANSITIONAL_STATUSES = frozenset({
    "NEW", "ACKED", "TRIGGER_PENDING", "APPROVED",
    "CANCELLATION_REQUESTED", "MODIFICATION_REQUESTED",
})


def _extract_float(response: dict[str, Any] | None, *keys: str) -> float | None:
    if not response:
        return None
    for key in keys:
        value = response.get(key)
        if value is None:
            continue
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if result:
            return result
    return None


def _extract_int(response: dict[str, Any] | None, *keys: str) -> int | None:
    if not response:
        return None
    for key in keys:
        value = response.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_fill_price(response: dict[str, Any] | None) -> float | None:
    # "average_fill_price"/"price" are the REST-style snake_case names
    # already confirmed working against this account; "avg_fill_price" is
    # kept as a defensive alternate spelling.
    return _extract_float(response, "average_fill_price", "avg_fill_price", "price")


def _extract_filled_quantity(response: dict[str, Any] | None) -> int | None:
    # Groww's own bundled protobuf schema for order-detail broadcasts
    # (OrderDetailUpdateDto in growwapi/groww/proto/stock_orders_socket_response_pb2.py)
    # names this field "filledQty" - the REST endpoints in this same SDK
    # consistently use the snake_case form of their websocket-broadcast
    # counterparts elsewhere (avgFillPrice -> average_fill_price,
    # growwOrderId -> groww_order_id), so "filled_quantity" is the expected
    # REST name; kept defensive with the camelCase form as a fallback since
    # this hasn't been confirmed against a real filled order on this
    # account (it has never had one).
    return _extract_int(response, "filled_quantity", "filledQty")


def _extract_remaining_quantity(response: dict[str, Any] | None) -> int | None:
    return _extract_int(response, "remaining_quantity", "remainingQty")


def compute_slippage(expected_price: float | None, actual_fill_price: float | None) -> float | None:
    """Slippage = actual fill price - expected price. Returns None if
    either side is unknown - never guessed. For a MARKET order on this
    account's Groww tier there is no live quote access, so `expected_price`
    is only ever known for orders where the caller itself specified a
    price (a LIMIT/SL/SL_M order) - a MARKET order's slippage is genuinely
    uncomputable pre-trade here, not silently reported as zero."""
    if expected_price is None or actual_fill_price is None:
        return None
    return actual_fill_price - expected_price


@dataclass(frozen=True)
class OrderVerificationResult:
    outcome: str  # "SUCCESS" | "PARTIAL" | "FAILED" | "CANCELLED" | "TIMEOUT"
    order_status: str | None  # the raw status string from Groww, if any
    groww_order_id: str
    reason: str | None = None  # populated for PARTIAL, FAILED, CANCELLED, TIMEOUT
    attempts: int = 0
    raw_response: dict[str, Any] | None = None
    average_fill_price: float | None = None  # the ACTUAL price the order filled at
    # (from Groww's average_fill_price) — the price MARKET orders were
    # submitted with is never known in advance, so this is the only
    # reliable source for what was actually paid/received, needed to
    # compute realized P&L on real trades.
    requested_quantity: int | None = None
    filled_quantity: int | None = None  # None if Groww's response didn't include it (never assumed equal to requested)
    remaining_quantity: int | None = None
    slippage: float | None = None  # actual_fill_price - expected_price; None if expected_price was never known


def verify_order_status(
    groww_client: Any,
    groww_order_id: str,
    segment: str,
    max_retries: int = 5,
    initial_delay: float = 0.5,
    requested_quantity: int | None = None,
    expected_price: float | None = None,
) -> OrderVerificationResult:
    """Polls Groww for a just-placed order's fill status with exponential
    backoff, and classifies the outcome so the caller can decide whether to
    proceed (SUCCESS), handle a partial position (PARTIAL), halt (FAILED),
    log a cancellation (CANCELLED), or treat it as unresolved after
    exhausting retries (TIMEOUT).

    Placing an order (place_order) only means Groww *accepted the request*
    into its order book — it says nothing about whether the order actually
    filled, was rejected for insufficient margin, or was cancelled. This
    function is the middleware that closes that gap: it must run
    immediately after every live order placement, before any code treats
    the trade as open.

    `requested_quantity` lets this function detect a genuine partial fill
    (Groww reaches a terminal "success" status but filled less than asked)
    rather than ever assuming a successful order filled in full.
    `expected_price` (the order's own LIMIT/SL price, when there is one)
    enables real slippage tracking — never guessed for a MARKET order where
    no pre-trade price is known.

    This only ever reads order status — it never places, cancels, or
    modifies an order.
    """
    delay = initial_delay
    last_status: str | None = None
    last_response: dict[str, Any] | None = None

    for attempt in range(1, max_retries + 1):
        status: str | None = None
        try:
            response = groww_client.get_order_status(segment=segment, groww_order_id=groww_order_id)
            last_response = response if isinstance(response, dict) else {}
            status = str(last_response.get("order_status", "")).upper() or None
            last_status = status
        except GrowwAPIException as error:
            # A transient status-lookup failure is not itself an order
            # failure - keep polling rather than treating a network hiccup
            # as a halt.
            last_response = {"lookup_error": str(error)}

        if status in SUCCESS_STATUSES:
            fill_price = _extract_fill_price(last_response)
            filled_quantity = _extract_filled_quantity(last_response)
            remaining_quantity = _extract_remaining_quantity(last_response)
            if (
                requested_quantity is not None and filled_quantity is not None
                and filled_quantity < requested_quantity
            ):
                return OrderVerificationResult(
                    outcome="PARTIAL", order_status=status, groww_order_id=groww_order_id,
                    reason=f"Only {filled_quantity} of {requested_quantity} requested filled",
                    attempts=attempt, raw_response=last_response, average_fill_price=fill_price,
                    requested_quantity=requested_quantity, filled_quantity=filled_quantity,
                    remaining_quantity=remaining_quantity if remaining_quantity is not None
                    else requested_quantity - filled_quantity,
                    slippage=compute_slippage(expected_price, fill_price),
                )
            return OrderVerificationResult(
                outcome="SUCCESS", order_status=status, groww_order_id=groww_order_id,
                attempts=attempt, raw_response=last_response, average_fill_price=fill_price,
                requested_quantity=requested_quantity,
                filled_quantity=filled_quantity if filled_quantity is not None else requested_quantity,
                remaining_quantity=remaining_quantity if remaining_quantity is not None else 0,
                slippage=compute_slippage(expected_price, fill_price),
            )
        if status in FAILURE_STATUSES:
            reason = str((last_response or {}).get("remark") or "No reason provided by Groww")
            return OrderVerificationResult(
                outcome="FAILED", order_status=status, groww_order_id=groww_order_id,
                reason=reason, attempts=attempt, raw_response=last_response,
                requested_quantity=requested_quantity,
            )
        if status in CANCELLED_STATUSES:
            reason = str((last_response or {}).get("remark") or "Order cancelled")
            filled_quantity = _extract_filled_quantity(last_response)
            return OrderVerificationResult(
                outcome="CANCELLED", order_status=status, groww_order_id=groww_order_id,
                reason=reason, attempts=attempt, raw_response=last_response,
                requested_quantity=requested_quantity, filled_quantity=filled_quantity,
            )

        # Transitional (known - NEW/ACKED/TRIGGER_PENDING/APPROVED/... - or
        # an unrecognized future status) - keep polling.
        if attempt < max_retries:
            time.sleep(delay)
            delay *= 2

    return OrderVerificationResult(
        outcome="TIMEOUT",
        order_status=last_status,
        groww_order_id=groww_order_id,
        reason=(
            f"Order did not reach a final state after {max_retries} status checks "
            f"(last known status: {last_status or 'unknown'}). Verify manually in "
            f"the Groww app before assuming this trade is or isn't live."
        ),
        attempts=max_retries,
        raw_response=last_response,
        requested_quantity=requested_quantity,
        filled_quantity=_extract_filled_quantity(last_response),
    )
