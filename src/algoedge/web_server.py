from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from growwapi.groww.exceptions import GrowwAPIException
from pydantic import BaseModel

from algoedge import alerts, db, exit_reasons
from algoedge.auto_trader import restore_account_state, run_cycle
from algoedge.auto_trading_report import compute_equity_curve
from algoedge.backtest import (
    compute_backtest_metrics,
    compute_regime_labels,
    pair_trades,
    split_train_validation_test,
)
from algoedge.backtest_export import build_excel_report, build_pdf_report
from algoedge.config import get_settings
from algoedge.cost_model import CostModel
from algoedge.daily_summary import aggregate_period_summary, compute_daily_summary
from algoedge.groww_broker import GrowwBroker
from algoedge.live_grid import LiveGridService
from algoedge.manual_trades import compute_manual_trades
from algoedge.manual_trading import (
    DASHBOARD_INDEX_IDS,
    ManualOrderRequest,
    list_expiries,
    list_strikes,
    place_manual_order,
    resolve_manual_contract,
    validate_order_request,
)
from algoedge.market_pulse import (
    INDEX_DEFINITIONS,
    TIMEFRAMES,
    get_index_candles,
    get_index_summary,
)
from algoedge.order_manager import OrderManager
from algoedge.pnl import compute_paper_unrealized_pnl, compute_realized_pnl
from algoedge.reconciliation_gate import ReconciliationGate
from algoedge.risk_manager import RiskManager
from algoedge.scheduler import AutoTradingScheduler
from algoedge.strategy_engine import DEFAULT_STRATEGY_CONFIG, StrategyConfig, evaluate
from algoedge.strategy_performance import compute_strategy_performance
from algoedge.token_service import BrokerNotConnectedError, BrokerValidationError, TokenService
from fno_signals.broker import ContractNotFoundError, resolve_contract
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.indicators import round_to_strike
from fno_signals.main import fetch_underlying_data
from fno_signals.strategy import run as run_strategy
from fno_signals.verification import verify_order_status

settings = get_settings()
cost_model = CostModel(
    brokerage_per_order=settings.cost_brokerage_per_order,
    stt_percent_on_sell=settings.cost_stt_percent_on_sell,
    exchange_charges_percent=settings.cost_exchange_charges_percent,
    gst_percent=settings.cost_gst_percent,
    stamp_duty_percent_on_buy=settings.cost_stamp_duty_percent_on_buy,
)

# DB must come up first: TokenService reads any previously-stored encrypted
# credentials from it, and RiskManager's prior-state restore (below) needs
# it too.
db.init_db(settings)

# Deliberately does NOT crash startup if Groww credentials are missing or
# invalid (unlike the old GrowwBroker.from_settings() + verify_connection()
# call this replaced) - the dashboard must still boot in a degraded
# "ACTION REQUIRED" state so API Management is reachable to fix the
# connection. TokenService.auto_refresh_if_needed() (called from its own
# __init__) never raises - see token_service.py.
token_service = TokenService(settings)
broker = GrowwBroker(settings=settings, token_service=token_service)
service = LiveGridService(broker, settings)
risk_manager = RiskManager()
reconciliation_gate = ReconciliationGate()
order_managers: dict[str, OrderManager] = {index_id: OrderManager() for index_id in INDEX_DEFINITIONS}
SCHEDULER_TICK_SECONDS = 300.0  # 5 minutes, matches the default 5m candle timeframe

_prior_risk_state = db.load_latest_risk_state(scope="paper")
if _prior_risk_state is not None:
    risk_manager.state.auto_trading_enabled = _prior_risk_state["auto_trading_enabled"]
    risk_manager.state.kill_switch = _prior_risk_state["kill_switch"]
    risk_manager.state.kill_switch_reason = _prior_risk_state["kill_switch_reason"]
    risk_manager.state.trades_today = _prior_risk_state["trades_today"]
    risk_manager.state.realized_pnl_today = _prior_risk_state["realized_pnl_today"]
    risk_manager.state.trade_day = _prior_risk_state["trade_day"]
    risk_manager.state.consecutive_losses = _prior_risk_state["consecutive_losses"]
    risk_manager.state.consecutive_loss_halt = _prior_risk_state["consecutive_loss_halt"]
    risk_manager.state.last_exit_at = _prior_risk_state["last_exit_at"]

# Restores each index's paper Auto Trade position/dedup state (see
# auto_trader.restore_account_state()'s own docstring for the exact
# fields and the fail-safe behavior on corrupted/missing data). A restart
# never force-flattens an open paper position on its own - only its own
# strategy exit (SL/target) closes it, same as if the process had never
# restarted.
for _index_id, _order_manager in order_managers.items():
    _prior_account_state = db.load_latest_auto_trade_account_state(_index_id)
    if _prior_account_state is not None:
        restore_account_state(_order_manager.account, _prior_account_state)

app = FastAPI()


