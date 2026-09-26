"""Phase 7 B5: one paper cycle at a time per index.

The dashboard's scheduler runs cycles on the event-loop thread and the manual
"Run cycle now" endpoint runs in FastAPI's threadpool, so these tests drive
both from real threads. The candle fetch is gated with threading.Events so
the interleaving is deterministic, and the per-index lock is wrapped to
record the moment a second cycle is actually waiting on it.
"""

import asyncio
import threading
from datetime import date, datetime

import pandas as pd
import pytest
from fastapi import HTTPException

from algoedge import auto_trader, web_server
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)
WAIT = 10  # seconds; generous upper bound so a broken lock fails instead of hanging


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


# A window ending on a genuine canonical ENTRY_CALL bar, and a "now" 7 minutes
# after that bar starts (fresh, inside trading hours, before the entry cutoff).
_FULL = rising(40)
_ENTRY = next(e for e in run_strategy(_FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
              if e.kind == "ENTRY_CALL")
ENTRY_WINDOW = _FULL.loc[:_ENTRY.timestamp]
NOW = (_ENTRY.timestamp + pd.Timedelta(minutes=7)).to_pydatetime()


class RecordingLock:
    """A threading.Lock that also records when a caller had to wait for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.locked():
            self.contended.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


@pytest.fixture()
def dashboard(monkeypatch):
    """Fresh paper state behind the real web_server functions; the fetch of
    the first call can be held open with `gate`."""
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    locks = {index_id: RecordingLock() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})
    monkeypatch.setattr(web_server, "_cycle_locks", locks)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(auto_trader, "datetime", FixedNow)

    # The gate holds the FIRST contract resolution for index `hold`: that is
    # the real race window - the event has been chosen but not yet filled, so
    # the account's last_event_at high-water mark has not advanced (in
    # production this step is a live instrument-master fetch).
    gate = {"hold": None, "entered": threading.Event(), "release": threading.Event(), "fetches": []}

    def resolve(index_id, event):
        if gate["hold"] == index_id and not gate["entered"].is_set():
            gate["entered"].set()
            assert gate["release"].wait(WAIT), "test gate never released"
        return OptionContract(trading_symbol=f"X{event.strike}{event.right}", underlying="NIFTY",
                              right=event.right, strike=event.strike, expiry=date(2026, 9, 30))

    def fetch(ticker, **_kwargs):
        gate["fetches"].append(ticker)
        return ENTRY_WINDOW

    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", resolve)
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
    return risk_manager, accounts, locks, gate


def ticker(index_id: str) -> str:
    return INDEX_MAP[web_server.DASHBOARD_INDEX_IDS[index_id]].ticker


def in_thread(fn, results: dict, key: str) -> threading.Thread:
    def target():
        try:
            results[key] = fn()
        except Exception as error:  # noqa: BLE001 - recorded for the assertion
            results[key] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# ---------------- 1 + 5: same index, manual endpoint vs scheduler ----------------


def test_manual_and_scheduled_cycles_for_the_same_index_process_the_event_once(dashboard, monkeypatch) -> None:
    risk_manager, accounts, locks, gate = dashboard
    monkeypatch.setattr(web_server._scheduler, "_index_ids", ["nifty-50"])
    gate["hold"] = "nifty-50"
    results: dict = {}

    manual = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "manual")
    assert gate["entered"].wait(WAIT)  # the manual cycle holds the lock, between choosing and filling
    scheduled = in_thread(lambda: asyncio.run(web_server._scheduler._tick()), results, "scheduled")
    assert locks["nifty-50"].contended.wait(WAIT)  # the scheduled cycle is now waiting on the lock
    assert gate["fetches"] == [ticker("nifty-50")]  # ... and has not fetched or evaluated anything yet
    gate["release"].set()
    manual.join(WAIT)
    scheduled.join(WAIT)

    assert results["manual"]["order"]["status"] == "PLACED"
    assert results["manual"]["signal"]["kind"] == "ENTRY_CALL"
    assert accounts["nifty-50"].quantity == 1  # filled exactly once
    assert risk_manager.state.trades_today == 1
    assert len(gate["fetches"]) == 2  # the waiting cycle did run afterwards - not dropped
    assert not locks["nifty-50"].locked()


def test_two_manual_cycles_for_the_same_index_process_the_event_once(dashboard) -> None:
    risk_manager, accounts, locks, gate = dashboard
    gate["hold"] = "nifty-50"
    results: dict = {}

    first = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "first")
    assert gate["entered"].wait(WAIT)
    second = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "second")
    assert locks["nifty-50"].contended.wait(WAIT)
    gate["release"].set()
    first.join(WAIT)
    second.join(WAIT)

    assert results["first"]["order"]["status"] == "PLACED"
    assert results["second"]["order"] is None
    assert results["second"]["risk"]["reason"].startswith("Signal already processed")
    assert accounts["nifty-50"].quantity == 1 and risk_manager.state.trades_today == 1


def test_without_the_lock_the_same_race_double_fills(dashboard, monkeypatch) -> None:
    # Proves the tests above exercise a real race: with the per-index lock
    # bypassed, the second cycle evaluates before the first advanced the
    # high-water mark, and the event is filled twice.
    _risk_manager, accounts, _locks, gate = dashboard
    monkeypatch.setattr(web_server, "_run_and_persist_cycle", web_server._execute_and_persist_cycle)
    gate["hold"] = "nifty-50"
    results: dict = {}

    first = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "first")
    assert gate["entered"].wait(WAIT)
    second = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "second")
    second.join(WAIT)  # nothing makes it wait
    gate["release"].set()
    first.join(WAIT)

    assert results["first"]["order"]["status"] == results["second"]["order"]["status"] == "PLACED"
    assert accounts["nifty-50"].quantity == 2  # the duplicate B5 prevents


# ---------------- 2: different indices run independently ----------------


def test_a_cycle_for_another_index_is_not_blocked(dashboard) -> None:
    _risk_manager, _accounts, locks, gate = dashboard
    gate["hold"] = "nifty-50"
    results: dict = {}

    held = in_thread(lambda: web_server.auto_trading_run("nifty-50"), results, "nifty")
    assert gate["entered"].wait(WAIT)  # nifty-50's lock is held ...
    other = in_thread(lambda: web_server.auto_trading_run("sensex"), results, "sensex")
    other.join(WAIT)
    assert not other.is_alive()  # ... and sensex completed anyway
    assert isinstance(results["sensex"], dict) and "risk" in results["sensex"]
    assert not locks["sensex"].contended.is_set()
    gate["release"].set()
    held.join(WAIT)
    assert isinstance(results["nifty"], dict)


# ---------------- 3-4: the lock is always released ----------------


def test_the_lock_is_released_after_a_successful_cycle(dashboard) -> None:
    _risk_manager, accounts, locks, _gate = dashboard
    assert web_server.auto_trading_run("nifty-50")["order"]["status"] == "PLACED"
    assert not locks["nifty-50"].locked()
    again = web_server.auto_trading_run("nifty-50")  # a later cycle runs normally
    assert again["risk"]["reason"].startswith("Signal already processed")
    assert accounts["nifty-50"].quantity == 1


def test_the_lock_is_released_when_a_cycle_raises(dashboard, monkeypatch) -> None:
    _risk_manager, accounts, locks, _gate = dashboard

    def broken(*_a, **_k):
        raise RuntimeError("No data returned for ^NSEI")

    monkeypatch.setattr(auto_trader, "fetch_underlying_data", broken)
    with pytest.raises(RuntimeError, match="No data returned"):
        web_server.auto_trading_run("nifty-50")
    assert not locks["nifty-50"].locked()

    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: ENTRY_WINDOW)
    assert web_server.auto_trading_run("nifty-50")["order"]["status"] == "PLACED"
    assert accounts["nifty-50"].quantity == 1


def test_the_lock_is_released_when_a_scheduled_cycle_raises(dashboard, monkeypatch) -> None:
    _risk_manager, _accounts, locks, _gate = dashboard
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("yfinance down")))
    asyncio.run(web_server._scheduler._tick())  # every index fails; the scheduler logs and continues
    assert not any(lock.locked() for lock in locks.values())


# ---------------- busy: explicit, never silent ----------------


def test_a_cycle_still_waiting_after_the_timeout_fails_explicitly(dashboard, monkeypatch, caplog) -> None:
    _risk_manager, accounts, locks, gate = dashboard
    monkeypatch.setattr(web_server, "_CYCLE_LOCK_TIMEOUT_SECONDS", 0.05)
    assert locks["nifty-50"].acquire()  # another cycle holds nifty-50
    try:
        with pytest.raises(HTTPException) as busy:
            web_server.auto_trading_run("nifty-50")
        assert busy.value.status_code == 409 and "still running" in busy.value.detail

        asyncio.run(web_server._scheduler._tick())
        assert "Scheduled auto-trading cycle failed for nifty-50" in caplog.text
        assert set(gate["fetches"]) == {ticker("sensex"), ticker("bank-nifty")}  # others still ran
        assert accounts["nifty-50"].quantity == 0
    finally:
        locks["nifty-50"].release()


# ---------------- 6: sequential behavior unchanged ----------------


def test_sequential_scheduler_ticks_are_unchanged(dashboard) -> None:
    risk_manager, accounts, locks, gate = dashboard
    asyncio.run(web_server._scheduler._tick())
    assert gate["fetches"] == [ticker(i) for i in INDEX_IDS]  # every index, in order, once
    assert sum(a.quantity for a in accounts.values()) == 1  # global max_open_positions still applies
    assert not any(lock.contended.is_set() for lock in locks.values())
    asyncio.run(web_server._scheduler._tick())
    assert risk_manager.state.trades_today == 1  # the second tick sees the event as processed
