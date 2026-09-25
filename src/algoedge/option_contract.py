from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class OptionContract:
    """A validated, instrument-master-confirmed option contract - the only
    shape Auto Trade's paper flow is allowed to open a position against.

    Deliberately independent of `fno_signals.broker.ResolvedContract`: this
    module has zero dependency on growwapi/TokenService, so anything that
    imports it (algoedge.auto_trader, algoedge.order_manager) stays exactly
    as free of a live-broker dependency as it was before Phase 5 - the
    actual instrument-master fetch happens elsewhere (web_server.py, which
    already legitimately owns a broker connection for Manual Trading/Live
    Grid) and only the resolved *result* (or None) ever reaches Auto Trade.
    """

    trading_symbol: str
    underlying: str  # e.g. "NIFTY" - Groww's underlying_symbol, not the display name
    right: str  # "CE" | "PE"
    strike: int
    expiry: date
    instrument_id: str | None = None  # Groww's groww_symbol/exchange_token, if the master provides one


class OptionContractResolutionError(RuntimeError):
    """Base class for every reason resolution can fail - callers (Auto
    Trade) must treat all of these identically: no valid contract, no
    paper order. Subclasses exist so tests can assert *which* failure mode
    occurred without string-matching a message."""


class NoMatchingInstrumentError(OptionContractResolutionError):
    """No instrument at all matches this underlying/right/strike combination."""


class NoUpcomingExpiryError(OptionContractResolutionError):
    """The underlying/right/strike combination exists, but every listed
    expiry for it has already passed `as_of` - never invent a future
    expiry that isn't actually in the instrument master."""


class AmbiguousContractError(OptionContractResolutionError):
    """More than one instrument matches the same underlying/right/strike/
    nearest-expiry - a data-quality condition that must never be resolved
    by silently picking one; the caller must refuse instead."""


_REQUIRED_COLUMNS = {"underlying_symbol", "instrument_type", "strike_price", "expiry_date", "trading_symbol"}


def resolve_option_contract(
    instruments: pd.DataFrame,
    underlying: str,
    strike: int,
    right: str,
    as_of: date,
) -> OptionContract:
    """Resolves a strike+right (already computed by the canonical strategy
    - see fno_signals.strategy.round_to_strike()/TradeEvent.strike - this
    function never recomputes or second-guesses that ATM/strike-step
    choice) against a live-fetched instrument-master DataFrame.

    Reuses the exact matching convention already established and verified
    live by fno_signals.broker.resolve_contract(): exact strike match,
    exact right match, nearest upcoming expiry >= as_of. Adds the explicit
    safety this phase asks for on top: rows with unparseable/missing
    strike or expiry data are dropped rather than crashing or silently
    matching, and more than one instrument surviving at the same nearest
    expiry raises rather than picking one arbitrarily.

    Raises a specific `OptionContractResolutionError` subclass for every
    failure mode - never returns a guessed/partial contract.
    """
    if instruments is None or instruments.empty or not _REQUIRED_COLUMNS.issubset(instruments.columns):
        raise NoMatchingInstrumentError(
            f"Instrument master is empty or missing required columns for {underlying} {strike}{right}"
        )

    candidates = instruments[
        (instruments["underlying_symbol"] == underlying)
        & (instruments["instrument_type"] == right)
    ].copy()

    # Invalid/stale row data (a non-numeric strike, an unparseable expiry,
    # a missing trading_symbol) is dropped defensively rather than crashing
    # resolution or letting a malformed row masquerade as a real contract.
    candidates["strike_price"] = pd.to_numeric(candidates["strike_price"], errors="coerce")
    candidates["expiry_date"] = pd.to_datetime(candidates["expiry_date"], errors="coerce")
    candidates = candidates.dropna(subset=["strike_price", "expiry_date", "trading_symbol"])
    candidates = candidates[candidates["trading_symbol"].astype(str).str.strip() != ""]

    candidates = candidates[candidates["strike_price"] == float(strike)]
    if candidates.empty:
        raise NoMatchingInstrumentError(f"No {right} contract found for {underlying} strike {strike}")

    candidates["expiry_date"] = candidates["expiry_date"].dt.date
    upcoming = candidates[candidates["expiry_date"] >= as_of]
    if upcoming.empty:
        raise NoUpcomingExpiryError(f"No upcoming expiry found for {underlying} {strike}{right}")

    nearest_expiry = upcoming["expiry_date"].min()
    nearest = upcoming[upcoming["expiry_date"] == nearest_expiry]
    # Ambiguity is judged on genuinely distinct contracts, not incidental
    # duplicate rows the instrument master sometimes carries for the exact
    # same trading_symbol (e.g. re-listed across a data refresh) - the
    # trading_symbol is Groww's own unique identifier for a contract.
    distinct_symbols = nearest["trading_symbol"].astype(str).unique()
    if len(distinct_symbols) > 1:
        raise AmbiguousContractError(
            f"Multiple distinct contracts match {underlying} {strike}{right} at expiry {nearest_expiry}: "
            f"{sorted(distinct_symbols)}"
        )

    row = nearest.iloc[0]
    instrument_id = None
    for id_column in ("groww_symbol", "exchange_token"):
        if id_column in row.index and pd.notna(row[id_column]):
            instrument_id = str(row[id_column])
            break

    return OptionContract(
        trading_symbol=str(row["trading_symbol"]),
        underlying=underlying,
        right=right,
        strike=int(row["strike_price"]),
        expiry=row["expiry_date"],
        instrument_id=instrument_id,
    )
