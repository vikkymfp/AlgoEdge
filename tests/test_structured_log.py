import logging

from algoedge.structured_log import log_signal_decision


def test_log_signal_decision_emits_one_info_line(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="algoedge.signal_decisions"):
        log_signal_decision(
            signal_id="AE-fno_signals-NIFTY-CALL-202609241015",
            underlying_symbol="NIFTY", underlying_price=24582.0, direction="CALL",
            strike=24550, option_type="CE", risk_status="PASSED", position="OPEN",
            option_symbol="NIFTY26SEP24550CE", order_side="BUY", requested_quantity=75,
            broker_order_id="GRW123456", fill_quantity=75, fill_price=145.5, status="FILLED",
        )

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "signal_id=AE-fno_signals-NIFTY-CALL-202609241015" in message
    assert "underlying=NIFTY" in message
    assert "status=FILLED" in message
    assert "broker_order=GRW123456" in message


def test_log_signal_decision_handles_none_fields_gracefully(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="algoedge.signal_decisions"):
        log_signal_decision(
            signal_id="AE-test", underlying_symbol="NIFTY", underlying_price=24582.0,
            direction="CALL", risk_status="REJECTED", position="FLAT", status="ORDER_FAILED",
        )

    assert len(caplog.records) == 1
    assert "status=ORDER_FAILED" in caplog.records[0].getMessage()
