from datetime import date

import pandas as pd
import pytest
from growwapi.groww.exceptions import GrowwAPIException

from algoedge.reconciliation_gate import ReconciliationGate
from algoedge.risk_manager import RiskLimits, RiskManager
from algoedge.signal_pipeline import SignalService
from fno_signals import main as main_module
from fno_signals.broker import ContractNotFoundError, GrowwSessionError, ResolvedContract
from fno_signals.config import INDEX_MAP
from fno_signals.strategy import TradeEvent
from fno_signals.verification import OrderVerificationResult


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    # .env may configure a real ALGOEDGE_DB_SERVER for this machine - tests
    # must never depend on (or slow down for) an actual SQL Server
    # connection, and db.record_* are already no-ops when unconfigured.
    monkeypatch.setattr(main_module.db, "init_db", lambda *a, **kw: False)
    monkeypatch.setattr(main_module.db, "_session_factory", None)


@pytest.fixture(autouse=True)
def fresh_signal_service(monkeypatch):
    # main_module.signal_service is a module-level singleton so repeated
    # scans can detect real duplicates across calls - but make_event() below
    # always uses the same fixed timestamp, so every test must start from a
    # clean instance or later tests would see earlier tests' signals as
    # duplicates.
    monkeypatch.setattr(main_module, "signal_service", SignalService())


@pytest.fixture(autouse=True)
def fresh_risk_manager(monkeypatch):
    # In production, main() enables this before any live order can be
    # placed (the --live flag + confirmation IS the enablement gate for
    # this CLI - see RISK_SCOPE's docstring). Tests that call
    # place_live_order() directly bypass main(), so they need the same
    # enabled, freshly-reset state main() would have already set up.
    manager = RiskManager(RiskLimits(max_quantity=500))  # matches production's real-lot-size headroom
    manager.enable_auto_trading()
    monkeypatch.setattr(main_module, "risk_manager", manager)


@pytest.fixture(autouse=True)
def fresh_reconciliation_gate(monkeypatch):
    # In production, main() runs a real reconciliation check before any
    # live order is permitted (fails closed until then - see
    # reconciliation_gate.py). Tests that call place_live_order() directly
    # bypass main(), so default to a gate that's already evaluated clean,
    # matching the real account's actual (flat, zero-position) state.
    gate = ReconciliationGate()
    gate.evaluate([], [])
    monkeypatch.setattr(main_module, "reconciliation_gate", gate)


def make_event(
    kind: str, price: float = 24500.0, sl: float = 24450.0, tp: float = 24650.0,
    symbol: str = "NIFTY 50 24500 CE", strike: int = 24500, right: str = "CE",
) -> TradeEvent:
    is_entry = kind.startswith("ENTRY")
    return TradeEvent(
        timestamp=pd.Timestamp("2026-09-23 10:15", tz="Asia/Kolkata"),
        kind=kind,
        underlying_price=price,
        option_symbol=symbol if is_entry else None,
        stop_loss=sl if is_entry else None,
        target=tp if is_entry else None,
        exit_level=None if is_entry else 24450.0,
        strike=strike if is_entry else None,
        right=right if is_entry else None,
    )


def test_format_event_entry_call_includes_required_fields() -> None:
    event = make_event("ENTRY_CALL")

    output = main_module.format_event(event, INDEX_MAP[1])

    assert "BUY_CALL" in output
    assert "NIFTY 50 24500 CE" in output
    assert "Lot Size" in output and "75" in output
    assert "24500.00" in output  # spot price
    assert "50.00 pts" in output  # sl distance
    assert "150.00 pts" in output  # tp distance


def test_format_event_entry_put_uses_buy_put_label() -> None:
    event = make_event("ENTRY_PUT", symbol="BANK NIFTY 48200 PE")

    output = main_module.format_event(event, INDEX_MAP[2])

    assert "BUY_PUT" in output
    assert "BANK NIFTY 48200 PE" in output


