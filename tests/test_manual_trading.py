from datetime import date

import pandas as pd
import pytest

from algoedge.manual_trading import (
    ManualOrderRequest,
    _instruments_cache,
    list_expiries,
    list_strikes,
    place_manual_order,
    resolve_manual_contract,
    validate_order_request,
)
from fno_signals.broker import ContractNotFoundError, ResolvedContract
from fno_signals.config import INDEX_MAP


def make_row(
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
        self.calls = 0

    def get_all_instruments(self) -> pd.DataFrame:
        self.calls += 1
        return self._instruments


@pytest.fixture(autouse=True)
def clear_instruments_cache():
    _instruments_cache.clear()
    yield
    _instruments_cache.clear()


def test_list_expiries_returns_sorted_unique_upcoming_dates() -> None:
    rows = [
        make_row("NIFTY", "CE", 24500, "2026-10-02"),
        make_row("NIFTY", "PE", 24500, "2026-09-25"),
        make_row("NIFTY", "CE", 24600, "2026-09-25"),  # duplicate date, different strike
        make_row("NIFTY", "CE", 24500, "2026-09-20"),  # already expired
    ]
    client = FakeClient(pd.DataFrame(rows))

    expiries = list_expiries(client, INDEX_MAP[1], as_of=date(2026, 9, 23))

    assert expiries == [date(2026, 9, 25), date(2026, 10, 2)]


def test_list_expiries_only_matches_the_requested_underlying() -> None:
    rows = [
        make_row("NIFTY", "CE", 24500, "2026-09-25"),
        make_row("BANKNIFTY", "CE", 56000, "2026-09-25"),
    ]
    client = FakeClient(pd.DataFrame(rows))

    expiries = list_expiries(client, INDEX_MAP[1], as_of=date(2026, 9, 23))

    assert expiries == [date(2026, 9, 25)]


def test_list_expiries_caches_the_instrument_master() -> None:
    rows = [make_row("NIFTY", "CE", 24500, "2026-09-25")]
    client = FakeClient(pd.DataFrame(rows))

    list_expiries(client, INDEX_MAP[1], as_of=date(2026, 9, 23))
    list_expiries(client, INDEX_MAP[1], as_of=date(2026, 9, 23))
    list_strikes(client, INDEX_MAP[1], date(2026, 9, 25))

    assert client.calls == 1


def test_list_strikes_returns_sorted_unique_strikes_for_the_expiry() -> None:
    rows = [
        make_row("NIFTY", "CE", 24700, "2026-09-25"),
        make_row("NIFTY", "PE", 24500, "2026-09-25"),  # same strike, other right
        make_row("NIFTY", "CE", 24600, "2026-09-25"),
        make_row("NIFTY", "CE", 24500, "2026-10-02"),  # different expiry
    ]
    client = FakeClient(pd.DataFrame(rows))

    strikes = list_strikes(client, INDEX_MAP[1], date(2026, 9, 25))

    assert strikes == [24500, 24600, 24700]


def test_resolve_manual_contract_succeeds_for_a_listed_expiry() -> None:
    rows = [make_row("NIFTY", "CE", 24500, "2026-09-25", trading_symbol="NIFTY26SEP24500CE")]
    client = FakeClient(pd.DataFrame(rows))

    contract = resolve_manual_contract(client, INDEX_MAP[1], date(2026, 9, 25), 24500, "CE")

    assert contract.trading_symbol == "NIFTY26SEP24500CE"


def test_resolve_manual_contract_rejects_an_expiry_that_does_not_exist_for_that_strike() -> None:
    # Only 2026-09-25 is listed; the user picked 2026-10-02 for this strike.
    rows = [make_row("NIFTY", "CE", 24500, "2026-09-25")]
    client = FakeClient(pd.DataFrame(rows))

    with pytest.raises(ContractNotFoundError):
        resolve_manual_contract(client, INDEX_MAP[1], date(2026, 10, 2), 24500, "CE")


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"right": "XX"}, "option type"),
        ({"side": "HOLD"}, "side"),
        ({"order_type": "STOP"}, "order type"),
        ({"product": "CNC"}, "product"),
        ({"lots": 0}, "positive"),
        ({"order_type": "LIMIT", "price": None}, "require a price"),
        ({"order_type": "SL", "price": 100.0, "trigger_price": None}, "require a trigger price"),
        ({"order_type": "SL_M", "trigger_price": None}, "require a trigger price"),
    ],
)
def test_validate_order_request_rejects_invalid_combinations(kwargs, message) -> None:
    base = {"right": "CE", "side": "BUY", "order_type": "MARKET", "lots": 1}
    base.update(kwargs)
    request = ManualOrderRequest(**base)

    with pytest.raises(ValueError, match=message):
        validate_order_request(request)


def test_validate_order_request_accepts_a_valid_market_order() -> None:
    request = ManualOrderRequest(right="CE", side="BUY", order_type="MARKET", lots=1)

    validate_order_request(request)  # should not raise


def test_validate_order_request_accepts_a_valid_limit_order() -> None:
    request = ManualOrderRequest(right="PE", side="SELL", order_type="LIMIT", lots=2, price=105.5)

    validate_order_request(request)  # should not raise


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def place_order(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"groww_order_id": "gid-1", "status": "ok"}


def make_contract(lot_size: int = 75, exchange: str = "NSE") -> ResolvedContract:
    return ResolvedContract(
        trading_symbol="NIFTY26SEP24500CE", exchange=exchange, expiry_date=date(2026, 9, 25),
        strike=24500, right="CE", lot_size=lot_size,
    )


def test_place_manual_order_computes_quantity_from_lots_times_live_lot_size() -> None:
    client = RecordingClient()
    request = ManualOrderRequest(right="CE", side="BUY", order_type="MARKET", lots=3)

    place_manual_order(client, make_contract(lot_size=75), request)

    assert client.calls[0]["quantity"] == 225  # 3 lots * 75


def test_place_manual_order_uses_exact_requested_order_type_and_side() -> None:
    client = RecordingClient()
    request = ManualOrderRequest(right="PE", side="SELL", order_type="LIMIT", lots=1, price=101.5)

    place_manual_order(client, make_contract(), request)

    call = client.calls[0]
    assert call["order_type"] == "LIMIT"
    assert call["transaction_type"] == "SELL"
    assert call["price"] == 101.5


def test_place_manual_order_uses_bse_for_sensex_contract() -> None:
    client = RecordingClient()
    request = ManualOrderRequest(right="CE", side="BUY", order_type="MARKET", lots=1)

    place_manual_order(client, make_contract(exchange="BSE"), request)

    assert client.calls[0]["exchange"] == "BSE"


def test_place_manual_order_rejects_invalid_request_before_calling_the_broker() -> None:
    client = RecordingClient()
    request = ManualOrderRequest(right="CE", side="BUY", order_type="LIMIT", lots=1, price=None)

    with pytest.raises(ValueError):
        place_manual_order(client, make_contract(), request)

    assert client.calls == []


def test_place_manual_order_passes_trigger_price_for_stop_loss_orders() -> None:
    client = RecordingClient()
    request = ManualOrderRequest(right="CE", side="BUY", order_type="SL", lots=1, price=100.0, trigger_price=99.5)

    place_manual_order(client, make_contract(), request)

    assert client.calls[0]["trigger_price"] == 99.5
