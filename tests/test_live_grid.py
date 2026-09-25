import json

import pandas as pd
from growwapi.groww.exceptions import GrowwAPIException

from algoedge.live_grid import LiveGridService


def make_service() -> LiveGridService:
    return LiveGridService(broker=None, settings=None)  # type: ignore[arg-type]


class FakeGrowwClient:
    """A minimal stand-in for growwapi.GrowwAPI exposing only the methods
    account_snapshot() actually calls, each independently configurable to
    succeed or raise - real per-capability failure isolation is the whole
    point of what's being tested here."""

    def __init__(self, *, instruments: pd.DataFrame | None = None, **overrides):
        self._overrides = overrides
        self._instruments = instruments if instruments is not None else pd.DataFrame(
            [{"trading_symbol": "NIFTY26SEP24500CE"}]
        )

    def _call_or_raise(self, name: str, default):
        value = self._overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def get_user_profile(self):
        return self._call_or_raise("profile", {"nse_enabled": True, "active_segments": ["NSE", "FNO"]})

    def get_holdings_for_user(self):
        return self._call_or_raise("holdings", {"holdings": []})

    def get_positions_for_user(self, segment=None):
        return self._call_or_raise("positions", {"positions": []})

    def get_available_margin_details(self):
        return self._call_or_raise("margin", {"equity_margin_details": {"clear_cash": 100000.0}})

    def get_order_list(self, segment=None, page=0, page_size=25):
        key = "cash_orders" if segment == "CASH" else "fno_orders"
        return self._call_or_raise(key, {"order_list": []})

    def get_all_instruments(self):
        return self._call_or_raise("instruments", self._instruments)


class FakeBroker:
    def __init__(self, client: FakeGrowwClient) -> None:
        self.client = client


def make_service_with_client(client: FakeGrowwClient) -> LiveGridService:
    return LiveGridService(broker=FakeBroker(client), settings=None)  # type: ignore[arg-type]


def test_account_snapshot_reports_connected_when_everything_succeeds() -> None:
    service = make_service_with_client(FakeGrowwClient())

    snapshot = service.account_snapshot()

    assert snapshot["profile"]["connected"] is True
    assert snapshot["profile"]["error"] is None
    assert snapshot["marginStatus"] == {"available": True, "error": None}
    assert snapshot["holdingsStatus"] == {"available": True, "error": None}
    assert snapshot["positionsStatus"] == {"available": True, "error": None}
    assert snapshot["ordersStatus"] == {"available": True, "error": None}
    assert snapshot["instrumentMaster"]["available"] is True
    assert snapshot["instrumentMaster"]["count"] == 1
    assert snapshot["instrumentMaster"]["error"] is None


def test_account_snapshot_profile_failure_does_not_report_connected() -> None:
    # This is the exact bug being fixed: profile.connected must reflect
    # whether get_user_profile() actually succeeded, never hardcoded True.
    client = FakeGrowwClient(profile=GrowwAPIException(code="401", msg="invalid token"))
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["profile"]["connected"] is False
    assert "invalid token" in snapshot["profile"]["error"]
    # A failure on one capability must not affect the others - they were
    # fetched in parallel and are independently real.
    assert snapshot["holdingsStatus"]["available"] is True


def test_account_snapshot_never_leaks_more_than_the_broker_error_message() -> None:
    client = FakeGrowwClient(margin=GrowwAPIException(code="403", msg="Access forbidden"))
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["marginStatus"]["available"] is False
    assert "Access forbidden" in snapshot["marginStatus"]["error"]
    assert "password" not in snapshot["marginStatus"]["error"].lower()
    assert "token" not in snapshot["marginStatus"]["error"].lower()


def test_account_snapshot_orders_unavailable_if_either_segment_fails() -> None:
    client = FakeGrowwClient(fno_orders=GrowwAPIException(code="500", msg="temporary glitch"))
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["ordersStatus"]["available"] is False
    assert "temporary glitch" in snapshot["ordersStatus"]["error"]


def test_account_snapshot_instrument_master_unavailable_on_failure() -> None:
    client = FakeGrowwClient(instruments=GrowwAPIException(code="502", msg="instrument master unreachable"))
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["instrumentMaster"]["available"] is False
    assert snapshot["instrumentMaster"]["count"] is None
    assert "instrument master unreachable" in snapshot["instrumentMaster"]["error"]


def test_number_parses_numeric_strings() -> None:
    assert LiveGridService._number("12.5", None) == 12.5


def test_number_falls_back_to_default_on_bad_input() -> None:
    assert LiveGridService._number("not-a-number", 0) == 0
    assert LiveGridService._number(None, None) is None


def test_payload_extracts_known_list_keys() -> None:
    response = {"payload": {"positions": [{"a": 1}], "other": "ignored"}}

    assert LiveGridService._payload(response) == [{"a": 1}]


