"""Phase 8.0 evidence-gap fix: R3/R4/R5 risk-rule reconciliation and the
exact D1 SL/target check, on deterministic rows shaped like the engine's."""

from datetime import date, datetime, timedelta

import pandas as pd

from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import run as run_strategy
from research.phase8.tools import bars, reconcile
from research.phase8.tools.common import IST

DAY = date(2026, 10, 1)
HALT_REASON = "Trading halted after 3 consecutive losses - reset required"


class Session:
    """Writes the rows a paper session would, updating counters, the loss
    streak, the halt and last_exit_at exactly as RiskManager does. `record`
    overrides what a row records (to simulate an engine defect)."""

    def __init__(self, day: date = DAY, *, baseline_risk: dict | None = None) -> None:
        self.day = day
        self.tables = {name: [] for name in ("strategy_signals", "orders", "risk_state_events",
                                             "auto_trade_account_snapshots", "paper_decision_events",
                                             "alert_events")}
        self.baseline = {f"account_snapshot:{i}": None for i in ("nifty-50", "sensex", "bank-nifty")}
        self.baseline["risk_state"] = baseline_risk
        self.ids = {name: 0 for name in self.tables}
        self.state = {"auto_trading_enabled": True, "kill_switch": False, "consecutive_losses": 0,
                      "consecutive_loss_halt": False, "last_exit_at": None, "entries_today": 0, "trades_today": 0,
                      "realized_pnl_today": 0.0, "trade_day": None}
        if baseline_risk:
            self.state.update({k: v for k, v in baseline_risk.items() if k in self.state})
        self.holding = {}
        self.range = (datetime.combine(day, datetime.min.time()).replace(hour=9),
                      datetime.combine(day, datetime.min.time()).replace(hour=16))

    def at(self, text: str) -> datetime:
        return datetime.fromisoformat(text) if len(text) > 8 else datetime.combine(self.day, datetime.strptime(
            text, "%H:%M:%S").time())

    def row(self, table, at, **fields):
        self.ids[table] += 1
        record = {"id": self.ids[table], "created_at": at.isoformat(), **fields}
        self.tables[table].append(record)
        return record

    def _new_day(self, at):
        if self.state["trade_day"] != at.date().isoformat():
            self.state.update(trade_day=at.date().isoformat(), entries_today=0, trades_today=0, realized_pnl_today=0.0)

    def risk_row(self, at, event, record=None):
        fields = {k: self.state[k] for k in ("auto_trading_enabled", "kill_switch", "consecutive_losses",
                                             "consecutive_loss_halt", "entries_today", "trades_today",
                                             "realized_pnl_today", "trade_day")}
        fields["last_exit_at"] = self.state["last_exit_at"].isoformat() if self.state["last_exit_at"] else None
        fields.update(record or {})
        return self.row("risk_state_events", at + timedelta(milliseconds=10), scope="paper", event=event, **fields)

    def entry(self, index_id, when, price=100.0, *, kind="ENTRY_CALL", bar=None, record=None):
        at = self.at(when)
        self._new_day(at)
        bar = bar or (at - timedelta(minutes=5, seconds=at.second, microseconds=at.microsecond)
                      - timedelta(minutes=at.minute % 5))
        self.row("strategy_signals", at, source=reconcile.AUTO_SOURCE, index_id=index_id, action=kind, reason="x",
                 price=price)
        self.row("orders", at, source=reconcile.AUTO_SOURCE, live=False, index_id=index_id, side="BUY", price=price,
                 quantity=1, outcome="PLACED", realized_pnl=0.0, exit_reason=None)
        self.state["entries_today"] += 1
        self.state["trades_today"] += 1
        self.risk_row(at, "TRADE_RECORDED", record)
        self.row("auto_trade_account_snapshots", at + timedelta(milliseconds=20), index_id=index_id, event=kind,
                 quantity=1, side=reconcile.ENTRY_KINDS[kind], average_price=price,
                 last_event_at=bar.isoformat(), square_off_date=None)
        self.holding[index_id] = (price, reconcile.ENTRY_KINDS[kind])

    def exit(self, index_id, when, price, *, kind="EXIT_TARGET", cycle_seconds=5, bar=None, record=None):
        at = self.at(when)
        self._new_day(at)
        entry_price, side = self.holding.pop(index_id)
        pnl = (price - entry_price) * (1 if side == "CALL" else -1)
        self.row("strategy_signals", at, source=reconcile.AUTO_SOURCE, index_id=index_id, action=kind, reason="x",
                 price=price)
        self.row("orders", at, source=reconcile.AUTO_SOURCE, live=False, index_id=index_id, side="SELL", price=price,
                 quantity=1, outcome="PLACED", realized_pnl=pnl, exit_reason=kind)
        self.state["trades_today"] += 1
        self.state["realized_pnl_today"] += pnl
        self.state["last_exit_at"] = at - timedelta(seconds=cycle_seconds)  # the engine's `now` for this exit
        if pnl < 0:
            self.state["consecutive_losses"] += 1
            if self.state["consecutive_losses"] >= 3:
                self.state["consecutive_loss_halt"] = True
        elif pnl > 0:
            self.state["consecutive_losses"] = 0
        self.risk_row(at, "TRADE_RECORDED", record)
        bar = bar or at - timedelta(minutes=5, seconds=at.second) - timedelta(minutes=at.minute % 5)
        self.row("auto_trade_account_snapshots", at + timedelta(milliseconds=20), index_id=index_id, event=kind,
                 quantity=0, side=None, average_price=None, last_event_at=bar.isoformat(),
                 square_off_date=at.date().isoformat() if kind == "SQUARE_OFF" and bar.date() == at.date() else None)
        return pnl

    def control(self, when, event):
        at = self.at(when)
        changes = {"ENABLE": {"auto_trading_enabled": True}, "DISABLE": {"auto_trading_enabled": False},
                   "KILL_SWITCH_ON": {"kill_switch": True}, "KILL_SWITCH_OFF": {"kill_switch": False},
                   "CONSECUTIVE_LOSS_HALT_RESET": {"consecutive_loss_halt": False, "consecutive_losses": 0}}
        self.state.update(changes[event])
        self.risk_row(at, event)

    def blocked(self, index_id, when, reason, *, event_kind="ENTRY_CALL", record=None):
        at = self.at(when)
        self.row("strategy_signals", at, source=reconcile.AUTO_SOURCE, index_id=index_id, action=event_kind,
                 reason="x", price=100.0)
        fields = {k: self.state[k] for k in ("auto_trading_enabled", "kill_switch", "consecutive_loss_halt",
                                             "entries_today", "trades_today", "realized_pnl_today")}
        fields.update(record or {})
        self.row("paper_decision_events", at + timedelta(milliseconds=10), index_id=index_id, decision="BLOCKED",
                 reason=reason, event_kind=event_kind, event_at=(at - timedelta(minutes=5)).isoformat(), price=100.0,
                 open_quantity=1 if index_id in self.holding else 0, **fields)

    def restart(self, when):
        self.row("alert_events", self.at(when), category="SYSTEM_RESTART", severity="INFO", message="started",
                 source="web_server")

    def reconcile(self, **kwargs):
        manifest = {"run_id": "t", "range": {"start": self.range[0].isoformat(), "end": self.range[1].isoformat()}}
        return reconcile.Reconciler(reconcile.Extraction(manifest, self.tables, self.baseline), **kwargs).run()


