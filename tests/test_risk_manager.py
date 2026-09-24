from datetime import datetime, time, timedelta

from algoedge.risk_manager import IST, RiskLimits, RiskManager

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=IST)
OFF_HOURS_NOW = datetime(2026, 9, 23, 20, 0, tzinfo=IST)


def make_enabled_manager(**limit_overrides) -> RiskManager:
    manager = RiskManager(RiskLimits(**limit_overrides))
    manager.enable_auto_trading()
    return manager


def test_blocks_when_auto_trading_disabled() -> None:
    manager = RiskManager()

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert decision.reason == "Auto trading is disabled"


def test_allows_a_valid_entry_when_enabled() -> None:
    manager = make_enabled_manager()

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is True


def test_blocks_when_kill_switch_engaged() -> None:
    manager = make_enabled_manager()
    manager.trip_kill_switch()

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "kill switch" in decision.reason.lower()


def test_kill_switch_reset_allows_trading_again() -> None:
    manager = make_enabled_manager()
    manager.trip_kill_switch()
    manager.reset_kill_switch()

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is True


def test_blocks_outside_trading_hours() -> None:
    manager = make_enabled_manager()

    decision = manager.check("BUY", quantity=1, open_positions=0, now=OFF_HOURS_NOW)

    assert decision.allowed is False
    assert "trading hours" in decision.reason.lower()


def test_blocks_buy_when_max_open_positions_reached() -> None:
    manager = make_enabled_manager(max_open_positions=1)

    decision = manager.check("BUY", quantity=1, open_positions=1, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "open positions" in decision.reason.lower()


def test_blocks_sell_when_no_open_position() -> None:
    manager = make_enabled_manager()

    decision = manager.check("SELL", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "no open position" in decision.reason.lower()


def test_blocks_quantity_over_max() -> None:
    manager = make_enabled_manager(max_quantity=10)

    decision = manager.check("BUY", quantity=11, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "quantity" in decision.reason.lower()


def test_blocks_after_max_trades_per_day() -> None:
    manager = make_enabled_manager(max_trades_per_day=2)
    manager.record_trade(realized_pnl=10.0, now=TRADING_HOURS_NOW)
    manager.record_trade(realized_pnl=10.0, now=TRADING_HOURS_NOW)

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "max trades" in decision.reason.lower()


def test_blocks_after_daily_loss_limit_hit() -> None:
    manager = make_enabled_manager(daily_loss_limit=100.0)
    manager.record_trade(realized_pnl=-150.0, now=TRADING_HOURS_NOW)

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is False
    assert "daily loss" in decision.reason.lower()


def test_trade_counters_reset_on_a_new_day() -> None:
    manager = make_enabled_manager(max_trades_per_day=1)
    manager.record_trade(realized_pnl=-1000.0, now=datetime(2026, 9, 22, 10, 0, tzinfo=IST))

    decision = manager.check("BUY", quantity=1, open_positions=0, now=TRADING_HOURS_NOW)

    assert decision.allowed is True


def test_trading_hours_boundaries_are_inclusive() -> None:
    manager = make_enabled_manager()

    opening = datetime.combine(TRADING_HOURS_NOW.date(), time(9, 15))
    closing = datetime.combine(TRADING_HOURS_NOW.date(), time(15, 30))

    assert manager.check("BUY", 1, 0, now=opening).allowed is True
    # A SELL (exit) is still allowed right up to trading_end - only a new
    # entry (BUY) is additionally gated by entry_cutoff, tested separately.
    assert manager.check("SELL", 1, 1, now=closing).allowed is True


# -- kill switch reason ----------------------------------------------


def test_kill_switch_reason_is_included_in_the_decision() -> None:
    manager = make_enabled_manager()
    manager.trip_kill_switch("Unexpected drawdown observed manually")

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW)

    assert "Unexpected drawdown observed manually" in decision.reason


def test_kill_switch_reason_defaults_when_not_given() -> None:
    manager = make_enabled_manager()
    manager.trip_kill_switch()

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW)

    assert "Manually engaged" in decision.reason


def test_reset_kill_switch_clears_the_reason() -> None:
    manager = make_enabled_manager()
    manager.trip_kill_switch("something bad")
    manager.reset_kill_switch()

    assert manager.state.kill_switch is False
    assert manager.state.kill_switch_reason is None
    assert manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW).allowed is True


# -- entry cutoff vs trading end ----------------------------------------------


def test_new_entry_blocked_past_entry_cutoff_but_still_within_trading_hours() -> None:
    manager = make_enabled_manager(entry_cutoff=time(15, 0), trading_end=time(15, 30))
    past_cutoff = datetime(2026, 9, 23, 15, 10, tzinfo=IST)

    decision = manager.check("BUY", 1, 0, now=past_cutoff)

    assert decision.allowed is False
    assert "entry cutoff" in decision.reason.lower()


def test_exit_still_allowed_past_entry_cutoff() -> None:
    manager = make_enabled_manager(entry_cutoff=time(15, 0), trading_end=time(15, 30))
    past_cutoff = datetime(2026, 9, 23, 15, 10, tzinfo=IST)

    decision = manager.check("SELL", 1, 1, now=past_cutoff)

    assert decision.allowed is True


# -- cooldown after exit ----------------------------------------------


def test_new_entry_blocked_during_cooldown_after_an_exit() -> None:
    manager = make_enabled_manager(cooldown_minutes=5)
    manager.record_trade(realized_pnl=100.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(minutes=2))

    assert decision.allowed is False
    assert "cooldown" in decision.reason.lower()


