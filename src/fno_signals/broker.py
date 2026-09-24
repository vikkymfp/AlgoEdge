from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIException

from fno_signals.config import IndexConfig

IST = ZoneInfo("Asia/Kolkata")


class GrowwSessionError(RuntimeError):
    """Raised when the daily Groww session cannot be authenticated or verified."""


class ContractNotFoundError(RuntimeError):
    """Raised when no live, tradeable contract matches the requested strike/right.

    Callers must treat this as fatal for that signal — never fall back to a
    hand-constructed symbol for order placement.
    """


def generate_daily_session() -> GrowwAPI:
    """Authenticates against the Groww API and verifies the session with a
    real API call before returning the client.

    Uses the same ALGOEDGE_GROWW_* environment variables as the main
    AlgoEdge dashboard (ALGOEDGE_GROWW_ACCESS_TOKEN, or both
    ALGOEDGE_GROWW_API_KEY and ALGOEDGE_GROWW_API_SECRET) rather than a
    separate credential set. Raises GrowwSessionError on any failure —
    callers must terminate rather than proceed with an unverified session.
    """
    access_token = os.environ.get("ALGOEDGE_GROWW_ACCESS_TOKEN")
    api_key = os.environ.get("ALGOEDGE_GROWW_API_KEY")
    api_secret = os.environ.get("ALGOEDGE_GROWW_API_SECRET")

    try:
        if access_token:
            token = access_token
        elif api_key and api_secret:
            token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
        else:
            raise GrowwSessionError(
                "Set ALGOEDGE_GROWW_ACCESS_TOKEN or both ALGOEDGE_GROWW_API_KEY "
                "and ALGOEDGE_GROWW_API_SECRET to trade live."
            )
        client = GrowwAPI(token)
        profile = client.get_user_profile()
        if not isinstance(profile, dict):
            raise GrowwSessionError("Groww session verification returned an unexpected response")
    except GrowwAPIException as error:
        raise GrowwSessionError(f"Groww authentication failed: {error}") from error

    return client


@dataclass(frozen=True)
class ResolvedContract:
    trading_symbol: str
    exchange: str
    expiry_date: date
    strike: int
    right: str  # "CE" | "PE"
    lot_size: int


def construct_option_symbol(groww_underlying: str, expiry: date, strike: int, right: str) -> str:
    """Builds the Groww/exchange-style contract string, e.g. NIFTY26SEP23200PE.

    Display/logging only. This is never trusted for order placement without
    being confirmed against the live instrument master via
    resolve_contract() — a one-character mismatch (month case, wrong
    expiry, a strike that doesn't exist) would target the wrong contract or
    fail outright, and there is no safe way to know that without checking.
    """
    month = expiry.strftime("%b").upper()
    year = expiry.strftime("%y")
    return f"{groww_underlying}{year}{month}{strike}{right}"


def resolve_contract(
    client: GrowwAPI,
    index_config: IndexConfig,
    strike: int,
    right: str,
    as_of: date | None = None,
) -> ResolvedContract:
    """Looks up the nearest-expiry, exact-strike option contract for this
    underlying directly from Groww's own instrument master.

    get_expiries/get_contracts/get_option_chain are gated behind Groww's
    paid Live Data API and return "Access forbidden" on a free-tier
    account; get_all_instruments() is not and returns the full instrument
    master including expiry_date/strike_price/lot_size per contract — so
    this is used as the (free, live-verified) source of truth instead of
    guessing an expiry date algorithmically.

    Raises ContractNotFoundError if nothing matches — the caller must never
    fall back to a guessed symbol in that case.
    """
    as_of = as_of or datetime.now(IST).date()
    instruments: pd.DataFrame = client.get_all_instruments()

    matches = instruments[
        (instruments["underlying_symbol"] == index_config.groww_underlying)
        & (instruments["instrument_type"] == right)
        & (instruments["strike_price"].astype(float) == float(strike))
    ].copy()
    if matches.empty:
        raise ContractNotFoundError(
            f"No {right} contract found for {index_config.groww_underlying} strike {strike}"
        )

    matches["expiry_date"] = pd.to_datetime(matches["expiry_date"]).dt.date
    upcoming = matches[matches["expiry_date"] >= as_of].sort_values("expiry_date")
    if upcoming.empty:
        raise ContractNotFoundError(
            f"No upcoming expiry found for {index_config.groww_underlying} {strike}{right}"
        )

    row = upcoming.iloc[0]
    return ResolvedContract(
        trading_symbol=str(row["trading_symbol"]),
        exchange=str(row["exchange"]),
        expiry_date=row["expiry_date"],
        strike=int(float(row["strike_price"])),
        right=right,
        lot_size=int(float(row["lot_size"])),
    )


def execute_market_order(client: GrowwAPI, contract: ResolvedContract, quantity: int) -> dict[str, Any]:
    """Places a live BUY market order for the resolved contract.

    NRML product (carry-forward), FNO segment, MARKET order type — matching
    the Pine script's delivery/carry-forward execution profile exactly.
    Never call this without first resolving the contract via
    resolve_contract() against the live instrument master.
    """
    exchange = GrowwAPI.EXCHANGE_NSE if contract.exchange == "NSE" else GrowwAPI.EXCHANGE_BSE
    return client.place_order(
        validity=GrowwAPI.VALIDITY_DAY,
        exchange=exchange,
        order_type=GrowwAPI.ORDER_TYPE_MARKET,
        product=GrowwAPI.PRODUCT_NRML,
        quantity=quantity,
        segment=GrowwAPI.SEGMENT_FNO,
        trading_symbol=contract.trading_symbol,
        transaction_type=GrowwAPI.TRANSACTION_TYPE_BUY,
    )
