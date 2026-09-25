import pytest

from fno_signals import broker as fno_broker_module


@pytest.fixture(autouse=True)
def _clear_fno_instruments_cache():
    """fno_signals.broker._get_instruments() caches Groww's instrument
    master for 5 minutes (real data doesn't change intraday - see its own
    docstring). Several test files build tiny fake instrument sets through
    it (test_fno_signals_broker.py, test_live_grid.py's
    check_instrument_master() tests, test_manual_trading.py's
    resolve_manual_contract() - which delegates to this same cache, not its
    own module-level one). Without a session-wide reset, whichever test
    happens to run first leaves its fake data behind for every later test
    in the run - this has been the actual root cause of three separate
    cross-file test failures this session already. One autouse fixture
    here replaces the same clear-before/clear-after boilerplate that had
    been copy-pasted into each file individually.
    """
    fno_broker_module._instruments_cache.clear()
    yield
    fno_broker_module._instruments_cache.clear()
