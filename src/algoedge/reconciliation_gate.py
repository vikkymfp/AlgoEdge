from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from algoedge.reconciliation import ReconciliationReport, reconcile
from algoedge.risk_manager import IST


class ReconciliationBlockedError(RuntimeError):
    """Raised when an order is attempted while the reconciliation gate is
    blocking - a position mismatch, an unconfirmed order, or (fail-closed)
    reconciliation never having run at all since startup."""


@dataclass(frozen=True)
class ReconciliationCheck:
    status: str  # "UNKNOWN" | "OK" | "MISMATCH" | "UNCONFIRMED"
    checked_at: datetime | None
    reason: str | None
    report: ReconciliationReport | None = None


class ReconciliationGate:
    """Sits in front of every real order-placement path. Per the spec:
    a position mismatch or an order whose fill status was never confirmed
    must BLOCK new orders and create a reconciliation event - never
    silently proceed, and never auto-place a corrective trade to make the
    numbers agree.

    Fails closed by design: until evaluate() has actually run at least
    once (e.g. right after a restart), status stays "UNKNOWN" and
    is_blocking() returns True. This is the code-level meaning of the
    spec's restart-safety diagram: "only after successful reconciliation
    should new orders be permitted."

    A human can temporarily override the block (`override()`) when a
    mismatch is understood and accepted (e.g. a manual trade placed
    outside AlgoEdge) rather than something AlgoEdge should keep refusing
    to trade around - but override never clears itself into "OK"; the next
    evaluate() call still runs for real and can re-block if the mismatch
    persists AND the override has expired.
    """

    def __init__(self) -> None:
        self._last_check = ReconciliationCheck(
            "UNKNOWN", None, "Reconciliation has not run yet since startup",
        )
        self._overridden_until: datetime | None = None
        self._override_reason: str | None = None

    @property
    def last_check(self) -> ReconciliationCheck:
        return self._last_check

    def evaluate(
        self, orders: list[dict[str, Any]], live_positions: list[dict[str, Any]], now: datetime | None = None,
    ) -> ReconciliationCheck:
        now = now or datetime.now(IST)
        report = reconcile(orders, live_positions)
        mismatches = [comparison for comparison in report.comparisons if not comparison.matches]
        if mismatches:
            symbols = ", ".join(comparison.trading_symbol for comparison in mismatches)
            status, reason = "MISMATCH", f"Position mismatch for: {symbols}"
        elif report.unconfirmed_orders:
            status, reason = "UNCONFIRMED", f"{len(report.unconfirmed_orders)} order(s) with unconfirmed fill status"
        else:
            status, reason = "OK", None
        self._last_check = ReconciliationCheck(status, now, reason, report)
        if status == "OK":
            # A genuine, freshly-computed match clears any earlier override -
            # this is the check resolving itself honestly, not a corrective
            # trade being placed to force agreement.
            self._overridden_until = None
            self._override_reason = None
        return self._last_check

    def mark_unavailable(self, reason: str, now: datetime | None = None) -> ReconciliationCheck:
        """Used when evaluate() itself can't run (e.g. Groww isn't
        connected) - stays fail-closed (blocking) rather than assuming
        either OK or MISMATCH without having actually checked."""
        now = now or datetime.now(IST)
        self._last_check = ReconciliationCheck("UNKNOWN", now, reason)
        return self._last_check

    def is_blocking(self, now: datetime | None = None) -> bool:
        if self._last_check.status == "OK":
            return False
        now = now or datetime.now(IST)
        return not (self._overridden_until is not None and now < self._overridden_until)

    def override(self, reason: str, duration_minutes: int = 60, now: datetime | None = None) -> None:
        now = now or datetime.now(IST)
        self._overridden_until = now + timedelta(minutes=duration_minutes)
        self._override_reason = reason

    def clear_override(self) -> None:
        self._overridden_until = None
        self._override_reason = None

    def status_payload(self) -> dict[str, Any]:
        check = self._last_check
        return {
            "status": check.status,
            "checkedAt": check.checked_at,
            "reason": check.reason,
            "blocking": self.is_blocking(),
            "overrideReason": self._override_reason,
            "overriddenUntil": self._overridden_until,
        }
