from datetime import date, datetime

from algoedge.daily_summary import compute_daily_summary
from algoedge.risk_manager import IST


def make_signal(created_at, action="BUY", source="algoedge.auto_trader"):
    return {"createdAt": created_at, "action": action, "source": source}


def make_order(
    created_at, side="BUY", quantity=65, price=100.0, outcome="SUCCESS",
    live=True, realized_pnl=None, symbol="NIFTY26SEP24500CE",
):
    return {
        "createdAt": created_at, "tradingSymbol": symbol, "side": side, "quantity": quantity,
        "price": price, "outcome": outcome, "live": live, "realizedPnl": realized_pnl,
    }


DAY1 = datetime(2026, 9, 22, 10, 0, tzinfo=IST)
DAY2 = datetime(2026, 9, 23, 10, 0, tzinfo=IST)


def test_groups_signals_by_calendar_day_and_action() -> None:
    signals = [
        make_signal(DAY1, action="BUY"),
        make_signal(DAY1, action="HOLD"),
        make_signal(DAY2, action="BUY"),
    ]

    summaries = compute_daily_summary([], signals)

    by_day = {s.day: s for s in summaries}
    assert by_day[date(2026, 9, 22)].signals_total == 2
    assert by_day[date(2026, 9, 22)].signals_by_action == {"BUY": 1, "HOLD": 1}
    assert by_day[date(2026, 9, 23)].signals_total == 1


def test_returns_days_newest_first() -> None:
    signals = [make_signal(DAY1), make_signal(DAY2)]

    summaries = compute_daily_summary([], signals)

    assert [s.day for s in summaries] == [date(2026, 9, 23), date(2026, 9, 22)]


def test_counts_orders_by_live_paper_and_outcome() -> None:
    orders = [
        make_order(DAY1, live=True, outcome="SUCCESS"),
        make_order(DAY1, live=False, outcome="SUCCESS"),
        make_order(DAY1, live=True, outcome="FAILED"),
    ]

    summaries = compute_daily_summary(orders, [])

    summary = summaries[0]
    assert summary.orders_placed == 3
    assert summary.orders_live == 2
    assert summary.orders_paper == 1
    assert summary.orders_by_outcome == {"SUCCESS": 2, "FAILED": 1}


def test_live_realized_pnl_is_attributed_to_the_closing_day_not_the_opening_day() -> None:
    # newest-first, matching db.list_orders()'s real ordering
    orders = [
        make_order(DAY2, side="SELL", price=120.0, live=True),
        make_order(DAY1, side="BUY", price=100.0, live=True),
    ]

    summaries = compute_daily_summary(orders, [])

    by_day = {s.day: s for s in summaries}
    assert by_day[date(2026, 9, 22)].realized_pnl == 0.0  # opening day: nothing realized yet
    assert by_day[date(2026, 9, 23)].realized_pnl_live == (120.0 - 100.0) * 65  # closing day
    assert by_day[date(2026, 9, 23)].realized_pnl == (120.0 - 100.0) * 65


def test_paper_realized_pnl_is_attributed_to_the_order_that_carries_it() -> None:
    orders = [
        make_order(DAY1, live=False, realized_pnl=None),  # entry, no P&L yet
        make_order(DAY2, live=False, realized_pnl=150.0),  # exit
    ]

    summaries = compute_daily_summary(orders, [])

    by_day = {s.day: s for s in summaries}
    assert by_day[date(2026, 9, 22)].realized_pnl_paper == 0.0
    assert by_day[date(2026, 9, 23)].realized_pnl_paper == 150.0
    assert by_day[date(2026, 9, 23)].realized_pnl == 150.0


def test_combines_live_and_paper_realized_pnl_on_the_same_day() -> None:
    # newest-first, matching db.list_orders()'s real ordering
    orders = [
        make_order(DAY2, live=False, realized_pnl=50.0),  # +50 paper
        make_order(DAY2, side="SELL", price=110.0, live=True, symbol="A"),  # +650 live
        make_order(DAY1, side="BUY", price=100.0, live=True, symbol="A"),
    ]

    summaries = compute_daily_summary(orders, [])

    summary = next(s for s in summaries if s.day == date(2026, 9, 23))
    assert summary.realized_pnl_live == (110.0 - 100.0) * 65
    assert summary.realized_pnl_paper == 50.0
    assert summary.realized_pnl == summary.realized_pnl_live + 50.0


def test_empty_input_returns_empty_summary() -> None:
    assert compute_daily_summary([], []) == []


def test_orders_and_signals_with_no_timestamp_are_ignored_not_crashed() -> None:
    orders = [make_order(None)]
    signals = [make_signal(None)]

    summaries = compute_daily_summary(orders, signals)

    assert summaries == []
