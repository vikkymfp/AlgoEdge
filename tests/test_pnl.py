import pytest

from algoedge.cost_model import CostModel
from algoedge.order_manager import SimulatedAccount
from algoedge.pnl import (
    compute_live_realized_pnl,
    compute_paper_realized_pnl,
    compute_paper_unrealized_pnl,
    compute_realized_pnl,
)


def make_order(symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, price=100.0, outcome="SUCCESS", live=True, realized_pnl=None):
    return {
        "tradingSymbol": symbol, "side": side, "quantity": quantity, "price": price,
        "outcome": outcome, "live": live, "realizedPnl": realized_pnl,
    }


def test_live_realized_pnl_is_zero_with_only_an_open_entry() -> None:
    orders = [make_order(side="BUY", quantity=65, price=100.0)]

    total, trades = compute_live_realized_pnl(orders)

    assert total == 0.0
    assert trades == []


def test_live_realized_pnl_computes_a_simple_round_trip_profit() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0),
        make_order(side="SELL", quantity=65, price=120.0),
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((120.0 - 100.0) * 65)
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(1300.0)


def test_live_realized_pnl_computes_a_round_trip_loss() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0),
        make_order(side="SELL", quantity=65, price=90.0),
    ]

    total, _trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((90.0 - 100.0) * 65)


def test_live_realized_pnl_fifo_matches_multiple_entries() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0),
        make_order(side="BUY", quantity=65, price=110.0),
        make_order(side="SELL", quantity=65, price=120.0),  # closes the FIRST lot (100), not the second
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((120.0 - 100.0) * 65)
    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(100.0)


def test_live_realized_pnl_handles_a_partial_close() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0),
        make_order(side="SELL", quantity=30, price=120.0),
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((120.0 - 100.0) * 30)
    assert trades[0].quantity == pytest.approx(30)


def test_live_realized_pnl_handles_a_short_round_trip() -> None:
    # SELL first = writing/shorting, then BUY to cover.
    orders = [
        make_order(side="SELL", quantity=65, price=120.0),
        make_order(side="BUY", quantity=65, price=100.0),
    ]

    total, _trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((120.0 - 100.0) * 65)


def test_realized_trade_net_pnl_equals_gross_when_no_cost_model_given() -> None:
    orders = [make_order(side="BUY", quantity=65, price=100.0), make_order(side="SELL", quantity=65, price=120.0)]

    _total, trades = compute_live_realized_pnl(orders)

    assert trades[0].costs == 0.0
    assert trades[0].net_pnl == pytest.approx(trades[0].pnl)


def test_realized_trade_net_pnl_deducts_costs_for_a_long_trade() -> None:
    # LONG: opened with BUY @100, closed with SELL @120 - STT (sell-only)
    # and stamp duty (buy-only) must map to the correct leg.
    orders = [make_order(side="BUY", quantity=65, price=100.0), make_order(side="SELL", quantity=65, price=120.0)]
    cost_model = CostModel(brokerage_per_order=20.0, stt_percent_on_sell=0.05, stamp_duty_percent_on_buy=0.003)

    _total, trades = compute_live_realized_pnl(orders, cost_model)

    trade = trades[0]
    assert trade.side == "LONG"
    expected_stt = (120.0 * 65) * 0.05 / 100
    expected_stamp_duty = (100.0 * 65) * 0.003 / 100
    expected_costs = 40.0 + expected_stt + expected_stamp_duty
    assert trade.costs == pytest.approx(expected_costs)
    assert trade.net_pnl == pytest.approx(trade.pnl - expected_costs)


def test_realized_trade_net_pnl_maps_buy_sell_legs_correctly_for_a_short_trade() -> None:
    # SHORT: opened with SELL @120, closed with BUY @100 - STT still
    # applies to the leg that was a SELL (the entry here, not the exit),
    # and stamp duty to the leg that was a BUY (the exit here).
    orders = [make_order(side="SELL", quantity=65, price=120.0), make_order(side="BUY", quantity=65, price=100.0)]
    cost_model = CostModel(stt_percent_on_sell=0.05, stamp_duty_percent_on_buy=0.003)

    _total, trades = compute_live_realized_pnl(orders, cost_model)

    trade = trades[0]
    assert trade.side == "SHORT"
    # STT on the SELL leg's value (120 * 65, the entry price here) -
    # NOT on exit_price (100), which would be wrong for a short.
    expected_stt = (120.0 * 65) * 0.05 / 100
    expected_stamp_duty = (100.0 * 65) * 0.003 / 100
    assert trade.costs == pytest.approx(expected_stt + expected_stamp_duty)


