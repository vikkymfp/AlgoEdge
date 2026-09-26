"""Phase 7: global max_open_positions for new paper entries across indices.

B5 serializes cycles per index only, so a NIFTY and a SENSEX cycle can run at
once. Before this fix each read the global open-position count when its cycle
started, both saw 0, and both filled. Now a new entry's risk check and fill
run inside one global guard (web_server._entry_guard) with the count re-read
inside it; the guard is not held for the candle fetch, contract resolution,
exits, square-off or persistence.

Real threads drive the dashboard's own cycle path; contract resolution (the
network step between choosing the signal and filling it) is gated with
threading.Events so every interleaving is deterministic.
"""

import asyncio
import random
import threading
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from algoedge import auto_trader, db, web_server
from algoedge.models import Base
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, OrderResult, SimulatedAccount
from algoedge.risk_manager import IST, RiskLimits, RiskManager
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy

INDEX_IDS = list(web_server.INDEX_DEFINITIONS)
WAIT = 10
FIVE = timedelta(minutes=5)
MAX_POSITIONS = "Max open positions reached"


def rising(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


FULL = rising(40)
ENTRY = next(e for e in run_strategy(FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
             if e.kind == "ENTRY_CALL")
ENTRY_WINDOW = FULL.loc[:ENTRY.timestamp]
NOW = (ENTRY.timestamp + FIVE + timedelta(seconds=30)).to_pydatetime()
EXIT_WINDOW = FULL.loc[:ENTRY.timestamp + FIVE]  # the next candle's High reaches a target 1 point above entry
EXIT_NOW = (ENTRY.timestamp + 2 * FIVE + timedelta(seconds=30)).to_pydatetime()
FLAT = pd.DataFrame({c: [110.0] * 20 for c in ("Open", "High", "Low", "Close")} | {"Volume": [0.0] * 20},
                    index=pd.date_range("2026-09-23 14:00", periods=20, freq="5min", tz="Asia/Kolkata"))


def contract(event) -> OptionContract:
    return OptionContract(trading_symbol=f"X{event.strike}{event.right}", underlying="NIFTY", right=event.right,
                          strike=event.strike, expiry=date(2026, 9, 30))


class Gates:
    """Per-index pause points inside contract resolution."""

    def __init__(self) -> None:
        self.hold: set[str] = set()
        self.entered = {i: threading.Event() for i in INDEX_IDS}
        self.release = {i: threading.Event() for i in INDEX_IDS}

    def resolve(self, index_id, event):
        if index_id in self.hold and not self.entered[index_id].is_set():
            self.entered[index_id].set()
            assert self.release[index_id].wait(WAIT), "gate never released"
        return contract(event)


@pytest.fixture()
def dashboard(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})
    monkeypatch.setattr(web_server, "_cycle_locks", {i: threading.Lock() for i in INDEX_IDS})
    monkeypatch.setattr(web_server, "_entry_guard", threading.Lock())
    gates = Gates()
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", gates.resolve)
    clock = {"now": NOW}
    windows = {}

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    ticker_to_index = {INDEX_MAP[web_server.DASHBOARD_INDEX_IDS[i]].ticker: i for i in INDEX_IDS}

    def fetch(ticker, **_kwargs):
        return windows.get(ticker_to_index[ticker], ENTRY_WINDOW)

    monkeypatch.setattr(auto_trader, "datetime", FixedNow)
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
    yield risk_manager, accounts, gates, clock, windows
    engine.dispose()


def in_thread(fn, results: dict, key: str) -> threading.Thread:
    def target():
        try:
            results[key] = fn()
        except Exception as error:  # noqa: BLE001 - recorded for the assertion
            results[key] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def cycle(index_id: str):
    return lambda: web_server._run_and_persist_cycle(index_id, "5m", 1)


def open_count(accounts) -> int:
    return sum(1 for a in accounts.values() if a.quantity > 0)


def hold_long(account: SimulatedAccount, target_offset: float = 1.0) -> None:
    account.quantity, account.average_price, account.side = 1, ENTRY.underlying_price, "CALL"
    account.stop_loss, account.target = ENTRY.underlying_price - 500.0, ENTRY.underlying_price + target_offset
    account.last_event_at = ENTRY.timestamp


# ---------------- the reproduced race ----------------


def test_the_reproduced_nifty_sensex_race_opens_only_one_position(dashboard) -> None:
    risk_manager, accounts, gates, clock, windows = dashboard
    assert risk_manager.limits.max_open_positions == 1
    gates.hold.add("nifty-50")
    results: dict = {}

    nifty = in_thread(cycle("nifty-50"), results, "nifty")  # e.g. the scheduler's cycle
    assert gates.entered["nifty-50"].wait(WAIT)  # NIFTY chose its entry, paused in contract resolution
    results["sensex"] = cycle("sensex")()  # e.g. a manual Run Cycle on another index
    gates.release["nifty-50"].set()
    nifty.join(WAIT)

    assert results["sensex"]["order"]["status"] == "PLACED"
    assert results["nifty"]["order"] is None and results["nifty"]["risk"]["reason"] == MAX_POSITIONS
    assert (accounts["sensex"].quantity, accounts["nifty-50"].quantity) == (1, 0)
    assert open_count(accounts) == 1
    assert risk_manager.state.entries_today == 1  # the blocked entry never counts
    (audit,) = db.list_paper_decision_events(index_id="nifty-50")  # B7: audited, engine reason verbatim
    assert (audit["decision"], audit["reason"], audit["openQuantity"]) == ("BLOCKED", MAX_POSITIONS, 0)

    # Both indices keep working independently afterward: SENSEX exits ...
    hold_long(accounts["sensex"])
    clock["now"], windows["sensex"] = EXIT_NOW, EXIT_WINDOW
    assert cycle("sensex")()["order"]["status"] == "PLACED" and accounts["sensex"].quantity == 0
    # ... which frees the global slot for a NIFTY entry, which later exits too.
    clock["now"], windows["nifty-50"] = NOW, ENTRY_WINDOW
    accounts["nifty-50"].last_event_at = None
    risk_manager.state.last_exit_at = None  # not testing the cooldown here
    assert cycle("nifty-50")()["order"]["status"] == "PLACED" and accounts["nifty-50"].quantity == 1
    hold_long(accounts["nifty-50"])
    clock["now"], windows["nifty-50"] = EXIT_NOW, EXIT_WINDOW
    assert cycle("nifty-50")()["order"]["status"] == "PLACED" and open_count(accounts) == 0
    assert risk_manager.state.entries_today == 2


def test_without_the_guard_the_same_interleaving_opens_two(dashboard, monkeypatch) -> None:
    # Control: with the pre-fix wiring (a count read once at cycle start, no
    # guard) the identical interleaving violates max_open_positions.
    _risk_manager, accounts, gates, _clock, _windows = dashboard
    real_run_cycle = web_server.run_cycle
    monkeypatch.setattr(web_server, "run_cycle", lambda *a, open_positions_fn=None, entry_guard=None, **k:
                        real_run_cycle(*a, **k))
    gates.hold.add("nifty-50")
    results: dict = {}
    nifty = in_thread(cycle("nifty-50"), results, "nifty")
    assert gates.entered["nifty-50"].wait(WAIT)
    results["sensex"] = cycle("sensex")()
    gates.release["nifty-50"].set()
    nifty.join(WAIT)
    assert results["nifty"]["order"]["status"] == results["sensex"]["order"]["status"] == "PLACED"
    assert open_count(accounts) == 2


def test_the_limit_still_allows_as_many_positions_as_configured(dashboard, monkeypatch) -> None:
    risk_manager, accounts, gates, _clock, _windows = dashboard
    risk_manager.limits = RiskLimits(max_open_positions=2)
    gates.hold.add("nifty-50")
    results: dict = {}
    nifty = in_thread(cycle("nifty-50"), results, "nifty")
    assert gates.entered["nifty-50"].wait(WAIT)
    results["sensex"] = cycle("sensex")()
    gates.release["nifty-50"].set()
    nifty.join(WAIT)
    assert open_count(accounts) == 2 and risk_manager.state.entries_today == 2  # not over-blocking


# ---------------- other interleavings ----------------


def test_same_index_concurrent_cycles_fill_once(dashboard) -> None:
    risk_manager, accounts, gates, _clock, _windows = dashboard
    gates.hold.add("nifty-50")
    results: dict = {}
    first = in_thread(cycle("nifty-50"), results, "first")
    assert gates.entered["nifty-50"].wait(WAIT)
    second = in_thread(cycle("nifty-50"), results, "second")  # waits on the B5 per-index lock
    gates.release["nifty-50"].set()
    first.join(WAIT)
    second.join(WAIT)
    assert results["first"]["order"]["status"] == "PLACED"
    assert results["second"]["risk"]["reason"].startswith("Signal already processed")
    assert accounts["nifty-50"].quantity == 1 and risk_manager.state.entries_today == 1


def test_an_entry_and_another_indexs_exit_interleave_on_the_fresh_count(dashboard) -> None:
    # SENSEX holds the only slot; NIFTY chooses an entry and pauses; SENSEX
    # exits meanwhile. The in-guard re-count sees the freed slot, so NIFTY fills.
    risk_manager, accounts, gates, clock, windows = dashboard
    risk_manager.limits = RiskLimits(cooldown_minutes=0)  # the post-exit cooldown is not under test here
    hold_long(accounts["sensex"])
    windows["sensex"] = EXIT_WINDOW
    clock["now"] = EXIT_NOW  # SENSEX's exit candle has closed; NIFTY's entry candle is still fresh
    gates.hold.add("nifty-50")
    results: dict = {}
    nifty = in_thread(cycle("nifty-50"), results, "nifty")
    assert gates.entered["nifty-50"].wait(WAIT)  # NIFTY read open=1 at cycle start, then paused
    results["sensex"] = cycle("sensex")()
    gates.release["nifty-50"].set()
    nifty.join(WAIT)
    assert results["sensex"]["signal"]["kind"] == "EXIT_TARGET"
    assert results["nifty"]["order"]["status"] == "PLACED"
    assert (accounts["sensex"].quantity, accounts["nifty-50"].quantity) == (0, 1)
    assert risk_manager.state.entries_today == 1  # the exit did not count


def test_a_failed_first_entry_leaves_the_slot_to_the_second(dashboard) -> None:
    risk_manager, accounts, gates, _clock, _windows = dashboard
    web_server.order_managers["nifty-50"].place_event = lambda *a, **k: OrderResult("FAILED", "fill rejected")
    gates.hold.update({"nifty-50", "sensex"})
    results: dict = {}
    nifty = in_thread(cycle("nifty-50"), results, "nifty")
    sensex = in_thread(cycle("sensex"), results, "sensex")
    assert gates.entered["nifty-50"].wait(WAIT) and gates.entered["sensex"].wait(WAIT)  # both mid-cycle
    gates.release["nifty-50"].set()
    nifty.join(WAIT)
    gates.release["sensex"].set()
    sensex.join(WAIT)
    assert results["nifty"]["order"]["status"] == "FAILED"
    assert results["sensex"]["order"]["status"] == "PLACED"
    assert open_count(accounts) == 1 and risk_manager.state.entries_today == 1


# ---------------- what the guard does NOT hold ----------------


def test_the_guard_is_not_held_during_fetch_or_contract_resolution(dashboard, monkeypatch) -> None:
    _risk_manager, accounts, gates, _clock, _windows = dashboard
    seen = []

    def fetch(ticker, **_kwargs):
        seen.append(("fetch", web_server._entry_guard.locked()))
        return ENTRY_WINDOW

    def resolve(index_id, event):
        seen.append(("resolve", web_server._entry_guard.locked()))
        return contract(event)

    monkeypatch.setattr(auto_trader, "fetch_underlying_data", fetch)
    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", resolve)
    assert cycle("nifty-50")()["order"]["status"] == "PLACED"
    assert seen == [("fetch", False), ("resolve", False)]
    assert not web_server._entry_guard.locked()


def test_a_cycle_fetches_and_resolves_while_another_holds_the_guard(dashboard) -> None:
    # Only the check-and-fill waits for the guard; everything before it runs.
    _risk_manager, accounts, gates, _clock, _windows = dashboard
    gates.hold.add("nifty-50")
    assert web_server._entry_guard.acquire()  # as if another index were mid-fill
    results: dict = {}
    nifty = in_thread(cycle("nifty-50"), results, "nifty")
    assert gates.entered["nifty-50"].wait(WAIT)  # fetched, evaluated, resolving - not blocked
    gates.release["nifty-50"].set()
    nifty.join(0.3)
    assert nifty.is_alive()  # now waiting for the guard, before its risk check
    web_server._entry_guard.release()
    nifty.join(WAIT)
    assert results["nifty"]["order"]["status"] == "PLACED"


@pytest.mark.parametrize("kind", ["exit", "square_off"])
def test_exits_and_square_off_never_wait_for_the_guard(dashboard, kind) -> None:
    _risk_manager, accounts, _gates, clock, windows = dashboard
    hold_long(accounts["sensex"])
    if kind == "exit":
        clock["now"], windows["sensex"] = EXIT_NOW, EXIT_WINDOW
    else:
        accounts["sensex"].last_event_at = FLAT.index[3]
        clock["now"], windows["sensex"] = datetime(2026, 9, 23, 15, 21, tzinfo=IST), FLAT
    assert web_server._entry_guard.acquire()  # held by a concurrent entry
    try:
        results: dict = {}
        worker = in_thread(cycle("sensex"), results, "sensex")
        worker.join(WAIT)
        assert not worker.is_alive()
        assert results["sensex"]["order"]["status"] == "PLACED" and accounts["sensex"].quantity == 0
        assert results["sensex"]["signal"]["kind"] == ("EXIT_TARGET" if kind == "exit" else "SQUARE_OFF")
    finally:
        web_server._entry_guard.release()


def test_the_guard_is_released_when_a_fill_raises(dashboard) -> None:
    _risk_manager, accounts, _gates, _clock, _windows = dashboard
    order_manager = web_server.order_managers["nifty-50"]

    def boom(*_a, **_k):
        raise RuntimeError("simulated failure inside the critical section")

    order_manager.place_event = boom
    with pytest.raises(RuntimeError, match="inside the critical section"):
        cycle("nifty-50")()
    assert not web_server._entry_guard.locked()
    assert not web_server._cycle_locks["nifty-50"].locked()
    del order_manager.place_event  # back to the real method
    assert cycle("sensex")()["order"]["status"] == "PLACED"


# ---------------- no deadlock, invariant under load ----------------


def test_no_deadlock_and_never_two_positions_under_concurrent_load(tmp_path, monkeypatch) -> None:
    # Per-index locks, the global guard and real (file) SQLite persistence
    # together: many rounds of 6 concurrent cycles (a scheduler-like and a
    # manual-like cycle per index) with random pauses in contract resolution.
    engine = create_engine(f"sqlite:///{tmp_path / 'paper.db'}", connect_args={"check_same_thread": False,
                                                                            "timeout": 30})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    accounts = {index_id: SimulatedAccount() for index_id in INDEX_IDS}
    monkeypatch.setattr(web_server, "risk_manager", risk_manager)
    monkeypatch.setattr(web_server, "order_managers", {i: OrderManager(a) for i, a in accounts.items()})
    monkeypatch.setattr(web_server, "_cycle_locks", {i: threading.Lock() for i in INDEX_IDS})
    monkeypatch.setattr(web_server, "_entry_guard", threading.Lock())
    rng = random.Random(7)
    max_open_seen = []

    def resolve(index_id, event):
        threading.Event().wait(rng.uniform(0, 0.01))
        return contract(event)

    real_fill = SimulatedAccount.fill_event

    def checked_fill(self, *a, **k):
        pnl = real_fill(self, *a, **k)
        max_open_seen.append(open_count(accounts))
        return pnl

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(web_server, "_resolve_auto_trade_contract", resolve)
    monkeypatch.setattr(SimulatedAccount, "fill_event", checked_fill)
    monkeypatch.setattr(auto_trader, "datetime", FixedNow)
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_k: ENTRY_WINDOW)

    fills_per_round = []
    for _ in range(15):
        for account in accounts.values():  # every round starts flat, with a fresh signal
            account.quantity, account.side, account.average_price, account.last_event_at = 0, None, None, None
            account.contract = None
        risk_manager.state.last_exit_at = None
        risk_manager.state.entries_today = 0  # stay under the B9 daily entry cap across rounds
        before = risk_manager.state.entries_today
        results: dict = {}
        threads = [in_thread(cycle(i), results, f"{i}-{n}") for i in INDEX_IDS for n in range(2)]
        for thread in threads:
            thread.join(WAIT)
        assert not any(t.is_alive() for t in threads), "deadlock: a cycle never finished"
        assert not any(isinstance(r, Exception) for r in results.values()), results
        fills_per_round.append(risk_manager.state.entries_today - before)
        assert open_count(accounts) == 1
    assert fills_per_round == [1] * 15
    assert max(max_open_seen) == 1  # never two open positions, even momentarily
    assert not web_server._entry_guard.locked()
    engine.dispose()


def test_the_scheduler_path_uses_the_guard(dashboard) -> None:
    # A plain sequential scheduler tick is unchanged: one entry, the other
    # indices blocked by max_open_positions exactly as before.
    risk_manager, accounts, _gates, _clock, _windows = dashboard
    asyncio.run(web_server._scheduler._tick())
    assert open_count(accounts) == 1 and risk_manager.state.entries_today == 1
    assert [e["reason"] for e in db.list_paper_decision_events()] == [MAX_POSITIONS, MAX_POSITIONS]
