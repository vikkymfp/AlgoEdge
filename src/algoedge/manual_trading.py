from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from growwapi import GrowwAPI

from fno_signals.broker import IST, ContractNotFoundError, ResolvedContract, resolve_contract
from fno_signals.config import IndexConfig

# Bridges this dashboard's string index ids (matching market_pulse's
# INDEX_DEFINITIONS, used throughout the existing chart/strategy/auto-trading
# endpoints) to fno_signals' integer-keyed INDEX_MAP, which is the source of
# truth for the per-index exchange/groww_underlying/strike_step/lot_size
# already verified live in that package.
DASHBOARD_INDEX_IDS: dict[str, int] = {"nifty-50": 1, "bank-nifty": 2, "sensex": 3}

ORDER_TYPES = {
    "MARKET": GrowwAPI.ORDER_TYPE_MARKET,
    "LIMIT": GrowwAPI.ORDER_TYPE_LIMIT,
    "SL": GrowwAPI.ORDER_TYPE_STOP_LOSS,
    "SL_M": GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET,
}
PRODUCTS = {"NRML": GrowwAPI.PRODUCT_NRML, "MIS": GrowwAPI.PRODUCT_MIS}
SIDES = {"BUY": GrowwAPI.TRANSACTION_TYPE_BUY, "SELL": GrowwAPI.TRANSACTION_TYPE_SELL}

# The instrument master is ~140k rows and doesn't change intraday, so it's
# cached briefly to keep the expiry/strike dropdowns and order preview snappy
# instead of re-fetching on every UI interaction.
_INSTRUMENTS_CACHE_TTL = 300.0
_instruments_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def _get_instruments(client: Any) -> pd.DataFrame:
    cached = _instruments_cache.get("all")
    now = time.monotonic()
    if cached is not None and now - cached[0] < _INSTRUMENTS_CACHE_TTL:
        return cached[1]
    instruments = client.get_all_instruments()
    _instruments_cache["all"] = (now, instruments)
    return instruments


def list_expiries(client: Any, index_config: IndexConfig, as_of: date | None = None) -> list[date]:
    """Live, currently-tradeable expiry dates for this underlying."""
    as_of = as_of or pd.Timestamp.now(tz=IST).date()
    instruments = _get_instruments(client)
    matches = instruments[
        (instruments["underlying_symbol"] == index_config.groww_underlying)
        & (instruments["instrument_type"].isin(["CE", "PE"]))
    ]
    expiries = pd.to_datetime(matches["expiry_date"]).dt.date
    return sorted({expiry for expiry in expiries if expiry >= as_of})


def list_strikes(client: Any, index_config: IndexConfig, expiry: date) -> list[int]:
    """Live strikes actually listed for this underlying/expiry."""
    instruments = _get_instruments(client)
    matches = instruments[
        (instruments["underlying_symbol"] == index_config.groww_underlying)
        & (instruments["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    matches["expiry_date"] = pd.to_datetime(matches["expiry_date"]).dt.date
    matches = matches[matches["expiry_date"] == expiry]
    return sorted({int(float(strike)) for strike in matches["strike_price"]})


@dataclass(frozen=True)
class ManualOrderRequest:
    right: str  # "CE" | "PE"
    side: str  # "BUY" | "SELL"
    order_type: str  # "MARKET" | "LIMIT" | "SL" | "SL_M"
    lots: int
    product: str = "NRML"
    price: float | None = None
    trigger_price: float | None = None


def validate_order_request(request: ManualOrderRequest) -> None:
    """Raises ValueError with a clear message for anything that would
    otherwise fail confusingly at the broker, or silently mean something
    other than what the user intended."""
    if request.right not in ("CE", "PE"):
        raise ValueError(f"Unsupported option type: {request.right}")
    if request.side not in SIDES:
        raise ValueError(f"Unsupported side: {request.side}")
    if request.order_type not in ORDER_TYPES:
        raise ValueError(f"Unsupported order type: {request.order_type}")
    if request.product not in PRODUCTS:
        raise ValueError(f"Unsupported product: {request.product}")
    if request.lots <= 0:
        raise ValueError("Lots must be a positive integer")
    if request.order_type in ("LIMIT", "SL") and not request.price:
        raise ValueError(f"{request.order_type} orders require a price")
    if request.order_type in ("SL", "SL_M") and not request.trigger_price:
        raise ValueError(f"{request.order_type} orders require a trigger price")


def resolve_manual_contract(
    client: Any, index_config: IndexConfig, expiry: date, strike: int, right: str,
) -> ResolvedContract:
    """Thin wrapper over fno_signals' live-verified contract resolution,
    pinned to a user-chosen expiry rather than "nearest upcoming" - manual
    trading lets the user pick the expiry explicitly.
    """
    contract = resolve_contract(client, index_config, strike, right, as_of=expiry)
    if contract.expiry_date != expiry:
        # resolve_contract() returns the *nearest* upcoming expiry at that
        # strike; if it doesn't match what the user selected, that combination
        # isn't actually listed - never silently substitute a different expiry.
        raise ContractNotFoundError(
            f"No {right} contract for strike {strike} at expiry {expiry} "
            f"(nearest available: {contract.expiry_date})"
        )
    return contract


def place_manual_order(client: Any, contract: ResolvedContract, request: ManualOrderRequest) -> dict[str, Any]:
    """Places the order exactly as specified - never a MARKET/NRML default
    substitution the way the automated scanner does. Caller must have
    already validated the request and gated on the live-trading flag.
    """
    validate_order_request(request)
    quantity = request.lots * contract.lot_size
    exchange = GrowwAPI.EXCHANGE_NSE if contract.exchange == "NSE" else GrowwAPI.EXCHANGE_BSE

    return client.place_order(
        validity=GrowwAPI.VALIDITY_DAY,
        exchange=exchange,
        order_type=ORDER_TYPES[request.order_type],
        product=PRODUCTS[request.product],
        quantity=quantity,
        segment=GrowwAPI.SEGMENT_FNO,
        trading_symbol=contract.trading_symbol,
        transaction_type=SIDES[request.side],
        price=request.price or 0.0,
        trigger_price=request.trigger_price,
    )
