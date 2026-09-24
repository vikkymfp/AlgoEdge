from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("algoedge.scheduler")

DEFAULT_TICK_SECONDS = 300.0  # 5 minutes, matches the default 5m candle timeframe


class AutoTradingScheduler:
    """Runs one auto-trading cycle per configured index on a fixed cadence,
    independent of the "Run cycle now" button.

    Deliberately a dumb, unconditional ticker: `RiskManager.check()` (inside
    `run_cycle`) already blocks trading when disabled/kill-switched/outside
    trading hours, so this loop doesn't duplicate that gating - it only
    checks `is_enabled()` up front to skip the yfinance candle fetch
    entirely while auto trading is off, rather than hitting the network
    every tick for no reason.

    A failure for one index (e.g. a transient yfinance error) is logged and
    must never stop the loop or block the other indices' ticks.
    """

    def __init__(
        self,
        index_ids: list[str],
        run_one: Callable[[str], object],
        is_enabled: Callable[[], bool],
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._index_ids = index_ids
        self._run_one = run_one
        self._is_enabled = is_enabled
        self._tick_seconds = tick_seconds
        self._task: asyncio.Task | None = None

    @property
    def tick_seconds(self) -> float:
        return self._tick_seconds

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            await self._tick()

    async def _tick(self) -> None:
        if not self._is_enabled():
            return
        for index_id in self._index_ids:
            try:
                result = self._run_one(index_id)
                if isinstance(result, Awaitable):
                    await result
            except Exception:
                logger.exception("Scheduled auto-trading cycle failed for %s", index_id)
