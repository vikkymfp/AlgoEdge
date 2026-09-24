from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import yfinance as yf
from growwapi.groww.exceptions import GrowwAPIException

from algoedge import alerts, db
from algoedge.config import get_settings
from algoedge.liquidity_check import check_liquidity
from algoedge.reconciliation import compute_expected_positions
from algoedge.reconciliation_gate import ReconciliationGate
from algoedge.risk_manager import RiskLimits, RiskManager
from algoedge.signal_pipeline import IngestOutcome, OptionSignal, SignalService, build_signal_id
from algoedge.signal_state import SignalState
from algoedge.structured_log import log_signal_decision
from fno_signals.broker import (
    ContractNotFoundError,
    GrowwSessionError,
    execute_market_order,
    generate_daily_session,
    resolve_contract,
)
from fno_signals.config import INDEX_MAP, IndexConfig, strategy_config_for
from fno_signals.strategy import TradeEvent, run
from fno_signals.verification import verify_order_status

# How long a signal stays actionable after the candle it was generated
# from - the spec's "signal expiration" requirement. A signal arriving
# for evaluation after this window has passed is rejected, never executed
# on stale data. Phase 2 may make this configurable per-index; a module
# constant is enough for now.
SIGNAL_EXPIRY_SECONDS = 120

# risk_manager.state.auto_trading_enabled is really a dashboard-Auto-Trading
# concept (a persistent toggle for the continuously-running paper
# scheduler). fno_signals is a manually-invoked CLI that already has its
# own gate (the --live flag plus the interactive per-order confirmation),
# so its own RiskManager is unconditionally enabled once --live is passed -
# see main(). Everything else (daily loss limit, trades/day, consecutive
# losses, cooldown, entry cutoff, kill switch) still applies for real.
#
# Tracked under a distinct scope ("live_fno_signals") from the dashboard's
# paper Auto Trading risk budget - a bad paper-trading day must never
# block real order capacity, and vice versa.
RISK_SCOPE = "live_fno_signals"

# One shared SignalService/RiskManager per process, matching how `db` is
# used module-wide - duplicate-signal/duplicate-order protection and daily
# risk counters only work if every call goes through the same instance.
signal_service = SignalService()
# RiskLimits() defaults are tuned for paper Auto Trading's synthetic
# 1-2 unit quantities - real NIFTY/BANK NIFTY/SENSEX lot sizes are 20-75
# per lot, so the generic max_quantity=50 default would wrongly block a
# single real lot. max_quantity here covers several lots of the largest
# configured index.
risk_manager = RiskManager(RiskLimits(max_quantity=500))
# Fails closed until a real reconciliation check runs at least once since
# this process started - see reconciliation_gate.py. Checked in main()
# right after authenticating, before the scanner is allowed to run live.
reconciliation_gate = ReconciliationGate()


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"


def fetch_underlying_data(ticker: str, period: str = "5d", interval: str = "5m"):
    history = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if history.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    return history


def format_event(event: TradeEvent, index_config: IndexConfig) -> str:
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M %Z")

    if event.kind in ("ENTRY_CALL", "ENTRY_PUT"):
        direction = "BUY_CALL" if event.kind == "ENTRY_CALL" else "BUY_PUT"
        color = Ansi.GREEN if direction == "BUY_CALL" else Ansi.RED
        sl_points = abs(event.underlying_price - event.stop_loss)
        tp_points = abs(event.target - event.underlying_price)
        rule = "=" * 60
        lines = [
            f"{color}{Ansi.BOLD}{rule}{Ansi.RESET}",
            f"{color}{Ansi.BOLD}  SIGNAL: {direction}{Ansi.RESET}",
            f"{color}{rule}{Ansi.RESET}",
            f"  Time           : {timestamp}",
            f"  Underlying     : {index_config.name} ({index_config.ticker})",
            f"  Spot Price     : {event.underlying_price:.2f}",
            f"  Option         : {event.option_symbol}",
            f"  Lot Size       : {index_config.lot_size}",
            f"  Stop Loss      : {event.stop_loss:.2f}  (-{sl_points:.2f} pts)",
            f"  Target         : {event.target:.2f}  (+{tp_points:.2f} pts)",
            f"{color}{rule}{Ansi.RESET}",
        ]
        return "\n".join(lines)

    reason = "SL" if event.kind == "EXIT_SL" else "TARGET"
    return (
        f"{Ansi.DIM}[{timestamp}] EXIT ({reason}) - {index_config.name} "
        f"@ {event.underlying_price:.2f} (level {event.exit_level:.2f}){Ansi.RESET}"
    )


