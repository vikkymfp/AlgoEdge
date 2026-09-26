from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("algoedge.scheduler")

DEFAULT_TICK_SECONDS = 300.0  # 5 minutes, matches the default 5m candle timeframe


class AutoTradingScheduler:
    """Runs one auto-trading cycle per configured index on a fixed cadence,
    independent of the "Run cycle now" button.

    Deliberately a dumb ticker: `RiskManager.check()` (inside `run_cycle`)
    already blocks NEW entries when disabled/kill-switched/outside trading
    hours, so this loop doesn't duplicate that gating. While `is_enabled()`
    is False it skips the candle fetch for every index EXCEPT those where
    `has_open_position(index_id)` is True: an open paper position must
    still reach its SL/target exit and the forced 15:20 square-off, which
    are risk-reducing and never blocked by those switches. Without
    `has_open_position` (the default) a disabled tick skips everything,
    as before.

    A failure for one index (e.g. a transient yfinance error) is logged and
    must never stop the loop or block the other indices' ticks.
    """

    def __init__(
        self,
        index_ids: list[str],
        run_one: Callable[[str], object],
        is_enabled: Callable[[], bool],
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        has_open_position: Callable[[str], bool] | None = None,
    ) -> None:
        self._index_ids = index_ids
        self._run_one = run_one
        self._is_enabled = is_enabled
        self._has_open_position = has_open_position
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
        enabled = self._is_enabled()
        if not enabled and self._has_open_position is None:
            return
        for index_id in self._index_ids:
            try:
                if not enabled and not self._has_open_position(index_id):
                    continue  # flat and entries are off - nothing risk-reducing to do
                result = self._run_one(index_id)
                if isinstance(result, Awaitable):
                    await result
            except Exception:
                logger.exception("Scheduled auto-trading cycle failed for %s", index_id)
