import pytest

from algoedge.order_manager import OrderManager, SimulatedAccount


def test_buy_fill_updates_quantity_cash_and_average_price() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    result = manager.place("BUY", price=100.0, quantity=10)

    assert result.status == "PLACED"
    assert result.realized_pnl == 0.0
    assert account.quantity == 10
    assert account.average_price == pytest.approx(100.0)
    assert account.cash == pytest.approx(99_000.0)


def test_repeated_buys_average_the_entry_price() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    manager.place("BUY", price=100.0, quantity=10)
    manager.place("BUY", price=120.0, quantity=10)

    assert account.quantity == 20
    assert account.average_price == pytest.approx(110.0)


def test_sell_realizes_profit_and_clears_position() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=10)

    result = manager.place("SELL", price=110.0, quantity=10)

    assert result.status == "PLACED"
    assert result.realized_pnl == pytest.approx(100.0)
    assert account.quantity == 0
    assert account.average_price is None


def test_sell_realizes_loss() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=10)

    result = manager.place("SELL", price=90.0, quantity=10)

    assert result.realized_pnl == pytest.approx(-100.0)


def test_partial_sell_keeps_remaining_position_at_same_average_price() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=10)

    manager.place("SELL", price=110.0, quantity=4)

    assert account.quantity == 6
    assert account.average_price == pytest.approx(100.0)


def test_sell_more_than_held_only_closes_available_quantity() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=5)

    result = manager.place("SELL", price=110.0, quantity=10)

    assert account.quantity == 0
    assert result.realized_pnl == pytest.approx(50.0)


def test_unsupported_action_fails_without_mutating_account() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    result = manager.place("HOLD", price=100.0, quantity=1)

    assert result.status == "FAILED"
    assert account.quantity == 0
    assert account.cash == pytest.approx(100_000.0)


def test_order_manager_defaults_to_a_fresh_account() -> None:
    manager = OrderManager()

    assert manager.account.quantity == 0
    assert manager.account.cash == pytest.approx(1_000_000.0)


def test_buy_records_which_index_the_position_is_in() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    manager.place("BUY", price=100.0, quantity=10, index_id="nifty-50")

    assert account.index_id == "nifty-50"


def test_adding_to_a_position_does_not_change_the_recorded_index() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=10, index_id="nifty-50")

    manager.place("BUY", price=120.0, quantity=10, index_id="nifty-50")

    assert account.index_id == "nifty-50"


def test_closing_a_position_clears_the_recorded_index() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place("BUY", price=100.0, quantity=10, index_id="nifty-50")

    manager.place("SELL", price=110.0, quantity=10, index_id="nifty-50")

    assert account.index_id is None
