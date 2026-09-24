import pytest

from algoedge.strategy_performance import compute_strategy_performance


def make_order(
    source, symbol="NIFTY26SEP24500CE", side="BUY", quantity=65, price=100.0,
    outcome="SUCCESS", live=True, realized_pnl=None,
):
    return {
        "source": source, "tradingSymbol": symbol, "side": side, "quantity": quantity,
        "price": price, "outcome": outcome, "live": live, "realizedPnl": realized_pnl,
    }


def test_no_orders_returns_empty_list() -> None:
    assert compute_strategy_performance([]) == []


def test_open_only_position_has_zero_closed_trades() -> None:
    orders = [make_order("fno_signals", side="BUY")]

    results = compute_strategy_performance(orders)

    assert results[0].source == "fno_signals"
    assert results[0].trades_closed == 0
    assert results[0].win_rate is None
    assert results[0].total_pnl == 0.0


def test_computes_win_rate_and_totals_for_a_live_source() -> None:
    # newest-first, matching db.list_orders()'s real ordering
    orders = [
        make_order("fno_signals", symbol="A", side="SELL", price=110.0),  # +10*65 win
        make_order("fno_signals", symbol="A", side="BUY", price=100.0),
        make_order("fno_signals", symbol="B", side="SELL", price=90.0),  # -10*65 loss
        make_order("fno_signals", symbol="B", side="BUY", price=100.0),
    ]

    results = compute_strategy_performance(orders)

    perf = results[0]
    assert perf.trades_closed == 2
    assert perf.wins == 1
    assert perf.losses == 1
    assert perf.win_rate == 50.0
    assert perf.total_pnl == pytest.approx(0.0)
    assert perf.best_trade == pytest.approx((110.0 - 100.0) * 65)
    assert perf.worst_trade == pytest.approx((90.0 - 100.0) * 65)


def test_computes_stats_for_a_paper_source_from_stored_realized_pnl() -> None:
    orders = [
        make_order("algoedge.auto_trader", live=False, realized_pnl=150.0),
        make_order("algoedge.auto_trader", live=False, realized_pnl=-50.0),
        make_order("algoedge.auto_trader", live=False, realized_pnl=None),  # open entry, ignored
    ]

    results = compute_strategy_performance(orders)

    perf = results[0]
    assert perf.trades_closed == 2
    assert perf.wins == 1
    assert perf.losses == 1
    assert perf.total_pnl == pytest.approx(100.0)
    assert perf.average_pnl == pytest.approx(50.0)


def test_keeps_different_sources_completely_independent() -> None:
    orders = [
        make_order("fno_signals", live=True, symbol="A", side="SELL", price=120.0),
        make_order("fno_signals", live=True, symbol="A", side="BUY", price=100.0),
        make_order("algoedge.auto_trader", live=False, realized_pnl=999.0),
    ]

    results = compute_strategy_performance(orders)

    by_source = {r.source: r for r in results}
    assert by_source["fno_signals"].total_pnl == pytest.approx((120.0 - 100.0) * 65)
    assert by_source["algoedge.auto_trader"].total_pnl == pytest.approx(999.0)


def test_never_matches_fills_from_different_sources_on_the_same_symbol() -> None:
    # Two different strategies both traded NIFTY - source A's SELL must
    # never close against source B's BUY.
    orders = [
        make_order("strategy_a", live=True, symbol="SHARED", side="SELL", price=200.0),
        make_order("strategy_b", live=True, symbol="SHARED", side="BUY", price=100.0),
    ]

    results = compute_strategy_performance(orders)

    # both remain open (no cross-source match), so no closed trades at all
    assert all(r.trades_closed == 0 for r in results)


def test_win_rate_is_zero_when_every_closed_trade_lost() -> None:
    orders = [
        make_order("fno_signals", live=True, side="SELL", price=90.0),
        make_order("fno_signals", live=True, side="BUY", price=100.0),
    ]

    results = compute_strategy_performance(orders)

    assert results[0].win_rate == 0.0
    assert results[0].wins == 0
    assert results[0].losses == 1