def test_format_event_exit_shows_reason_and_level() -> None:
    event = make_event("EXIT_SL")

    output = main_module.format_event(event, INDEX_MAP[1])

    assert "EXIT (SL)" in output
    assert "24450.00" in output


def test_parse_args_index_choice() -> None:
    args = main_module.parse_args(["--index", "2"])

    assert args.index == 2
    assert args.period == "5d"
    assert args.interval == "5m"


def test_parse_args_rejects_out_of_range_index() -> None:
    with pytest.raises(SystemExit):
        main_module.parse_args(["--index", "9"])


def test_parse_args_defaults_to_none_index_for_interactive_mode() -> None:
    args = main_module.parse_args([])

    assert args.index is None


def test_run_scan_prints_no_signals_message_when_empty(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "fetch_underlying_data", lambda *_a, **_kw: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr(main_module, "run", lambda *_a, **_kw: (None, []))

    main_module.run_scan(1)

    assert "No signals for NIFTY 50" in capsys.readouterr().out


def test_run_scan_prints_formatted_events(monkeypatch, capsys) -> None:
    event = make_event("ENTRY_CALL")
    monkeypatch.setattr(main_module, "fetch_underlying_data", lambda *_a, **_kw: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr(main_module, "run", lambda *_a, **_kw: (None, [event]))

    main_module.run_scan(1)

    assert "BUY_CALL" in capsys.readouterr().out


def test_main_with_index_flag_runs_scan_once(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: calls.append(index))

    main_module.main(["--index", "3"])

    assert calls == [3]


def test_interactive_loop_exits_immediately_on_choice_4(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "4")
    scan_calls = []
    monkeypatch.setattr(main_module, "run_scan", lambda *a, **kw: scan_calls.append(a))

    main_module.interactive_loop(period="5d", interval="5m")

    assert scan_calls == []
    assert "Goodbye" in capsys.readouterr().out


def test_interactive_loop_reprompts_on_invalid_choice_then_exits(monkeypatch) -> None:
    responses = iter(["9", "4"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    scan_calls = []
    monkeypatch.setattr(main_module, "run_scan", lambda *a, **kw: scan_calls.append(a))

    main_module.interactive_loop(period="5d", interval="5m")

    assert scan_calls == []


def test_interactive_loop_runs_a_scan_for_a_valid_choice_then_exits(monkeypatch) -> None:
    responses = iter(["1", "4"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    scan_calls = []
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: scan_calls.append(index))

    main_module.interactive_loop(period="5d", interval="5m")

    assert scan_calls == [1]


def test_parse_args_live_and_yes_flags_default_false() -> None:
    args = main_module.parse_args(["--index", "1"])

    assert args.live is False
    assert args.yes is False


def test_parse_args_live_and_yes_flags_can_be_set() -> None:
    args = main_module.parse_args(["--index", "1", "--live", "--yes"])

    assert args.live is True
    assert args.yes is True


def test_run_scan_without_client_prints_dry_run_note_and_places_no_order(monkeypatch, capsys) -> None:
    event = make_event("ENTRY_CALL")
    monkeypatch.setattr(main_module, "fetch_underlying_data", lambda *_a, **_kw: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr(main_module, "run", lambda *_a, **_kw: (None, [event]))
    order_calls = []
    monkeypatch.setattr(main_module, "place_live_order", lambda *a, **kw: order_calls.append(a))

    main_module.run_scan(1, client=None)

    assert order_calls == []
    assert "dry-run" in capsys.readouterr().out


def test_run_scan_with_client_delegates_entries_to_place_live_order(monkeypatch) -> None:
    event = make_event("ENTRY_CALL")
    monkeypatch.setattr(main_module, "fetch_underlying_data", lambda *_a, **_kw: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr(main_module, "run", lambda *_a, **_kw: (None, [event]))
    order_calls = []
    monkeypatch.setattr(main_module, "place_live_order", lambda client, index_config, evt, confirm: order_calls.append((client, evt, confirm)))

    sentinel_client = object()
    main_module.run_scan(1, client=sentinel_client, confirm=False)

    assert len(order_calls) == 1
    assert order_calls[0][0] is sentinel_client
    assert order_calls[0][2] is False


def test_run_scan_does_not_call_place_live_order_for_exit_events(monkeypatch) -> None:
    event = make_event("EXIT_SL")
    monkeypatch.setattr(main_module, "fetch_underlying_data", lambda *_a, **_kw: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr(main_module, "run", lambda *_a, **_kw: (None, [event]))
    order_calls = []
    monkeypatch.setattr(main_module, "place_live_order", lambda *a, **kw: order_calls.append(a))

    main_module.run_scan(1, client=object())

    assert order_calls == []


def make_contract() -> ResolvedContract:
    return ResolvedContract(
        trading_symbol="NIFTY26SEP24500CE", exchange="NSE", expiry_date=date(2026, 9, 30),
        strike=24500, right="CE", lot_size=75,
    )


def test_place_live_order_skips_when_contract_not_found(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: (_ for _ in ()).throw(ContractNotFoundError("no match")))
    order_calls = []
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: order_calls.append(a))

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=True)

    assert order_calls == []
    assert "Could not resolve" in capsys.readouterr().out


def test_place_live_order_skips_when_not_confirmed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    order_calls = []
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: order_calls.append(a))

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=True)

    assert order_calls == []
    assert "not confirmed" in capsys.readouterr().out


def test_place_live_order_places_when_confirmed_and_verifies_execution(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    order_calls = []
    monkeypatch.setattr(
        main_module, "execute_market_order",
        lambda client, contract, quantity: order_calls.append((contract, quantity)) or {"groww_order_id": "gid-1"},
    )
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult("SUCCESS", "EXECUTED", "gid-1", attempts=1),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=True)

    assert len(order_calls) == 1
    contract, quantity = order_calls[0]
    assert contract.trading_symbol == "NIFTY26SEP24500CE"
    assert quantity == 75  # live-verified lot size, not the hardcoded config value
    assert "ORDER EXECUTED" in capsys.readouterr().out


def test_place_live_order_skips_prompt_when_confirm_false(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(AssertionError("should not prompt")))
    order_calls = []
    monkeypatch.setattr(
        main_module, "execute_market_order",
        lambda client, contract, quantity: order_calls.append(quantity) or {"groww_order_id": "gid-1"},
    )
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult("SUCCESS", "EXECUTED", "gid-1", attempts=1),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert order_calls == [75]


def test_place_live_order_reports_placement_failure_without_crashing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(
        main_module, "execute_market_order",
        lambda *a, **kw: (_ for _ in ()).throw(GrowwAPIException(code="500", msg="rejected")),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert "Order placement failed" in capsys.readouterr().out


def test_place_live_order_handles_missing_groww_order_id(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"status": "ok"})  # no groww_order_id
    verify_calls = []
    monkeypatch.setattr(main_module, "verify_order_status", lambda *a, **kw: verify_calls.append(a))

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert verify_calls == []
    assert "cannot verify fill status" in capsys.readouterr().out


def test_place_live_order_halts_on_verification_failed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult(
            "FAILED", "REJECTED", "gid-1", reason="Insufficient margin", attempts=1,
        ),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    out = capsys.readouterr().out
    assert "ORDER FAILED" in out
    assert "Insufficient margin" in out
    assert "No position was opened" in out


def test_place_live_order_halts_on_verification_timeout(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult("TIMEOUT", "NEW", "gid-1", reason="gave up", attempts=5),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert "TIMEOUT" in capsys.readouterr().out


def test_place_live_order_logs_cancellation(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult(
            "CANCELLED", "CANCELLED", "gid-1", reason="User cancelled", attempts=2,
        ),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert "cancelled" in capsys.readouterr().out.lower()


def test_main_live_mode_exits_1_on_auth_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        main_module, "generate_daily_session",
        lambda: (_ for _ in ()).throw(GrowwSessionError("bad creds")),
    )
    monkeypatch.setattr(main_module, "run_scan", lambda *a, **kw: pytest.fail("should not scan"))

    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["--index", "1", "--live"])

    assert excinfo.value.code == 1
    assert "authentication failed" in capsys.readouterr().out


class FakeSessionClient:
    """A minimal stand-in for the authenticated GrowwAPI client used by
    main()'s startup reconciliation check - real production code calls
    client.get_positions_for_user(segment="FNO")."""

    def get_positions_for_user(self, segment: str) -> dict:
        return {"positions": []}


def test_main_live_mode_proceeds_with_authenticated_client(monkeypatch) -> None:
    sentinel_client = FakeSessionClient()
    monkeypatch.setattr(main_module, "generate_daily_session", lambda: sentinel_client)
    scan_calls = []
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: scan_calls.append(kw))

    main_module.main(["--index", "1", "--live", "--yes"])

    assert scan_calls[0]["client"] is sentinel_client
    assert scan_calls[0]["confirm"] is False


def test_main_without_live_never_authenticates(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module, "generate_daily_session",
        lambda: pytest.fail("should not authenticate without --live"),
    )
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: None)

    main_module.main(["--index", "1"])


def test_main_live_mode_enables_and_restores_the_risk_manager(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "generate_daily_session", lambda: FakeSessionClient())
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: None)
    monkeypatch.setattr(
        main_module.db, "load_latest_risk_state",
        lambda *, scope: {
            "kill_switch": False, "kill_switch_reason": None, "trades_today": 3,
            "realized_pnl_today": -200.0, "trade_day": "2026-09-24",
            "consecutive_losses": 1, "consecutive_loss_halt": False, "last_exit_at": None,
        } if scope == main_module.RISK_SCOPE else None,
    )

    main_module.main(["--index", "1", "--live", "--yes"])

    assert main_module.risk_manager.state.auto_trading_enabled is True
    assert main_module.risk_manager.state.trades_today == 3
    assert main_module.risk_manager.state.realized_pnl_today == -200.0


def test_main_live_mode_warns_when_kill_switch_is_engaged(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "generate_daily_session", lambda: FakeSessionClient())
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: None)
    monkeypatch.setattr(
        main_module.db, "load_latest_risk_state",
        lambda *, scope: {
            "kill_switch": True, "kill_switch_reason": "manual halt", "trades_today": 0,
            "realized_pnl_today": 0.0, "trade_day": "2026-09-24",
            "consecutive_losses": 0, "consecutive_loss_halt": False, "last_exit_at": None,
        } if scope == main_module.RISK_SCOPE else None,
    )

    main_module.main(["--index", "1", "--live", "--yes"])

    assert "ACTION REQUIRED" in capsys.readouterr().out