def status(report, check):
    return report["checks"][check]["status"]


def codes(report, check):
    return [f["code"] for f in report["findings"] if f["check"] == check]


def finding(report, code):
    return next(f for f in report["findings"] if f["code"] == code)


def normal_session():
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.blocked("sensex", "10:00:40", "Max open positions reached")
    s.exit("nifty-50", "10:30:05", 110.0)
    s.blocked("sensex", "10:31:00", "Cooldown active until 10:35:00")
    s.entry("sensex", "10:40:30")
    s.exit("sensex", "11:10:05", 105.0)
    return s


# ---------------- R5: entry window, cutoff, cooldown, cap ----------------


def test_a_normal_session_passes_every_entry_rule() -> None:
    report = normal_session().reconcile()
    for check in ("ENTRY_RULES", "DECISION_STATE", "R2_COUNTERS", "R1_MAX_OPEN"):
        assert status(report, check) == "PASS", (check, report["findings"])
    assert report["checks"]["ENTRY_RULES"]["observations"]["entries_before_cutoff"] == 2
    assert report["checks"]["ENTRY_RULES"]["criteria"] == ["R5", "R1"]


def test_an_entry_after_the_cutoff_is_a_p0() -> None:
    s = Session()
    s.entry("nifty-50", "15:05:00")
    report = s.reconcile()
    assert finding(report, "ENTRY_AFTER_CUTOFF")["severity"] == "P0" and report["stop_campaign"]