def test_realized_pnl_report_net_total_sums_live_net_and_paper_gross() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0),
        make_order(side="SELL", quantity=65, price=120.0),
        make_order(side="SELL", quantity=1, price=None, live=False, realized_pnl=50.0),
    ]
    cost_model = CostModel(brokerage_per_order=20.0)

    report = compute_realized_pnl(orders, cost_model)

    live_costs = 40.0
    assert report.live_costs == pytest.approx(live_costs)
    assert report.live_net_total == pytest.approx((120.0 - 100.0) * 65 - live_costs)
    assert report.net_total == pytest.approx(report.live_net_total + 50.0)


def test_live_realized_pnl_ignores_paper_orders() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0, live=False),
        make_order(side="SELL", quantity=65, price=200.0, live=False),
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == 0.0
    assert trades == []


def test_live_realized_pnl_ignores_non_success_outcomes() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0, outcome="FAILED"),
        make_order(side="SELL", quantity=65, price=200.0, outcome="TIMEOUT"),
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == 0.0
    assert trades == []


def test_live_realized_pnl_skips_orders_with_no_known_fill_price() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=None),
        make_order(side="SELL", quantity=65, price=120.0),
    ]

    # the BUY has no price to match against, so the SELL just opens a new
    # (short) lot rather than being silently matched at a guessed price.
    total, trades = compute_live_realized_pnl(orders)

    assert total == 0.0
    assert trades == []


def test_live_realized_pnl_tracks_symbols_independently() -> None:
    orders = [
        make_order(symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, price=100.0),
        make_order(symbol="BANKNIFTY26SEP56000PE", side="BUY", quantity=30, price=200.0),
        make_order(symbol="NIFTY26SEP24500CE", side="SELL", quantity=65, price=110.0),
    ]

    total, trades = compute_live_realized_pnl(orders)

    assert total == pytest.approx((110.0 - 100.0) * 65)
    assert len(trades) == 1


def test_paper_realized_pnl_sums_stored_values() -> None:
    orders = [
        make_order(live=False, realized_pnl=150.0),
        make_order(live=False, realized_pnl=-50.0),
        make_order(live=False, realized_pnl=None),  # an entry order, no exit yet
    ]

    assert compute_paper_realized_pnl(orders) == pytest.approx(100.0)


def test_paper_realized_pnl_ignores_live_orders() -> None:
    orders = [make_order(live=True, realized_pnl=999.0)]

    assert compute_paper_realized_pnl(orders) == 0.0


def test_compute_realized_pnl_combines_live_and_paper_independently() -> None:
    orders_newest_first = [
        make_order(side="SELL", quantity=65, price=110.0, live=True),  # newest
        make_order(live=False, realized_pnl=50.0),
        make_order(side="BUY", quantity=65, price=100.0, live=True),  # oldest
    ]

    report = compute_realized_pnl(orders_newest_first)

    assert report.live_total == pytest.approx((110.0 - 100.0) * 65)
    assert report.paper_total == pytest.approx(50.0)
    assert report.total == pytest.approx(report.live_total + 50.0)


def test_paper_unrealized_pnl_is_none_when_flat() -> None:
    account = SimulatedAccount()

    assert compute_paper_unrealized_pnl(account, current_price=25000.0) is None


def test_paper_unrealized_pnl_is_none_when_current_price_unavailable() -> None:
    account = SimulatedAccount(quantity=10, average_price=100.0)

    assert compute_paper_unrealized_pnl(account, current_price=None) is None


def test_paper_unrealized_pnl_computes_gain_for_a_long_position() -> None:
    account = SimulatedAccount(quantity=10, average_price=100.0)

    assert compute_paper_unrealized_pnl(account, current_price=120.0) == pytest.approx(200.0)


def test_paper_unrealized_pnl_computes_loss_for_a_long_position() -> None:
    account = SimulatedAccount(quantity=10, average_price=100.0)

    assert compute_paper_unrealized_pnl(account, current_price=90.0) == pytest.approx(-100.0)