@app.get("/api/grids")
def grids() -> dict:
    try:
        return service.snapshot()
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (GrowwAPIException, KeyError, OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Live broker data unavailable") from error


@app.get("/api/positions")
def positions() -> dict:
    try:
        return service.positions_snapshot()
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (GrowwAPIException, KeyError, OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Live position data unavailable") from error


@app.get("/api/orders")
def orders() -> dict:
    try:
        return service.orders_snapshot()
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (GrowwAPIException, KeyError, OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Live order data unavailable") from error


@app.get("/api/account")
def account() -> dict:
    try:
        return service.account_snapshot()
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (GrowwAPIException, KeyError, OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Live account data unavailable") from error


@app.get("/api/market")
def market() -> dict:
    indices = []
    for index_id in INDEX_DEFINITIONS:
        summary = get_index_summary(index_id)
        indices.append(
            {
                "id": summary.id,
                "name": summary.name,
                "price": summary.price,
                "change": summary.change,
                "changePercent": summary.change_percent,
                "sparkline": summary.sparkline,
                "status": "LIVE" if summary.price is not None else "UNAVAILABLE",
            }
        )
    return {"source": "YAHOO FINANCE (FREE FEED)", "indices": indices}


@app.get("/api/market/candles/{index_id}")
def market_candles(index_id: str, interval: str = "5m") -> dict:
    if index_id not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown index")
    if interval not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported interval")
    return {
        "indexId": index_id,
        "interval": interval,
        "candles": get_index_candles(index_id, interval),
    }


@app.get("/api/strategy/signal/{index_id}")
def strategy_signal(
    index_id: str,
    interval: str = "5m",
    rsi_length: int = DEFAULT_STRATEGY_CONFIG.rsi_length,
    rsi_lower: float = DEFAULT_STRATEGY_CONFIG.rsi_lower,
    rsi_upper: float = DEFAULT_STRATEGY_CONFIG.rsi_upper,
    ema_length: int = DEFAULT_STRATEGY_CONFIG.ema_length,
    stop_loss_percent: float = DEFAULT_STRATEGY_CONFIG.stop_loss_percent,
    target_percent: float = DEFAULT_STRATEGY_CONFIG.target_percent,
    entry_price: float | None = None,
) -> dict:
    if index_id not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown index")
    if interval not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported interval")
    config = StrategyConfig(
        rsi_length=rsi_length,
        rsi_lower=rsi_lower,
        rsi_upper=rsi_upper,
        ema_length=ema_length,
        stop_loss_percent=stop_loss_percent,
        target_percent=target_percent,
    )
    candles = get_index_candles(index_id, interval)
    closes = pd.Series([candle["close"] for candle in candles])
    signal = evaluate(closes, config, entry_price=entry_price)
    signal_price = None if pd.isna(signal.price) else signal.price
    db.record_signal(
        source="algoedge.strategy_engine", index_id=index_id, timeframe=interval,
        action=signal.action, reason=signal.reason, price=signal_price,
        rsi=signal.rsi, ema=signal.ema,
    )
    return {
        "indexId": index_id,
        "interval": interval,
        "config": {
            "rsiLength": config.rsi_length,
            "rsiLower": config.rsi_lower,
            "rsiUpper": config.rsi_upper,
            "emaLength": config.ema_length,
            "stopLossPercent": config.stop_loss_percent,
            "targetPercent": config.target_percent,
        },
        "signal": {
            "action": signal.action,
            "reason": signal.reason,
            "price": signal_price,
            "rsi": signal.rsi,
            "ema": signal.ema,
        },
    }


def _total_open_positions() -> int:
    return sum(1 for manager in order_managers.values() if manager.account.quantity > 0)


@app.get("/api/auto-trading/status")
def auto_trading_status() -> dict:
    state = risk_manager.state
    return {
        "enabled": state.auto_trading_enabled,
        "killSwitch": state.kill_switch,
        "killSwitchReason": state.kill_switch_reason,
        "tradesToday": state.trades_today,
        "realizedPnlToday": state.realized_pnl_today,
        "consecutiveLosses": state.consecutive_losses,
        "consecutiveLossHalt": state.consecutive_loss_halt,
        "lastExitAt": state.last_exit_at,
        "limits": {
            "dailyLossLimit": risk_manager.limits.daily_loss_limit,
            "maxTradesPerDay": risk_manager.limits.max_trades_per_day,
            "maxOpenPositions": risk_manager.limits.max_open_positions,
            "maxQuantity": risk_manager.limits.max_quantity,
            "tradingStart": risk_manager.limits.trading_start.isoformat(),
            "tradingEnd": risk_manager.limits.trading_end.isoformat(),
            "entryCutoff": risk_manager.limits.entry_cutoff.isoformat(),
            "squareOffTime": risk_manager.limits.square_off_time.isoformat(),
            "maxConsecutiveLosses": risk_manager.limits.max_consecutive_losses,
            "cooldownMinutes": risk_manager.limits.cooldown_minutes,
        },
        "accounts": {
            index_id: {
                "indexName": INDEX_DEFINITIONS[index_id][0],
                "cash": manager.account.cash,
                "quantity": manager.account.quantity,
                "averagePrice": manager.account.average_price,
                "side": manager.account.side,
            }
            for index_id, manager in order_managers.items()
        },
        "scheduler": {
            "tickSeconds": SCHEDULER_TICK_SECONDS,
            "indexIds": list(order_managers.keys()),
        },
    }


@app.post("/api/auto-trading/enable")
def auto_trading_enable() -> dict:
    risk_manager.enable_auto_trading()
    db.record_risk_snapshot(risk_manager, event="ENABLE")
    return auto_trading_status()


@app.post("/api/auto-trading/disable")
def auto_trading_disable() -> dict:
    risk_manager.disable_auto_trading()
    db.record_risk_snapshot(risk_manager, event="DISABLE")
    return auto_trading_status()


@app.post("/api/auto-trading/kill-switch")
def auto_trading_kill_switch(reason: str = "Manually engaged") -> dict:
    risk_manager.trip_kill_switch(reason)
    db.record_risk_snapshot(risk_manager, event="KILL_SWITCH_ON")
    alerts.raise_alert(alerts.KILL_SWITCH_ACTIVATED, reason, source="web_server")
    return auto_trading_status()


@app.post("/api/auto-trading/kill-switch/reset")
def auto_trading_kill_switch_reset() -> dict:
    risk_manager.reset_kill_switch()
    db.record_risk_snapshot(risk_manager, event="KILL_SWITCH_OFF")
    return auto_trading_status()


@app.post("/api/auto-trading/consecutive-loss-halt/reset")
def auto_trading_consecutive_loss_halt_reset() -> dict:
    risk_manager.reset_consecutive_loss_halt()
    db.record_risk_snapshot(risk_manager, event="CONSECUTIVE_LOSS_HALT_RESET")
    return auto_trading_status()


def _run_and_persist_cycle(index_id: str, interval: str, quantity: int) -> dict:
    """Shared by the "Run cycle now" endpoint and the background scheduler,
    so a manual click and a scheduled tick always execute and persist the
    exact same way.

    Uses the canonical fno_signals strategy (via auto_trader.run_cycle) -
    `config` is always the per-index default built by
    `fno_signals.config.strategy_config_for()`, matching Backtest exactly,
    so there is no per-request RSI/EMA/SL%/TP% override anymore (the old
    strategy_engine knobs had no equivalent in the canonical strategy's
    EMA/RSI/Supertrend/ATR config)."""
    order_manager = order_managers[index_id]
    was_halted = risk_manager.state.consecutive_loss_halt
    result = run_cycle(
        index_id, interval, risk_manager, order_manager, quantity=quantity,
        total_open_positions=_total_open_positions(),
    )
    if not result.risk.allowed and result.risk.reason == "Daily loss limit reached":
        alerts.raise_alert(
            alerts.DAILY_LOSS_LIMIT_REACHED,
            f"{index_id}: realized P&L today {risk_manager.state.realized_pnl_today:.2f}",
            source="algoedge.auto_trader",
        )
    if not was_halted and risk_manager.state.consecutive_loss_halt:
        alerts.raise_alert(
            alerts.TRADING_HALTED,
            f"{risk_manager.state.consecutive_losses} consecutive losses - reset required",
            source="algoedge.auto_trader",
        )
    event = result.event
    if event is None:
        return {
            "indexId": index_id,
            "interval": interval,
            "signal": None,
            "risk": {"allowed": result.risk.allowed, "reason": result.risk.reason},
            "order": None,
            "account": {
                "cash": order_manager.account.cash,
                "quantity": order_manager.account.quantity,
                "averagePrice": order_manager.account.average_price,
                "side": order_manager.account.side,
            },
        }
    is_exit = event.kind in ("EXIT_SL", "EXIT_TARGET")
    exit_reason = (exit_reasons.STOP_LOSS if event.kind == "EXIT_SL" else exit_reasons.TARGET) if is_exit else None
    db.record_signal(
        source="algoedge.auto_trader", index_id=index_id, timeframe=interval,
        action=event.kind, reason=event.option_symbol or f"exit @ {event.exit_level}",
        price=event.underlying_price,
    )
    if result.order is not None:
        db.record_order(
            source="algoedge.auto_trader", live=False, index_id=index_id,
            side="SELL" if is_exit else "BUY", right=event.right, strike=event.strike,
            order_type="MARKET", quantity=quantity, price=event.underlying_price,
            outcome=result.order.status, reason=result.order.detail,
            realized_pnl=result.order.realized_pnl, exit_reason=exit_reason,
        )
        db.record_risk_snapshot(risk_manager, event="TRADE_RECORDED")
        if result.order.status == "PLACED":
            # Persists the paper account's post-fill state (quantity/side/
            # average_price/last_event_at) so a process restart can restore
            # it - see the startup restoration block above `app = FastAPI()`.
            # A persistence failure here fails safe: db.record_* never
            # raises, the in-memory account state (and the order already
            # placed) is unaffected either way, only next restart's
            # recovery would be degraded.
            db.record_auto_trade_account_snapshot(index_id, order_manager.account, event=event.kind)
    return {
        "indexId": index_id,
        "interval": interval,
        "signal": {
            "kind": event.kind,
            "optionSymbol": event.option_symbol,
            "price": event.underlying_price,
            "stopLoss": event.stop_loss,
            "target": event.target,
            "exitLevel": event.exit_level,
            "strike": event.strike,
            "right": event.right,
        },
        "risk": {"allowed": result.risk.allowed, "reason": result.risk.reason},
        "order": None
        if result.order is None
        else {
            "status": result.order.status,
            "detail": result.order.detail,
            "realizedPnl": result.order.realized_pnl,
        },
        "account": {
            "cash": order_manager.account.cash,
            "quantity": order_manager.account.quantity,
            "averagePrice": order_manager.account.average_price,
            "side": order_manager.account.side,
        },
    }


@app.post("/api/auto-trading/run/{index_id}")
def auto_trading_run(index_id: str, interval: str = "5m", quantity: int = 1) -> dict:
    if index_id not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown index")
    if interval not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported interval")
    return _run_and_persist_cycle(index_id, interval, quantity)


@app.get("/api/auto-trading/signals")
def auto_trading_signals(limit: int = 20) -> dict:
    return {"signals": db.list_signals(source="algoedge.auto_trader", limit=limit)}


@app.get("/api/auto-trading/equity-curve")
def auto_trading_equity_curve() -> dict:
    orders = db.list_orders(source="algoedge.auto_trader", live=False, limit=2000)
    points = compute_equity_curve(orders)
    return {
        "points": [
            {"closedAt": point.closed_at, "realizedPnl": point.realized_pnl, "cumulativePnl": point.cumulative_pnl}
            for point in points
        ],
    }


_OPTION_QUOTE_UNAVAILABLE_REASON = (
    "Live option quotes (premium/bid/ask/LTP) require Groww's paid Live Data API "
    "(get_quote/get_ltp/get_option_chain), which this account's free tier does not have."
)


@app.get("/api/auto-trading/option-context/{index_id}")
def auto_trading_option_context(index_id: str) -> dict:
    """Read-only informational context for the Auto Trading page's 'Live
    Option Market' panel: real spot price, a real live-resolved ATM
    strike/expiry/lot size per leg (via fno_signals' own instrument-master
    lookup), and the CALL/PUT setup condition from fno_signals' own strategy
    evaluation - the same engine the live --live CLI uses. The dashboard's
    own Auto Trading scheduler (auto_trader.py) has no concept of options at
    all (it trades the underlying directly); this endpoint never touches
    that scheduler and never places an order. Premium/bid/ask/LTP are always
    null - never fabricated - since this account's Groww tier has no live
    option-quote access."""
    index_config = _index_config_for(index_id)
    summary = get_index_summary(index_id)
    spot = summary.price

    atm_strike = None
    signal_state = None
    if spot is not None:
        atm_strike = round_to_strike(spot, index_config.strike_step)
        try:
            data = fetch_underlying_data(index_config.ticker, period="5d", interval="5m")
            strategy_config = strategy_config_for(index_config)
            results, _events = run_strategy(data, strategy_config, underlying_label=index_config.name)
            last = results.iloc[-1]
            signal_state = {
                "call": "BUY CALL" if bool(last["bull_setup"]) else "HOLD",
                "put": "BUY PUT" if bool(last["bear_setup"]) else "HOLD",
                "asOf": results.index[-1].isoformat(),
            }
        except (GrowwAPIException, KeyError, OSError, TypeError, ValueError, IndexError):
            signal_state = None

    def _resolve_leg(right: str) -> dict:
        if atm_strike is None:
            return {"available": False, "reason": "Spot price unavailable"}
        try:
            contract = resolve_contract(broker.client, index_config, atm_strike, right)
        except BrokerNotConnectedError:
            return {"available": False, "reason": "Groww is not connected"}
        except ContractNotFoundError as error:
            return {"available": False, "reason": str(error)}
        except GrowwAPIException as error:
            return {"available": False, "reason": f"Could not resolve contract: {error}"}
        return {
            "available": True,
            "tradingSymbol": contract.trading_symbol,
            "expiryDate": contract.expiry_date.isoformat(),
            "strike": contract.strike,
            "lotSize": contract.lot_size,
            "premium": None,
            "bid": None,
            "ask": None,
            "ltp": None,
            "quoteUnavailableReason": _OPTION_QUOTE_UNAVAILABLE_REASON,
        }

    return {
        "indexId": index_id,
        "underlyingName": index_config.name,
        "spot": spot,
        "atmStrike": atm_strike,
        "signal": signal_state,
        "call": _resolve_leg("CE"),
        "put": _resolve_leg("PE"),
    }


_scheduler = AutoTradingScheduler(
    index_ids=list(INDEX_DEFINITIONS.keys()),
    run_one=lambda index_id: _run_and_persist_cycle(index_id, "5m", 1),
    is_enabled=lambda: risk_manager.state.auto_trading_enabled and not risk_manager.state.kill_switch,
    tick_seconds=SCHEDULER_TICK_SECONDS,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    alerts.raise_alert(alerts.SYSTEM_RESTART, "AlgoEdge dashboard started", source="web_server")
    # Restart safety (spec §12): only after a real reconciliation check has
    # run should new (real) orders be permitted - the gate fails closed
    # (blocking) until this runs even once, so this must happen before the
    # app finishes starting, not lazily on the first page load.
    _run_reconciliation_check()
    _scheduler.start()
    yield
    await _scheduler.stop()


app.router.lifespan_context = _lifespan


def _index_config_for(index_id: str):
    key = DASHBOARD_INDEX_IDS.get(index_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Unknown index")
    return INDEX_MAP[key]


def _parse_expiry(expiry: str) -> date:
    try:
        return date.fromisoformat(expiry)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="expiry must be YYYY-MM-DD") from error


@app.get("/api/manual-trading/expiries/{index_id}")
def manual_trading_expiries(index_id: str) -> dict:
    index_config = _index_config_for(index_id)
    try:
        expiries = list_expiries(broker.client, index_config)
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GrowwAPIException as error:
        raise HTTPException(status_code=502, detail=f"Could not fetch expiries: {error}") from error
    return {"indexId": index_id, "expiries": [expiry.isoformat() for expiry in expiries]}


@app.get("/api/manual-trading/strikes/{index_id}")
def manual_trading_strikes(index_id: str, expiry: str) -> dict:
    index_config = _index_config_for(index_id)
    expiry_date = _parse_expiry(expiry)
    try:
        strikes = list_strikes(broker.client, index_config, expiry_date)
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GrowwAPIException as error:
        raise HTTPException(status_code=502, detail=f"Could not fetch strikes: {error}") from error
    return {"indexId": index_id, "expiry": expiry, "strikes": strikes}


@app.get("/api/manual-trading/preview")
def manual_trading_preview(
    index_id: str,
    expiry: str,
    strike: int,
    right: str,
    side: str = "BUY",
    order_type: str = "MARKET",
    lots: int = 1,
    product: str = "NRML",
    price: float | None = None,
    trigger_price: float | None = None,
) -> dict:
    index_config = _index_config_for(index_id)
    expiry_date = _parse_expiry(expiry)
    request = ManualOrderRequest(
        right=right, side=side, order_type=order_type, lots=lots,
        product=product, price=price, trigger_price=trigger_price,
    )
    try:
        validate_order_request(request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        contract = resolve_manual_contract(broker.client, index_config, expiry_date, strike, right)
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ContractNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GrowwAPIException as error:
        raise HTTPException(status_code=502, detail=f"Could not resolve contract: {error}") from error

    return {
        "liveTradingEnabled": settings.live_trading,
        "contract": {
            "tradingSymbol": contract.trading_symbol,
            "exchange": contract.exchange,
            "expiryDate": contract.expiry_date.isoformat(),
            "strike": contract.strike,
            "right": contract.right,
            "lotSize": contract.lot_size,
        },
        "quantity": lots * contract.lot_size,
    }


@app.post("/api/manual-trading/order")
def manual_trading_place_order(
    index_id: str,
    expiry: str,
    strike: int,
    right: str,
    side: str = "BUY",
    order_type: str = "MARKET",
    lots: int = 1,
    product: str = "NRML",
    price: float | None = None,
    trigger_price: float | None = None,
) -> dict:
    if not settings.live_trading:
        raise HTTPException(
            status_code=403,
            detail="Live trading is disabled. Set ALGOEDGE_LIVE_TRADING=true to place real orders.",
        )
    # Order Request -> API Connection Check -> Token Valid? -> ... -> Groww API.
    # Checked up front, before resolving a contract or placing anything, so
    # a missing connection never gets partway through an order attempt.
    # effective_client() runs first so an expired session is regenerated
    # from the API key/secret rather than rejected on a stale status; a
    # genuine authentication failure (no session obtainable) still blocks.
    try:
        token_service.effective_client()
    except BrokerNotConnectedError:
        pass  # reported by the connection check just below
    if not token_service.is_connected():
        raise HTTPException(
            status_code=503,
            detail="Groww is not connected. Configure it in API Management before placing an order.",
        )
    # Order Request -> API Connection Check -> Token Valid? -> Risk Check ->
    # ... A reconciliation mismatch or an order with an unconfirmed fill
    # status means AlgoEdge's own record of the account's real positions
    # can't be trusted - never place another order on top of an unknown
    # state. Re-check fresh rather than trusting a stale cached status.
    _run_reconciliation_check()
    if reconciliation_gate.is_blocking():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Reconciliation check blocked new orders: {reconciliation_gate.last_check.reason}. "
                "Review the Positions page or override in API Management."
            ),
        )
    index_config = _index_config_for(index_id)
    expiry_date = _parse_expiry(expiry)
    request = ManualOrderRequest(
        right=right, side=side, order_type=order_type, lots=lots,
        product=product, price=price, trigger_price=trigger_price,
    )
    try:
        validate_order_request(request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    # Always re-resolve fresh right before firing - never trust a
    # client-cached preview for the actual placement.
    try:
        contract = resolve_manual_contract(broker.client, index_config, expiry_date, strike, right)
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ContractNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GrowwAPIException as error:
        raise HTTPException(status_code=502, detail=f"Could not resolve contract: {error}") from error

    try:
        response = place_manual_order(broker.client, contract, request)
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except GrowwAPIException as error:
        raise HTTPException(status_code=502, detail=f"Order placement failed: {error}") from error

    quantity = lots * contract.lot_size
    groww_order_id = response.get("groww_order_id") if isinstance(response, dict) else None
    if not groww_order_id:
        db.record_order(
            source="algoedge.manual_trading", live=True, index_id=index_id,
            trading_symbol=contract.trading_symbol, exchange=contract.exchange,
            expiry_date=contract.expiry_date, strike=contract.strike, right=right,
            side=side, order_type=order_type, product=product, quantity=quantity,
            price=price, trigger_price=trigger_price, outcome="UNKNOWN",
            reason="Order submitted but no groww_order_id was returned",
        )
        return {
            "outcome": "UNKNOWN",
            "reason": "Order submitted but no groww_order_id was returned - cannot verify fill status.",
        }

    # LIMIT/SL orders have a real pre-trade reference price (what the user
    # actually specified) - MARKET/SL_M don't, so expected_price stays None
    # for those and slippage is correctly left uncomputable rather than
    # guessed against the trigger price (which is not a fill-price target).
    expected_price = price if order_type in {"LIMIT", "SL"} else None
    try:
        result = verify_order_status(
            broker.client, groww_order_id, segment="FNO",
            requested_quantity=quantity, expected_price=expected_price,
        )
    except BrokerNotConnectedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    db.record_order(
        source="algoedge.manual_trading", live=True, index_id=index_id,
        trading_symbol=contract.trading_symbol, exchange=contract.exchange,
        expiry_date=contract.expiry_date, strike=contract.strike, right=right,
        side=side, order_type=order_type, product=product, quantity=quantity,
        # prefer the ACTUAL fill price over the originally requested price -
        # for MARKET orders there is no requested price to fall back to.
        price=result.average_fill_price or price,
        trigger_price=trigger_price, groww_order_id=result.groww_order_id,
        outcome=result.outcome, order_status=result.order_status, reason=result.reason,
        attempts=result.attempts,
        filled_quantity=result.filled_quantity, remaining_quantity=result.remaining_quantity,
        expected_price=expected_price, slippage=result.slippage,
    )
    if result.outcome == "FAILED":
        alerts.raise_alert(
            alerts.ORDER_FAILED, f"{contract.trading_symbol}: {result.reason}", source="algoedge.manual_trading",
        )
    elif result.outcome == "CANCELLED":
        alerts.raise_alert(
            alerts.ORDER_REJECTED, f"{contract.trading_symbol}: {result.reason}", source="algoedge.manual_trading",
        )
    return {
        "outcome": result.outcome,
        "orderStatus": result.order_status,
        "growwOrderId": result.groww_order_id,
        "reason": result.reason,
        "attempts": result.attempts,
        "contract": {
            "tradingSymbol": contract.trading_symbol,
            "exchange": contract.exchange,
            "lotSize": contract.lot_size,
        },
        "quantity": quantity,
    }


@app.get("/api/manual-trading/trades")
def manual_trading_trades() -> dict:
    orders = db.list_orders(limit=2000)
    trades = compute_manual_trades(orders)
    return {
        "trades": [
            {
                "tradingSymbol": trade.trading_symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "entryPrice": trade.entry_price,
                "entryTime": trade.entry_time.isoformat() if hasattr(trade.entry_time, "isoformat") else trade.entry_time,
                "status": trade.status,
                "exitPrice": trade.exit_price,
                "exitTime": trade.exit_time.isoformat() if hasattr(trade.exit_time, "isoformat") else trade.exit_time,
                "pnl": trade.pnl,
            }
            for trade in trades
        ],
    }


def _run_reconciliation_check() -> dict:
    """Fetches DB orders + live Groww positions, re-evaluates the
    reconciliation gate, and records the outcome as an audit event - the
    single source of truth both the dashboard's polling and the startup
    check funnel through, so they can never drift out of sync with each
    other."""
    previous_status = reconciliation_gate.last_check.status
    orders = db.list_orders(live=True)
    try:
        live_positions = broker.client.get_positions_for_user(segment="FNO").get("positions", [])
    except BrokerNotConnectedError as error:
        reconciliation_gate.mark_unavailable(str(error))
        db.record_reconciliation_event(status="UNKNOWN", reason=str(error))
        if previous_status != "UNKNOWN":
            alerts.raise_alert(alerts.BROKER_DISCONNECTED, str(error), source="web_server")
        return _reconciliation_payload()
    except (GrowwAPIException, KeyError, TypeError, ValueError) as error:
        reason = f"Could not fetch live positions: {error}"
        reconciliation_gate.mark_unavailable(reason)
        db.record_reconciliation_event(status="UNKNOWN", reason=reason)
        return _reconciliation_payload()

    check = reconciliation_gate.evaluate(orders, live_positions)
    mismatch_count = sum(1 for c in check.report.comparisons if not c.matches) if check.report else 0
    unconfirmed_count = len(check.report.unconfirmed_orders) if check.report else 0
    db.record_reconciliation_event(
        status=check.status, reason=check.reason,
        mismatch_count=mismatch_count, unconfirmed_count=unconfirmed_count,
    )
    # Only alert on the transition INTO a mismatch, not every repeated poll
    # while it's still unresolved - the dashboard banner (always visible
    # while blocking) already covers the "still a problem" case.
    if check.status == "MISMATCH" and previous_status != "MISMATCH":
        alerts.raise_alert(alerts.POSITION_MISMATCH, check.reason or "Position mismatch detected", source="web_server")
    return _reconciliation_payload()


def _reconciliation_payload() -> dict:
    check = reconciliation_gate.last_check
    report = check.report
    return {
        "source": "DB ORDERS vs LIVE GROWW POSITIONS",
        "gate": reconciliation_gate.status_payload(),
        "comparisons": [
            {
                "tradingSymbol": comparison.trading_symbol,
                "expectedQuantity": comparison.expected_quantity,
                "actualQuantity": comparison.actual_quantity,
                "matches": comparison.matches,
            }
            for comparison in report.comparisons
        ] if report else [],
        "unconfirmedOrders": [
            {
                "id": order.get("id"),
                "createdAt": order.get("createdAt").isoformat() if order.get("createdAt") else None,
                "tradingSymbol": order.get("tradingSymbol"),
                "side": order.get("side"),
                "quantity": order.get("quantity"),
                "outcome": order.get("outcome"),
                "growwOrderId": order.get("growwOrderId"),
            }
            for order in report.unconfirmed_orders
        ] if report else [],
    }


@app.get("/api/reconciliation")
def reconciliation() -> dict:
    return _run_reconciliation_check()


@app.post("/api/reconciliation/override")
def reconciliation_override(reason: str, duration_minutes: int = 60) -> dict:
    reconciliation_gate.override(reason, duration_minutes=duration_minutes)
    db.record_reconciliation_event(
        status="OVERRIDDEN", reason=f"Manually overridden for {duration_minutes}m: {reason}",
    )
    return _reconciliation_payload()


@app.post("/api/reconciliation/override/clear")
def reconciliation_override_clear() -> dict:
    reconciliation_gate.clear_override()
    return _reconciliation_payload()


@app.get("/api/reconciliation/history")
def reconciliation_history(limit: int = 20) -> dict:
    return {"events": db.list_reconciliation_events(limit=limit)}


@app.get("/api/alerts")
def list_alerts(unacknowledged_only: bool = False, limit: int = 50) -> dict:
    return {"alerts": db.list_alert_events(unacknowledged_only=unacknowledged_only, limit=limit)}


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int) -> dict:
    acknowledged = db.acknowledge_alert_event(alert_id)
    if not acknowledged:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"acknowledged": True}


@app.post("/api/alerts/acknowledge-all")
def acknowledge_all_alerts() -> dict:
    count = db.acknowledge_all_alert_events()
    return {"acknowledgedCount": count}


@app.get("/api/trade-ledger")
def trade_ledger(source: str | None = None, live: bool | None = None, limit: int = 200) -> dict:
    orders = db.list_orders(live=live, source=source, limit=limit)
    return {
        "orders": [
            {
                "id": order.get("id"),
                "createdAt": order.get("createdAt").isoformat() if order.get("createdAt") else None,
                "source": order.get("source"),
                "live": order.get("live"),
                "indexId": order.get("indexId"),
                "tradingSymbol": order.get("tradingSymbol"),
                "side": order.get("side"),
                "quantity": order.get("quantity"),
                "price": order.get("price"),
                "orderType": order.get("orderType"),
                "outcome": order.get("outcome"),
                "orderStatus": order.get("orderStatus"),
                "reason": order.get("reason"),
                "realizedPnl": order.get("realizedPnl"),
                "filledQuantity": order.get("filledQuantity"),
                "remainingQuantity": order.get("remainingQuantity"),
                "expectedPrice": order.get("expectedPrice"),
                "slippage": order.get("slippage"),
            }
            for order in orders
        ],
    }


@app.get("/api/pnl")
def pnl() -> dict:
    orders = db.list_orders()
    realized = compute_realized_pnl(orders, cost_model)

    paper_positions = []
    paper_unrealized_total = 0.0
    has_open_paper_position = False
    for index_id, manager in order_managers.items():
        account = manager.account
        if account.index_id is None:
            continue
        summary = get_index_summary(account.index_id)
        unrealized = compute_paper_unrealized_pnl(account, summary.price)
        if unrealized is not None:
            has_open_paper_position = True
            paper_unrealized_total += unrealized
        paper_positions.append({"indexId": index_id, "unrealizedPnl": unrealized})

    return {
        "realized": {
            "total": realized.total,
            "live": realized.live_total,
            "paper": realized.paper_total,
            "liveTradeCount": len(realized.live_trades),
            # Net = gross - brokerage/STT/exchange/GST/stamp duty for live
            # trades (paper never incurs real costs, so its net == gross).
            # costModelConfigured is false until real rates are set in
            # .env - net numbers below are then identical to gross, not
            # silently wrong.
            "netTotal": realized.net_total,
            "liveNet": realized.live_net_total,
            "liveCosts": realized.live_costs,
            "costModelConfigured": cost_model.is_configured(),
        },
        "unrealized": {
            "paper": paper_unrealized_total if has_open_paper_position else None,
            "paperPositions": paper_positions,
            "live": None,
            "liveUnavailableReason": (
                "Live option quotes require Groww's paid Live Data API, "
                "which this account's free tier does not have."
            ),
        },
    }


@app.get("/api/reports/daily-summary")
def daily_summary_report(days: int = 30) -> dict:
    orders = db.list_orders(limit=2000)
    signals = db.list_signals(limit=2000)
    summaries = compute_daily_summary(orders, signals)[:days]
    return {
        "days": [
            {
                "date": summary.day.isoformat(),
                "signalsTotal": summary.signals_total,
                "signalsByAction": summary.signals_by_action,
                "ordersPlaced": summary.orders_placed,
                "ordersLive": summary.orders_live,
                "ordersPaper": summary.orders_paper,
                "ordersByOutcome": summary.orders_by_outcome,
                "realizedPnl": summary.realized_pnl,
                "realizedPnlLive": summary.realized_pnl_live,
                "realizedPnlPaper": summary.realized_pnl_paper,
            }
            for summary in summaries
        ],
    }


@app.get("/api/reports/period-summary")
def period_summary_report(period: str = "weekly", periods: int = 12) -> dict:
    if period not in ("weekly", "monthly"):
        raise HTTPException(status_code=400, detail="period must be 'weekly' or 'monthly'")
    orders = db.list_orders(limit=2000)
    signals = db.list_signals(limit=2000)
    daily = compute_daily_summary(orders, signals)
    buckets = aggregate_period_summary(daily, period)[:periods]
    return {
        "period": period,
        "buckets": [
            {
                "label": bucket.period_label,
                "start": bucket.period_start.isoformat(),
                "end": bucket.period_end.isoformat(),
                "signalsTotal": bucket.signals_total,
                "ordersPlaced": bucket.orders_placed,
                "ordersLive": bucket.orders_live,
                "ordersPaper": bucket.orders_paper,
                "realizedPnl": bucket.realized_pnl,
                "realizedPnlLive": bucket.realized_pnl_live,
                "realizedPnlPaper": bucket.realized_pnl_paper,
            }
            for bucket in buckets
        ],
    }


@app.get("/api/reports/strategy-performance")
def strategy_performance_report() -> dict:
    orders = db.list_orders(limit=2000)
    performance = compute_strategy_performance(orders)
    return {
        "strategies": [
            {
                "source": perf.source,
                "tradesClosed": perf.trades_closed,
                "wins": perf.wins,
                "losses": perf.losses,
                "totalPnl": perf.total_pnl,
                "averagePnl": perf.average_pnl,
                "bestTrade": perf.best_trade,
                "worstTrade": perf.worst_trade,
                "winRate": perf.win_rate,
            }
            for perf in performance
        ],
    }


def _broker_status_payload() -> dict:
    status = token_service.status()
    return {
        "broker": status.broker.upper(),
        "apiKeyMasked": status.api_key_masked,
        "apiSecretMasked": status.api_secret_masked,
        # The API key/secret are the persistent credentials. The access
        # token is session state generated from them, so it is never shown
        # as a credential - only the session's status and lifecycle are.
        "authMode": status.auth_mode,
        "autoReauthAvailable": status.auto_reauth_available,
        "sessionStatus": status.token_status,
        "connectionStatus": status.connection_status,
        "sessionCreatedAt": status.token_created_at,
        "sessionExpiresAt": status.token_expiry_at,
        "sessionExpiryIsEstimated": status.token_expiry_is_estimated,
        "lastValidatedAt": status.last_validated_at,
        "lastSuccessfulRequestAt": status.last_successful_request_at,
        "lastError": status.last_error,
        "credentialsPersisted": status.credentials_persisted,
        # Endpoint-level availability (e.g. market data denied by a 403).
        # Informational only - connectionStatus alone says whether the
        # broker session is connected.
        "capabilities": status.capabilities,
        # Manual/live trading requires an active Groww connection. Auto
        # Trading is deliberately paper-only (see auto_trader.py/README
        # notes) and never calls Groww to place an order, so a broken
        # connection doesn't need to block it - it's blocked/unblocked by
        # its own enable/disable + kill switch instead.
        "manualTradingBlocked": not token_service.is_connected(),
    }


def _broker_update_payload() -> dict:
    """The current status plus the result of the update operation itself,
    kept separate: "update.persisted" says whether the new credentials were
    stored, while connectionStatus is the only thing that says whether the
    broker is connected right now."""
    return {**_broker_status_payload(), "update": {"persisted": bool(token_service.last_update_persisted)}}


@app.get("/api/broker/status")
def broker_status() -> dict:
    return _broker_status_payload()


class CredentialsUpdateRequest(BaseModel):
    apiKey: str
    apiSecret: str


class AccessTokenUpdateRequest(BaseModel):
    accessToken: str


@app.post("/api/broker/credentials")
def broker_update_credentials(request: CredentialsUpdateRequest) -> dict:
    """Configures the API key/secret and immediately mints + validates a
    fresh access token via Groww's own approval-checksum flow. Never echoes
    the submitted values back - the response is just the resulting masked
    status."""
    try:
        token_service.update_credentials(request.apiKey, request.apiSecret)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except BrokerValidationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _broker_update_payload()


@app.post("/api/broker/access-token")
def broker_update_access_token(request: AccessTokenUpdateRequest) -> dict:
    """The manual "Update Access Token" workflow: validates against a real,
    safe/read-only Groww endpoint before storing anything. Never invents or
    generates a token - only stores exactly what was submitted, once it's
    been proven to work."""
    try:
        token_service.update_access_token(request.accessToken)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except BrokerValidationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _broker_update_payload()


@app.post("/api/broker/reauthenticate")
def broker_reauthenticate() -> dict:
    """Generates a new session from the stored API key/secret right away
    (e.g. after approving API access in the Groww app). Never places an
    order."""
    try:
        token_service.reauthenticate()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except BrokerValidationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return _broker_status_payload()


@app.post("/api/broker/test-connection")
def broker_test_connection() -> dict:
    """Re-validates the current connection via a safe, read-only Groww
    call. Never places an order."""
    result = token_service.test_connection()
    return {"connected": result.connected, "message": result.message}


@app.get("/api/broker/history")
def broker_history(limit: int = 20) -> dict:
    return {"events": db.list_token_audit_events(broker="groww", limit=limit)}


# Backtesting reuses fno_signals' own bar-by-bar strategy state machine
# (fno_signals.strategy.run()) rather than re-implementing signal logic a
# second time - the exact same code path that generates real live signals
# also generates backtest trades, so a strategy config change is tested
# identically in both places. These periods push toward yfinance/Yahoo's
# real per-interval history limits (1m: ~7d, 5m/15m/1h: up to ~60d/2y,
# 1d: effectively unlimited) - longer than Market Pulse's own
# TIMEFRAMES map, which is sized for a live chart, not a backtest.
BACKTEST_TIMEFRAMES: dict[str, tuple[str, str]] = {
    "1m": ("7d", "1m"),
    "5m": ("60d", "5m"),
    "15m": ("60d", "15m"),
    "1h": ("730d", "1h"),
    "1d": ("5y", "1d"),
}


def _backtest_metrics_payload(metrics) -> dict:
    return {
        "totalTrades": metrics.total_trades,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "winRate": metrics.win_rate,
        "profitFactor": metrics.profit_factor,
        "netPoints": metrics.net_points,
        "averageTradePoints": metrics.average_trade_points,
        "expectancyPoints": metrics.expectancy_points,
        "largestWinPoints": metrics.largest_win_points,
        "largestLossPoints": metrics.largest_loss_points,
        "maxConsecutiveLosses": metrics.max_consecutive_losses,
        "maxDrawdownPoints": metrics.max_drawdown_points,
        "callPerformance": vars(metrics.call_performance),
        "putPerformance": vars(metrics.put_performance),
        "timeOfDayPerformance": [vars(bucket) for bucket in metrics.time_of_day_performance],
        "marketRegimePerformance": [vars(bucket) for bucket in metrics.market_regime_performance],
    }


def _backtest_trades_payload(trades: list) -> list[dict]:
    return [
        {
            "entryTime": trade.entry_time.isoformat() if hasattr(trade.entry_time, "isoformat") else str(trade.entry_time),
            "exitTime": trade.exit_time.isoformat() if hasattr(trade.exit_time, "isoformat") else str(trade.exit_time),
            "direction": trade.direction,
            "entryPrice": trade.entry_price,
            "exitPrice": trade.exit_price,
            "exitReason": trade.exit_reason,
            "points": trade.points,
            "strike": trade.strike,
            "optionSymbol": trade.option_symbol,
        }
        for trade in sorted(trades, key=lambda t: t.entry_time)
    ]


def _run_backtest_segment(data, strategy_config, index_config, assumed_slippage_points: float) -> dict:
    """Runs the strategy against one contiguous slice of underlying data.
    The single place both /api/backtest/run and the Excel/PDF export
    endpoints build from, so an export can never drift from what the
    dashboard itself shows for the same parameters."""
    _results, events = run_strategy(data, strategy_config, underlying_label=index_config.name)
    trades = pair_trades(events, assumed_slippage_points=assumed_slippage_points)
    regime_labels = compute_regime_labels(data)
    metrics = compute_backtest_metrics(trades, regime_labels)
    return {
        "candleCount": len(data),
        "metrics": _backtest_metrics_payload(metrics),
        "trades": _backtest_trades_payload(trades),
    }


_BACKTEST_DISCLAIMER = (
    "Points-based backtest on the UNDERLYING's own price - NOT a rupee option-premium P&L "
    "(this account's Groww tier has no historical option-quote data). A real diagnostic of "
    "entry/exit timing quality, not validated real-money profitability. Past performance never "
    "guarantees future results."
)


def _compute_backtest_payload(
    index_id: str, interval: str, period: str | None, assumed_slippage_points: float, split: bool,
) -> dict:
    if interval not in BACKTEST_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported interval: {interval}")
    index_config = _index_config_for(index_id)
    default_period, yf_interval = BACKTEST_TIMEFRAMES[interval]
    data = fetch_underlying_data(index_config.ticker, period=period or default_period, interval=yf_interval)
    strategy_config = strategy_config_for(index_config)

    base = {
        "indexId": index_id, "indexName": index_config.name, "interval": interval,
        "period": period or default_period, "candleCount": len(data),
        "assumedSlippagePoints": assumed_slippage_points, "disclaimer": _BACKTEST_DISCLAIMER,
        "split": split,
    }

    if not split:
        segment = _run_backtest_segment(data, strategy_config, index_config, assumed_slippage_points)
        return {**base, "metrics": segment["metrics"], "trades": segment["trades"]}

    # Anti-overfitting (spec section 34): a strategy that only performs
    # well on one slice of history isn't production-ready - report train/
    # validation/out-of-sample side by side rather than a single number
    # that could be an artifact of the specific window chosen.
    splits = split_train_validation_test(data)
    split_results = {}
    for split_name, split_data in splits.items():
        if len(split_data) < 10:
            split_results[split_name] = {"candleCount": len(split_data), "metrics": None, "trades": []}
            continue
        split_results[split_name] = _run_backtest_segment(
            split_data, strategy_config, index_config, assumed_slippage_points,
        )
    return {**base, "splits": split_results}


@app.get("/api/backtest/run")
def backtest_run(
    index_id: str, interval: str = "5m", period: str | None = None,
    assumed_slippage_points: float = 0.0, split: bool = False,
) -> dict:
    """Runs the real fno_signals strategy against historical underlying
    data and reports points-based (NOT rupee option-premium) metrics - see
    algoedge/backtest.py's module docstring for why. Never places an
    order or touches Groww; purely a read of yfinance history."""
    return _compute_backtest_payload(index_id, interval, period, assumed_slippage_points, split)


@app.get("/api/backtest/export/xlsx")
def backtest_export_xlsx(
    index_id: str, interval: str = "5m", period: str | None = None,
    assumed_slippage_points: float = 0.0, split: bool = False,
) -> Response:
    """Formats the exact same backtest computation as /api/backtest/run
    into a workbook - never a separate/re-derived calculation."""
    payload = _compute_backtest_payload(index_id, interval, period, assumed_slippage_points, split)
    content = build_excel_report(payload)
    filename = f"algoedge-backtest-{index_id}-{interval}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/backtest/export/pdf")
def backtest_export_pdf(
    index_id: str, interval: str = "5m", period: str | None = None,
    assumed_slippage_points: float = 0.0, split: bool = False,
) -> Response:
    """Formats the exact same backtest computation as /api/backtest/run
    into a PDF report - never a separate/re-derived calculation."""
    payload = _compute_backtest_payload(index_id, interval, period, assumed_slippage_points, split)
    content = build_pdf_report(payload)
    filename = f"algoedge-backtest-{index_id}-{interval}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/system/health")
def system_health() -> dict:
    """A single, lightweight operational overview - deliberately a pure
    read/aggregation of state other endpoints already expose (broker
    connection, DB availability, reconciliation gate, risk engine, recent
    signal/order/alert activity), never a new source of truth. Never
    places an order or mutates anything."""
    broker = token_service.status()
    reconciliation_status = reconciliation_gate.last_check.status
    risk_status = (
        "KILL_SWITCH" if risk_manager.state.kill_switch
        else "HALTED" if risk_manager.state.consecutive_loss_halt
        else "ACTIVE"
    )
    last_signal = next(iter(db.list_signals(limit=1)), None)
    last_order = next(iter(db.list_orders(limit=1)), None)
    last_alert = next(iter(db.list_alert_events(limit=1)), None)
    last_db_write = max(
        (t for t in (
            last_signal.get("createdAt") if last_signal else None,
            last_order.get("createdAt") if last_order else None,
        ) if t is not None),
        default=None,
    )
    db_check = db.check_connection()
    return {
        "database": {
            "status": "CONNECTED" if db_check["connected"] else "DISCONNECTED",
            "databaseName": db_check["databaseName"],
            "error": db_check["error"],
            "lastSuccessfulCheckAt": db_check["lastSuccessfulCheckAt"],
        },
        "broker": {
            "status": broker.connection_status,
            "capabilities": broker.capabilities,
        },
        "reconciliation": {"status": reconciliation_status},
        "riskEngine": {"status": risk_status},
        "scheduler": {"tickSeconds": SCHEDULER_TICK_SECONDS},
        "lastSignal": last_signal,
        "lastBrokerSync": broker.last_successful_request_at,
        "lastDatabaseWrite": last_db_write,
        "lastReconciliation": reconciliation_gate.last_check.checked_at,
        "lastError": last_alert.get("message") if last_alert and last_alert.get("severity") in ("WARNING", "CRITICAL") else None,
    }


web_root = Path(__file__).resolve().parents[2] / "web"
app.mount("/", StaticFiles(directory=str(web_root), html=True), name="static")


def main() -> None:
    import uvicorn

    print("AlgoEdge live dashboard: http://127.0.0.1:5173")
    uvicorn.run(app, host="127.0.0.1", port=5173, log_level="warning")


if __name__ == "__main__":
    main()
