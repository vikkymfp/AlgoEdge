from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# The unit realized P&L - and therefore daily_loss_limit - is measured in.
# Paper Auto Trade fills at the underlying index's price, never an option
# premium (see algoedge.order_manager.SimulatedAccount.fill_event), so its
# realized P&L is (exit - entry) underlying price x quantity: index points,
# not rupees.
PNL_UNIT_UNDERLYING_POINTS = "UNDERLYING_POINTS"


@dataclass(frozen=True)
class RiskLimits:
    """Configurable risk limits, per the blueprint's Risk Manager spec.

    These are EXECUTION / RISK CONTROLS that paper Auto Trade applies on top
    of the canonical strategy's signals - they are deliberately NOT strategy
    rules, so the Backtest (which measures the strategy's signal quality,
    fno_signals.strategy.run()) does not model them: the entry cutoff,
    forced square-off, cooldown, daily trade cap, daily loss limit,
    portfolio-wide max open positions and the consecutive-loss halt. A
    paper result can therefore legitimately differ from the Backtest for
    the same window; see research/phase6/FINDINGS.md section 5.
    """

    # Expressed in daily_loss_limit_unit, the same unit record_trade()'s
    # realized_pnl is accumulated in - never assumed to be rupees.
    daily_loss_limit: float = 5000.0
    daily_loss_limit_unit: str = PNL_UNIT_UNDERLYING_POINTS
    max_trades_per_day: int = 10
    max_open_positions: int = 1
    max_quantity: int = 50
    trading_start: time = time(9, 15)  # IST, NSE cash-market open
    trading_end: time = time(15, 30)  # IST, NSE cash-market close, hard stop for everything

    # Distinct from trading_end: no NEW positions may be opened after
    # entry_cutoff, but an existing position may still be exited up until
    # trading_end. square_off_time is enforced for paper Auto Trade by
    # algoedge.auto_trader.run_cycle() (Phase 4) - once reached, any open
    # simulated CALL/PUT position is closed unconditionally, once per
    # trading day. This field itself is just the configured boundary;
    # RiskManager.check() never references it directly, since square-off
    # is a forced close, not a gated new-order decision.
    entry_cutoff: time = time(15, 0)
    square_off_time: time = time(15, 20)

    # "3 consecutive losses -> HALTED" from the spec. Deliberately requires
    # an explicit reset (see RiskManager.reset_consecutive_loss_halt) - the
    # spec is explicit that this must never auto-reopen on its own.
    # A paper Auto Trade RISK CONTROL, not a strategy indicator or filter:
    # it never changes which signals the strategy generates, only whether
    # paper may act on them, so its effect is reported separately from
    # strategy (Backtest) performance - see research/phase6/FINDINGS.md.
    max_consecutive_losses: int = 3

    # Minimum gap between an exit and the next new entry - "no blind
    # re-entry" from the spec.
    cooldown_minutes: int = 5

    # order_value/capital_allocated checks in `check()` are skipped
    # (not evaluated) whenever the caller can't supply a real number -
    # this account's Groww tier has no live option-quote access, so a
    # MARKET order's actual premium is genuinely unknown before it fills.
    # Still real, tested limits for any caller that DOES have a price
    # (e.g. paper Auto Trading, which prices off yfinance).
    max_capital_allocation: float = 200_000.0
    max_order_value: float = 100_000.0
    max_option_premium_exposure: float = 100_000.0