def test_new_entry_allowed_once_cooldown_elapses() -> None:
    manager = make_enabled_manager(cooldown_minutes=5)
    manager.record_trade(realized_pnl=100.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(minutes=6))

    assert decision.allowed is True


def test_cooldown_does_not_apply_to_exits() -> None:
    manager = make_enabled_manager(cooldown_minutes=5)
    manager.record_trade(realized_pnl=100.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("SELL", 1, 1, now=TRADING_HOURS_NOW + timedelta(minutes=1))

    assert decision.allowed is True


def test_non_exit_trades_do_not_start_a_cooldown() -> None:
    manager = make_enabled_manager(cooldown_minutes=5)
    manager.record_trade(realized_pnl=0.0, now=TRADING_HOURS_NOW, is_exit=False)  # an entry, not an exit

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(minutes=1))

    assert decision.allowed is True


# -- consecutive losses ----------------------------------------------


def test_consecutive_losses_below_threshold_do_not_halt() -> None:
    manager = make_enabled_manager(max_consecutive_losses=3)
    for _ in range(2):
        manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(hours=1))

    assert decision.allowed is True
    assert manager.state.consecutive_loss_halt is False


def test_reaching_max_consecutive_losses_halts_trading() -> None:
    manager = make_enabled_manager(max_consecutive_losses=3)
    for _ in range(3):
        manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(hours=1))

    assert decision.allowed is False
    assert "consecutive losses" in decision.reason.lower()


def test_a_win_resets_the_consecutive_loss_streak() -> None:
    manager = make_enabled_manager(max_consecutive_losses=3)
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    manager.record_trade(realized_pnl=100.0, now=TRADING_HOURS_NOW, is_exit=True)  # win resets streak
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)

    assert manager.state.consecutive_losses == 1
    assert manager.state.consecutive_loss_halt is False


def test_a_breakeven_exit_does_not_change_the_streak() -> None:
    manager = make_enabled_manager(max_consecutive_losses=3)
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    manager.record_trade(realized_pnl=0.0, now=TRADING_HOURS_NOW, is_exit=True)  # scratch trade

    assert manager.state.consecutive_losses == 1


def test_consecutive_loss_halt_does_not_clear_on_a_new_day() -> None:
    # The spec is explicit: do not automatically reopen trading - a halt
    # from one day must still require a deliberate reset, unlike the
    # ordinary daily trades/pnl counters which do reset at midnight.
    manager = make_enabled_manager(max_consecutive_losses=2)
    manager.record_trade(realized_pnl=-50.0, now=datetime(2026, 9, 22, 10, 0, tzinfo=IST), is_exit=True)
    manager.record_trade(realized_pnl=-50.0, now=datetime(2026, 9, 22, 10, 5, tzinfo=IST), is_exit=True)
    assert manager.state.consecutive_loss_halt is True

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW)  # a new day

    assert decision.allowed is False
    assert "consecutive losses" in decision.reason.lower()


def test_reset_consecutive_loss_halt_clears_it_and_the_streak() -> None:
    manager = make_enabled_manager(max_consecutive_losses=2)
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    manager.record_trade(realized_pnl=-50.0, now=TRADING_HOURS_NOW, is_exit=True)
    assert manager.state.consecutive_loss_halt is True

    manager.reset_consecutive_loss_halt()

    assert manager.state.consecutive_loss_halt is False
    assert manager.state.consecutive_losses == 0
    assert manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW + timedelta(hours=1)).allowed is True


# -- order value / capital allocation / premium exposure ----------------------------------------------


def test_order_value_over_limit_is_blocked() -> None:
    manager = make_enabled_manager(max_order_value=10_000.0)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW, order_value=15_000.0)

    assert decision.allowed is False
    assert "order value" in decision.reason.lower()


def test_order_value_within_limit_is_allowed() -> None:
    manager = make_enabled_manager(max_order_value=10_000.0)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW, order_value=5_000.0)

    assert decision.allowed is True


def test_option_premium_exposure_over_limit_is_blocked() -> None:
    manager = make_enabled_manager(max_order_value=1_000_000.0, max_option_premium_exposure=10_000.0)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW, order_value=15_000.0)

    assert decision.allowed is False
    assert "premium exposure" in decision.reason.lower()


def test_order_value_checks_are_skipped_when_not_provided() -> None:
    # This account's Groww tier has no live option-quote access, so a
    # MARKET order's real premium is genuinely unknown pre-trade - callers
    # without a real number must not be blocked by a guessed one.
    manager = make_enabled_manager(max_order_value=1.0, max_option_premium_exposure=1.0)

    decision = manager.check("BUY", 1, 0, now=TRADING_HOURS_NOW)

    assert decision.allowed is True


def test_max_capital_allocation_over_limit_is_blocked() -> None:
    manager = make_enabled_manager(max_capital_allocation=100_000.0)

    decision = manager.check(
        "BUY", 1, 0, now=TRADING_HOURS_NOW, order_value=30_000.0, capital_allocated=80_000.0,
    )

    assert decision.allowed is False
    assert "capital allocation" in decision.reason.lower()


def test_max_capital_allocation_within_limit_is_allowed() -> None:
    manager = make_enabled_manager(max_capital_allocation=100_000.0)

    decision = manager.check(
        "BUY", 1, 0, now=TRADING_HOURS_NOW, order_value=30_000.0, capital_allocated=50_000.0,
    )

    assert decision.allowed is True