def test_an_entry_just_past_the_cutoff_is_unverifiable_not_failed_or_passed() -> None:
    s = Session()
    s.entry("nifty-50", "15:00:40")  # engine time may have been 14:59:xx
    report = s.reconcile()
    assert status(report, "ENTRY_RULES") == "UNVERIFIABLE" and "ENTRY_CUTOFF_BOUNDARY" in codes(report, "ENTRY_RULES")


def test_an_entry_before_the_open_fails() -> None:
    s = Session()
    s.entry("nifty-50", "09:10:00")
    assert "ENTRY_BEFORE_OPEN" in codes(s.reconcile(), "ENTRY_RULES")


def test_an_entry_inside_the_cooldown_is_a_p0() -> None:
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.exit("nifty-50", "10:30:05", 110.0)  # engine exit time 10:30:00 -> cooldown until 10:35:00
    s.entry("sensex", "10:33:00")
    report = s.reconcile()
    assert finding(report, "ENTRY_IN_COOLDOWN")["severity"] == "P0"


def test_an_entry_at_the_cooldown_boundary_is_unverifiable() -> None:
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.exit("nifty-50", "10:30:05", 110.0)
    s.entry("sensex", "10:35:20")  # committed after 10:35:00, engine time unknown within 120 s
    report = s.reconcile()
    assert "ENTRY_COOLDOWN_BOUNDARY" in codes(report, "ENTRY_RULES") and status(report, "ENTRY_RULES") == "UNVERIFIABLE"


def daily_entries(n, *, baseline_risk=None):
    s = Session(baseline_risk=baseline_risk)
    start = datetime.combine(DAY, datetime.min.time()).replace(hour=9, minute=20, second=30)
    for k in range(n):
        entry_at = start + timedelta(minutes=20 * k)
        s.entry("nifty-50", entry_at.isoformat())
        s.exit("nifty-50", (entry_at + timedelta(minutes=5)).isoformat(), 101.0)
    return s


def test_ten_new_entries_a_day_are_allowed_and_counted() -> None:
    report = daily_entries(10).reconcile()
    assert status(report, "ENTRY_RULES") == "PASS" and status(report, "R2_COUNTERS") == "PASS"


def test_an_eleventh_new_entry_breaks_the_daily_cap() -> None:
    report = daily_entries(11).reconcile()
    (cap,) = [f for f in report["findings"] if f["code"] == "ENTRY_OVER_DAILY_CAP"]
    assert "entry #11" in cap["detail"] and cap["severity"] == "P0"


def test_the_cap_counts_entries_made_before_the_range() -> None:
    baseline = {"trade_day": DAY.isoformat(), "entries_today": 9, "trades_today": 18, "realized_pnl_today": 9.0,
                "consecutive_losses": 0, "consecutive_loss_halt": False, "auto_trading_enabled": True,
                "kill_switch": False, "last_exit_at": None}
    report = daily_entries(2, baseline_risk=baseline).reconcile()
    assert "ENTRY_OVER_DAILY_CAP" in codes(report, "ENTRY_RULES")


def test_counters_reset_on_a_new_ist_day() -> None:
    s = daily_entries(10)
    s.entry("nifty-50", "2026-10-02T09:30:30")  # the 11th overall, the 1st of the new day
    s.range = (s.range[0], datetime(2026, 10, 2, 16, 0))
    report = s.reconcile()
    assert "ENTRY_OVER_DAILY_CAP" not in codes(report, "ENTRY_RULES") and status(report, "R2_COUNTERS") == "PASS"


# ---------------- R5: consecutive-loss halt ----------------


def losing_streak(record_third=None):
    s = Session()
    for k, hour in enumerate((10, 11, 12)):
        s.entry("nifty-50", f"{hour}:00:30")
        s.exit("nifty-50", f"{hour}:20:05", 95.0, kind="EXIT_SL", record=record_third if k == 2 else None)
    return s


def test_the_third_consecutive_loss_trips_the_halt() -> None:
    report = losing_streak().reconcile()
    assert status(report, "HALT") == "PASS"
    assert report["checks"]["HALT"]["observations"]["halt_trips_verified"] == 1


