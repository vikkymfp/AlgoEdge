from algoedge.manual_trades import compute_manual_trades


def make_order(
    symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, price=100.0,
    outcome="SUCCESS", live=True, source="algoedge.manual_trading", created_at="2026-09-24T10:00:00",
):
    return {
        "tradingSymbol": symbol, "side": side, "quantity": quantity, "price": price,
        "outcome": outcome, "live": live, "source": source, "createdAt": created_at,
    }


def test_open_long_position_shows_as_open_with_no_pnl() -> None:
    # newest-first (only order)
    orders = [make_order(side="BUY", quantity=65, price=100.0)]

    trades = compute_manual_trades(orders)

    assert len(trades) == 1
    assert trades[0].status == "OPEN"
    assert trades[0].side == "LONG"
    assert trades[0].entry_price == 100.0
    assert trades[0].pnl is None
    assert trades[0].exit_price is None


def test_open_short_position_is_tracked_correctly() -> None:
    orders = [make_order(side="SELL", quantity=65, price=120.0)]

    trades = compute_manual_trades(orders)

    assert trades[0].status == "OPEN"
    assert trades[0].side == "SHORT"
    assert trades[0].entry_price == 120.0


def test_closed_long_round_trip_shows_entry_exit_and_pnl() -> None:
    # newest-first, matching db.list_orders()'s real ordering
    orders = [
        make_order(side="SELL", quantity=65, price=120.0, created_at="2026-09-24T14:00:00"),
        make_order(side="BUY", quantity=65, price=100.0, created_at="2026-09-24T10:00:00"),
    ]

    trades = compute_manual_trades(orders)

    assert len(trades) == 1
    assert trades[0].status == "CLOSED"
    assert trades[0].side == "LONG"
    assert trades[0].entry_price == 100.0
    assert trades[0].exit_price == 120.0
    assert trades[0].pnl == (120.0 - 100.0) * 65
    assert trades[0].entry_time == "2026-09-24T10:00:00"
    assert trades[0].exit_time == "2026-09-24T14:00:00"


def test_closed_short_round_trip_computes_correct_pnl() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0, created_at="2026-09-24T14:00:00"),  # covers
        make_order(side="SELL", quantity=65, price=120.0, created_at="2026-09-24T10:00:00"),  # opens short
    ]

    trades = compute_manual_trades(orders)

    assert trades[0].side == "SHORT"
    assert trades[0].pnl == (120.0 - 100.0) * 65


def test_ignores_orders_from_other_sources() -> None:
    orders = [
        make_order(side="BUY", quantity=65, price=100.0, source="fno_signals"),
        make_order(side="BUY", quantity=30, price=200.0, source="algoedge.auto_trader", live=False),
    ]

    trades = compute_manual_trades(orders)

    assert trades == []


def test_partial_close_leaves_the_remainder_open() -> None:
    orders = [
        make_order(side="SELL", quantity=30, price=120.0, created_at="2026-09-24T14:00:00"),
        make_order(side="BUY", quantity=65, price=100.0, created_at="2026-09-24T10:00:00"),
    ]

    trades = compute_manual_trades(orders)

    statuses = {t.status: t for t in trades}
    assert statuses["CLOSED"].quantity == 30
    assert statuses["OPEN"].quantity == 35
    assert statuses["OPEN"].entry_price == 100.0


def test_multiple_symbols_are_tracked_independently() -> None:
    orders = [
        make_order(symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, price=100.0),
        make_order(symbol="BANKNIFTY26SEP56000PE", side="BUY", quantity=30, price=200.0),
    ]

    trades = compute_manual_trades(orders)

    symbols = {t.trading_symbol for t in trades}
    assert symbols == {"NIFTY26SEP24500CE", "BANKNIFTY26SEP56000PE"}
    assert all(t.status == "OPEN" for t in trades)


def test_no_manual_orders_returns_empty_list() -> None:
    assert compute_manual_trades([]) == []
