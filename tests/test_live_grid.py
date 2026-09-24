import json

from algoedge.live_grid import LiveGridService


def make_service() -> LiveGridService:
    return LiveGridService(broker=None, settings=None)  # type: ignore[arg-type]


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
