from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from algoedge import alerts

logger = logging.getLogger("algoedge.failure_monitor")

# The paper-engine failure kinds that actually exist in the code:
# - CYCLE: a paper cycle raised (candle fetch error, any other exception,
#   including CycleBusyError) or returned "No valid market data".
# - DATABASE: the cycle ran, but its atomic persistence
#   (db.record_paper_cycle) failed and rolled back - B6's DATABASE_FAILURE.
CYCLE = "CYCLE"
DATABASE = "DATABASE"
ALERT_CATEGORY = {CYCLE: alerts.PAPER_CYCLE_FAILURE, DATABASE: alerts.DATABASE_FAILURE}

# Consecutive failures of one kind, for one index, before alerting. The
# scheduler ticks every 5 minutes, so 3 is ~15 minutes of an index failing
# every cycle - a single transient failure (or two) never alerts.
REPEATED_FAILURE_THRESHOLD = 3


class RepeatedFailureMonitor:
    """Counts consecutive paper cycle failures per (index, kind) and raises
    ONE alert when a streak reaches the threshold; further failures in the
    same streak do not alert again (no alert storm). A cycle that completes
    without that kind of failure ends the streak, so a later streak can
    alert again. In-memory only: a restart starts every count at zero.

    Thread-safe: the scheduler (event-loop thread) and the manual "Run
    cycle now" endpoint (threadpool) both report here."""

    def __init__(
        self,
        threshold: int = REPEATED_FAILURE_THRESHOLD,
        alert: Callable[..., None] | None = None,
        source: str = "algoedge.auto_trader",
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self._alert = alert or alerts.raise_alert
        self._source = source
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def record_failure(self, index_id: str, kind: str, detail: str) -> bool:
        """Counts one failure; returns True if this failure raised the alert.
        `detail` must be non-sensitive (e.g. an exception class name, never a
        raw driver/broker message)."""
        if kind not in ALERT_CATEGORY:
            raise ValueError(f"unknown failure kind {kind!r}")
        with self._lock:
            count = self._counts.get((index_id, kind), 0) + 1
            self._counts[(index_id, kind)] = count
        if count != self.threshold:
            return False
        self._alert(
            ALERT_CATEGORY[kind],
            f"{index_id}: {count} consecutive paper cycle failures ({kind}) - last: {detail}"[:255],
            source=self._source,
        )
        return True

    def record_success(self, index_id: str, kinds: tuple[str, ...] = (CYCLE, DATABASE)) -> None:
        """Ends the streaks of `kinds` for this index."""
        with self._lock:
            for kind in kinds:
                self._counts.pop((index_id, kind), None)

    def consecutive_failures(self, index_id: str, kind: str) -> int:
        with self._lock:
            return self._counts.get((index_id, kind), 0)
