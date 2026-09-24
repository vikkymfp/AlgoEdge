from __future__ import annotations

import logging

from algoedge import db

logger = logging.getLogger("algoedge.alerts")

# Spec §29's exact list. WEBHOOK_AUTH_FAILURE is kept even though no
# webhook exists yet in this codebase (see project_production_hardening
# memory - signals stay Python-generated for now) - ready for when one is
# added, rather than omitted and forgotten.
ORDER_REJECTED = "ORDER_REJECTED"
ORDER_FAILED = "ORDER_FAILED"
BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
POSITION_MISMATCH = "POSITION_MISMATCH"
DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
UNEXPECTED_ORDER = "UNEXPECTED_ORDER"
WEBHOOK_AUTH_FAILURE = "WEBHOOK_AUTH_FAILURE"
DATABASE_FAILURE = "DATABASE_FAILURE"
TRADING_HALTED = "TRADING_HALTED"
SYSTEM_RESTART = "SYSTEM_RESTART"

SEVERITY = {
    ORDER_REJECTED: "WARNING",
    ORDER_FAILED: "WARNING",
    BROKER_DISCONNECTED: "CRITICAL",
    POSITION_MISMATCH: "CRITICAL",
    DAILY_LOSS_LIMIT_REACHED: "CRITICAL",
    KILL_SWITCH_ACTIVATED: "CRITICAL",
    UNEXPECTED_POSITION: "WARNING",
    UNEXPECTED_ORDER: "WARNING",
    WEBHOOK_AUTH_FAILURE: "WARNING",
    DATABASE_FAILURE: "CRITICAL",
    TRADING_HALTED: "CRITICAL",
    SYSTEM_RESTART: "INFO",
}


def raise_alert(category: str, message: str, *, source: str) -> None:
    """Persists an alert and logs it - the only "delivery channel" this
    app has today is the database + dashboard banner (explicit user
    choice: no email/SMS integration). A DB outage means this can't
    persist the alert (recording an alert about the DB being down, in the
    DB, is circular) - the logger.warning() call below is the honest
    fallback for that case, not a silent no-op.
    """
    severity = SEVERITY.get(category, "WARNING")
    logger.warning("ALERT [%s/%s] %s: %s", severity, category, source, message)
    db.record_alert_event(severity=severity, category=category, message=message, source=source)