class FakeMismatchedSessionClient:
    """A live position Groww actually reports that this CLI's own DB order
    history knows nothing about - a genuine reconciliation mismatch."""

    def get_positions_for_user(self, segment: str) -> dict:
        return {"positions": [{"trading_symbol": "NIFTY26SEP24500CE", "quantity": 75}]}


def test_main_live_mode_reconciliation_mismatch_warns_and_blocks_orders(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module, "generate_daily_session", lambda: FakeMismatchedSessionClient())
    monkeypatch.setattr(main_module, "run_scan", lambda index, **kw: None)
    monkeypatch.setattr(main_module.db, "list_orders", lambda **kw: [])  # DB knows of no such position

    main_module.main(["--index", "1", "--live", "--yes"])

    assert "ACTION REQUIRED" in capsys.readouterr().out
    assert main_module.reconciliation_gate.is_blocking() is True


def test_place_live_order_blocked_when_reconciliation_gate_is_blocking(monkeypatch, capsys) -> None:
    main_module.reconciliation_gate.mark_unavailable("test: forced unavailable")
    resolve_calls = []
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: resolve_calls.append(a) or make_contract())

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert resolve_calls == []
    assert "reconciliation gate" in capsys.readouterr().out.lower()


def test_place_live_order_blocked_by_risk_check_before_touching_the_broker(monkeypatch, capsys) -> None:
    main_module.risk_manager.state.kill_switch = True
    main_module.risk_manager.state.kill_switch_reason = "test halt"
    resolve_calls = []
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: resolve_calls.append(a) or make_contract())

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert resolve_calls == []  # never even attempted to resolve a contract
    out = capsys.readouterr().out
    assert "Risk check failed" in out
    assert "test halt" in out


