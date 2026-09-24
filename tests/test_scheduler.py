import asyncio

from algoedge.scheduler import AutoTradingScheduler


def test_tick_skips_entirely_when_disabled() -> None:
    calls: list[str] = []
    scheduler = AutoTradingScheduler(
        index_ids=["nifty-50", "sensex"],
        run_one=calls.append,
        is_enabled=lambda: False,
    )

    asyncio.run(scheduler._tick())

    assert calls == []


def test_tick_runs_every_configured_index_once_when_enabled() -> None:
    calls: list[str] = []
    scheduler = AutoTradingScheduler(
        index_ids=["nifty-50", "sensex", "bank-nifty"],
        run_one=calls.append,
        is_enabled=lambda: True,
    )

    asyncio.run(scheduler._tick())

    assert calls == ["nifty-50", "sensex", "bank-nifty"]


def test_tick_continues_past_a_failing_index() -> None:
    calls: list[str] = []

    def run_one(index_id: str) -> None:
        if index_id == "sensex":
            raise RuntimeError("yfinance hiccup")
        calls.append(index_id)

    scheduler = AutoTradingScheduler(
        index_ids=["nifty-50", "sensex", "bank-nifty"],
        run_one=run_one,
        is_enabled=lambda: True,
    )

    asyncio.run(scheduler._tick())

    assert calls == ["nifty-50", "bank-nifty"]


def test_start_and_stop_cancels_the_background_task() -> None:
    calls: list[str] = []
    scheduler = AutoTradingScheduler(
        index_ids=["nifty-50"],
        run_one=calls.append,
        is_enabled=lambda: True,
        tick_seconds=0.01,
    )

    async def _run() -> None:
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

    asyncio.run(_run())

    assert len(calls) >= 1
    assert scheduler._task is None


def test_run_one_may_be_a_coroutine() -> None:
    calls: list[str] = []

    async def run_one(index_id: str) -> None:
        calls.append(index_id)

    scheduler = AutoTradingScheduler(
        index_ids=["nifty-50"],
        run_one=run_one,
        is_enabled=lambda: True,
    )

    asyncio.run(scheduler._tick())

    assert calls == ["nifty-50"]