def test_payload_returns_dict_payload_unchanged_when_no_list_key_matches() -> None:
    response = {"payload": {"foo": "bar"}}

    assert LiveGridService._payload(response) == {"foo": "bar"}


def test_payload_extracts_holdings_key() -> None:
    # Confirmed live against the real account: get_holdings_for_user()
    # wraps its list under "holdings", the same shape as positions/
    # order_list/quote - previously unhandled, silently returning the
    # wrapper dict instead of the list (account.holdings.length would be
    # undefined on the frontend).
    response = {"holdings": [{"isin": "INE123A01011"}]}

    assert LiveGridService._payload(response) == [{"isin": "INE123A01011"}]


def test_payload_returns_empty_list_for_unexpected_shape() -> None:
    assert LiveGridService._payload({"payload": "unexpected"}) == []


def test_normalize_order_maps_broker_fields_and_grid_level() -> None:
    service = make_service()
    order = {
        "order_reference_id": "ref-1",
        "trading_symbol": "RELIANCE",
        "transaction_type": "buy",
        "quantity": "5",
        "price": "101.5",
        "order_status": "open",
    }

    normalized = service._normalize_order(order, ledger={"ref-1": 100})

    assert normalized == {
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 5.0,
        "actualPrice": 101.5,
        "gridLevel": 100,
        "status": "OPEN",
    }


def test_normalize_order_falls_back_to_average_fill_price() -> None:
    service = make_service()
    order = {"average_fill_price": "99.0", "order_status": "filled"}

    normalized = service._normalize_order(order, ledger={})

    assert normalized["actualPrice"] == 99.0
    assert normalized["gridLevel"] is None


def test_position_computes_side_and_unrealized_pnl(monkeypatch) -> None:
    service = make_service()
    monkeypatch.setattr(service, "_quote", lambda _symbol: {"ltp": "110.0"})
    position = {
        "trading_symbol": "RELIANCE",
        "quantity": "10",
        "net_price": "100.0",
        "realised_pnl": "50.0",
    }

    result = service._position(position)

    assert result == {
        "symbol": "RELIANCE",
        "quantity": 10.0,
        "side": "LONG",
        "averagePrice": 100.0,
        "ltp": 110.0,
        "unrealizedPnl": 100.0,
        "realizedPnl": 50.0,
    }


def test_position_marks_flat_and_omits_ltp_when_no_symbol() -> None:
    service = make_service()
    position = {"quantity": "0", "net_price": None, "realised_pnl": "0"}

    result = service._position(position)

    assert result["side"] == "FLAT"
    assert result["ltp"] is None
    assert result["unrealizedPnl"] is None


def test_position_marks_short_for_negative_quantity(monkeypatch) -> None:
    service = make_service()
    monkeypatch.setattr(service, "_quote", lambda _symbol: None)
    position = {"trading_symbol": "TCS", "quantity": "-5", "net_price": "3400.0"}

    result = service._position(position)

    assert result["side"] == "SHORT"
    assert result["ltp"] is None


def test_load_ledger_returns_empty_dict_when_file_missing(tmp_path) -> None:
    service = make_service()
    service.order_ledger_path = tmp_path / "missing.json"

    assert service._load_ledger() == {}


def test_load_ledger_returns_empty_dict_on_invalid_json(tmp_path) -> None:
    service = make_service()
    ledger_path = tmp_path / "grid_orders.json"
    ledger_path.write_text("not json", encoding="utf-8")
    service.order_ledger_path = ledger_path

    assert service._load_ledger() == {}


def test_load_ledger_reads_valid_json(tmp_path) -> None:
    service = make_service()
    ledger_path = tmp_path / "grid_orders.json"
    ledger_path.write_text(json.dumps({"ref-1": 100}), encoding="utf-8")
    service.order_ledger_path = ledger_path

    assert service._load_ledger() == {"ref-1": 100}


def test_account_snapshot_source_reflects_total_failure_not_a_fixed_label() -> None:
    # "LIVE BROKER DATA" must mean at least one real endpoint actually
    # responded - an expired token failing all five calls must never still
    # claim success via a hardcoded label.
    all_fail = GrowwAPIException(code="401", msg="Authentication failed")
    client = FakeGrowwClient(
        profile=all_fail, holdings=all_fail, positions=all_fail, margin=all_fail,
        cash_orders=all_fail, fno_orders=all_fail, instruments=all_fail,
    )
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["source"] == "GROWW DATA UNAVAILABLE"


def test_account_snapshot_source_stays_live_broker_data_when_partially_working() -> None:
    client = FakeGrowwClient(margin=GrowwAPIException(code="403", msg="Access forbidden"))
    service = make_service_with_client(client)

    snapshot = service.account_snapshot()

    assert snapshot["source"] == "LIVE BROKER DATA"