def test_place_live_order_blocked_when_max_open_positions_reached(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        main_module, "compute_expected_positions", lambda _orders: {"NIFTY26SEP24500CE": 75},
    )
    resolve_calls = []
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: resolve_calls.append(a) or make_contract())

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert resolve_calls == []
    assert "Max open positions" in capsys.readouterr().out


def test_place_live_order_records_a_trade_and_snapshot_on_success(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult("SUCCESS", "EXECUTED", "gid-1", attempts=1),
    )
    snapshots = []
    monkeypatch.setattr(
        main_module.db, "record_risk_snapshot",
        lambda manager, event, *, scope: snapshots.append((event, scope)),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert main_module.risk_manager.state.trades_today == 1
    assert snapshots == [("TRADE_RECORDED", main_module.RISK_SCOPE)]


def test_place_live_order_uses_underlying_based_risk_model(monkeypatch) -> None:
    # event.stop_loss/target are ATR levels on the underlying's own close
    # price, never an option premium - the signal must be tagged
    # UNDERLYING_BASED, not the dataclass's OPTION_PREMIUM_BASED default.
    captured = {}
    real_ingest = main_module.signal_service.ingest

    def spying_ingest(signal, **kw):
        captured["signal"] = signal
        return real_ingest(signal, **kw)

    monkeypatch.setattr(main_module.signal_service, "ingest", spying_ingest)
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult("SUCCESS", "EXECUTED", "gid-1", attempts=1),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert captured["signal"].risk_model == "UNDERLYING_BASED"


def test_place_live_order_blocked_by_a_failed_liquidity_check(monkeypatch, capsys) -> None:
    from algoedge.liquidity_check import LiquidityCheckResult

    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(
        main_module, "check_liquidity", lambda *a, **kw: LiquidityCheckResult(False, "Spread too wide", spread=12.0),
    )
    order_calls = []
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: order_calls.append(a))

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    assert order_calls == []
    assert "Liquidity check failed" in capsys.readouterr().out


def test_place_live_order_partial_fill_does_not_advance_to_position_open(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "resolve_contract", lambda *a, **kw: make_contract())
    monkeypatch.setattr(main_module, "execute_market_order", lambda *a, **kw: {"groww_order_id": "gid-1"})
    monkeypatch.setattr(
        main_module, "verify_order_status",
        lambda *a, **kw: OrderVerificationResult(
            "PARTIAL", "EXECUTED", "gid-1", reason="Only 25 of 75 requested filled",
            attempts=1, requested_quantity=75, filled_quantity=25, remaining_quantity=50,
        ),
    )

    main_module.place_live_order(object(), INDEX_MAP[1], make_event("ENTRY_CALL"), confirm=False)

    signal_id = main_module.build_signal_id(
        "fno_signals", "NIFTY", "CALL", make_event("ENTRY_CALL").timestamp.to_pydatetime(),
    )
    record = main_module.db.get_signal_by_id(signal_id) or main_module.signal_service._memory_signals.get(signal_id)
    assert record["state"] == "PARTIALLY_FILLED"
