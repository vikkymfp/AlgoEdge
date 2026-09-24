from algoedge.paper_broker import PaperBroker


def test_buy_deducts_cash_and_opens_position() -> None:
    broker = PaperBroker(cash=1000.0)

    broker.execute("buy", 100.0)

    assert broker.cash == 900.0
    assert broker.position == 1


def test_sell_without_position_is_a_noop() -> None:
    broker = PaperBroker(cash=1000.0)

    broker.execute("sell", 100.0)

    assert broker.cash == 1000.0
    assert broker.position == 0


def test_buy_while_already_holding_a_position_is_a_noop() -> None:
    broker = PaperBroker(cash=1000.0, position=1)

    broker.execute("buy", 100.0)

    assert broker.cash == 1000.0
    assert broker.position == 1


def test_buy_with_insufficient_cash_is_a_noop() -> None:
    broker = PaperBroker(cash=50.0)

    broker.execute("buy", 100.0)

    assert broker.cash == 50.0
    assert broker.position == 0


def test_sell_after_buy_closes_position_and_returns_cash() -> None:
    broker = PaperBroker(cash=1000.0)
    broker.execute("buy", 100.0)

    broker.execute("sell", 120.0)

    assert broker.cash == 1020.0
    assert broker.position == 0


def test_equity_reflects_open_position_value() -> None:
    broker = PaperBroker(cash=900.0, position=1)

    assert broker.equity(150.0) == 1050.0


def test_equity_with_no_position_is_just_cash() -> None:
    broker = PaperBroker(cash=1000.0)

    assert broker.equity(150.0) == 1000.0
