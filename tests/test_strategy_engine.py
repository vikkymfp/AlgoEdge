import pandas as pd

from algoedge import strategy_engine
from algoedge.strategy_engine import StrategyConfig, evaluate


def make_closes(n: int = 25, start: float = 100.0, step: float = 1.0) -> pd.Series:
    return pd.Series([start + i * step for i in range(n)])


def test_evaluate_holds_with_insufficient_price_history() -> None:
    result = evaluate(pd.Series([100.0, 101.0]))

    assert result.action == "HOLD"
    assert result.reason == "Not enough price history yet"
    assert result.rsi is None


def test_evaluate_buys_when_rsi_high_and_price_above_ema(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([60.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))

    result = evaluate(make_closes(), StrategyConfig())

    assert result.action == "BUY"
    assert "RSI above" in result.reason
    assert result.rsi == 60.0
    assert result.ema == 90.0


def test_evaluate_holds_when_rsi_high_but_price_below_ema(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([60.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([1000.0] * len(closes)))

    result = evaluate(make_closes(), StrategyConfig())

    assert result.action == "HOLD"
    assert result.reason == "Entry conditions not met"


def test_evaluate_holds_when_rsi_below_upper_even_if_price_above_ema(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([50.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))

    result = evaluate(make_closes(), StrategyConfig())

    assert result.action == "HOLD"


def test_evaluate_exits_on_stop_loss(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([60.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))
    closes = make_closes()  # last close = 124.0
    config = StrategyConfig(stop_loss_percent=1.0, target_percent=5.0)

    result = evaluate(closes, config, entry_price=130.0)

    assert result.action == "SELL"
    assert result.reason == "Stop loss hit"


def test_evaluate_exits_on_target(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([60.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))
    closes = make_closes()  # last close = 124.0
    config = StrategyConfig(stop_loss_percent=1.0, target_percent=5.0)

    result = evaluate(closes, config, entry_price=100.0)

    assert result.action == "SELL"
    assert result.reason == "Target hit"


def test_evaluate_exits_when_rsi_drops_below_lower(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([40.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))
    closes = make_closes()  # last close = 124.0
    config = StrategyConfig(rsi_lower=45.0, stop_loss_percent=1.0, target_percent=5.0)

    result = evaluate(closes, config, entry_price=124.0)

    assert result.action == "SELL"
    assert "RSI dropped below" in result.reason


def test_evaluate_holds_open_position_when_no_exit_condition_met(monkeypatch) -> None:
    monkeypatch.setattr(strategy_engine, "rsi", lambda closes, length: pd.Series([50.0] * len(closes)))
    monkeypatch.setattr(strategy_engine, "ema", lambda closes, length: pd.Series([90.0] * len(closes)))
    closes = make_closes()  # last close = 124.0
    config = StrategyConfig(rsi_lower=45.0, stop_loss_percent=1.0, target_percent=5.0)

    result = evaluate(closes, config, entry_price=124.0)

    assert result.action == "HOLD"
    assert result.reason == "Position open, exit conditions not met"


def test_strategy_config_defaults_match_blueprint_example() -> None:
    config = StrategyConfig()

    assert config.rsi_length == 14
    assert config.rsi_lower == 45.0
    assert config.rsi_upper == 55.0
    assert config.ema_length == 20
