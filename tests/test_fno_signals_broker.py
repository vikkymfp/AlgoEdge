from datetime import date

import pandas as pd
import pytest

from fno_signals import broker as broker_module
from fno_signals.broker import (
    ContractNotFoundError,
    ResolvedContract,
    construct_option_symbol,
    execute_market_order,
    resolve_contract,
)
from fno_signals.config import INDEX_MAP


def test_construct_option_symbol_matches_verified_live_format() -> None:
    # Verified against Groww's real instrument master: NIFTY26SEP28050CE.
    assert construct_option_symbol("NIFTY", date(2026, 9, 29), 28050, "CE") == "NIFTY26SEP28050CE"


def test_construct_option_symbol_bank_nifty() -> None:
    assert construct_option_symbol("BANKNIFTY", date(2026, 9, 29), 56300, "PE") == "BANKNIFTY26SEP56300PE"


def make_instrument_row(
    underlying: str, right: str, strike: int, expiry: str,
    exchange: str = "NSE", trading_symbol: str | None = None, lot_size: int = 75,
) -> dict:
    return {
        "underlying_symbol": underlying,
        "instrument_type": right,
        "strike_price": strike,
        "expiry_date": expiry,
        "exchange": exchange,
        "trading_symbol": trading_symbol or f"{underlying}{strike}{right}",
        "lot_size": lot_size,
    }


class FakeClient:
    def __init__(self, instruments: pd.DataFrame) -> None:
        self._instruments = instruments

    def get_all_instruments(self) -> pd.DataFrame:
        return self._instruments


def test_resolve_contract_picks_nearest_upcoming_expiry() -> None:
    rows = [
        make_instrument_row("NIFTY", "CE", 24500, "2026-09-25", trading_symbol="NIFTY26SEP24500CE"),
        make_instrument_row("NIFTY", "CE", 24500, "2026-10-02", trading_symbol="NIFTY26OCT24500CE"),
    ]
    client = FakeClient(pd.DataFrame(rows))

    contract = resolve_contract(client, INDEX_MAP[1], strike=24500, right="CE", as_of=date(2026, 9, 23))

    assert contract.trading_symbol == "NIFTY26SEP24500CE"
    assert contract.exchange == "NSE"
    assert contract.lot_size == 75


def test_resolve_contract_ignores_already_expired_contracts() -> None:
    rows = [
        make_instrument_row("NIFTY", "CE", 24500, "2026-09-20", trading_symbol="EXPIRED"),
        make_instrument_row("NIFTY", "CE", 24500, "2026-09-30", trading_symbol="NIFTY26SEP24500CE"),
    ]
    client = FakeClient(pd.DataFrame(rows))

    contract = resolve_contract(client, INDEX_MAP[1], strike=24500, right="CE", as_of=date(2026, 9, 23))

    assert contract.trading_symbol == "NIFTY26SEP24500CE"


def test_resolve_contract_raises_when_strike_does_not_exist() -> None:
    rows = [make_instrument_row("NIFTY", "CE", 24500, "2026-09-30")]
    client = FakeClient(pd.DataFrame(rows))

    with pytest.raises(ContractNotFoundError):
        resolve_contract(client, INDEX_MAP[1], strike=99999, right="CE", as_of=date(2026, 9, 23))


def test_resolve_contract_raises_when_no_upcoming_expiry() -> None:
    rows = [make_instrument_row("NIFTY", "CE", 24500, "2020-01-01")]
    client = FakeClient(pd.DataFrame(rows))

    with pytest.raises(ContractNotFoundError):
        resolve_contract(client, INDEX_MAP[1], strike=24500, right="CE", as_of=date(2026, 9, 23))


def test_resolve_contract_uses_bse_for_sensex() -> None:
    rows = [make_instrument_row(
        "SENSEX", "PE", 70200, "2026-09-24", exchange="BSE", trading_symbol="SENSEX26SEP70200PE",
    )]
    client = FakeClient(pd.DataFrame(rows))

    contract = resolve_contract(client, INDEX_MAP[3], strike=70200, right="PE", as_of=date(2026, 9, 23))

    assert contract.exchange == "BSE"


class RecordingClient:
    EXCHANGE_NSE = "NSE"
    EXCHANGE_BSE = "BSE"
    VALIDITY_DAY = "DAY"
    ORDER_TYPE_MARKET = "MARKET"
    PRODUCT_NRML = "NRML"
    SEGMENT_FNO = "FNO"
    TRANSACTION_TYPE_BUY = "BUY"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def place_order(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"status": "ok"}


def test_execute_market_order_uses_the_exact_required_settings(monkeypatch) -> None:
    monkeypatch.setattr(broker_module, "GrowwAPI", RecordingClient)
    client = RecordingClient()
    contract = ResolvedContract(
        trading_symbol="NIFTY26SEP24500CE", exchange="NSE", expiry_date=date(2026, 9, 30),
        strike=24500, right="CE", lot_size=75,
    )

    response = execute_market_order(client, contract, quantity=75)

    call = client.calls[0]
    assert call["exchange"] == "NSE"
    assert call["order_type"] == "MARKET"
    assert call["product"] == "NRML"
    assert call["segment"] == "FNO"
    assert call["transaction_type"] == "BUY"
    assert call["trading_symbol"] == "NIFTY26SEP24500CE"
    assert call["quantity"] == 75
    assert response["status"] == "ok"


def test_execute_market_order_uses_bse_for_sensex_contract(monkeypatch) -> None:
    monkeypatch.setattr(broker_module, "GrowwAPI", RecordingClient)
    client = RecordingClient()
    contract = ResolvedContract(
        trading_symbol="SENSEX26SEP70200PE", exchange="BSE", expiry_date=date(2026, 9, 24),
        strike=70200, right="PE", lot_size=20,
    )

    execute_market_order(client, contract, quantity=20)

    assert client.calls[0]["exchange"] == "BSE"