def test_a_third_loss_without_the_halt_is_a_p0() -> None:
    report = losing_streak(record_third={"consecutive_loss_halt": False}).reconcile()
    assert finding(report, "HALT_NOT_TRIPPED")["severity"] == "P0"


def test_a_win_resets_the_streak_but_not_the_halt() -> None:
    s = losing_streak()
    s.control("12:30:00", "KILL_SWITCH_ON")  # unrelated control row: the halt must persist through it
    s.blocked("sensex", "12:40:00", HALT_REASON)
    report = s.reconcile()
    assert status(report, "HALT") == "PASS" and status(report, "DECISION_STATE") == "PASS"


def test_a_halt_that_clears_without_a_reset_fails() -> None:
    s = losing_streak()
    s.blocked("sensex", "12:40:00", "Max open positions reached", record={"consecutive_loss_halt": False})
    assert "HALT_CLEARED_WITHOUT_RESET" in codes(s.reconcile(), "HALT")


def test_the_halt_survives_a_restart_and_clears_only_on_reset() -> None:
    s = losing_streak()
    s.restart("13:00:00")
    s.blocked("sensex", "13:10:00", HALT_REASON)
    s.control("13:20:00", "CONSECUTIVE_LOSS_HALT_RESET")
    s.entry("sensex", "13:30:30")
    report = s.reconcile()
    assert status(report, "HALT") == "PASS" and status(report, "ENTRY_RULES") == "PASS"
    assert report["checks"]["HALT"]["observations"]["resets"] == 1


def test_no_third_loss_means_the_halt_trip_is_unverifiable() -> None:
    report = normal_session().reconcile()
    assert status(report, "HALT") == "UNVERIFIABLE" and "HALT_TRIP_NOT_EXERCISED" in codes(report, "HALT")


def test_a_new_entry_filled_during_the_halt_is_a_p0() -> None:
    s = losing_streak()
    s.entry("sensex", "13:00:30")
    assert finding(s.reconcile(), "ENTRY_WHILE_RESTRICTED")["severity"] == "P0"


def test_a_blocked_entry_during_the_halt_matches_its_recorded_state() -> None:
    s = losing_streak()
    s.blocked("sensex", "13:00:30", HALT_REASON)
    assert status(s.reconcile(), "DECISION_STATE") == "PASS"


def test_a_halt_block_without_a_recorded_halt_fails() -> None:
    s = Session()
    s.blocked("sensex", "10:00:30", HALT_REASON)
    assert "DECISION_STATE_MISMATCH" in codes(s.reconcile(), "DECISION_STATE")


# ---------------- R3: exits under entry restrictions ----------------


def test_a_risk_reducing_exit_is_filled_while_halted_and_kill_switched() -> None:
    s = Session()
    for hour in (10, 11):
        s.entry("nifty-50", f"{hour}:00:30")
        s.exit("nifty-50", f"{hour}:20:05", 95.0, kind="EXIT_SL")
    s.entry("nifty-50", "12:00:30")
    s.control("12:10:00", "KILL_SWITCH_ON")
    s.exit("nifty-50", "12:20:05", 95.0, kind="EXIT_SL")  # third loss: halted; kill switch on - still exits
    report = s.reconcile()
    assert status(report, "EXIT_RULES") == "PASS"
    assert report["checks"]["EXIT_RULES"]["observations"]["exits_filled_while_restricted"] == 1


def test_an_exit_blocked_by_an_entry_restriction_is_a_p0() -> None:
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.control("10:10:00", "KILL_SWITCH_ON")
    s.blocked("nifty-50", "10:20:05", "Emergency kill switch is engaged (Manually engaged)", event_kind="EXIT_SL")
    assert finding(s.reconcile(), "EXIT_BLOCKED_BY_ENTRY_RESTRICTION")["severity"] == "P0"


def test_without_any_restricted_exit_r3_is_unverifiable() -> None:
    report = normal_session().reconcile()
    assert status(report, "EXIT_RULES") == "UNVERIFIABLE"


# ---------------- R4: square-off timing and recovery ----------------


def test_a_square_off_in_the_window_passes() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.exit("nifty-50", "15:21:10", 101.0, kind="SQUARE_OFF")
    report = s.reconcile()
    assert status(report, "SQUARE_OFF") == "PASS"
    assert report["checks"]["SQUARE_OFF"]["observations"]["same_day_square_offs"] == 1


