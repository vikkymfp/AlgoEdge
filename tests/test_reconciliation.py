from algoedge.reconciliation import (
    compute_expected_positions,
    find_unconfirmed_orders,
    reconcile,
)


def make_order(
    trading_symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, outcome="SUCCESS", live=True,
    filled_quantity=None,
):
    return {
        "tradingSymbol": trading_symbol, "side": side, "quantity": quantity, "outcome": outcome,
        "live": live, "filledQuantity": filled_quantity,
    }


def test_compute_expected_positions_sums_buys_and_sells() -> None:
    orders = [
        make_order(side="BUY", quantity=65),
        make_order(side="BUY", quantity=65),
        make_order(side="SELL", quantity=65),
    ]

    expected = compute_expected_positions(orders)

    assert expected == {"NIFTY26SEP24500CE": 65}


def test_compute_expected_positions_nets_a_fully_closed_position_to_zero() -> None:
    orders = [make_order(side="BUY", quantity=65), make_order(side="SELL", quantity=65)]

    expected = compute_expected_positions(orders)

    assert expected == {}


def test_compute_expected_positions_ignores_paper_orders() -> None:
    orders = [make_order(live=False, quantity=100)]

    assert compute_expected_positions(orders) == {}


def test_compute_expected_positions_ignores_failed_and_cancelled_orders() -> None:
    orders = [make_order(outcome="FAILED"), make_order(outcome="CANCELLED")]

    assert compute_expected_positions(orders) == {}


def test_compute_expected_positions_excludes_unconfirmed_outcomes() -> None:
    # TIMEOUT/UNKNOWN orders are genuinely ambiguous - never guessed into
    # the expected quantity either way.
    orders = [make_order(outcome="TIMEOUT"), make_order(outcome="UNKNOWN")]

    assert compute_expected_positions(orders) == {}


def test_compute_expected_positions_tracks_multiple_symbols_independently() -> None:
    orders = [
        make_order(trading_symbol="NIFTY26SEP24500CE", side="BUY", quantity=65),
        make_order(trading_symbol="BANKNIFTY26SEP56000PE", side="BUY", quantity=30),
    ]

    expected = compute_expected_positions(orders)

    assert expected == {"NIFTY26SEP24500CE": 65, "BANKNIFTY26SEP56000PE": 30}


def test_find_unconfirmed_orders_returns_only_live_timeout_or_unknown() -> None:
    orders = [
        make_order(outcome="SUCCESS"),
        make_order(outcome="TIMEOUT"),
        make_order(outcome="UNKNOWN"),
        make_order(outcome="TIMEOUT", live=False),  # paper - not a real unconfirmed order
    ]

    unconfirmed = find_unconfirmed_orders(orders)

    assert len(unconfirmed) == 2
    assert all(order["outcome"] in ("TIMEOUT", "UNKNOWN") for order in unconfirmed)


# -- partial fills ----------------------------------------------


def test_compute_expected_positions_counts_only_the_filled_portion_of_a_partial_order() -> None:
    # Requested 75, only 25 actually filled - the position is 25, not 75
    # and not 0 (an earlier version of this function excluded PARTIAL
    # entirely, silently ignoring a real, partial position).
    orders = [make_order(side="BUY", quantity=75, outcome="PARTIAL", filled_quantity=25)]

    expected = compute_expected_positions(orders)

    assert expected == {"NIFTY26SEP24500CE": 25}


def test_compute_expected_positions_combines_a_partial_fill_with_a_later_success() -> None:
    # Partially filled at 25, then a separate order completes the rest.
    orders = [
        make_order(side="BUY", quantity=75, outcome="PARTIAL", filled_quantity=25),
        make_order(side="BUY", quantity=50, outcome="SUCCESS"),
    ]

    expected = compute_expected_positions(orders)

    assert expected == {"NIFTY26SEP24500CE": 75}


def test_partial_order_is_both_counted_and_flagged_as_unconfirmed() -> None:
    # Both true at once: the filled 25 is a real position (counted), and
    # the unfilled 50 remaining is still unresolved (flagged for review) -
    # neither view alone would be honest about a partial fill.
    orders = [make_order(side="BUY", quantity=75, outcome="PARTIAL", filled_quantity=25)]

    expected = compute_expected_positions(orders)
    unconfirmed = find_unconfirmed_orders(orders)

    assert expected == {"NIFTY26SEP24500CE": 25}
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["outcome"] == "PARTIAL"


def test_reconcile_matches_when_broker_position_equals_the_filled_portion() -> None:
    orders = [make_order(side="BUY", quantity=75, outcome="PARTIAL", filled_quantity=25)]
    live_positions = [{"trading_symbol": "NIFTY26SEP24500CE", "quantity": 25}]

    report = reconcile(orders, live_positions)

    assert all(comparison.matches for comparison in report.comparisons)
    assert len(report.unconfirmed_orders) == 1  # still flagged, even though it reconciles cleanly


def test_reconcile_reports_a_match_when_db_and_broker_agree() -> None:
    orders = [make_order(side="BUY", quantity=65)]
    live_positions = [{"trading_symbol": "NIFTY26SEP24500CE", "quantity": "65"}]

    report = reconcile(orders, live_positions)

    assert len(report.comparisons) == 1
    assert report.comparisons[0].matches is True
    assert report.comparisons[0].expected_quantity == 65
    assert report.comparisons[0].actual_quantity == 65


def test_reconcile_flags_a_quantity_mismatch() -> None:
    orders = [make_order(side="BUY", quantity=65)]
    live_positions = [{"trading_symbol": "NIFTY26SEP24500CE", "quantity": "130"}]

    report = reconcile(orders, live_positions)

    assert report.comparisons[0].matches is False


def test_reconcile_flags_a_position_the_db_does_not_know_about() -> None:
    # e.g. a trade placed manually in the Groww app, outside AlgoEdge entirely.
    orders = []
    live_positions = [{"trading_symbol": "SENSEX26SEP70200PE", "quantity": "20"}]

    report = reconcile(orders, live_positions)

    assert len(report.comparisons) == 1
    assert report.comparisons[0].expected_quantity == 0
    assert report.comparisons[0].actual_quantity == 20
    assert report.comparisons[0].matches is False


def test_reconcile_flags_a_position_the_broker_no_longer_shows() -> None:
    # e.g. AlgoEdge thinks a position is open but it was actually squared off elsewhere.
    orders = [make_order(side="BUY", quantity=65)]
    live_positions = []

    report = reconcile(orders, live_positions)

    assert report.comparisons[0].expected_quantity == 65
    assert report.comparisons[0].actual_quantity == 0
    assert report.comparisons[0].matches is False


def test_reconcile_with_no_orders_and_no_positions_is_clean() -> None:
    report = reconcile([], [])

    assert report.comparisons == []
    assert report.unconfirmed_orders == []


def test_reconcile_surfaces_unconfirmed_orders_alongside_comparisons() -> None:
    orders = [make_order(side="BUY", quantity=65, outcome="SUCCESS"), make_order(outcome="TIMEOUT")]
    live_positions = [{"trading_symbol": "NIFTY26SEP24500CE", "quantity": "65"}]

    report = reconcile(orders, live_positions)

    assert report.comparisons[0].matches is True
    assert len(report.unconfirmed_orders) == 1
