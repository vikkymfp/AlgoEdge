"""Phase 8.0: the offline reconciliation tool, on deterministic synthetic
database evidence shaped exactly like the paper engine's rows."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy
from research.phase8.tools import bars, reconcile
from research.phase8.tools.common import IST

T0 = datetime(2026, 10, 1, 10, 0, 0)  # naive database time (IST)
DAY = "2026-10-01"


class Evidence:
    """Builds the rows one paper cycle writes in one transaction (web_server
    _execute_and_persist_cycle -> db.record_paper_cycle)."""

    def __init__(self) -> None:
        self.tables = {name: [] for name in ("strategy_signals", "orders", "risk_state_events",
                                             "auto_trade_account_snapshots", "paper_decision_events",
                                             "alert_events")}
        self.baseline = {"account_snapshot:nifty-50": None, "account_snapshot:sensex": None,
                         "account_snapshot:bank-nifty": None, "risk_state": None}
        self._ids = {name: 0 for name in self.tables}

    def row(self, table, seconds, **fields):
        self._ids[table] += 1
        record = {"id": self._ids[table], "created_at": (T0 + timedelta(seconds=seconds)).isoformat(), **fields}
        self.tables[table].append(record)
        return record

    def signal(self, index_id, seconds, kind, price):
        return self.row("strategy_signals", seconds, source=reconcile.AUTO_SOURCE, index_id=index_id,
                        action=kind, reason="x", price=price)

    def fill(self, index_id, seconds, kind, price, *, bar, entries, trades, pnl_today, qty_after, side_after,
             avg_after, realized=0.0, skip=()):
        entry = kind in reconcile.ENTRY_KINDS
        if "signal" not in skip:
            self.signal(index_id, seconds, kind, price)
        order = self.row("orders", seconds, source=reconcile.AUTO_SOURCE, live=False, index_id=index_id,
                         side="BUY" if entry else "SELL", price=price, quantity=1, outcome="PLACED",
                         realized_pnl=realized, exit_reason=None if entry else kind)
        if "risk" not in skip:
            self.row("risk_state_events", seconds + 0.01, scope="paper", event="TRADE_RECORDED", trade_day=DAY,
                     entries_today=entries, trades_today=trades, realized_pnl_today=pnl_today)
        if "snapshot" not in skip:
            self.row("auto_trade_account_snapshots", seconds + 0.02, index_id=index_id, event=kind,
                     quantity=qty_after, side=side_after, average_price=avg_after, last_event_at=bar)
        return order

    def entry(self, index_id, seconds, price=100.0, *, bar="2026-10-01T09:50:00", entries=1, trades=1,
              pnl_today=0.0, kind="ENTRY_CALL", skip=()):
        side = reconcile.ENTRY_KINDS[kind]
        return self.fill(index_id, seconds, kind, price, bar=bar, entries=entries, trades=trades,
                         pnl_today=pnl_today, qty_after=1, side_after=side, avg_after=price, skip=skip)

    def exit(self, index_id, seconds, price, realized, *, bar="2026-10-01T10:05:00", entries=1, trades=2,
             pnl_today=None, kind="EXIT_TARGET"):
        return self.fill(index_id, seconds, kind, price, bar=bar, entries=entries, trades=trades,
                         pnl_today=realized if pnl_today is None else pnl_today, qty_after=0, side_after=None,
                         avg_after=None, realized=realized)

    def decision(self, index_id, seconds, decision, reason, *, event_kind="ENTRY_CALL",
                 event_at="2026-10-01T09:50:00", entries=0, trades=0, open_quantity=0, signal=True):
        if signal and event_kind:
            self.signal(index_id, seconds, event_kind, 100.0)
        return self.row("paper_decision_events", seconds + 0.01, index_id=index_id, decision=decision,
                        reason=reason, event_kind=event_kind, event_at=event_at, price=100.0,
                        entries_today=entries, trades_today=trades, open_quantity=open_quantity)

    def restart(self, seconds):
        self.row("alert_events", seconds, category="SYSTEM_RESTART", severity="INFO",
                 message="AlgoEdge dashboard started", source="web_server")

    def reconcile(self, **kwargs):
        extraction = reconcile.Extraction({"run_id": "t", "range": {}}, self.tables, self.baseline)
        return reconcile.Reconciler(extraction, **kwargs).run()


def status(report, check):
    return report["checks"][check]["status"]


def codes(report, check=None):
    return [f["code"] for f in report["findings"] if check is None or f["check"] == check]


CORE = ("LIVE_ORDERS", "P1_GROUPING", "TRANSITIONS", "R1_MAX_OPEN", "C2_DUPLICATES", "R2_COUNTERS", "PNL")


# ---------------- the required scenarios ----------------


def test_a_valid_entry_exit_chain_reconciles() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, 100.0)
    ev.exit("nifty-50", 600, 110.0, realized=10.0)
    report = ev.reconcile()
    assert {check: status(report, check) for check in CORE} == dict.fromkeys(CORE, "PASS")
    assert report["groups"]["by_status"]["RECONCILED"] == 2 and report["fills"] == 2
    assert report["stop_campaign"] is False
    # Not evaluated without their inputs - never reported as passing.
    assert {status(report, c) for c in ("BARS", "S1_REPLAY", "DASHBOARD", "AUDIT")} == {"UNVERIFIABLE"}


def test_a_put_exit_pnl_is_in_underlying_points_with_direction() -> None:
    ev = Evidence()
    ev.entry("sensex", 0, 200.0, kind="ENTRY_PUT")
    ev.exit("sensex", 600, 190.0, realized=10.0, kind="EXIT_TARGET")  # PUT gains when price falls
    assert status(ev.reconcile(), "PNL") == "PASS"


def test_a_blocked_decision_reconciles_and_does_not_count_as_an_entry() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.decision("sensex", 60, "BLOCKED", "Max open positions reached", entries=1, trades=1)
    report = ev.reconcile()
    assert {check: status(report, check) for check in CORE + ("AUDIT",)} == dict.fromkeys(CORE + ("AUDIT",), "PASS")


def test_a_blocked_entry_that_incremented_the_counter_fails_r2() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.decision("sensex", 60, "BLOCKED", "Max open positions reached", entries=2, trades=1)
    report = ev.reconcile()
    assert status(report, "R2_COUNTERS") == "FAIL" and "DECISION_COUNTER_MISMATCH" in codes(report)


def test_a_block_reason_the_positions_do_not_support_fails_audit() -> None:
    ev = Evidence()
    ev.decision("sensex", 60, "BLOCKED", "Max open positions reached")  # nothing was open
    report = ev.reconcile()
    assert "BLOCK_REASON_INCONSISTENT" in codes(report, "AUDIT")


def test_an_expired_event_is_audited_with_its_signal() -> None:
    ev = Evidence()
    ev.decision("nifty-50", 0, "EXPIRED", "Stale signal expired (1 event(s) older than 0:10:00 after bar close)")
    report = ev.reconcile()
    assert status(report, "P1_GROUPING") == "PASS" and status(report, "AUDIT") == "PASS"
    assert status(report, "R2_COUNTERS") == "PASS"


def test_a_square_off_pending_decision_needs_no_signal() -> None:
    ev = Evidence()
    ev.decision("nifty-50", 0, "SQUARE_OFF_PENDING", "Missed square-off ... waiting", event_kind=None,
                event_at=None, signal=False)
    assert status(ev.reconcile(), "P1_GROUPING") == "PASS"


def test_a_duplicate_fill_is_a_p0() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, bar="2026-10-01T09:50:00")
    ev.exit("nifty-50", 600, 110.0, realized=10.0)
    ev.entry("nifty-50", 1200, bar="2026-10-01T09:50:00", entries=2, trades=3, pnl_today=10.0)  # same bar again
    report = ev.reconcile()
    assert status(report, "C2_DUPLICATES") == "FAIL" and "DUPLICATE_FILL" in codes(report)
    assert report["stop_campaign"] is True


def test_a_max_open_positions_violation_is_a_p0() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.entry("sensex", 60, entries=2, trades=2)  # NIFTY still open
    report = ev.reconcile()
    (finding,) = [f for f in report["findings"] if f["check"] == "R1_MAX_OPEN"]
    assert (finding["status"], finding["code"], finding["severity"]) == ("FAIL", "MAX_OPEN_POSITIONS", "P0")
    assert report["stop_campaign"] is True


def test_the_limit_is_configurable_for_max_open_positions_above_one() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.entry("sensex", 60, entries=2, trades=2)
    assert status(ev.reconcile(max_open_positions=2), "R1_MAX_OPEN") == "PASS"


def test_a_missing_persistence_row_fails_p1_grouping() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, skip=("snapshot",))
    report = ev.reconcile()
    (finding,) = [f for f in report["findings"] if f["code"] == "MISSING_PERSISTENCE_ROW"]
    assert "snapshot" in finding["detail"] and finding["severity"] == "P0"
    assert report["groups"]["by_status"]["INCOMPLETE"] == 1 and report["stop_campaign"] is True


def test_an_orphan_row_is_reported() -> None:
    ev = Evidence()
    ev.signal("nifty-50", 0, "ENTRY_CALL", 100.0)  # a signal with no order and no decision
    assert "ORPHAN_ROW" in codes(ev.reconcile(), "P1_GROUPING")


def test_an_ambiguous_timestamp_group_is_unreconciled_not_guessed() -> None:
    ev = Evidence()
    ev.decision("nifty-50", 0, "BLOCKED", "Cooldown active until 10:05:00")
    ev.decision("nifty-50", 0.4, "BLOCKED", "Cooldown active until 10:05:00")  # both signals within 1 s of both
    report = ev.reconcile()
    assert status(report, "P1_GROUPING") == "UNRECONCILED"
    assert report["groups"]["by_status"]["UNRECONCILED"] == 2
    finding = next(f for f in report["findings"] if f["code"] == "AMBIGUOUS_GROUP")
    assert sorted(finding["evidence"]) == sorted(["paper_decision_events#1", "strategy_signals#1",
                                                  "strategy_signals#2"])
    assert not any(f["status"] == "FAIL" for f in report["findings"])
    assert len(ev.tables["strategy_signals"]) == 2  # source evidence untouched


def test_an_entry_racing_another_indexs_exit_is_unreconciled_not_a_violation() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.entry("sensex", 60.0, entries=2, trades=3, pnl_today=10.0)
    ev.exit("nifty-50", 60.5, 110.0, realized=10.0, entries=1, trades=2)  # order within 1 s is unknowable
    report = ev.reconcile()
    assert status(report, "R1_MAX_OPEN") == "UNRECONCILED"
    assert "MAX_OPEN_POSITIONS" not in codes(report)


def test_restart_continuity_and_re_audit_after_restart() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, bar="2026-10-01T09:50:00")
    ev.decision("sensex", 100, "EXPIRED", "Stale signal expired", event_at="2026-10-01T09:30:00",
                entries=1, trades=1)
    ev.restart(200)
    # After a restart the in-memory high-water mark is the persisted one, so the
    # same stale event is expired (and audited) again - existing behavior.
    ev.decision("sensex", 300, "EXPIRED", "Stale signal expired", event_at="2026-10-01T09:30:00",
                entries=1, trades=1)
    ev.exit("nifty-50", 600, 110.0, realized=10.0)  # the position restored across the restart exits
    report = ev.reconcile()
    assert {check: status(report, check) for check in CORE} == dict.fromkeys(CORE, "PASS")
    assert status(report, "AUDIT") == "PASS"


def test_a_re_fill_after_restart_is_a_duplicate() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, bar="2026-10-01T09:50:00")
    ev.exit("nifty-50", 60, 101.0, realized=1.0)
    ev.restart(100)
    ev.entry("nifty-50", 150, bar="2026-10-01T09:50:00", entries=2, trades=3, pnl_today=1.0)
    assert "DUPLICATE_FILL" in codes(ev.reconcile(), "C2_DUPLICATES")


def test_a_position_open_before_the_range_comes_from_the_baseline() -> None:
    ev = Evidence()
    ev.exit("nifty-50", 0, 110.0, realized=10.0, entries=1, trades=2, pnl_today=10.0)
    assert "EXIT_WHILE_FLAT" in codes(ev.reconcile(), "TRANSITIONS")  # no baseline: flat
    ev.baseline["account_snapshot:nifty-50"] = {"quantity": 1, "side": "CALL", "average_price": 100.0,
                                                "last_event_at": "2026-10-01T09:50:00"}
    ev.baseline["risk_state"] = {"trade_day": DAY, "entries_today": 1, "trades_today": 1, "realized_pnl_today": 0.0}
    report = ev.reconcile()
    exercised = tuple(c for c in CORE if c != "R1_MAX_OPEN")
    assert {check: status(report, check) for check in exercised} == dict.fromkeys(exercised, "PASS")
    assert status(report, "R1_MAX_OPEN") == "UNVERIFIABLE"  # no entry in range: nothing to check, not a pass


def test_a_pnl_mismatch_fails() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0, 100.0)
    ev.exit("nifty-50", 600, 110.0, realized=12.0)
    assert "PNL_MISMATCH" in codes(ev.reconcile(), "PNL")


def test_any_live_order_is_a_p0() -> None:
    ev = Evidence()
    ev.row("orders", 0, source="algoedge.manual_trading", live=True, index_id="nifty-50", side="BUY", price=1.0,
           quantity=1, outcome="PLACED", realized_pnl=0.0)
    report = ev.reconcile()
    assert status(report, "LIVE_ORDERS") == "FAIL" and report["stop_campaign"] is True


def test_a_snapshot_contradicting_the_order_sequence_is_a_position_mismatch() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    ev.tables["auto_trade_account_snapshots"][0]["quantity"] = 2
    assert "POSITION_MISMATCH" in codes(ev.reconcile(), "TRANSITIONS")


# ---------------- bars and the canonical replay ----------------


def rising(n=40):
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


FULL = rising()
CANONICAL = next(e for e in run_strategy(FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
                 if e.kind == "ENTRY_CALL")


def capture_through(observed: datetime):
    frame = FULL.loc[FULL.index <= observed]
    analysis = bars.analyze(frame, observed, reconcile.BAR_LENGTH)
    return {"nifty-50": [bars.CaptureWindow("nifty-50", observed, {b["bar_start"]: b for b in analysis["bars"]})]}


def replay_evidence(fill_at: datetime, price=None, bar=None):
    ev = Evidence()
    global T0
    saved, T0 = T0, fill_at.replace(tzinfo=None)
    try:
        ev.entry("nifty-50", 0, CANONICAL.underlying_price if price is None else price,
                 bar=(bar or CANONICAL.timestamp).tz_localize(None).isoformat())
    finally:
        T0 = saved
    for table in ("risk_state_events",):
        for row in ev.tables[table]:
            row["trade_day"] = fill_at.date().isoformat()
    return ev


def test_an_entry_from_a_completed_canonical_bar_verifies() -> None:
    fill_at = (CANONICAL.timestamp + timedelta(minutes=5, seconds=30)).to_pydatetime()
    ev = replay_evidence(fill_at)
    report = ev.reconcile(captures=capture_through(fill_at - timedelta(seconds=20)))
    assert status(report, "BARS") == "PASS" and status(report, "S1_REPLAY") == "PASS"


def test_an_entry_from_a_still_forming_bar_is_a_p0() -> None:
    fill_at = (CANONICAL.timestamp + timedelta(minutes=2, seconds=30)).to_pydatetime()
    ev = replay_evidence(fill_at)
    report = ev.reconcile(captures=capture_through(fill_at - timedelta(seconds=20)))
    assert "FORMING_BAR_ENTRY" in codes(report, "BARS") and report["stop_campaign"] is True


def test_a_fill_price_not_in_the_evidence_is_unreconciled() -> None:
    fill_at = (CANONICAL.timestamp + timedelta(minutes=5, seconds=30)).to_pydatetime()
    ev = replay_evidence(fill_at, price=CANONICAL.underlying_price + 0.5)
    report = ev.reconcile(captures=capture_through(fill_at - timedelta(seconds=20)))
    assert "ENTRY_PRICE_NOT_IN_EVIDENCE" in codes(report, "BARS")
    assert status(report, "S1_REPLAY") == "UNRECONCILED"


def test_an_entry_the_canonical_strategy_did_not_produce_is_unreconciled() -> None:
    fill_at = (CANONICAL.timestamp + timedelta(minutes=10, seconds=30)).to_pydatetime()
    other = CANONICAL.timestamp + timedelta(minutes=5)  # the bar after the real entry
    ev = replay_evidence(fill_at, price=float(FULL.loc[other, "Close"]), bar=other)
    report = ev.reconcile(captures=capture_through(fill_at - timedelta(seconds=20)))
    assert "REPLAY_MISMATCH" in codes(report, "S1_REPLAY")


def test_a_fill_without_bar_evidence_is_unverifiable_not_passed() -> None:
    fill_at = (CANONICAL.timestamp + timedelta(minutes=5, seconds=30)).to_pydatetime()
    report = replay_evidence(fill_at).reconcile(captures={})
    assert status(report, "BARS") == "UNVERIFIABLE" and status(report, "S1_REPLAY") == "UNVERIFIABLE"


# ---------------- dashboard samples ----------------


def sample(seconds, accounts, entries, *, limits=None, ok=True, seq=0):
    moment = (T0 + timedelta(seconds=seconds)).replace(tzinfo=IST)
    frozen = {"dailyLossLimit": 5000.0, "maxTradesPerDay": 10, "maxOpenPositions": 1, "maxQuantity": 50,
              "tradingStart": "09:15:00", "tradingEnd": "15:30:00", "entryCutoff": "15:00:00",
              "squareOffTime": "15:20:00", "maxConsecutiveLosses": 3, "cooldownMinutes": 5}
    holding = sorted(i for i, a in accounts.items() if a["quantity"] > 0)
    return {"run_id": "s", "seq": seq, "ok": ok, "collected_at": {"utc": moment.isoformat()},
            "status": {"extracted": {"accounts": accounts, "entriesToday": entries, "limits": {**frozen, **(limits or {})},
                                     "openPositions": {"count": len(holding), "indexIds": holding}}}}


FLAT = {"quantity": 0, "side": None}
LONG = {"quantity": 1, "side": "CALL"}


def test_dashboard_samples_matching_the_persisted_state_pass() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    samples = [sample(300, {"nifty-50": LONG, "sensex": FLAT}, 1, seq=1)]
    assert status(ev.reconcile(status_samples=samples), "DASHBOARD") == "PASS"


def test_dashboard_mismatches_fail() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    samples = [sample(300, {"nifty-50": FLAT, "sensex": FLAT}, 1, seq=1),
               sample(400, {"nifty-50": LONG, "sensex": FLAT}, 2, seq=2),
               sample(500, {"nifty-50": LONG, "sensex": FLAT}, 1, limits={"maxOpenPositions": 2}, seq=3),
               sample(600, {"nifty-50": LONG, "sensex": LONG}, 1, seq=4)]
    found = codes(ev.reconcile(status_samples=samples), "DASHBOARD")
    for code in ("DASHBOARD_POSITION_MISMATCH", "DASHBOARD_ENTRIES_MISMATCH", "LIMITS_CHANGED",
                 "MAX_OPEN_OBSERVED"):
        assert code in found


def test_a_sample_within_the_poll_lag_of_a_fill_is_not_judged() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    samples = [sample(-20, {"nifty-50": FLAT, "sensex": FLAT}, 0)]  # taken just before the fill landed
    assert status(ev.reconcile(status_samples=samples), "DASHBOARD") == "PASS"


def test_unusable_samples_make_the_dashboard_check_unverifiable() -> None:
    ev = Evidence()
    ev.entry("nifty-50", 0)
    report = ev.reconcile(status_samples=[sample(300, {}, None, ok=False)])
    assert status(report, "DASHBOARD") == "UNVERIFIABLE" and "COLLECTOR_GAPS" in codes(report)


@pytest.mark.parametrize("tampered", [1])
def test_tampered_status_evidence_is_a_p0(tampered) -> None:
    ev = Evidence()
    report = ev.reconcile(status_samples=[], tampered_samples=tampered)
    assert "TAMPERED_SAMPLES" in codes(report) and report["stop_campaign"] is True
