"""Phase 5 - the pure OptionContractResolver (algoedge.option_contract),
independent of Auto Trade/run_cycle wiring - see
tests/test_auto_trade_contract_resolution.py for the integration tests.
"""

from datetime import date

import pandas as pd
import pytest

from algoedge.option_contract import (
    AmbiguousContractError,
    NoMatchingInstrumentError,
    NoUpcomingExpiryError,
    resolve_option_contract,
)
from fno_signals.indicators import round_to_strike

AS_OF = date(2026, 9, 23)


def make_row(
    underlying: str, right: str, strike, expiry: str,
    trading_symbol: str | None = None, exchange: str = "NSE",
    groww_symbol: str | None = None, exchange_token: str | None = None,
) -> dict:
    return {
        "underlying_symbol": underlying, "instrument_type": right, "strike_price": strike,
        "expiry_date": expiry, "exchange": exchange,
        "trading_symbol": trading_symbol or f"{underlying}{strike}{right}",
        "groww_symbol": groww_symbol, "exchange_token": exchange_token,
    }


# ---------- CALL -> CE / PUT -> PE ----------


def test_call_right_resolves_a_ce_contract() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", 24500, "2026-09-30")])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.right == "CE"


def test_put_right_resolves_a_pe_contract() -> None:
    df = pd.DataFrame([make_row("NIFTY", "PE", 24500, "2026-09-30")])

    contract = resolve_option_contract(df, "NIFTY", 24500, "PE", as_of=AS_OF)

    assert contract.right == "PE"


def test_ce_never_matches_a_pe_row_at_the_same_strike() -> None:
    df = pd.DataFrame([make_row("NIFTY", "PE", 24500, "2026-09-30")])

    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


# ---------- ATM/current strike, and the existing (nearest, not lower) rule ----------


def test_atm_current_strike_resolves_exactly_not_a_neighboring_strike() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24450, "2026-09-30", trading_symbol="LOWER"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="EXACT"),
        make_row("NIFTY", "CE", 24550, "2026-09-30", trading_symbol="HIGHER"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "EXACT"
    assert contract.strike == 24500


def test_existing_strike_step_rule_is_nearest_not_lower_and_is_unchanged() -> None:
    # fno_signals.indicators.round_to_strike() - what TradeEvent.strike is
    # actually built from (fno_signals/strategy.py's run()) - uses
    # round-half-away-from-zero (nearest), never floor/"lower strike".
    # resolve_option_contract() must never recompute or second-guess this
    # choice, only validate the already-computed strike it's given -
    # pinning the current rule down so a future change can't silently
    # alter it without this test failing.
    assert round_to_strike(24537, 50) == 24550  # nearest 50, not the lower 24500
    assert round_to_strike(24512, 50) == 24500  # nearest 50, not the higher 24550


# ---------- weekly / monthly expiry ----------


def test_weekly_expiry_is_selected_when_it_is_the_nearest_upcoming() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-25", trading_symbol="WEEKLY"),  # nearest weekly
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="MONTHLY"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "WEEKLY"
    assert contract.expiry == date(2026, 9, 25)


def test_monthly_expiry_is_selected_when_no_weekly_is_listed() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="MONTHLY_ONLY")])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.expiry == date(2026, 9, 30)


def test_already_expired_contracts_are_ignored_in_favor_of_the_next_upcoming() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-18", trading_symbol="EXPIRED"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="UPCOMING"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "UPCOMING"


# ---------- no expiry ----------


def test_no_upcoming_expiry_raises_rather_than_inventing_one() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", 24500, "2026-09-18")])  # only an expired one exists

    with pytest.raises(NoUpcomingExpiryError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


# ---------- no matching instrument ----------


def test_no_matching_instrument_raises_for_an_unlisted_strike() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", 24500, "2026-09-30")])

    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(df, "NIFTY", 99999, "CE", as_of=AS_OF)


def test_empty_instrument_master_raises_no_matching_instrument() -> None:
    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(pd.DataFrame(), "NIFTY", 24500, "CE", as_of=AS_OF)


def test_instrument_master_missing_required_columns_raises_safely() -> None:
    df = pd.DataFrame([{"foo": "bar"}])

    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


# ---------- duplicate instrument matches ----------


def test_duplicate_distinct_contracts_at_the_same_expiry_raise_ambiguous() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="CONTRACT_A"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="CONTRACT_B"),
    ])

    with pytest.raises(AmbiguousContractError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


def test_exact_duplicate_rows_of_the_same_contract_are_not_ambiguous() -> None:
    # The instrument master occasionally lists the identical contract
    # twice (e.g. re-listed across a data refresh) - that's not the same
    # data-quality problem as two genuinely different contracts.
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="SAME"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="SAME"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "SAME"


# ---------- invalid/stale instrument data ----------


def test_a_row_with_unparseable_strike_is_skipped_not_crashed() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", "not-a-number", "2026-09-30", trading_symbol="BAD_STRIKE"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="GOOD"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "GOOD"


def test_a_row_with_unparseable_expiry_is_skipped_not_crashed() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "not-a-date", trading_symbol="BAD_EXPIRY"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="GOOD"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "GOOD"


def test_a_row_with_a_blank_trading_symbol_is_skipped_not_crashed() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="   "),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="GOOD"),
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.trading_symbol == "GOOD"


def test_only_invalid_rows_present_resolves_to_no_matching_instrument() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", "garbage", "2026-09-30")])

    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


# ---------- wrong underlying ----------


def test_wrong_underlying_never_matches_even_at_the_same_strike_and_expiry() -> None:
    df = pd.DataFrame([make_row("BANKNIFTY", "CE", 24500, "2026-09-30")])

    with pytest.raises(NoMatchingInstrumentError):
        resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)


# ---------- instrument identifier ----------


def test_instrument_id_prefers_groww_symbol_when_present() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-30", groww_symbol="NIFTY-GS-1", exchange_token="123")
    ])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.instrument_id == "NIFTY-GS-1"


def test_instrument_id_falls_back_to_exchange_token() -> None:
    df = pd.DataFrame([make_row("NIFTY", "CE", 24500, "2026-09-30", exchange_token="456")])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.instrument_id == "456"


def test_instrument_id_is_none_when_the_master_provides_neither() -> None:
    df = pd.DataFrame([{
        "underlying_symbol": "NIFTY", "instrument_type": "CE", "strike_price": 24500,
        "expiry_date": "2026-09-30", "trading_symbol": "NIFTY24500CE",
    }])

    contract = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert contract.instrument_id is None


# ---------- deterministic repeated resolution ----------


def test_repeated_resolution_of_identical_inputs_is_deterministic() -> None:
    df = pd.DataFrame([
        make_row("NIFTY", "CE", 24500, "2026-09-25", trading_symbol="WEEKLY"),
        make_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="MONTHLY"),
    ])

    first = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)
    second = resolve_option_contract(df, "NIFTY", 24500, "CE", as_of=AS_OF)

    assert first == second