@dataclass
class RiskState:
    auto_trading_enabled: bool = False
    kill_switch: bool = False
    kill_switch_reason: str | None = None
    # Every successful fill today (entries, exits, square-offs) - the unit
    # the live fno_signals CLI's max_trades_per_day check counts in.
    trades_today: int = 0
    # Successful NEW ENTRY fills today (record_entry()), counted only by paper
    # Auto Trade: paper's max_trades_per_day means new entries, so exits,
    # square-offs and recoveries never use up its allowance.
    entries_today: int = 0
    realized_pnl_today: float = 0.0
    trade_day: str | None = None
    consecutive_losses: int = 0
    consecutive_loss_halt: bool = False
    last_exit_at: datetime | None = None


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskManager:
    """Sits between the strategy signal and the Order Manager.

    If a critical risk check fails, the order must be blocked — this class
    never places orders itself, it only decides whether one may proceed.
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.state = RiskState()

    def _reset_if_new_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.trade_day != today:
            self.state.trade_day = today
            self.state.trades_today = 0
            self.state.entries_today = 0
            self.state.realized_pnl_today = 0.0
            # Deliberately NOT reset here: consecutive_losses/
            # consecutive_loss_halt. The spec requires an explicit reset,
            # not a new trading day quietly reopening a halted account.

    def record_trade(
        self, realized_pnl: float = 0.0, now: datetime | None = None, *, is_exit: bool = False,
    ) -> None:
        now = now or datetime.now(IST)
        self._reset_if_new_day(now)
        self.state.trades_today += 1
        self.state.realized_pnl_today += realized_pnl
        if is_exit:
            self.state.last_exit_at = now
            if realized_pnl < 0:
                self.state.consecutive_losses += 1
                if self.state.consecutive_losses >= self.limits.max_consecutive_losses:
                    self.state.consecutive_loss_halt = True
            elif realized_pnl > 0:
                self.state.consecutive_losses = 0
            # A breakeven exit (realized_pnl == 0) leaves the streak
            # unchanged - a scratch trade is neither a win nor a loss.

    def record_entry(self, now: datetime | None = None) -> None:
        """Counts one successful NEW ENTRY fill toward today's paper entry
        cap (see check(..., cap_new_entries=True)). Called by paper Auto
        Trade in addition to record_trade(), never for an exit/square-off."""
        now = now or datetime.now(IST)
        self._reset_if_new_day(now)
        self.state.entries_today += 1

    def enable_auto_trading(self) -> None:
        self.state.auto_trading_enabled = True

    def disable_auto_trading(self) -> None:
        self.state.auto_trading_enabled = False

    def trip_kill_switch(self, reason: str = "Manually engaged") -> None:
        self.state.kill_switch = True
        self.state.kill_switch_reason = reason

    def reset_kill_switch(self) -> None:
        self.state.kill_switch = False
        self.state.kill_switch_reason = None

    def reset_consecutive_loss_halt(self) -> None:
        self.state.consecutive_loss_halt = False
        self.state.consecutive_losses = 0

    def check(
        self,
        action: str,
        quantity: int,
        open_positions: int,
        now: datetime | None = None,
        *,
        order_value: float | None = None,
        capital_allocated: float | None = None,
        risk_reducing: bool = False,
        cap_new_entries: bool = False,
    ) -> RiskDecision:
        """`risk_reducing=True` marks a SELL that closes an existing paper
        position (a strategy SL/target exit): the kill switch, the
        consecutive-loss halt and the auto-trading-enabled switch stop NEW
        risk, so they must not trap an open position without its exit (the
        halt is shared across indices - one index's losing streak must not
        freeze another index's stop). The switches and the halt themselves
        stay as they are. Ignored for BUY. Off by default, so every other
        caller is unchanged.

        `cap_new_entries=True` (paper Auto Trade) makes max_trades_per_day
        count successful NEW ENTRY fills (state.entries_today) instead of
        every fill (state.trades_today, the default - still what the live
        fno_signals CLI counts)."""
        now = now or datetime.now(IST)
        self._reset_if_new_day(now)

        if action not in {"BUY", "SELL"}:
            return RiskDecision(False, f"Unsupported action: {action}")
        exit_only = risk_reducing and action == "SELL"
        if self.state.kill_switch and not exit_only:
            reason = self.state.kill_switch_reason or "no reason recorded"
            return RiskDecision(False, f"Emergency kill switch is engaged ({reason})")
        if self.state.consecutive_loss_halt and not exit_only:
            return RiskDecision(
                False,
                f"Trading halted after {self.state.consecutive_losses} consecutive losses - reset required",
            )
        if not self.state.auto_trading_enabled and not exit_only:
            return RiskDecision(False, "Auto trading is disabled")
        if not (self.limits.trading_start <= now.time() <= self.limits.trading_end):
            return RiskDecision(False, "Outside configured trading hours")
        if action == "BUY" and now.time() > self.limits.entry_cutoff:
            return RiskDecision(False, "Past entry cutoff - no new positions may be opened")
        if action == "BUY" and self.state.last_exit_at is not None:
            cooldown_until = self.state.last_exit_at + timedelta(minutes=self.limits.cooldown_minutes)
            if now < cooldown_until:
                return RiskDecision(False, f"Cooldown active until {cooldown_until.time().isoformat()}")
        # Both gate taking on NEW risk only - a SELL closes an existing
        # position's SL/target exit, which must never be trapped open by
        # the very loss/activity it is part of limiting.
        counted = self.state.entries_today if cap_new_entries else self.state.trades_today
        if action == "BUY" and counted >= self.limits.max_trades_per_day:
            return RiskDecision(False, "Max trades per day reached")
        if action == "BUY" and self.state.realized_pnl_today <= -abs(self.limits.daily_loss_limit):
            return RiskDecision(False, "Daily loss limit reached")
        if quantity <= 0:
            return RiskDecision(False, "Quantity must be positive")
        if quantity > self.limits.max_quantity:
            return RiskDecision(False, "Quantity exceeds max allowed")
        if order_value is not None and order_value > self.limits.max_order_value:
            return RiskDecision(False, "Order value exceeds max allowed")
        if order_value is not None and order_value > self.limits.max_option_premium_exposure:
            return RiskDecision(False, "Option premium exposure exceeds max allowed")
        if (
            order_value is not None and capital_allocated is not None
            and (capital_allocated + order_value) > self.limits.max_capital_allocation
        ):
            return RiskDecision(False, "Max capital allocation exceeded")
        if action == "BUY" and open_positions >= self.limits.max_open_positions:
            return RiskDecision(False, "Max open positions reached")
        if action == "SELL" and open_positions <= 0:
            return RiskDecision(False, "No open position to exit")
        return RiskDecision(True, "Risk checks passed")


def restore_risk_state(risk_manager: RiskManager, snapshot: dict[str, Any]) -> None:
    """Applies a persisted risk-state snapshot (see
    `algoedge.db.load_latest_risk_state()`) onto `risk_manager.state` at
    startup, so the daily-loss/trades-today counters, kill switch and
    consecutive-loss halt survive a restart.

    `last_exit_at` is stored in a timezone-naive DB DateTime column and
    comes back naive; it is restored as Asia/Kolkata, the same convention
    `algoedge.auto_trader.restore_account_state()` uses for the paper
    account's `last_event_at`. Left naive, the cooldown comparison in
    `check()` against the timezone-aware current time would raise
    TypeError on every new-entry check after a restart.

    `entries_today` is restored as saved; see _restored_entries_today() for
    snapshots written before it existed.
    """
    state = risk_manager.state
    last_exit_at = snapshot["last_exit_at"]
    if last_exit_at is not None and last_exit_at.tzinfo is None:
        last_exit_at = last_exit_at.replace(tzinfo=IST)
    state.auto_trading_enabled = snapshot["auto_trading_enabled"]
    state.kill_switch = snapshot["kill_switch"]
    state.kill_switch_reason = snapshot["kill_switch_reason"]
    state.trades_today = snapshot["trades_today"]
    state.entries_today = _restored_entries_today(snapshot)
    state.realized_pnl_today = snapshot["realized_pnl_today"]
    state.trade_day = snapshot["trade_day"]
    state.consecutive_losses = snapshot["consecutive_losses"]
    state.consecutive_loss_halt = snapshot["consecutive_loss_halt"]
    state.last_exit_at = last_exit_at


def _restored_entries_today(snapshot: dict[str, Any]) -> int:
    """Legacy compatibility fallback: a snapshot saved before entries_today
    existed has none (NULL), so derive it from that day's fill count. Paper
    holds at most one position at a time across all indices
    (max_open_positions=1), so historical fills normally alternate entry,
    exit, entry, ... and ceil(fills / 2) is exact - a B4 missed-square-off
    recovery fill can make it one too high, a conservative overestimate
    (fewer entries allowed). It only affects the rest of that IST day: the
    counter resets to 0 on the next IST day."""
    entries = snapshot.get("entries_today")
    if entries is not None:
        return int(entries)
    return (int(snapshot["trades_today"]) + 1) // 2
