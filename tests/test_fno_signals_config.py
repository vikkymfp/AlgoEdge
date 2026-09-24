from fno_signals.config import DEFAULT_CONFIG, INDEX_MAP, IndexConfig, strategy_config_for


def test_default_signal_config_has_vwap_disabled() -> None:
    assert DEFAULT_CONFIG.signal.use_vwap is False


def test_index_map_matches_the_required_spec() -> None:
    assert INDEX_MAP[1] == IndexConfig(
        name="NIFTY 50", ticker="^NSEI", strike_step=50, lot_size=75,
        exchange="NSE", groww_underlying="NIFTY",
    )
    assert INDEX_MAP[2] == IndexConfig(
        name="BANK NIFTY", ticker="^NSEBANK", strike_step=100, lot_size=30,
        exchange="NSE", groww_underlying="BANKNIFTY",
    )
    assert INDEX_MAP[3] == IndexConfig(
        name="SENSEX", ticker="^BSESN", strike_step=100, lot_size=20,
        exchange="BSE", groww_underlying="SENSEX",
    )


def test_sensex_options_list_on_bse_not_nse() -> None:
    # A common mistake: assuming all NSE indices' options trade on NSE.
    # Sensex options are listed on BSE.
    assert INDEX_MAP[3].exchange == "BSE"
    assert INDEX_MAP[1].exchange == "NSE"
    assert INDEX_MAP[2].exchange == "NSE"


def test_strategy_config_for_wires_in_the_index_strike_step() -> None:
    config = strategy_config_for(INDEX_MAP[2])

    assert config.option.strike_step == 100
    assert config.signal == DEFAULT_CONFIG.signal
    assert config.risk == DEFAULT_CONFIG.risk
    assert config.session == DEFAULT_CONFIG.session


def test_strategy_config_for_does_not_mutate_default_config() -> None:
    strategy_config_for(INDEX_MAP[3])

    assert DEFAULT_CONFIG.option.strike_step == 50