def place_live_order(client: Any, index_config: IndexConfig, event: TradeEvent, confirm: bool) -> None:
    """Resolves the entry event's strike/right against Groww's live
    instrument master and, if confirmed, places the order. Never places an
    order for a symbol that wasn't just verified to exist and be tradeable.

    Every real order now goes through SignalService first: a deterministic
    signal_id (so re-scanning the same closed candle can never place a
    second order), an expiry check, and a duplicate-order check against
    anything already in flight for this exact symbol+direction - the
    idempotency and duplicate-protection rules from the production
    hardening spec, enforced before Groww is ever touched.
    """
    direction = "CALL" if event.kind == "ENTRY_CALL" else "PUT"
    candle_time = event.timestamp.to_pydatetime()
    now = candle_time  # signals are evaluated synchronously right after the candle closes
    signal = OptionSignal(
        signal_id=build_signal_id("fno_signals", index_config.groww_underlying, direction, candle_time),
        source="fno_signals",
        underlying_symbol=index_config.groww_underlying,
        underlying_price=event.underlying_price,
        direction=direction,
        created_at=now,
        expires_at=candle_time + timedelta(seconds=SIGNAL_EXPIRY_SECONDS),
        option_strike=event.strike,
        option_type=event.right,
        # event.stop_loss/target are ATR-based levels computed on the
        # UNDERLYING's own close price (sl_price = close - atr_distance in
        # strategy.py), never an option premium - mislabeling this as
        # OPTION_PREMIUM_BASED (the dataclass default) would have been
        # exactly the underlying-vs-premium confusion the spec's own §16
        # warns against.
        risk_model="UNDERLYING_BASED",
    )

    ingest_result = signal_service.ingest(signal, now=now)
    if ingest_result.outcome != IngestOutcome.ACCEPTED:
        print(
            f"{Ansi.YELLOW}  -> Signal not accepted ({ingest_result.outcome.value}): "
            f"{ingest_result.reason}. Order NOT placed.{Ansi.RESET}\n"
        )
        return

    signal_service.advance(signal.signal_id, SignalState.RISK_CHECK)

    if reconciliation_gate.is_blocking():
        signal_service.advance(
            signal.signal_id, SignalState.RISK_REJECTED,
            reason=f"Reconciliation gate blocking: {reconciliation_gate.last_check.reason}",
        )
        print(
            f"{Ansi.RED}  -> Blocked by reconciliation gate: {reconciliation_gate.last_check.reason}. "
            f"Order NOT placed.{Ansi.RESET}\n"
        )
        return

    conflicting = signal_service.check_duplicate_order(
        source="fno_signals", underlying_symbol=index_config.groww_underlying, direction=direction,
    )
    if conflicting is not None:
        signal_service.advance(
            signal.signal_id, SignalState.RISK_REJECTED,
            reason=f"Duplicate order - {conflicting['signalId']} is already in flight",
        )
        print(
            f"{Ansi.YELLOW}  -> An order for {index_config.groww_underlying} {direction} is already "
            f"in flight ({conflicting['signalId']}). Order NOT placed.{Ansi.RESET}\n"
        )
        return

    # Real orders on record (DB), not a live Groww call - fast, and
    # consistent with how the dashboard's Auto Trading risk gate counts
    # open positions. The reconciliation_gate check above is what keeps
    # this count trustworthy against Groww's actual truth.
    open_positions = len(compute_expected_positions(db.list_orders(source="fno_signals", live=True)))
    # The live-resolved lot size isn't known until resolve_contract()
    # succeeds, just below - use the configured reference lot size for
    # this pre-check (may occasionally be stale; see fno_signals/config.py)
    # rather than delaying the risk gate until after a network call.
    decision = risk_manager.check("BUY", index_config.lot_size, open_positions, now=now)
    if not decision.allowed:
        signal_service.advance(signal.signal_id, SignalState.RISK_REJECTED, reason=decision.reason)
        print(f"{Ansi.YELLOW}  -> Risk check failed: {decision.reason}. Order NOT placed.{Ansi.RESET}\n")
        return

    try:
        contract = resolve_contract(client, index_config, event.strike, event.right)
    except ContractNotFoundError as error:
        signal_service.advance(signal.signal_id, SignalState.OPTION_SELECTION_FAILED, reason=str(error))
        print(f"{Ansi.RED}  -> Could not resolve a live contract: {error}. Order NOT placed.{Ansi.RESET}\n")
        return
    signal_service.advance(signal.signal_id, SignalState.OPTION_SELECTED)

    print(
        f"{Ansi.CYAN}  -> Resolved live contract: {Ansi.BOLD}{contract.trading_symbol}{Ansi.RESET}"
        f"{Ansi.CYAN} ({contract.exchange}, expiry {contract.expiry_date}, lot size {contract.lot_size}){Ansi.RESET}"
    )

    # No bid/ask is available on this account's Groww tier (get_quote is
    # forbidden), so this always returns "skipped" today - the check
    # itself is real and tested (liquidity_check.py), ready for when quote
    # access exists, rather than silently absent from the pipeline.
    liquidity = check_liquidity(bid=None, ask=None)
    if not liquidity.allowed:
        # OPTION_SELECTED already succeeded (a real, tradeable contract was
        # found) - a bad spread is a reason not to proceed with THIS order,
        # not a failure to select the option itself, so this routes through
        # ORDER_REJECTED rather than OPTION_SELECTION_FAILED.
        signal_service.advance(signal.signal_id, SignalState.ORDER_REJECTED, reason=liquidity.reason)
        print(f"{Ansi.RED}  -> Liquidity check failed: {liquidity.reason}. Order NOT placed.{Ansi.RESET}\n")
        return

    if confirm:
        try:
            answer = input(
                f"  Place LIVE market order: BUY {contract.lot_size} x {contract.trading_symbol}? "
                f"Type 'yes' to confirm: "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            signal_service.advance(signal.signal_id, SignalState.ORDER_REJECTED, reason="Not confirmed by operator")
            print(f"{Ansi.YELLOW}  -> Order skipped (not confirmed).{Ansi.RESET}\n")
            return

    signal_service.advance(signal.signal_id, SignalState.ORDER_PENDING)

    try:
        response = execute_market_order(client, contract, quantity=contract.lot_size)
    except GrowwAPIException as error:
        signal_service.advance(signal.signal_id, SignalState.ORDER_FAILED, reason=str(error))
        print(f"{Ansi.RED}  -> Order placement failed: {error}{Ansi.RESET}\n")
        return

    groww_order_id = response.get("groww_order_id") if isinstance(response, dict) else None
    if not groww_order_id:
        signal_service.advance(
            signal.signal_id, SignalState.ORDER_FAILED, reason="No groww_order_id was returned",
        )
        print(
            f"{Ansi.RED}  -> Order was submitted but no groww_order_id was returned - "
            f"cannot verify fill status. Response: {response}{Ansi.RESET}\n"
        )
        return

    print(f"{Ansi.CYAN}  -> Order submitted ({groww_order_id}). Verifying fill status...{Ansi.RESET}")

    # --- Order Status Verification middleware: runs immediately after
    # placement, before this trade is ever treated as an open position. ---
    # No expected_price is passed - this is always a MARKET order, so no
    # pre-trade reference price is known on this account's Groww tier;
    # slippage is correctly left uncomputable rather than guessed.
    result = verify_order_status(
        client, groww_order_id, segment="FNO", requested_quantity=contract.lot_size,
    )

    db.record_order(
        source="fno_signals", live=True, index_id=index_config.groww_underlying,
        trading_symbol=contract.trading_symbol, exchange=contract.exchange,
        expiry_date=contract.expiry_date, strike=contract.strike, right=contract.right,
        side="BUY", order_type="MARKET", product="NRML", quantity=contract.lot_size,
        price=result.average_fill_price, groww_order_id=result.groww_order_id,
        outcome=result.outcome, order_status=result.order_status,
        reason=result.reason, attempts=result.attempts,
        filled_quantity=result.filled_quantity, remaining_quantity=result.remaining_quantity,
        slippage=result.slippage,
    )

    def _log_decision(*, status: str) -> None:
        log_signal_decision(
            signal_id=signal.signal_id, underlying_symbol=index_config.groww_underlying,
            underlying_price=event.underlying_price, direction=direction,
            strike=event.strike, option_type=event.right, risk_status="PASSED",
            position="OPEN" if result.outcome == "SUCCESS" else "FLAT",
            option_symbol=contract.trading_symbol, order_side="BUY",
            requested_quantity=result.requested_quantity, broker_order_id=result.groww_order_id,
            fill_quantity=result.filled_quantity, fill_price=result.average_fill_price, status=status,
        )

    if result.outcome == "PARTIAL":
        signal_service.advance(signal.signal_id, SignalState.PARTIALLY_FILLED, reason=result.reason)
        risk_manager.record_trade(realized_pnl=0.0, now=now)
        db.record_risk_snapshot(risk_manager, event="TRADE_RECORDED", scope=RISK_SCOPE)
        _log_decision(status="PARTIAL")
        print(
            f"{Ansi.YELLOW}{Ansi.BOLD}  -> PARTIAL FILL: {result.filled_quantity} of "
            f"{result.requested_quantity} filled ({result.remaining_quantity} remaining).{Ansi.RESET}\n"
            f"{Ansi.YELLOW}     The remainder was NOT auto-completed or cancelled - "
            f"check the Groww app and decide manually how to proceed.{Ansi.RESET}\n"
        )
        return

    if result.outcome == "SUCCESS":
        signal_service.advance(signal.signal_id, SignalState.FILLED)
        signal_service.advance(signal.signal_id, SignalState.POSITION_OPEN)
        # Always an entry (fno_signals --live only ever places BUY orders -
        # exits are detected/logged but not auto-executed live yet), so
        # realized_pnl is always 0 here and is_exit stays False;
        # consecutive-loss/cooldown tracking has nothing to react to until
        # live exits exist. Still counted toward trades_today/daily budget.
        risk_manager.record_trade(realized_pnl=0.0, now=now)
        db.record_risk_snapshot(risk_manager, event="TRADE_RECORDED", scope=RISK_SCOPE)
        _log_decision(status="FILLED")
        print(
            f"{Ansi.GREEN}{Ansi.BOLD}  -> ORDER EXECUTED ({result.order_status}) "
            f"after {result.attempts} check(s). Position is now live.{Ansi.RESET}\n"
        )
        return

    if result.outcome == "FAILED":
        signal_service.advance(signal.signal_id, SignalState.ORDER_FAILED, reason=result.reason)
        alerts.raise_alert(
            alerts.ORDER_FAILED, f"{contract.trading_symbol}: {result.reason}", source="fno_signals",
        )
        _log_decision(status="FAILED")
        print(
            f"{Ansi.RED}{Ansi.BOLD}  !! ORDER FAILED ({result.order_status}): {result.reason}{Ansi.RESET}\n"
            f"{Ansi.RED}     No position was opened. Halting this signal's execution.{Ansi.RESET}\n"
        )
        return

    if result.outcome == "CANCELLED":
        signal_service.advance(signal.signal_id, SignalState.ORDER_REJECTED, reason=result.reason)
        alerts.raise_alert(
            alerts.ORDER_REJECTED, f"{contract.trading_symbol}: {result.reason}", source="fno_signals",
        )
        _log_decision(status="CANCELLED")
        print(f"{Ansi.YELLOW}  -> Order cancelled ({result.order_status}): {result.reason}{Ansi.RESET}\n")
        return

    # TIMEOUT: verification never reached a final state within max_retries.
    # Treated as needing reconciliation, not a clean failure - never
    # silently assume the trade is or isn't live.
    signal_service.advance(
        signal.signal_id, SignalState.RECONCILIATION_REQUIRED,
        reason=f"Order status timeout after {result.attempts} check(s)",
    )
    _log_decision(status="TIMEOUT")
    print(
        f"{Ansi.RED}{Ansi.BOLD}  !! ORDER STATUS TIMEOUT after {result.attempts} check(s) "
        f"(last known status: {result.order_status or 'unknown'}).{Ansi.RESET}\n"
        f"{Ansi.RED}     Could not confirm this order's fate - check the Groww app manually "
        f"before assuming any position is or isn't open.{Ansi.RESET}\n"
    )


def run_scan(
    index_choice: int,
    period: str = "5d",
    interval: str = "5m",
    client: Any = None,
    confirm: bool = True,
) -> None:
    index_config = INDEX_MAP[index_choice]
    config = strategy_config_for(index_config)

    print(f"{Ansi.DIM}Fetching {index_config.name} ({index_config.ticker}) - {period} @ {interval}...{Ansi.RESET}")
    data = fetch_underlying_data(index_config.ticker, period=period, interval=interval)
    _results, events = run(data, config, underlying_label=index_config.name)

    if not events:
        print(f"{Ansi.YELLOW}No signals for {index_config.name} in the fetched window.{Ansi.RESET}\n")
        return

    for event in events:
        print(format_event(event, index_config))
        db.record_signal(
            source="fno_signals", index_id=index_config.groww_underlying, timeframe=interval,
            action=event.kind, reason=event.option_symbol or f"exit @ {event.exit_level}",
            price=event.underlying_price,
        )
        if event.kind in ("ENTRY_CALL", "ENTRY_PUT"):
            if client is not None:
                place_live_order(client, index_config, event, confirm=confirm)
            else:
                print(f"{Ansi.DIM}  (dry-run - no order placed; pass --live to trade){Ansi.RESET}\n")
    print()


def print_menu() -> None:
    rule = "=" * 42
    print(f"{Ansi.BOLD}{Ansi.CYAN}{rule}")
    print("  AlgoEdge F&O Signal Scanner")
    print(f"{rule}{Ansi.RESET}")
    for key, index_config in INDEX_MAP.items():
        print(f"  {Ansi.BOLD}[{key}]{Ansi.RESET} {index_config.name}")
    print(f"  {Ansi.BOLD}[4]{Ansi.RESET} Exit")
    print()


def interactive_loop(
    period: str, interval: str, client: Any = None, confirm: bool = True,
) -> None:
    while True:
        print_menu()
        try:
            choice = input("Select an option: ").strip()
        except EOFError:
            return
        if choice in ("4", "exit", "quit", "q"):
            print("Goodbye.")
            return
        if choice not in {"1", "2", "3"}:
            print(f"{Ansi.RED}Invalid choice - enter 1, 2, 3, or 4.{Ansi.RESET}\n")
            continue
        run_scan(int(choice), period=period, interval=interval, client=client, confirm=confirm)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AlgoEdge NSE F&O signal scanner")
    parser.add_argument(
        "--index", type=int, choices=sorted(INDEX_MAP),
        help="1=Nifty 50, 2=Bank Nifty, 3=Sensex. Omit for the interactive menu.",
    )
    parser.add_argument("--period", default="5d", help="yfinance history period (default: 5d)")
    parser.add_argument("--interval", default="5m", help="yfinance candle interval (default: 5m)")
    parser.add_argument(
        "--live", action="store_true",
        help=(
            "Place REAL orders on Groww. Without this flag the scanner only "
            "prints calculations - no network trade endpoints are touched."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the per-order confirmation prompt in --live mode, for "
            "unattended/headless automation. Has no effect without --live."
        ),
    )
    return parser.parse_args(argv)


def _restore_risk_state() -> None:
    """Loads this CLI's own (real-money) risk budget from the DB, scoped
    separately from the dashboard's paper Auto Trading budget - daily
    loss/trades-today/consecutive-loss counters must survive across
    separate runs of this script within the same trading day, same as the
    always-running dashboard already does for its own risk manager."""
    prior = db.load_latest_risk_state(scope=RISK_SCOPE)
    if prior is None:
        return
    risk_manager.state.kill_switch = prior["kill_switch"]
    risk_manager.state.kill_switch_reason = prior["kill_switch_reason"]
    risk_manager.state.trades_today = prior["trades_today"]
    risk_manager.state.realized_pnl_today = prior["realized_pnl_today"]
    risk_manager.state.trade_day = prior["trade_day"]
    risk_manager.state.consecutive_losses = prior["consecutive_losses"]
    risk_manager.state.consecutive_loss_halt = prior["consecutive_loss_halt"]
    risk_manager.state.last_exit_at = prior["last_exit_at"]


def _run_reconciliation_check(client: Any) -> None:
    """Restart safety (spec §12): compares this CLI's own recorded live
    orders against Groww's actual FNO positions before any new order is
    permitted this run. The gate fails closed (blocking) until this
    succeeds - if it can't even be attempted (e.g. a transient Groww
    error), it stays blocked rather than assuming things are fine."""
    orders = db.list_orders(source="fno_signals", live=True)
    try:
        live_positions = client.get_positions_for_user(segment="FNO").get("positions", [])
    except GrowwAPIException as error:
        reconciliation_gate.mark_unavailable(f"Could not fetch live positions: {error}")
        return
    check = reconciliation_gate.evaluate(orders, live_positions)
    if check.status != "OK":
        print(
            f"{Ansi.RED}{Ansi.BOLD}ACTION REQUIRED: reconciliation {check.status} - "
            f"{check.reason}{Ansi.RESET}\n"
            f"{Ansi.RED}     New live orders are blocked until this is resolved. Check the "
            f"Groww app and the dashboard's Positions page.{Ansi.RESET}"
        )
        if check.status == "MISMATCH":
            alerts.raise_alert(
                alerts.POSITION_MISMATCH, check.reason or "Position mismatch detected", source="fno_signals",
            )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    db.init_db(get_settings())

    client = None
    if args.live:
        print(f"{Ansi.YELLOW}{Ansi.BOLD}LIVE MODE - real orders can be placed on your Groww account.{Ansi.RESET}")
        try:
            client = generate_daily_session()
        except GrowwSessionError as error:
            print(f"{Ansi.RED}Groww authentication failed: {error}{Ansi.RESET}")
            sys.exit(1)
        print(f"{Ansi.GREEN}Groww session verified.{Ansi.RESET}\n")

        _restore_risk_state()
        # The --live flag plus the per-order interactive confirmation IS
        # this CLI's enablement gate - see the RISK_SCOPE comment above.
        risk_manager.enable_auto_trading()
        if risk_manager.state.kill_switch:
            print(
                f"{Ansi.RED}{Ansi.BOLD}ACTION REQUIRED: kill switch is engaged "
                f"({risk_manager.state.kill_switch_reason or 'no reason recorded'}).{Ansi.RESET}"
            )
        if risk_manager.state.consecutive_loss_halt:
            print(
                f"{Ansi.RED}{Ansi.BOLD}ACTION REQUIRED: trading halted after "
                f"{risk_manager.state.consecutive_losses} consecutive losses - reset required.{Ansi.RESET}"
            )

        _run_reconciliation_check(client)

    if args.index is not None:
        run_scan(args.index, period=args.period, interval=args.interval, client=client, confirm=not args.yes)
    else:
        interactive_loop(period=args.period, interval=args.interval, client=client, confirm=not args.yes)


if __name__ == "__main__":
    main()
