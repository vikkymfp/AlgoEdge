from __future__ import annotations

# The formal exit categorization from the production-hardening spec
# (§19 "Exit Management"). Every order that closes a position should
# carry one of these, distinct from the free-text `reason` field used to
# explain order-pipeline outcomes (a rejection, a timeout, etc).
STOP_LOSS = "STOP_LOSS"
TARGET = "TARGET"
STRATEGY_REVERSAL = "REVERSAL"
TIME_BASED = "TIME_BASED"
MANUAL = "MANUAL"
RISK_EXIT = "RISK"
KILL_SWITCH = "KILL_SWITCH"
END_OF_SESSION = "END_OF_SESSION"

ALL_EXIT_REASONS = frozenset({
    STOP_LOSS, TARGET, STRATEGY_REVERSAL, TIME_BASED,
    MANUAL, RISK_EXIT, KILL_SWITCH, END_OF_SESSION,
})