def test_a_square_off_before_15_20_fails() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.exit("nifty-50", "15:10:10", 101.0, kind="SQUARE_OFF")
    assert "SQUARE_OFF_TOO_EARLY" in codes(s.reconcile(), "SQUARE_OFF")


def test_a_square_off_after_the_session_fails() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.exit("nifty-50", "15:40:00", 101.0, kind="SQUARE_OFF")
    assert "SQUARE_OFF_AFTER_SESSION" in codes(s.reconcile(), "SQUARE_OFF")


def test_a_square_off_without_its_date_recorded_fails() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.exit("nifty-50", "15:21:10", 101.0, kind="SQUARE_OFF")
    s.tables["auto_trade_account_snapshots"][-1]["square_off_date"] = None
    assert "SQUARE_OFF_DATE_NOT_RECORDED" in codes(s.reconcile(), "SQUARE_OFF")


def test_a_position_left_open_past_the_session_is_a_missed_square_off() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    report = s.reconcile()
    assert "SQUARE_OFF_MISSED" in codes(report, "SQUARE_OFF")


def test_a_missed_square_off_across_a_restart_is_recovered_next_session() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.restart("2026-10-02T09:05:00")  # the app was down over the 15:20 window (drill R-4b)
    s.exit("nifty-50", "2026-10-02T09:20:10", 99.0, kind="SQUARE_OFF")
    s.range = (s.range[0], datetime(2026, 10, 2, 16, 0))
    report = s.reconcile()
    assert status(report, "SQUARE_OFF") == "PASS", report["findings"]
    assert report["checks"]["SQUARE_OFF"]["observations"] == {"missed_during_outage": 1, "recoveries": 1}


def test_a_prior_day_position_closed_by_a_strategy_exit_is_not_a_recovery() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.restart("2026-10-02T09:05:00")
    s.exit("nifty-50", "2026-10-02T09:40:10", 110.0, kind="EXIT_TARGET")
    s.range = (s.range[0], datetime(2026, 10, 2, 16, 0))
    assert "RECOVERY_NOT_FIRST" in codes(s.reconcile(), "SQUARE_OFF")


# ---------------- decisions vs recorded state ----------------


def test_a_cooldown_block_names_the_exact_engine_boundary() -> None:
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.exit("nifty-50", "10:30:05", 110.0)  # engine exit 10:30:00
    s.blocked("sensex", "10:31:00", "Cooldown active until 10:35:00")
    assert status(s.reconcile(), "DECISION_STATE") == "PASS"
    s.blocked("sensex", "10:32:00", "Cooldown active until 10:36:00")  # wrong boundary
    assert "COOLDOWN_UNTIL_MISMATCH" in codes(s.reconcile(), "DECISION_STATE")


def test_a_kill_switch_block_without_the_kill_switch_fails() -> None:
    s = Session()
    s.blocked("sensex", "10:00:30", "Emergency kill switch is engaged (Manually engaged)")
    assert "DECISION_STATE_MISMATCH" in codes(s.reconcile(), "DECISION_STATE")


def test_a_cutoff_block_before_the_cutoff_fails() -> None:
    s = Session()
    s.blocked("sensex", "14:30:00", "Past entry cutoff - no new positions may be opened")
    assert "DECISION_TIME_MISMATCH" in codes(s.reconcile(), "DECISION_STATE")


def test_an_unblocking_control_during_the_entry_cycle_is_unverifiable() -> None:
    s = Session()
    s.control("10:00:00", "DISABLE")
    s.control("10:00:20", "ENABLE")  # re-enabled while the next cycle may already have been checking
    s.entry("nifty-50", "10:00:30")
    assert "ENTRY_STATE_CHANGED_IN_CYCLE" in codes(s.reconcile(), "ENTRY_RULES")


# ---------------- no false PASS on missing evidence ----------------


def test_missing_state_fields_never_pass() -> None:
    s = normal_session()
    for row in s.tables["risk_state_events"] + s.tables["paper_decision_events"]:
        for name in ("auto_trading_enabled", "kill_switch", "consecutive_loss_halt", "consecutive_losses",
                     "last_exit_at"):
            row.pop(name, None)
    report = s.reconcile()
    for check in ("ENTRY_RULES", "HALT", "EXIT_RULES"):
        assert status(report, check) == "UNVERIFIABLE", check
    assert "ENTRY_STATE_FIELDS_MISSING" in codes(report, "ENTRY_RULES")
    # Without last_exit_at the cooldown boundary cannot be rebuilt: never a PASS.
    assert "COOLDOWN_SOURCE_UNKNOWN" in codes(report, "DECISION_STATE")
    assert status(report, "DECISION_STATE") == "UNVERIFIABLE"


