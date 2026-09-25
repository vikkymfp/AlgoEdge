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


# ---------- fill_event() / place_event() - the CALL/PUT path used by the
# canonical strategy (fno_signals.strategy.run()) via algoedge.auto_trader.
# Distinct from the BUY/SELL fill()/place() path above, which is untouched.


def test_call_entry_event_opens_a_long_position() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    result = manager.place_event("ENTRY_CALL", price=100.0, quantity=10, index_id="nifty-50")

    assert result.status == "PLACED"
    assert account.side == "CALL"
    assert account.quantity == 10
    assert account.average_price == pytest.approx(100.0)
    assert account.index_id == "nifty-50"


def test_put_entry_event_opens_a_short_position() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    result = manager.place_event("ENTRY_PUT", price=100.0, quantity=10, index_id="nifty-50")

    assert result.status == "PLACED"
    assert account.side == "PUT"
    assert account.quantity == 10
    assert account.average_price == pytest.approx(100.0)


def test_call_exit_event_realizes_profit_when_price_rises() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place_event("ENTRY_CALL", price=100.0, quantity=10)

    result = manager.place_event("EXIT_TARGET", price=110.0, quantity=10)

    assert result.status == "PLACED"
    assert result.realized_pnl == pytest.approx(100.0)
    assert account.quantity == 0
    assert account.side is None


def test_put_exit_event_realizes_profit_when_price_falls() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place_event("ENTRY_PUT", price=100.0, quantity=10)

    result = manager.place_event("EXIT_SL", price=90.0, quantity=10)

    assert result.status == "PLACED"
    assert result.realized_pnl == pytest.approx(100.0)
    assert account.quantity == 0
    assert account.side is None


def test_put_exit_event_realizes_loss_when_price_rises() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place_event("ENTRY_PUT", price=100.0, quantity=10)

    result = manager.place_event("EXIT_SL", price=110.0, quantity=10)

    assert result.realized_pnl == pytest.approx(-100.0)


def test_repeated_call_entries_average_the_entry_price() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)

    manager.place_event("ENTRY_CALL", price=100.0, quantity=10)
    manager.place_event("ENTRY_CALL", price=120.0, quantity=10)

    assert account.quantity == 20
    assert account.average_price == pytest.approx(110.0)


def test_cannot_open_a_put_while_a_call_is_already_open() -> None:
    account = SimulatedAccount(cash=100_000.0)
    manager = OrderManager(account)
    manager.place_event("ENTRY_CALL", price=100.0, quantity=10)

    result = manager.place_event("ENTRY_PUT", price=100.0, quantity=10)

    assert result.status == "FAILED"
    assert account.side == "CALL"
    assert account.quantity == 10


def test_exit_event_with_no_open_position_fails() -> None:
    manager = OrderManager()

    result = manager.place_event("EXIT_TARGET", price=100.0, quantity=10)

    assert result.status == "FAILED"
