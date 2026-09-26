"""Phase 5 - Auto Trade's integration with OptionContractResolver
(algoedge.option_contract): resolving before opening, refusing an
unresolved/ambiguous contract, and restoring an already-resolved position
across a restart without re-resolving. See tests/test_option_contract.py
for the resolver's own pure-function tests.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algoedge import auto_trader
from algoedge.auto_trader import restore_account_state
from algoedge.option_contract import OptionContract
from algoedge.order_manager import OrderManager, SimulatedAccount
from algoedge.risk_manager import IST, RiskManager

TRADING_HOURS_NOW = datetime(2026, 9, 23, 10, 40, tzinfo=IST)  # the 10:35 entry bar has just closed (B8)


def trending_df(n: int, start_price: float, step: float, start: str = "2026-09-23 09:15") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [start_price + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + abs(step) / 2 + 1 for c in closes],
            "Low": [c - abs(step) / 2 - 1 for c in closes],
            "Close": closes,
            "Volume": [0.0] * n,
        },
        index=index,
    )


# ENTRY_CALL fires at 10:35, strike 150, right CE (verified against the
# real canonical strategy - see the derivation in earlier Phase 3/4 tests).
UPTREND_60 = trending_df(60, start_price=100.0, step=2.0)


def patch_fetch(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(auto_trader, "fetch_underlying_data", lambda *_a, **_kw: df)


def good_resolver(event) -> OptionContract:
    return OptionContract(
        trading_symbol=f"NIFTY26SEP{event.strike}{event.right}",
        underlying="NIFTY", right=event.right, strike=event.strike, expiry=date(2026, 9, 30),
    )


def none_resolver(event) -> None:
    return None


# ---------- unresolved contract blocks the paper order ----------


def test_no_resolver_provided_blocks_the_entry(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
    )  # resolve_contract_fn not passed - defaults to None

    assert result.order is None
    assert "resolution unavailable" in result.risk.reason.lower()
    assert order_manager.account.quantity == 0


def test_resolver_returning_none_blocks_the_entry(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=none_resolver,
    )

    assert result.order is None
    assert "no matching option contract" in result.risk.reason.lower()
    assert order_manager.account.quantity == 0
    assert order_manager.account.contract is None


def test_unresolved_contract_never_reaches_order_manager(monkeypatch) -> None:
    # A stronger version of the above: proves OrderManager.place_event()
    # itself is never even called when resolution fails, not merely that
    # its result happens to look like a no-op.
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()
    calls = []
    original_place_event = order_manager.place_event
    order_manager.place_event = lambda *a, **kw: calls.append((a, kw)) or original_place_event(*a, **kw)

    auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=none_resolver,
    )

    assert calls == []


def test_a_resolver_exception_does_not_crash_run_cycle(monkeypatch) -> None:
    # run_cycle() only accepts a resolver that either returns an
    # OptionContract or None - callers (web_server.py) are responsible for
    # catching OptionContractResolutionError themselves and translating it
    # to None (see _resolve_auto_trade_contract there). Confirms that
    # contract even if a badly-behaved resolver raises, it doesn't corrupt
    # account state (the exception simply propagates, nothing gets
    # half-applied).
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    def broken_resolver(_event):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        auto_trader.run_cycle(
            "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
            resolve_contract_fn=broken_resolver,
        )
    assert order_manager.account.quantity == 0


# ---------- successful resolution opens the position with the contract attached ----------


def test_resolved_contract_is_stored_on_the_account(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=good_resolver,
    )

    assert result.order.status == "PLACED"
    assert order_manager.account.contract == OptionContract(
        trading_symbol="NIFTY26SEP150CE", underlying="NIFTY", right="CE", strike=150, expiry=date(2026, 9, 30),
    )


def test_exit_does_not_require_or_call_the_resolver(monkeypatch) -> None:
    # An exit only ever closes whatever contract the position was already
    # opened against - the resolver must never be invoked for it.
    calls = []

    def spy_resolver(event):
        calls.append(event)
        return good_resolver(event)

    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=spy_resolver,
    )
    assert len(calls) == 1  # the entry

    patch_fetch(monkeypatch, UPTREND_60)  # window now also contains the exit
    exit_result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5), resolve_contract_fn=spy_resolver,
    )

    assert exit_result.event.kind == "EXIT_TARGET"
    assert len(calls) == 1  # unchanged - the resolver was not called again for the exit


def test_the_canonical_strategys_own_strike_and_right_are_passed_through_unchanged(monkeypatch) -> None:
    # Confirms the resolver is handed EXACTLY the ATM strike/right the
    # canonical strategy already computed (event.strike/event.right),
    # never a value recomputed or adjusted by Auto Trade's own code.
    seen = {}

    def capturing_resolver(event):
        seen["strike"] = event.strike
        seen["right"] = event.right
        return good_resolver(event)

    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=capturing_resolver,
    )

    assert seen == {"strike": 150, "right": "CE"}


# ---------- deterministic repeated resolution ----------


def test_deterministic_repeated_resolution_across_separate_accounts(monkeypatch) -> None:
    # Same event, same resolver, two independent accounts - must resolve
    # to an identical contract both times (no hidden randomness/state).
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager_a = RiskManager()
    risk_manager_a.enable_auto_trading()
    order_manager_a = OrderManager()
    risk_manager_b = RiskManager()
    risk_manager_b.enable_auto_trading()
    order_manager_b = OrderManager()

    auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager_a, order_manager_a, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=good_resolver,
    )
    auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager_b, order_manager_b, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=good_resolver,
    )

    assert order_manager_a.account.contract == order_manager_b.account.contract


# ---------- restart with an already-resolved position ----------


def test_restart_restores_the_resolved_contract_without_calling_the_resolver() -> None:
    snapshot = {
        "cash": 999_850.0, "quantity": 1, "average_price": 150.0, "side": "CALL",
        "last_event_at": pd.Timestamp("2026-09-23 10:35", tz="Asia/Kolkata"), "square_off_date": None,
        "contract": {
            "trading_symbol": "NIFTY26SEP150CE", "underlying": "NIFTY", "right": "CE",
            "strike": 150, "expiry": date(2026, 9, 30), "instrument_id": "NIFTY-GS-1",
        },
    }
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.contract == OptionContract(
        trading_symbol="NIFTY26SEP150CE", underlying="NIFTY", right="CE", strike=150,
        expiry=date(2026, 9, 30), instrument_id="NIFTY-GS-1",
    )


def test_restart_with_a_resolved_position_closes_cleanly_without_a_resolver(monkeypatch) -> None:
    # A restored, already-resolved position's own exit must not require
    # resolve_contract_fn at all (exits never call the resolver - see
    # test_exit_does_not_require_or_call_the_resolver above).
    snapshot = {
        "cash": 999_850.0, "quantity": 1, "average_price": 100.0, "side": "CALL",
        # The entry itself (10:35) is already processed - only the later
        # exit in the window should be picked up.
        "last_event_at": pd.Timestamp("2026-09-23 10:35", tz="Asia/Kolkata"), "square_off_date": None,
        "contract": {
            "trading_symbol": "NIFTY26SEP150CE", "underlying": "NIFTY", "right": "CE",
            "strike": 150, "expiry": date(2026, 9, 30), "instrument_id": None,
        },
    }
    account = SimulatedAccount()
    restore_account_state(account, snapshot)
    order_manager = OrderManager(account)
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, UPTREND_60)  # contains the target exit

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
    )  # no resolve_contract_fn passed at all

    assert result.event.kind == "EXIT_TARGET"
    assert result.order.status == "PLACED"
    assert account.quantity == 0
    assert account.contract is None  # cleared on close, exactly like side/average_price


def test_corrupted_contract_snapshot_fails_safe_and_leaves_account_flat() -> None:
    snapshot = {
        "cash": 999_850.0, "quantity": 1, "average_price": 150.0, "side": "CALL",
        "last_event_at": None, "square_off_date": None,
        "contract": {"trading_symbol": "X"},  # missing required keys
    }
    account = SimulatedAccount()

    restore_account_state(account, snapshot)

    assert account.quantity == 0
    assert account.contract is None


# ---------- regression: Phase 2/3/4 behavior intact ----------


def test_duplicate_signal_protection_still_works_with_contract_resolution(monkeypatch) -> None:
    patch_fetch(monkeypatch, UPTREND_60.iloc[:17])
    risk_manager = RiskManager()
    risk_manager.enable_auto_trading()
    order_manager = OrderManager()

    first = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1, now=TRADING_HOURS_NOW,
        resolve_contract_fn=good_resolver,
    )
    assert first.order.status == "PLACED"

    second = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=TRADING_HOURS_NOW + timedelta(minutes=5), resolve_contract_fn=good_resolver,
    )

    assert second.order is None
    assert "duplicate" in second.risk.reason.lower()
    assert order_manager.account.quantity == 1  # unaffected


def test_square_off_still_closes_a_resolved_position_without_the_resolver(monkeypatch) -> None:
    from algoedge.risk_manager import RiskLimits

    flat_index = pd.date_range("2026-09-23 14:50", periods=10, freq="5min", tz="Asia/Kolkata")
    flat_df = pd.DataFrame(
        {"Open": [110.0] * 10, "High": [110.0] * 10, "Low": [110.0] * 10, "Close": [110.0] * 10,
         "Volume": [0.0] * 10}, index=flat_index,
    )
    account = SimulatedAccount(
        quantity=1, average_price=100.0, side="CALL",
        contract=OptionContract(
            trading_symbol="NIFTY26SEP150CE", underlying="NIFTY", right="CE", strike=150,
            expiry=date(2026, 9, 30),
        ),
    )
    order_manager = OrderManager(account)
    risk_manager = RiskManager(RiskLimits())
    risk_manager.enable_auto_trading()
    patch_fetch(monkeypatch, flat_df)

    result = auto_trader.run_cycle(
        "nifty-50", "5m", risk_manager, order_manager, quantity=1,
        now=datetime(2026, 9, 23, 15, 25, tzinfo=IST),
    )  # no resolve_contract_fn - square-off must not need one

    assert result.event.kind == "SQUARE_OFF"
    assert account.quantity == 0
    assert account.contract is None