def test_an_unreconciled_group_leaves_the_rules_unverifiable() -> None:
    s = Session()
    s.entry("nifty-50", "10:00:30")
    s.tables["risk_state_events"].clear()  # the entry's risk row is missing
    report = s.reconcile()
    assert "ENTRY_STATE_UNKNOWN" in codes(report, "ENTRY_RULES") and status(report, "ENTRY_RULES") != "PASS"


# ---------------- D1: exits at the exact SL/target level ----------------


def rising(n=60):
    index = pd.date_range("2026-09-23 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + 6.0 * i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": [c + 4 for c in closes], "Low": [c - 4 for c in closes],
                         "Close": closes, "Volume": [0.0] * n}, index=index)


FULL = rising()
EVENTS = run_strategy(FULL, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")[1]
ENTRY = next(e for e in EVENTS if e.kind == "ENTRY_CALL")
EXIT = next(e for e in EVENTS if e.kind == "EXIT_TARGET" and e.timestamp > ENTRY.timestamp)


def canonical_trade(exit_price=None, *, with_bars=True):
    s = Session(day=date(2026, 9, 23))
    s.range = (datetime(2026, 9, 23, 9, 0), datetime(2026, 9, 23, 16, 0))
    entry_at = (ENTRY.timestamp + timedelta(minutes=5, seconds=30)).tz_localize(None)
    exit_at = (EXIT.timestamp + timedelta(minutes=5, seconds=30)).tz_localize(None)
    s.entry("nifty-50", entry_at.isoformat(), ENTRY.underlying_price, bar=ENTRY.timestamp.tz_localize(None))
    s.exit("nifty-50", exit_at.isoformat(), EXIT.exit_level if exit_price is None else exit_price,
           bar=EXIT.timestamp.tz_localize(None))
    captures = None
    if with_bars:
        windows = []
        for observed in (entry_at, exit_at):
            moment = (observed - timedelta(seconds=20)).replace(tzinfo=IST)
            analysis = bars.analyze(FULL.loc[FULL.index <= moment], moment, reconcile.BAR_LENGTH)
            windows.append(bars.CaptureWindow("nifty-50", moment, {b["bar_start"]: b for b in analysis["bars"]}))
        captures = {"nifty-50": windows}
    return s.reconcile(captures=captures)


def test_an_exit_at_the_reconstructed_target_verifies() -> None:
    assert EXIT.exit_level == ENTRY.target  # the canonical exit fills at the entry's own target
    report = canonical_trade()
    assert status(report, "S1_REPLAY") == "PASS" and status(report, "D1_EXIT_LEVEL") == "PASS"
    assert report["checks"]["D1_EXIT_LEVEL"]["evaluated"] == 1


def test_an_exit_off_the_reconstructed_level_is_unreconciled_even_inside_the_bar_range() -> None:
    report = canonical_trade(exit_price=EXIT.exit_level - 1.0)  # still inside the exit bar's high-low range
    assert "EXIT_NOT_AT_RECONSTRUCTED_LEVEL" in codes(report, "D1_EXIT_LEVEL")
    assert "EXIT_PRICE_NOT_IN_EVIDENCE" not in codes(report, "BARS")  # the range check alone would have passed


def test_without_bar_evidence_the_exact_level_is_unverifiable() -> None:
    report = canonical_trade(with_bars=False)
    assert status(report, "D1_EXIT_LEVEL") == "UNVERIFIABLE"
    assert "SL_TP_NOT_RECONSTRUCTIBLE" in codes(report, "D1_EXIT_LEVEL")


def test_a_square_off_later_than_the_first_tick_is_unreconciled() -> None:
    s = Session()
    s.entry("nifty-50", "14:00:30")
    s.exit("nifty-50", "15:28:30", 101.0, kind="SQUARE_OFF")  # in the window, but past 15:20 + 300 s + 120 s
    report = s.reconcile()
    assert "SQUARE_OFF_NOT_FIRST_TICK" in codes(report, "SQUARE_OFF") and status(report, "SQUARE_OFF") == "UNRECONCILED"
