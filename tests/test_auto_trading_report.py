from algoedge.auto_trading_report import compute_equity_curve


def make_order(source="algoedge.auto_trader", live=False, realized_pnl=None, created_at="t"):
    return {"source": source, "live": live, "realizedPnl": realized_pnl, "createdAt": created_at}


def test_no_orders_returns_empty_curve() -> None:
    assert compute_equity_curve([]) == []


def test_ignores_open_entries_with_no_realized_pnl() -> None:
    orders = [make_order(realized_pnl=None)]

    assert compute_equity_curve(orders) == []


def test_ignores_live_orders() -> None:
    orders = [make_order(live=True, realized_pnl=100.0)]

    assert compute_equity_curve(orders) == []


def test_ignores_other_sources() -> None:
    orders = [make_order(source="fno_signals", realized_pnl=100.0)]

    assert compute_equity_curve(orders) == []


def test_accumulates_in_chronological_order_from_newest_first_input() -> None:
    # newest-first, matching db.list_orders()'s real ordering
    orders = [
        make_order(realized_pnl=-50.0, created_at="third"),
        make_order(realized_pnl=150.0, created_at="second"),
        make_order(realized_pnl=200.0, created_at="first"),
    ]

    points = compute_equity_curve(orders)

    assert [p.closed_at for p in points] == ["first", "second", "third"]
    assert [p.realized_pnl for p in points] == [200.0, 150.0, -50.0]
    assert [p.cumulative_pnl for p in points] == [200.0, 350.0, 300.0]
