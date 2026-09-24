import pytest

from algoedge.cost_model import CostModel, compute_trade_costs


def test_default_cost_model_is_unconfigured() -> None:
    assert CostModel().is_configured() is False


def test_any_nonzero_field_marks_it_configured() -> None:
    assert CostModel(brokerage_per_order=20.0).is_configured() is True
    assert CostModel(stt_percent_on_sell=0.05).is_configured() is True


def test_all_zero_cost_model_produces_zero_costs() -> None:
    costs = compute_trade_costs(100.0, 120.0, 75, CostModel())

    assert costs == 0.0


def test_brokerage_charged_per_leg() -> None:
    costs = compute_trade_costs(100.0, 120.0, 75, CostModel(brokerage_per_order=20.0))

    assert costs == pytest.approx(40.0)  # two legs, 20 each


def test_stt_applies_only_to_sell_value() -> None:
    costs = compute_trade_costs(100.0, 120.0, 75, CostModel(stt_percent_on_sell=0.05))

    sell_value = 120.0 * 75
    assert costs == pytest.approx(sell_value * 0.05 / 100)


def test_exchange_charges_apply_to_both_legs() -> None:
    costs = compute_trade_costs(100.0, 120.0, 75, CostModel(exchange_charges_percent=0.05))

    buy_value = 100.0 * 75
    sell_value = 120.0 * 75
    assert costs == pytest.approx((buy_value + sell_value) * 0.05 / 100)


def test_stamp_duty_applies_only_to_buy_value() -> None:
    costs = compute_trade_costs(100.0, 120.0, 75, CostModel(stamp_duty_percent_on_buy=0.003))

    buy_value = 100.0 * 75
    assert costs == pytest.approx(buy_value * 0.003 / 100)


def test_gst_applies_to_brokerage_plus_exchange_charges_only() -> None:
    cost_model = CostModel(brokerage_per_order=20.0, exchange_charges_percent=0.05, gst_percent=18.0)

    costs = compute_trade_costs(100.0, 120.0, 75, cost_model)

    brokerage = 40.0
    exchange = (100.0 * 75 + 120.0 * 75) * 0.05 / 100
    expected_gst = (brokerage + exchange) * 18.0 / 100
    assert costs == pytest.approx(brokerage + exchange + expected_gst)


def test_full_cost_stack_adds_up() -> None:
    cost_model = CostModel(
        brokerage_per_order=20.0, stt_percent_on_sell=0.05, exchange_charges_percent=0.05,
        gst_percent=18.0, stamp_duty_percent_on_buy=0.003,
    )

    costs = compute_trade_costs(100.0, 120.0, 75, cost_model)

    buy_value, sell_value = 100.0 * 75, 120.0 * 75
    brokerage = 40.0
    stt = sell_value * 0.05 / 100
    exchange = (buy_value + sell_value) * 0.05 / 100
    stamp_duty = buy_value * 0.003 / 100
    gst = (brokerage + exchange) * 18.0 / 100
    assert costs == pytest.approx(brokerage + stt + exchange + stamp_duty + gst)
