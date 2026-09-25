from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class RiskLimits:
    """Configurable risk limits, per the blueprint's Risk Manager spec."""

    daily_loss_limit: float = 5000.0
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
    trades_today: int = 0
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
    ) -> RiskDecision:
        now = now or datetime.now(IST)
        self._reset_if_new_day(now)

        if action not in {"BUY", "SELL"}:
            return RiskDecision(False, f"Unsupported action: {action}")
        if self.state.kill_switch:
            reason = self.state.kill_switch_reason or "no reason recorded"
            return RiskDecision(False, f"Emergency kill switch is engaged ({reason})")
        if self.state.consecutive_loss_halt:
            return RiskDecision(
                False,
                f"Trading halted after {self.state.consecutive_losses} consecutive losses - reset required",
            )
        if not self.state.auto_trading_enabled:
            return RiskDecision(False, "Auto trading is disabled")
        if not (self.limits.trading_start <= now.time() <= self.limits.trading_end):
            return RiskDecision(False, "Outside configured trading hours")
        if action == "BUY" and now.time() > self.limits.entry_cutoff:
            return RiskDecision(False, "Past entry cutoff - no new positions may be opened")
        if action == "BUY" and self.state.last_exit_at is not None:
            cooldown_until = self.state.last_exit_at + timedelta(minutes=self.limits.cooldown_minutes)
            if now < cooldown_until:
                return RiskDecision(False, f"Cooldown active until {cooldown_until.time().isoformat()}")
        if self.state.trades_today >= self.limits.max_trades_per_day:
            return RiskDecision(False, "Max trades per day reached")
        if self.state.realized_pnl_today <= -abs(self.limits.daily_loss_limit):
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
