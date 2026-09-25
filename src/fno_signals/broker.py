from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIException

from algoedge.config import Settings, get_settings
from algoedge.token_service import BrokerNotConnectedError, TokenService
from fno_signals.config import IndexConfig

IST = ZoneInfo("Asia/Kolkata")

# The instrument master is ~140k rows and doesn't change intraday (same
# justification already established in algoedge/manual_trading.py's own
# cache for this exact Groww endpoint) - cached here too so a caller that
# resolves both legs of an option chain (e.g. CALL then PUT) in one request,
# or polls this on a timer, doesn't re-fetch the whole instrument master
# from Groww every time. Never affects order price/quantity - those come
# from the signal/event, not this lookup - only which already-listed
# contract a strike+right resolves to, which is stable within a trading day.
_INSTRUMENTS_CACHE_TTL = 300.0
_instruments_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def _get_instruments(client: GrowwAPI) -> pd.DataFrame:
    cached = _instruments_cache.get("all")
    now = time.monotonic()
    if cached is not None and now - cached[0] < _INSTRUMENTS_CACHE_TTL:
        return cached[1]
    instruments = client.get_all_instruments()
    _instruments_cache["all"] = (now, instruments)
    return instruments


def check_instrument_master(client: GrowwAPI) -> dict[str, Any]:
    """A real, live-checked status of Groww's instrument master lookup (the
    ~140k-row table resolve_contract() depends on for every strike/expiry/
    lot-size resolution across this app) - reuses the same cached
    _get_instruments() every live/backtest/diagnostic call already goes
    through, so this never issues an extra Groww request beyond what
    normal use already causes. Never assumed available just because the
    broker session itself is connected - a real query is what's checked."""
    try:
        instruments = _get_instruments(client)
        count = len(instruments) if instruments is not None else 0
        return {"available": count > 0, "count": count, "error": None}
    except GrowwAPIException as error:
        return {"available": False, "count": None, "error": str(error)}


class GrowwSessionError(RuntimeError):
    """Raised when the daily Groww session cannot be authenticated or verified."""


class GrowwSessionUnavailableError(GrowwAPIException):
    """Raised by SessionBoundClient when no Groww session can be obtained
    (it expired and could not be regenerated from the API key/secret). A
    GrowwAPIException so every existing `except GrowwAPIException` handler
    in the live flow treats it exactly like any other failed Groww call -
    the order is not placed and is never retried."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg=msg, code="SESSION_UNAVAILABLE")


class SessionBoundClient:
    """What `fno_signals --live` holds instead of one permanent client:
    every broker operation asks the process-lifetime TokenService for its
    current session first (TokenService.effective_client() regenerates an
    expired session from the stored API key/secret). Only the lookup is
    repeated - a call that fails, including place_order, is never retried
    here."""

    def __init__(self, token_service: TokenService) -> None:
        self.token_service = token_service

    def __getattr__(self, name: str) -> Any:
        try:
            client = self.token_service.effective_client()
        except BrokerNotConnectedError as error:
            raise GrowwSessionUnavailableError(str(error)) from error
        return getattr(client, name)


class ContractNotFoundError(RuntimeError):
    """Raised when no live, tradeable contract matches the requested strike/right.

    Callers must treat this as fatal for that signal — never fall back to a
    hand-constructed symbol for order placement.
    """


def generate_daily_session(settings: Settings | None = None) -> SessionBoundClient:
    """Authenticates against the Groww API and verifies the session with a
    real API call before returning a client bound to it for the whole run
    (see SessionBoundClient - a session that expires mid-run is regenerated
    on the next broker operation, not left dead until a restart).

    Delegates entirely to algoedge's TokenService - the same credential
    resolution the web dashboard uses (ALGOEDGE_GROWW_* settings as the
    initial/fallback values, overridden by any encrypted credentials saved
    from API Management) - so an update made in the dashboard applies here
    too. Callers must have run db.init_db() first for stored credentials to
    be visible. Raises GrowwSessionError on any failure - callers must
    terminate rather than proceed with an unverified session.
    """
    token_service = TokenService(settings or get_settings())
    try:
        token_service.effective_client()
    except BrokerNotConnectedError as error:
        raise GrowwSessionError(str(error)) from error
    return SessionBoundClient(token_service)


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
    instruments: pd.DataFrame = _get_instruments(client)

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
