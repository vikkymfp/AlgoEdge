"""Read-only Phase 8.1 reconciliation: exported database events (+ optional
captured bars and status samples) -> signal / risk decision / order / fill /
position / exit / P&L / risk state / audit / dashboard.

Rules (Phase 8.1 protocol section 3.7):
- A PLACED or FAILED paper order, and every paper decision other than
  ORDER_FAILED, anchors one transaction group. Its members are the rows of
  the same index (risk_state_events has no index column) whose created_at
  is within GROUP_TOLERANCE (1 s) of the anchor.
- Exactly one member of each expected kind is required. None -> FAIL
  (missing persistence row). More than one, or a row two anchors could both
  claim -> UNRECONCILED: the tool never guesses and never repairs a row;
  the rows involved are listed as evidence.
- Positions, R1 (global max open positions), C2 (duplicate fills), R2
  (entries_today/trades_today/realized_pnl_today), realized P&L in
  underlying points, audit rows, bar evidence (D1/D2/D4), the canonical
  entry replay (S1) and the dashboard samples are then checked against
  that reconstruction.
- Risk rules: entry window/cutoff, cooldown, daily cap and entries while
  restricted (R5), the consecutive-loss halt (R5), exits under entry
  restrictions (R3), square-off timing and missed-square-off recovery (R4),
  blocked decisions against their recorded state, and exits at the exact
  reconstructed SL/target level (D1).

Engine clock: a cycle takes `now` when it starts and commits its rows when
it ends. Exits persist their engine time (risk_state_events.last_exit_at);
entries do not, so an entry's engine time is only known to lie in
[created_at - engine_clock_tolerance, created_at]. A time rule is FAIL only
when violated for every time in that interval, PASS only when satisfied for
every time in it, and UNVERIFIABLE otherwise.

Every check ends PASS, FAIL, UNRECONCILED or UNVERIFIABLE (not enough
evidence). Any FAIL of severity P0 sets `stop_campaign`.

    PYTHONPATH=src:. python -m research.phase8.tools.reconcile --db <extract dir> [--bars <dir>] [--status <dir>] \
        --out research/phase8/evidence/<campaign>/reconcile
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from algoedge.auto_trader import SIGNAL_FRESHNESS_BARS
from algoedge.risk_manager import RiskLimits
from fno_signals.config import INDEX_MAP, strategy_config_for
from fno_signals.strategy import drop_invalid_bars
from fno_signals.strategy import run as run_strategy
from research.phase8.tools.bars import INDEX_CHOICE, CaptureWindow, load_capture_windows
from research.phase8.tools.common import (
    IST,
    EvidenceFile,
    new_run_id,
    read_jsonl,
    sha256_file,
    utc_now,
    verify_record,
)

SCHEMA = "phase8.reconcile.v1"
AUTO_SOURCE = "algoedge.auto_trader"
GROUP_TOLERANCE = timedelta(seconds=1)
BAR_LENGTH = timedelta(minutes=5)
ENTRY_KINDS = {"ENTRY_CALL": "CALL", "ENTRY_PUT": "PUT"}
EXIT_KINDS = {"EXIT_SL", "EXIT_TARGET", "SQUARE_OFF"}
DECISIONS = {"BLOCKED", "EXPIRED", "SKIPPED", "ORDER_FAILED", "SQUARE_OFF_PENDING"}
MONEY_TOLERANCE = 1e-6
ENGINE_CLOCK_TOLERANCE = timedelta(seconds=120)  # assumed upper bound on a cycle's start-to-commit time
LEVEL_EXITS = ("EXIT_SL", "EXIT_TARGET")
# The scheduler sleeps this long between ticks (algoedge.web_server.SCHEDULER_TICK_SECONDS; not imported -
# importing web_server starts the app). The first tick after 15:20 therefore starts within one tick.
SCHEDULER_TICK = timedelta(seconds=300)
RESTRICTION_REASONS = ("Emergency kill switch is engaged", "Trading halted after", "Auto trading is disabled")
CONTROL_EVENTS = {"ENABLE", "DISABLE", "KILL_SWITCH_ON", "KILL_SWITCH_OFF", "CONSECUTIVE_LOSS_HALT_RESET"}
UNBLOCKING_EVENTS = {"ENABLE", "KILL_SWITCH_OFF", "CONSECUTIVE_LOSS_HALT_RESET"}
BEGINNING = datetime(1970, 1, 1, tzinfo=IST)  # "before the range" in a position timeline

CHECKS = ("LIVE_ORDERS", "P1_GROUPING", "TRANSITIONS", "R1_MAX_OPEN", "C2_DUPLICATES", "R2_COUNTERS",
          "PNL", "AUDIT", "BARS", "S1_REPLAY", "DASHBOARD", "ENTRY_RULES", "HALT", "EXIT_RULES",
          "SQUARE_OFF", "DECISION_STATE", "D1_EXIT_LEVEL")
# Which Phase 8.1 criteria each check provides evidence for (protocol section 6).
PROTOCOL_CRITERIA = {
    "LIVE_ORDERS": ["scope"], "P1_GROUPING": ["P1", "P3", "S3", "A1"], "TRANSITIONS": ["P2", "R4"],
    "R1_MAX_OPEN": ["R1", "C1"], "C2_DUPLICATES": ["C2"], "R2_COUNTERS": ["R2"], "PNL": ["P&L"],
    "AUDIT": ["A1", "A2"], "BARS": ["D1", "D2", "D4"], "S1_REPLAY": ["S1", "D3"], "DASHBOARD": ["UI1", "UI2", "UI3", "R1"],
    "ENTRY_RULES": ["R5", "R1"], "HALT": ["R5"], "EXIT_RULES": ["R3"], "SQUARE_OFF": ["R4"],
    "DECISION_STATE": ["R3", "R5"], "D1_EXIT_LEVEL": ["D1"],
}


# ---------------------------------------------------------------- inputs


@dataclass
class Extraction:
    manifest: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]
    baseline: dict[str, Any]


def db_time(value: Any) -> datetime | None:
    """A database timestamp (naive, server local = IST under A3) as aware IST."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.replace(tzinfo=IST) if moment.tzinfo is None else moment.astimezone(IST)


def load_extraction(directory: Path) -> Extraction:
    """Loads an extract.py directory, refusing any file whose SHA-256 no
    longer matches the manifest."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, meta in manifest["tables"].items():
        path = directory / meta["file"]
        if sha256_file(path) != meta["sha256"]:
            raise ValueError(f"{path.name}: sha256 does not match the manifest")
        tables[table] = read_jsonl(path)
    baseline_path = directory / manifest["baseline_file"]["file"]
    if sha256_file(baseline_path) != manifest["baseline_file"]["sha256"]:
        raise ValueError("baseline.json: sha256 does not match the manifest")
    return Extraction(manifest, tables, json.loads(baseline_path.read_text(encoding="utf-8")))


def load_status_samples(directory: Path) -> tuple[list[dict[str, Any]], int]:
    """Collector records whose self-hash still verifies, and how many did not."""
    samples, tampered = [], 0
    for path in sorted(directory.glob("status_*.jsonl")):
        for record in read_jsonl(path):
            if not verify_record(record):
                tampered += 1
                continue
            samples.append(record)
    samples.sort(key=lambda record: record["collected_at"]["utc"])
    return samples, tampered


# ---------------------------------------------------------------- results


@dataclass
class Finding:
    check: str
    status: str  # FAIL | UNRECONCILED | UNVERIFIABLE
    code: str
    detail: str
    severity: str | None = None  # P0..P3 for FAIL
    index_id: str | None = None
    at: str | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class Group:
    anchor_table: str
    anchor: dict[str, Any]
    index_id: str
    at: datetime
    expected: tuple[str, ...]
    candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    status: str = "RECONCILED"  # RECONCILED | INCOMPLETE | UNRECONCILED | INCONSISTENT

    def member(self, kind: str) -> dict[str, Any] | None:
        rows = self.candidates.get(kind) or []
        return rows[0] if self.status == "RECONCILED" and len(rows) == 1 else None

    def ref(self) -> str:
        return f"{self.anchor_table}#{self.anchor['id']}"


def ref(table: str, row: dict[str, Any]) -> str:
    return f"{table}#{row['id']}"


@dataclass
class Fill:
    order: dict[str, Any]
    index_id: str
    at: datetime
    is_entry: bool
    group: Group | None
    kind: str | None = None  # from the group's signal when reconciled
    qty_after: int | None = None
    side_after: str | None = None


class Reconciler:
    def __init__(self, extraction: Extraction, *, captures: dict[str, list[CaptureWindow]] | None = None,
                 status_samples: list[dict[str, Any]] | None = None, tampered_samples: int = 0,
                 max_open_positions: int | None = None, status_lag: timedelta = timedelta(seconds=65),
                 engine_clock_tolerance: timedelta = ENGINE_CLOCK_TOLERANCE) -> None:
        self.x = extraction
        self.captures = captures
        self.samples = status_samples
        self.tampered_samples = tampered_samples
        self.limits = RiskLimits()
        self.max_open = max_open_positions if max_open_positions is not None else self.limits.max_open_positions
        self.status_lag = status_lag
        self.clock_tol = engine_clock_tolerance
        self.findings: list[Finding] = []
        self.observations: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.replayed: dict[int, Any] = {}  # entry order id -> the matching canonical TradeEvent
        self.evaluated: dict[str, int] = defaultdict(int)
        self.groups: list[Group] = []
        self.fills: list[Fill] = []
        self.timeline: dict[str, list[tuple[datetime, int, str | None]]] = defaultdict(list)
        self.counters: list[tuple[datetime, Any, int, int]] = []  # (at, day, entries, trades) after each fill

    # -- helpers
    def add(self, check: str, status: str, code: str, detail: str, *, severity: str | None = None,
            index_id: str | None = None, at: datetime | None = None, evidence: list[str] | None = None) -> None:
        self.findings.append(Finding(check, status, code, detail, severity, index_id,
                                     at.isoformat() if at else None, evidence or []))

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.x.tables.get(table, [])

    # -- 1. live orders
    def check_live_orders(self) -> None:
        for order in self.rows("orders"):
            self.evaluated["LIVE_ORDERS"] += 1
            if order.get("live"):
                self.add("LIVE_ORDERS", "FAIL", "LIVE_ORDER", "a live (real) order exists in the campaign range",
                         severity="P0", index_id=order.get("index_id"), at=db_time(order["created_at"]),
                         evidence=[ref("orders", order)])

    # -- 2. transaction groups
    def build_groups(self) -> None:
        signals = [r for r in self.rows("strategy_signals") if r.get("source") == AUTO_SOURCE]
        orders = [r for r in self.rows("orders") if r.get("source") == AUTO_SOURCE and not r.get("live")]
        risk = [r for r in self.rows("risk_state_events")
                if r.get("scope") == "paper" and r.get("event") == "TRADE_RECORDED"]
        snaps = [r for r in self.rows("auto_trade_account_snapshots")
                 if r.get("event") in ENTRY_KINDS or r.get("event") in EXIT_KINDS]
        decisions = self.rows("paper_decision_events")
        failed = [r for r in decisions if r.get("decision") == "ORDER_FAILED"]

        def near(pool, at, index_id=None):
            return [row for row in pool
                    if abs(db_time(row["created_at"]) - at) <= GROUP_TOLERANCE
                    and (index_id is None or row.get("index_id") == index_id)]

        for order in orders:
            at, index_id = db_time(order["created_at"]), order.get("index_id")
            placed = order.get("outcome") == "PLACED"
            group = Group("orders", order, index_id, at,
                          ("signal", "risk", "snapshot") if placed else ("signal", "risk", "order_failed"))
            group.candidates = {"signal": near(signals, at, index_id), "risk": near(risk, at)}
            if placed:
                group.candidates["snapshot"] = near(snaps, at, index_id)
            else:
                group.candidates["order_failed"] = near(failed, at, index_id)
            self.groups.append(group)
        for decision in decisions:
            if decision.get("decision") == "ORDER_FAILED":
                continue
            at, index_id = db_time(decision["created_at"]), decision.get("index_id")
            expected = ("signal",) if decision.get("event_kind") else ()
            group = Group("paper_decision_events", decision, index_id, at, expected)
            if expected:
                group.candidates = {"signal": near(signals, at, index_id)}
            self.groups.append(group)

        claims: dict[tuple[str, int], list[Group]] = defaultdict(list)
        table_of = {"signal": "strategy_signals", "risk": "risk_state_events",
                    "snapshot": "auto_trade_account_snapshots", "order_failed": "paper_decision_events"}
        for group in self.groups:
            for kind, rows in group.candidates.items():
                for row in rows:
                    claims[(table_of[kind], row["id"])].append(group)

        for group in self.groups:
            self.evaluated["P1_GROUPING"] += 1
            evidence = [group.ref()] + [ref(table_of[k], r) for k, rows in group.candidates.items() for r in rows]
            missing = [kind for kind in group.expected if not group.candidates.get(kind)]
            multiple = [kind for kind in group.expected if len(group.candidates.get(kind, [])) > 1]
            shared = [kind for kind in group.expected for row in group.candidates.get(kind, [])
                      if len(claims[(table_of[kind], row["id"])]) > 1]
            if multiple or shared:
                group.status = "UNRECONCILED"
                self.add("P1_GROUPING", "UNRECONCILED", "AMBIGUOUS_GROUP",
                         f"more than one candidate row within {GROUP_TOLERANCE.total_seconds():g}s for "
                         f"{sorted(set(multiple + shared))} - not guessed", index_id=group.index_id,
                         at=group.at, evidence=evidence)
            elif missing:
                group.status = "INCOMPLETE"
                self.add("P1_GROUPING", "FAIL", "MISSING_PERSISTENCE_ROW",
                         f"transaction group lacks {missing}", severity="P0", index_id=group.index_id,
                         at=group.at, evidence=evidence)
            else:
                self._check_consistency(group, evidence)

        claimed = set(claims)
        orphan_pools = [("strategy_signals", signals), ("risk_state_events", risk),
                        ("auto_trade_account_snapshots", snaps), ("paper_decision_events", failed)]
        for table, pool in orphan_pools:
            for row in pool:
                if (table, row["id"]) not in claimed:
                    self.add("P1_GROUPING", "FAIL", "ORPHAN_ROW",
                             "row belongs to no order/decision transaction group", severity="P1",
                             index_id=row.get("index_id"), at=db_time(row["created_at"]),
                             evidence=[ref(table, row)])

    def _check_consistency(self, group: Group, evidence: list[str]) -> None:
        if group.anchor_table != "orders":
            return
        signal = group.candidates["signal"][0]
        kind = signal.get("action")
        side = group.anchor.get("side")
        problems = []
        if kind in ENTRY_KINDS and side != "BUY" or kind in EXIT_KINDS and side != "SELL":
            problems.append(f"order side {side} does not fit signal {kind}")
        if kind not in ENTRY_KINDS and kind not in EXIT_KINDS:
            problems.append(f"signal action {kind!r} is not a paper fill kind")
        snapshot = (group.candidates.get("snapshot") or [None])[0]
        if snapshot is not None and snapshot.get("event") != kind:
            problems.append(f"snapshot event {snapshot.get('event')} != signal {kind}")
        if problems:
            group.status = "INCONSISTENT"
            self.add("P1_GROUPING", "FAIL", "GROUP_INCONSISTENT", "; ".join(problems), severity="P1",
                     index_id=group.index_id, at=group.at, evidence=evidence)

    # -- 3. positions, P&L, R1, C2
    def build_positions(self) -> None:
        group_by_order = {g.anchor["id"]: g for g in self.groups if g.anchor_table == "orders"}
        placed = [o for o in self.rows("orders")
                  if o.get("source") == AUTO_SOURCE and not o.get("live") and o.get("outcome") == "PLACED"]
        for order in placed:
            group = group_by_order.get(order["id"])
            signal = group.member("signal") if group else None
            self.fills.append(Fill(order, order.get("index_id"), db_time(order["created_at"]),
                                   order.get("side") == "BUY", group, signal.get("action") if signal else None))
        self.fills.sort(key=lambda fill: (fill.at, fill.order["id"]))

        state: dict[str, dict[str, Any]] = {}
        for index_id in sorted(INDEX_CHOICE):
            snap = self.x.baseline.get(f"account_snapshot:{index_id}")
            # No snapshot before the range = this index never filled (one is
            # written on every PLACED fill) = flat.
            state[index_id] = {"qty": int(snap["quantity"]) if snap else 0, "side": snap.get("side") if snap else None,
                               "avg": snap.get("average_price") if snap else None}
            self.timeline[index_id].append((BEGINNING, state[index_id]["qty"],
                                            state[index_id]["side"]))

        for fill in self.fills:
            self.evaluated["TRANSITIONS"] += 1
            self.evaluated["PNL"] += 1
            current = state.setdefault(fill.index_id, {"qty": 0, "side": None, "avg": None})
            order = fill.order
            qty, price = int(order.get("quantity") or 0), float(order["price"])
            pnl = float(order.get("realized_pnl") or 0.0)
            evidence = [ref("orders", order)]
            if fill.is_entry:
                if current["qty"] > 0:
                    self.add("TRANSITIONS", "FAIL", "ENTRY_WHILE_HOLDING",
                             f"entry while already holding {current['qty']}", severity="P0",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                if abs(pnl) > MONEY_TOLERANCE:
                    self.add("PNL", "FAIL", "ENTRY_WITH_PNL", f"entry realized_pnl {pnl} != 0", severity="P1",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                side = ENTRY_KINDS.get(fill.kind) if fill.kind else None
                total = current["qty"] + qty
                avg = price if current["qty"] == 0 else ((current["avg"] or price) * current["qty"] + price * qty) / total
                current.update(qty=total, side=side, avg=avg)
            else:
                if current["qty"] <= 0:
                    self.add("TRANSITIONS", "FAIL", "EXIT_WHILE_FLAT", "exit fill while flat", severity="P0",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                    current.update(qty=0, side=None, avg=None)
                else:
                    closed = min(qty, current["qty"])
                    if current["side"] not in ("CALL", "PUT") or current["avg"] is None:
                        self.add("PNL", "UNVERIFIABLE", "PNL_SIDE_UNKNOWN",
                                 "position side/entry price unknown (entry group not reconciled)",
                                 index_id=fill.index_id, at=fill.at, evidence=evidence)
                    else:
                        direction = 1 if current["side"] == "CALL" else -1
                        expected = (price - current["avg"]) * closed * direction
                        if abs(expected - pnl) > MONEY_TOLERANCE * max(1.0, abs(expected)):
                            self.add("PNL", "FAIL", "PNL_MISMATCH",
                                     f"realized_pnl {pnl} != ({price} - {current['avg']}) x {closed} x "
                                     f"{direction} = {expected} (underlying points)", severity="P1",
                                     index_id=fill.index_id, at=fill.at, evidence=evidence)
                    remaining = current["qty"] - closed
                    current.update(qty=remaining, side=current["side"] if remaining else None,
                                   avg=current["avg"] if remaining else None)
            fill.qty_after, fill.side_after = current["qty"], current["side"]
            self.timeline[fill.index_id].append((fill.at, current["qty"], current["side"]))
            snapshot = fill.group.member("snapshot") if fill.group else None
            if snapshot is not None:
                expected_state = (current["qty"], current["side"] if current["qty"] else None)
                actual = (int(snapshot["quantity"]), snapshot.get("side"))
                if current["side"] is None and current["qty"]:
                    expected_state = (current["qty"], actual[1])  # side unknowable without a reconciled entry
                if actual != expected_state:
                    self.add("TRANSITIONS", "FAIL", "POSITION_MISMATCH",
                             f"account snapshot {actual} != reconstructed {expected_state}", severity="P0",
                             index_id=fill.index_id, at=fill.at,
                             evidence=evidence + [ref("auto_trade_account_snapshots", snapshot)])

    def qty_at(self, index_id: str, at: datetime) -> tuple[int, bool]:
        """Reconstructed quantity at `at`, and whether a fill of that index
        within the grouping tolerance makes it ambiguous."""
        points = self.timeline.get(index_id) or [(BEGINNING, 0, None)]
        qty = points[0][1]
        ambiguous = False
        for moment, quantity, _side in points[1:]:
            if abs(moment - at) <= GROUP_TOLERANCE:
                ambiguous = True
            if moment <= at:
                qty = quantity
        return qty, ambiguous

    def check_max_open(self) -> None:
        holding: dict[str, int] = {i: self.timeline[i][0][1] for i in self.timeline}
        for position, fill in enumerate(self.fills):
            if fill.is_entry:
                self.evaluated["R1_MAX_OPEN"] += 1
                open_now = {i for i, q in holding.items() if q > 0 and i != fill.index_id}
                later_exits = {f.index_id for f in self.fills[position + 1:]
                               if not f.is_entry and f.index_id != fill.index_id and f.at - fill.at <= GROUP_TOLERANCE}
                earlier_exits = {f.index_id for f in self.fills[:position]
                                 if not f.is_entry and f.index_id != fill.index_id and fill.at - f.at <= GROUP_TOLERANCE}
                lowest = len(open_now - later_exits) + 1
                highest = len(open_now | earlier_exits) + 1
                evidence = [ref("orders", fill.order)]
                if lowest > self.max_open:
                    self.add("R1_MAX_OPEN", "FAIL", "MAX_OPEN_POSITIONS",
                             f"{lowest} positions open after this entry (max {self.max_open}); "
                             f"also open: {sorted(open_now)}", severity="P0", index_id=fill.index_id,
                             at=fill.at, evidence=evidence)
                elif highest > self.max_open:
                    self.add("R1_MAX_OPEN", "UNRECONCILED", "MAX_OPEN_ORDER_AMBIGUOUS",
                             f"entry within {GROUP_TOLERANCE.total_seconds():g}s of another index's exit - "
                             "order cannot be proven from timestamps", index_id=fill.index_id, at=fill.at,
                             evidence=evidence)
            holding[fill.index_id] = fill.qty_after or 0

    def check_duplicates(self) -> None:
        seen: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for snap in self.rows("auto_trade_account_snapshots"):
            kind = snap.get("event")
            if kind in ENTRY_KINDS or kind in EXIT_KINDS:
                self.evaluated["C2_DUPLICATES"] += 1
                seen[(snap.get("index_id"), kind, str(snap.get("last_event_at")))].append(snap)
        for (index_id, kind, bar), rows in seen.items():
            if len(rows) > 1:
                self.add("C2_DUPLICATES", "FAIL", "DUPLICATE_FILL",
                         f"{kind} for bar {bar} filled {len(rows)} times", severity="P0", index_id=index_id,
                         at=db_time(rows[1]["created_at"]),
                         evidence=[ref("auto_trade_account_snapshots", r) for r in rows])

    # -- 4. R2 counters
    def check_counters(self) -> None:
        baseline = self.x.baseline.get("risk_state")
        day = None
        entries = trades = 0
        pnl = 0.0
        known = True
        failed_groups = [g for g in self.groups if g.anchor_table == "orders"
                         and g.anchor.get("outcome") != "PLACED" and g.member("risk") is not None]
        events = sorted(
            [("fill", f.at, f) for f in self.fills]
            + [("failed", g.at, g) for g in failed_groups]
            + [("decision", db_time(d["created_at"]), d) for d in self.rows("paper_decision_events")],
            key=lambda item: item[1])
        if events and baseline is not None:
            first_day = events[0][1].date().isoformat()
            if baseline.get("trade_day") == first_day:
                day = first_day
                known = baseline.get("entries_today") is not None
                entries = int(baseline.get("entries_today") or 0)
                trades = int(baseline.get("trades_today") or 0)
                pnl = float(baseline.get("realized_pnl_today") or 0.0)
        for kind, at, item in events:
            today = at.date().isoformat()
            if kind == "failed":
                # A FAILED fill records no trade: the cycle's risk row keeps the counters.
                risk = item.member("risk")
                self.evaluated["R2_COUNTERS"] += 1
                if today == day and known and (risk.get("entries_today"), risk.get("trades_today")) != (entries, trades):
                    self.add("R2_COUNTERS", "FAIL", "COUNTER_CHANGED_BY_FAILED_FILL",
                             f"risk row {(risk.get('entries_today'), risk.get('trades_today'))} after a FAILED fill "
                             f"!= {(entries, trades)}", severity="P1", index_id=item.index_id, at=at,
                             evidence=[item.ref(), ref("risk_state_events", risk)])
                continue
            if kind == "fill":
                if today != day:
                    day, entries, trades, pnl, known = today, 0, 0, 0.0, True
                trades += 1
                entries += 1 if item.is_entry else 0
                pnl += float(item.order.get("realized_pnl") or 0.0)
                self.counters.append((at, day, entries, trades))
                risk = item.group.member("risk") if item.group else None
                if risk is None:
                    continue
                self.evaluated["R2_COUNTERS"] += 1
                evidence = [ref("orders", item.order), ref("risk_state_events", risk)]
                if risk.get("trade_day") != day:
                    self.add("R2_COUNTERS", "FAIL", "TRADE_DAY_MISMATCH",
                             f"risk trade_day {risk.get('trade_day')} != fill day {day}", severity="P1",
                             index_id=item.index_id, at=at, evidence=evidence)
                if not known:
                    self.add("R2_COUNTERS", "UNVERIFIABLE", "LEGACY_COUNTER",
                             "day started from a legacy snapshot without entries_today", at=at, evidence=evidence)
                    continue
                actual = (risk.get("entries_today"), risk.get("trades_today"))
                if actual != (entries, trades):
                    self.add("R2_COUNTERS", "FAIL", "COUNTER_MISMATCH",
                             f"(entries_today, trades_today) {actual} != expected {(entries, trades)}",
                             severity="P1", index_id=item.index_id, at=at, evidence=evidence)
                if abs(float(risk.get("realized_pnl_today") or 0.0) - pnl) > MONEY_TOLERANCE * max(1.0, abs(pnl)):
                    self.add("R2_COUNTERS", "FAIL", "DAILY_PNL_MISMATCH",
                             f"realized_pnl_today {risk.get('realized_pnl_today')} != sum of fills {pnl}",
                             severity="P1", index_id=item.index_id, at=at, evidence=evidence)
            else:
                decision = item
                if decision.get("decision") == "ORDER_FAILED":
                    continue  # recorded in the same cycle as its FAILED order; counters unchanged
                self.evaluated["R2_COUNTERS"] += 1
                actual = (decision.get("entries_today"), decision.get("trades_today"))
                if today == day:
                    allowed = {(entries, trades)} if known else None
                else:
                    # Lazy day reset (existing behavior): until a risk check runs
                    # on the new day, the in-memory counters still hold the
                    # previous day's values.
                    allowed = {(0, 0)} | ({(entries, trades)} if day is not None and known else set())
                if allowed is None:
                    continue
                if actual not in allowed:
                    self.add("R2_COUNTERS", "FAIL", "DECISION_COUNTER_MISMATCH",
                             f"{decision.get('decision')} recorded {actual}, expected one of {sorted(allowed)} "
                             "(a blocked/expired/skipped decision never changes the counters)", severity="P1",
                             index_id=decision.get("index_id"), at=at,
                             evidence=[ref("paper_decision_events", decision)])

    # -- 5. audit
    def check_audit(self) -> None:
        for decision in self.rows("paper_decision_events"):
            self.evaluated["AUDIT"] += 1
            at, index_id = db_time(decision["created_at"]), decision.get("index_id")
            evidence = [ref("paper_decision_events", decision)]
            if decision.get("decision") not in DECISIONS:
                self.add("AUDIT", "FAIL", "UNKNOWN_DECISION", f"decision {decision.get('decision')!r}",
                         severity="P1", index_id=index_id, at=at, evidence=evidence)
            if not (decision.get("reason") or "").strip():
                self.add("AUDIT", "FAIL", "EMPTY_REASON", "audit reason is empty", severity="P1",
                         index_id=index_id, at=at, evidence=evidence)
            qty, ambiguous = self.qty_at(index_id, at)
            if ambiguous:
                continue
            if decision.get("open_quantity") != qty:
                self.add("AUDIT", "FAIL", "AUDIT_POSITION_MISMATCH",
                         f"open_quantity {decision.get('open_quantity')} != reconstructed {qty}", severity="P1",
                         index_id=index_id, at=at, evidence=evidence)
            if decision.get("reason") == "Max open positions reached":
                others = [(i, *self.qty_at(i, at)) for i in self.timeline if i != index_id]
                if any(amb for _i, _q, amb in others):
                    continue
                open_others = sum(1 for _i, q, _a in others if q > 0)
                if open_others < self.max_open:
                    self.add("AUDIT", "FAIL", "BLOCK_REASON_INCONSISTENT",
                             f"blocked for max open positions but only {open_others} other position(s) open",
                             severity="P1", index_id=index_id, at=at, evidence=evidence)

    # -- 6. bars: D1/D2/D4 and S1 replay
    def _capture_at(self, index_id: str, at: datetime) -> CaptureWindow | None:
        chosen = None
        for capture in (self.captures or {}).get(index_id, []):
            if capture.observed_at <= at:
                chosen = capture
        return chosen

    def _bar_everywhere(self, index_id: str, bar_start: str) -> list[dict[str, Any]]:
        return [c.bars[bar_start] for c in (self.captures or {}).get(index_id, []) if bar_start in c.bars]

    def check_bars(self) -> None:
        if self.captures is None:
            return
        for fill in self.fills:
            snapshot = fill.group.member("snapshot") if fill.group else None
            if snapshot is None or fill.kind is None:
                self.add("BARS", "UNVERIFIABLE", "NO_RECONCILED_GROUP", "fill group not reconciled",
                         index_id=fill.index_id, at=fill.at, evidence=[ref("orders", fill.order)])
                continue
            self.evaluated["BARS"] += 1
            bar_at = db_time(snapshot.get("last_event_at"))
            key = pd.Timestamp(bar_at).isoformat()
            price = float(fill.order["price"])
            evidence = [ref("orders", fill.order), ref("auto_trade_account_snapshots", snapshot)]
            observed = self._bar_everywhere(fill.index_id, key)
            if not observed:
                self.add("BARS", "UNVERIFIABLE", "BAR_NOT_CAPTURED", f"bar {key} not in bar evidence",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
                continue
            if fill.kind in ENTRY_KINDS:
                if bar_at + BAR_LENGTH > fill.at:
                    self.add("BARS", "FAIL", "FORMING_BAR_ENTRY",
                             f"entry from bar {key} that had not closed at fill time", severity="P0",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                if any(not bar["valid"] for bar in observed if bar["state"] == "completed"):
                    self.add("BARS", "FAIL", "INVALID_BAR_FILL", f"bar {key} was captured invalid",
                             severity="P0", index_id=fill.index_id, at=fill.at, evidence=evidence)
                if not any(_same(bar["close"], price) for bar in observed if bar["state"] == "completed"):
                    self.add("BARS", "UNRECONCILED", "ENTRY_PRICE_NOT_IN_EVIDENCE",
                             f"entry price {price} is not the captured close of bar {key}",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
            elif fill.kind == "SQUARE_OFF":
                if not any(_same(bar["close"], price) for bar in observed):
                    self.add("BARS", "UNVERIFIABLE", "SQUARE_OFF_PRICE_NOT_CAPTURED",
                             f"square-off price {price} is not a captured close of bar {key} "
                             "(latest-price fill; capture resolution)", index_id=fill.index_id, at=fill.at,
                             evidence=evidence)
            else:
                in_range = any(_num(bar["low"]) <= price <= _num(bar["high"]) for bar in observed)
                capture = self._capture_at(fill.index_id, fill.at)
                latest_close = [bar["close"] for bar in (capture.bars.values() if capture else [])][-1:]
                if not in_range and not any(_same(close, price) for close in latest_close):
                    self.add("BARS", "UNRECONCILED", "EXIT_PRICE_NOT_IN_EVIDENCE",
                             f"exit price {price} outside captured range of bar {key}", index_id=fill.index_id,
                             at=fill.at, evidence=evidence)

    def check_replay(self) -> None:
        if self.captures is None:
            return
        max_age = BAR_LENGTH * SIGNAL_FRESHNESS_BARS
        restarts = sorted(db_time(a["created_at"]) for a in self.rows("alert_events")
                          if a.get("category") == "SYSTEM_RESTART")
        snaps = sorted(self.rows("auto_trade_account_snapshots"), key=lambda r: (r["created_at"], r["id"]))
        for fill in self.fills:
            if not fill.is_entry:
                continue
            snapshot = fill.group.member("snapshot") if fill.group else None
            capture = self._capture_at(fill.index_id, fill.at)
            if snapshot is None or fill.kind is None or capture is None:
                self.add("S1_REPLAY", "UNVERIFIABLE", "REPLAY_INPUT_MISSING",
                         "no reconciled group or no bar capture before the fill", index_id=fill.index_id,
                         at=fill.at, evidence=[ref("orders", fill.order)])
                continue
            self.evaluated["S1_REPLAY"] += 1
            high_water = self._high_water(fill, snaps, restarts)
            frame = _frame(capture.bars)
            completed = frame.loc[[ts + BAR_LENGTH <= fill.at for ts in frame.index]] if len(frame) else frame
            index_config = INDEX_MAP[INDEX_CHOICE[fill.index_id]]
            config = strategy_config_for(index_config)
            chosen = None
            for _ in range(len(completed) + 1):  # the engine's expire-then-retry loop for a flat account
                kwargs = {} if high_water is None else {"start_after": pd.Timestamp(high_water)}
                events = run_strategy(completed, config, underlying_label=index_config.name, **kwargs)[1] \
                    if len(completed) else []
                pending = [e for e in events if high_water is None or e.timestamp > pd.Timestamp(high_water)]
                if not pending:
                    break
                candidate = pending[0]
                if fill.at - (candidate.timestamp.to_pydatetime() + BAR_LENGTH) <= max_age:
                    chosen = candidate
                    break
                high_water = candidate.timestamp.to_pydatetime()
            bar_at = db_time(snapshot.get("last_event_at"))
            evidence = [ref("orders", fill.order), ref("auto_trade_account_snapshots", snapshot)]
            if chosen is not None and chosen.kind == fill.kind and chosen.timestamp == pd.Timestamp(bar_at) \
                    and _same(chosen.underlying_price, float(fill.order["price"])):
                self.replayed[fill.order["id"]] = chosen
            else:
                got = None if chosen is None else (chosen.kind, chosen.timestamp.isoformat(), chosen.underlying_price)
                self.add("S1_REPLAY", "UNRECONCILED", "REPLAY_MISMATCH",
                         f"canonical replay on captured bars gives {got}, paper filled "
                         f"{(fill.kind, bar_at.isoformat(), fill.order['price'])}", index_id=fill.index_id,
                         at=fill.at, evidence=evidence)

    def _high_water(self, fill: Fill, snaps: list[dict[str, Any]], restarts: list[datetime]) -> datetime | None:
        """The engine's in-memory account.last_event_at just before this
        fill's cycle: the last persisted fill bar of the index, advanced by
        EXPIRED/SKIPPED decisions audited since the last restart (a restart
        restores only the persisted snapshot)."""
        mark = db_time((self.x.baseline.get(f"account_snapshot:{fill.index_id}") or {}).get("last_event_at"))
        for snap in snaps:
            if snap.get("index_id") == fill.index_id and db_time(snap["created_at"]) < fill.at - GROUP_TOLERANCE:
                mark = db_time(snap.get("last_event_at")) or mark
        last_restart = max((r for r in restarts if r < fill.at), default=None)
        for decision in self.rows("paper_decision_events"):
            at = db_time(decision["created_at"])
            if (decision.get("index_id") == fill.index_id and decision.get("decision") in ("EXPIRED", "SKIPPED")
                    and at < fill.at - GROUP_TOLERANCE and (last_restart is None or at > last_restart)
                    and decision.get("event_at")):
                event_at = db_time(decision["event_at"])
                mark = event_at if mark is None or event_at > mark else mark
        return mark

    # -- 7. dashboard
    def check_dashboard(self) -> None:
        if self.samples is None:
            return
        if self.tampered_samples:
            self.add("DASHBOARD", "FAIL", "TAMPERED_SAMPLES",
                     f"{self.tampered_samples} status record(s) fail their own sha256", severity="P0")
        expected_limits = {
            "dailyLossLimit": self.limits.daily_loss_limit, "maxTradesPerDay": self.limits.max_trades_per_day,
            "maxOpenPositions": self.limits.max_open_positions, "maxQuantity": self.limits.max_quantity,
            "tradingStart": self.limits.trading_start.isoformat(), "tradingEnd": self.limits.trading_end.isoformat(),
            "entryCutoff": self.limits.entry_cutoff.isoformat(),
            "squareOffTime": self.limits.square_off_time.isoformat(),
            "maxConsecutiveLosses": self.limits.max_consecutive_losses,
            "cooldownMinutes": self.limits.cooldown_minutes,
        }
        gaps = 0
        for sample in self.samples:
            if not sample.get("ok"):
                gaps += 1
                continue
            self.evaluated["DASHBOARD"] += 1
            at = datetime.fromisoformat(sample["collected_at"]["utc"]).astimezone(IST)
            data = sample["status"]["extracted"]
            evidence = [f"status:{sample['run_id']}#{sample['seq']}"]
            open_count = data["openPositions"]["count"]
            if open_count is not None and open_count > self.max_open:
                self.add("DASHBOARD", "FAIL", "MAX_OPEN_OBSERVED",
                         f"{open_count} open positions in memory: {data['openPositions']['indexIds']}",
                         severity="P0", at=at, evidence=evidence)
            limits = data.get("limits") or {}
            changed = {k: (limits.get(k), v) for k, v in expected_limits.items() if limits.get(k) != v}
            if changed:
                self.add("DASHBOARD", "FAIL", "LIMITS_CHANGED", f"limits differ from frozen values: {changed}",
                         severity="P1", at=at, evidence=evidence)
            for index_id, account in sorted((data.get("accounts") or {}).items()):
                if self._near_fill(index_id, at):
                    continue
                qty, _ambiguous = self.qty_at(index_id, at)
                side = self._side_at(index_id, at)
                if account.get("quantity") != qty or (qty and side is not None and account.get("side") != side):
                    self.add("DASHBOARD", "FAIL", "DASHBOARD_POSITION_MISMATCH",
                             f"dashboard ({account.get('quantity')}, {account.get('side')}) != persisted "
                             f"({qty}, {side})", severity="P0", index_id=index_id, at=at, evidence=evidence)
            if not any(abs(fill.at - at) <= self.status_lag for fill in self.fills):
                allowed = self._entries_allowed_at(at)
                if allowed is not None and data.get("entriesToday") not in allowed:
                    self.add("DASHBOARD", "FAIL", "DASHBOARD_ENTRIES_MISMATCH",
                             f"entriesToday {data.get('entriesToday')} not in expected {sorted(allowed)}",
                             severity="P1", at=at, evidence=evidence)
        if gaps:
            self.add("DASHBOARD", "UNVERIFIABLE", "COLLECTOR_GAPS", f"{gaps} status sample(s) not usable (ok=false)")

    def _near_fill(self, index_id: str, at: datetime) -> bool:
        return any(f.index_id == index_id and abs(f.at - at) <= self.status_lag for f in self.fills)

    def _side_at(self, index_id: str, at: datetime) -> str | None:
        side = None
        for moment, _qty, point_side in self.timeline.get(index_id, []):
            if moment <= at:
                side = point_side
        return side

    def _entries_allowed_at(self, at: datetime) -> set[int] | None:
        last = None
        for point in self.counters:
            if point[0] <= at:
                last = point
        if last is None:
            return None  # counters before the first fill in range are not reconstructed
        if last[1] == at.date().isoformat():
            return {last[2]}
        return {0, last[2]}  # lazy day reset

    # -- 8. risk rules: shared evidence helpers
    def _restrictions(self, row: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Entry restrictions active in a recorded state row, and any of the
        three state fields that are missing (never read as "inactive")."""
        active, missing = [], []
        for name, field_name, blocked_when in (("auto trading disabled", "auto_trading_enabled", False),
                                                ("kill switch", "kill_switch", True),
                                                ("consecutive-loss halt", "consecutive_loss_halt", True)):
            value = row.get(field_name)
            if value is None:
                missing.append(field_name)
            elif bool(value) is blocked_when:
                active.append(name)
        return active, missing

    def _state_changes_near(self, at: datetime, events: set[str], *, other_exits_of: str | None = None) -> list[str]:
        """Control rows (and optionally other indices' exits, which can trip
        the halt) that landed between a cycle's engine time and its commit."""
        lo, hi = at - self.clock_tol, at + GROUP_TOLERANCE
        found = [ref("risk_state_events", r) for r in self.rows("risk_state_events")
                 if r.get("scope") == "paper" and r.get("event") in events and lo <= db_time(r["created_at"]) <= hi]
        if other_exits_of is not None:
            found += [ref("orders", f.order) for f in self.fills
                      if not f.is_entry and f.index_id != other_exits_of and lo <= f.at <= hi]
        return found

    def _exit_engine_time(self, fill: Fill) -> tuple[datetime, datetime]:
        """An exit's engine time: exact from its risk row's last_exit_at,
        else only bounded by its commit time."""
        risk = fill.group.member("risk") if fill.group else None
        exact = db_time(risk.get("last_exit_at")) if risk else None
        return (exact, exact) if exact is not None else (fill.at - self.clock_tol, fill.at)

    def _last_exit_before(self, at: datetime) -> tuple[str, Any]:
        """('fill', Fill) | ('baseline', datetime) | ('none', None) |
        ('ambiguous', Fill) for the exit whose last_exit_at the engine held at `at`."""
        prior = [f for f in self.fills if not f.is_entry and f.at <= at + GROUP_TOLERANCE]
        if prior:
            latest = max(prior, key=lambda f: (f.at, f.order["id"]))
            return ("ambiguous" if abs(latest.at - at) <= GROUP_TOLERANCE else "fill"), latest
        baseline = db_time((self.x.baseline.get("risk_state") or {}).get("last_exit_at"))
        return ("baseline", baseline) if baseline is not None else ("none", None)

    # -- 8a. R5 entry rules (+ R1 via R1_MAX_OPEN)
    def check_entry_rules(self) -> None:
        limits, check = self.limits, "ENTRY_RULES"
        cooldown = timedelta(minutes=limits.cooldown_minutes)
        baseline = self.x.baseline.get("risk_state") or {}
        day, count, known, first_day = None, 0, True, True
        for fill in self.fills:
            if not fill.is_entry:
                continue
            self.evaluated[check] += 1
            at, earliest = fill.at, fill.at - self.clock_tol
            evidence = [ref("orders", fill.order)]
            same_day = earliest.date() == at.date()
            # Trading window and entry cutoff (engine: now.time() in [09:15, 15:00]).
            if at.time() < limits.trading_start:
                self.add(check, "FAIL", "ENTRY_BEFORE_OPEN", f"entry committed {at.time()} < {limits.trading_start}",
                         severity="P0", index_id=fill.index_id, at=at, evidence=evidence)
            elif same_day and earliest.time() < limits.trading_start:
                self.add(check, "UNVERIFIABLE", "ENTRY_OPEN_BOUNDARY",
                         "entry within the engine-clock tolerance of the session open", index_id=fill.index_id,
                         at=at, evidence=evidence)
            if same_day and earliest.time() > limits.entry_cutoff:
                self.add(check, "FAIL", "ENTRY_AFTER_CUTOFF",
                         f"entry committed {at.time()}: engine time after the {limits.entry_cutoff} cutoff even "
                         f"allowing {self.clock_tol.total_seconds():g}s", severity="P0", index_id=fill.index_id,
                         at=at, evidence=evidence)
            elif at.time() > limits.entry_cutoff:
                self.add(check, "UNVERIFIABLE", "ENTRY_CUTOFF_BOUNDARY",
                         f"entry committed {at.time()}, within the engine-clock tolerance of the cutoff",
                         index_id=fill.index_id, at=at, evidence=evidence)
            else:
                self.observations[check]["entries_before_cutoff"] += 1
            # Daily new-entry cap (engine: blocked when entries_today >= max_trades_per_day).
            today = at.date().isoformat()
            if today != day:
                day, count, known = today, 0, True
                if first_day and baseline.get("trade_day") == today:
                    count = baseline.get("entries_today")
                    known = count is not None
                    count = int(count or 0)
                first_day = False
            if not known:
                self.add(check, "UNVERIFIABLE", "CAP_COUNT_UNKNOWN",
                         "the day started from a legacy risk row without entries_today", at=at, evidence=evidence)
            elif count >= limits.max_trades_per_day:
                self.add(check, "FAIL", "ENTRY_OVER_DAILY_CAP",
                         f"entry #{count + 1} of {today}; the cap is {limits.max_trades_per_day} new entries",
                         severity="P0", index_id=fill.index_id, at=at, evidence=evidence)
            count += 1
            # Cooldown after the most recent exit (engine: blocked while now < last_exit_at + cooldown).
            kind, prior = self._last_exit_before(at)
            if kind == "ambiguous":
                self.add(check, "UNVERIFIABLE", "COOLDOWN_ORDER_AMBIGUOUS",
                         f"an exit committed within {GROUP_TOLERANCE.total_seconds():g}s of this entry",
                         index_id=fill.index_id, at=at, evidence=evidence + [ref("orders", prior.order)])
            elif kind in ("fill", "baseline"):
                lo_x, hi_x = self._exit_engine_time(prior) if kind == "fill" else (prior, prior)
                if at < lo_x + cooldown:
                    self.add(check, "FAIL", "ENTRY_IN_COOLDOWN",
                             f"entry committed {at.isoformat()} before the cooldown ended at "
                             f"{(lo_x + cooldown).isoformat()}", severity="P0", index_id=fill.index_id, at=at,
                             evidence=evidence)
                elif earliest < hi_x + cooldown:
                    self.add(check, "UNVERIFIABLE", "ENTRY_COOLDOWN_BOUNDARY",
                             f"the cooldown ended at {(hi_x + cooldown).isoformat()}, inside this entry's "
                             "engine-clock tolerance - the entry's engine time is not persisted",
                             index_id=fill.index_id, at=at, evidence=evidence)
                else:
                    self.observations[check]["entries_clear_of_cooldown"] += 1
            # No entry while disabled / kill-switched / halted, read from the entry's own risk row.
            risk = fill.group.member("risk") if fill.group else None
            if risk is None:
                self.add(check, "UNVERIFIABLE", "ENTRY_STATE_UNKNOWN", "entry group not reconciled: state unknown",
                         index_id=fill.index_id, at=at, evidence=evidence)
                continue
            active, missing = self._restrictions(risk)
            evidence.append(ref("risk_state_events", risk))
            if missing:
                self.add(check, "UNVERIFIABLE", "ENTRY_STATE_FIELDS_MISSING", f"risk row lacks {missing}",
                         index_id=fill.index_id, at=at, evidence=evidence)
            elif active:
                nearby = self._state_changes_near(at, CONTROL_EVENTS, other_exits_of=fill.index_id)
                if nearby:
                    self.add(check, "UNVERIFIABLE", "ENTRY_STATE_CHANGED_IN_CYCLE",
                             f"{active} recorded, but the state changed during the cycle: {nearby}",
                             index_id=fill.index_id, at=at, evidence=evidence + nearby)
                else:
                    self.add(check, "FAIL", "ENTRY_WHILE_RESTRICTED", f"new entry filled with {active} active",
                             severity="P0", index_id=fill.index_id, at=at, evidence=evidence)
            else:
                nearby = self._state_changes_near(at, UNBLOCKING_EVENTS)
                if nearby:
                    self.add(check, "UNVERIFIABLE", "ENTRY_STATE_CHANGED_IN_CYCLE",
                             f"an unblocking control event landed during the cycle: {nearby}",
                             index_id=fill.index_id, at=at, evidence=evidence + nearby)

    # -- 8b. R5 consecutive-loss halt
    def check_halt(self) -> None:
        check, limit = "HALT", self.limits.max_consecutive_losses
        baseline = self.x.baseline.get("risk_state")
        if baseline is None:
            losses, halt, known = 0, False, True  # no row before the range: a fresh campaign database
        else:
            losses, halt = baseline.get("consecutive_losses"), baseline.get("consecutive_loss_halt")
            known = losses is not None and halt is not None
            losses, halt = int(losses or 0), bool(halt)
        effects: dict[int, tuple[str, Any]] = {}
        for group in self.groups:
            risk = group.member("risk")
            if risk is None:
                continue
            signal = group.member("signal")
            placed = group.anchor.get("outcome") == "PLACED"
            is_exit = placed and signal is not None and signal.get("action") in EXIT_KINDS
            effects[risk["id"]] = ("exit", group) if is_exit else ("none", group)
        events = sorted(
            [("risk", db_time(r["created_at"]), r) for r in self.rows("risk_state_events") if r.get("scope") == "paper"]
            + [("decision", db_time(d["created_at"]), d) for d in self.rows("paper_decision_events")],
            key=lambda item: (item[1], item[2]["id"]))
        trips = 0

        def resync(row: dict[str, Any]) -> tuple[int, bool, bool]:
            recorded_l, recorded_h = row.get("consecutive_losses"), row.get("consecutive_loss_halt")
            if recorded_l is None or recorded_h is None:
                return 0, False, False
            return int(recorded_l), bool(recorded_h), True

        for kind, at, row in events:
            evidence = [ref("risk_state_events" if kind == "risk" else "paper_decision_events", row)]
            tripped_here = False
            if kind == "risk":
                event = row.get("event")
                if event == "CONSECUTIVE_LOSS_HALT_RESET":
                    losses, halt = 0, False
                    self.observations[check]["resets"] += 1
                elif event == "TRADE_RECORDED":
                    effect = effects.get(row["id"])
                    if effect is None:
                        self.add(check, "UNVERIFIABLE", "TRADE_ROW_NOT_RECONCILED",
                                 "a trade's risk row is not in a reconciled group - streak re-synced from it",
                                 at=at, evidence=evidence)
                        losses, halt, known = resync(row)
                        continue
                    if effect[0] == "exit":
                        group = effect[1]
                        concurrent = [f for f in self.fills if not f.is_entry and f.order["id"] != group.anchor["id"]
                                      and abs(f.at - group.at) <= GROUP_TOLERANCE]
                        if concurrent:
                            self.add(check, "UNVERIFIABLE", "CONCURRENT_EXITS",
                                     "two exits committed within the grouping tolerance - streak order unknown",
                                     at=at, evidence=evidence + [ref("orders", f.order) for f in concurrent])
                            losses, halt, known = resync(row)
                            continue
                        pnl = float(group.anchor.get("realized_pnl") or 0.0)
                        if pnl < 0:
                            losses += 1
                            if losses >= limit and not halt:
                                halt, tripped_here = True, True
                        elif pnl > 0:
                            losses = 0
                elif event not in CONTROL_EVENTS:
                    self.observations[check][f"other_event:{event}"] += 1
            recorded_h = row.get("consecutive_loss_halt")
            recorded_l = row.get("consecutive_losses") if kind == "risk" else losses
            if recorded_h is None or recorded_l is None:
                self.add(check, "UNVERIFIABLE", "HALT_FIELDS_MISSING", "row lacks the halt/streak fields", at=at,
                         evidence=evidence)
                continue
            if not known:
                if kind == "risk":
                    losses, halt, known = resync(row)
                continue
            self.evaluated[check] += 1
            if halt and not bool(recorded_h):
                code = "HALT_NOT_TRIPPED" if tripped_here else "HALT_CLEARED_WITHOUT_RESET"
                self.add(check, "FAIL", code, f"expected the halt set ({losses} consecutive losses, limit {limit}); "
                         "recorded clear", severity="P0", at=at, evidence=evidence)
            elif not halt and bool(recorded_h):
                self.add(check, "FAIL", "HALT_UNEXPECTED", f"halt recorded set with {losses} consecutive losses",
                         severity="P1", at=at, evidence=evidence)
            elif int(recorded_l) != losses:
                self.add(check, "FAIL", "LOSS_STREAK_MISMATCH", f"consecutive_losses {recorded_l} != expected {losses}",
                         severity="P1", at=at, evidence=evidence)
            elif tripped_here:
                trips += 1
            if kind == "risk":
                losses, halt, known = resync(row)  # continue from the recorded state after any finding
            if bool(recorded_h) and kind == "decision" and row.get("event_kind") in ENTRY_KINDS:
                self.observations[check]["entries_blocked_or_audited_while_halted"] += 1
        self.observations[check]["halt_trips_verified"] = trips
        if trips == 0:
            self.add(check, "UNVERIFIABLE", "HALT_TRIP_NOT_EXERCISED",
                     f"no {limit}th consecutive loss in range - the trip itself was not exercised")

    # -- 8c. R3 exits are never blocked by entry restrictions
    def check_exit_rules(self) -> None:
        check = "EXIT_RULES"
        for fill in self.fills:
            if fill.is_entry:
                continue
            risk = fill.group.member("risk") if fill.group else None
            if risk is None:
                continue
            active, missing = self._restrictions(risk)
            if active and not missing:
                self.evaluated[check] += 1
                self.observations[check]["exits_filled_while_restricted"] += 1
        for decision in self.rows("paper_decision_events"):
            if decision.get("event_kind") not in LEVEL_EXITS or decision.get("decision") != "BLOCKED":
                continue
            self.evaluated[check] += 1
            reason = decision.get("reason") or ""
            if reason.startswith(RESTRICTION_REASONS):
                self.add(check, "FAIL", "EXIT_BLOCKED_BY_ENTRY_RESTRICTION",
                         f"a risk-reducing exit was blocked: {reason!r}", severity="P0",
                         index_id=decision.get("index_id"), at=db_time(decision["created_at"]),
                         evidence=[ref("paper_decision_events", decision)])
            else:
                self.observations[check][f"exit_blocked:{reason.split(' - ')[0][:40]}"] += 1
        if not self.observations[check]["exits_filled_while_restricted"]:
            self.add(check, "UNVERIFIABLE", "EXIT_UNDER_RESTRICTION_NOT_EXERCISED",
                     "no exit was filled while an entry restriction was active")

    # -- 8d. R4 square-off timing and missed-square-off recovery
    def check_square_off(self) -> None:
        check, limits = "SQUARE_OFF", self.limits
        entry_bar: dict[str, datetime | None] = {}
        for index_id in self.timeline:
            snap = self.x.baseline.get(f"account_snapshot:{index_id}")
            entry_bar[index_id] = db_time(snap.get("last_event_at")) if snap and int(snap["quantity"]) > 0 else None
        fills_by_index: dict[str, list[Fill]] = defaultdict(list)
        for fill in self.fills:
            fills_by_index[fill.index_id].append(fill)
        for fill in self.fills:
            evidence = [ref("orders", fill.order)]
            if fill.is_entry:
                snap = fill.group.member("snapshot") if fill.group else None
                entry_bar[fill.index_id] = db_time(snap.get("last_event_at")) if snap else None
                continue
            bar = entry_bar.get(fill.index_id)
            entry_bar[fill.index_id] = None
            stale = bar is not None and bar.astimezone(IST).date() < fill.at.date()
            if fill.kind is None:
                if stale:
                    self.add(check, "UNVERIFIABLE", "RECOVERY_KIND_UNKNOWN",
                             "a prior-day position was closed by a fill whose kind is not reconciled",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                continue
            if stale and fill.kind != "SQUARE_OFF":
                self.add(check, "FAIL", "RECOVERY_NOT_FIRST",
                         f"a position from {bar.date()} was closed by {fill.kind}, not the missed-square-off "
                         "recovery", severity="P1", index_id=fill.index_id, at=fill.at, evidence=evidence)
            if fill.kind != "SQUARE_OFF":
                continue
            self.evaluated[check] += 1
            if bar is None:
                self.add(check, "UNVERIFIABLE", "SQUARE_OFF_ENTRY_UNKNOWN", "the closed position's entry bar is unknown",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
                continue
            earliest = fill.at - self.clock_tol
            if stale:  # missed-square-off recovery: any in-session cycle, before anything else that day
                self.observations[check]["recoveries"] += 1
                earlier_today = [f for f in fills_by_index[fill.index_id]
                                 if f.at.date() == fill.at.date() and f.at < fill.at]
                if earlier_today:
                    self.add(check, "FAIL", "RECOVERY_NOT_FIRST", "another fill of this index preceded the recovery",
                             severity="P1", index_id=fill.index_id, at=fill.at,
                             evidence=evidence + [ref("orders", f.order) for f in earlier_today])
                window_start, window_name = limits.trading_start, "session open"
            else:
                self.observations[check]["same_day_square_offs"] += 1
                window_start, window_name = limits.square_off_time, "square-off time"
                snap = fill.group.member("snapshot") if fill.group else None
                if snap is None:
                    self.add(check, "UNVERIFIABLE", "SQUARE_OFF_SNAPSHOT_MISSING", "no account snapshot for the fill",
                             index_id=fill.index_id, at=fill.at, evidence=evidence)
                elif snap.get("square_off_date") != fill.at.date().isoformat():
                    self.add(check, "FAIL", "SQUARE_OFF_DATE_NOT_RECORDED",
                             f"square_off_date {snap.get('square_off_date')!r} != {fill.at.date()}", severity="P1",
                             index_id=fill.index_id, at=fill.at,
                             evidence=evidence + [ref("auto_trade_account_snapshots", snap)])
            if fill.at.time() < window_start:
                self.add(check, "FAIL", "SQUARE_OFF_TOO_EARLY", f"forced close committed {fill.at.time()} before the "
                         f"{window_name} {window_start}", severity="P1", index_id=fill.index_id, at=fill.at,
                         evidence=evidence)
            elif earliest.date() == fill.at.date() and earliest.time() > limits.trading_end:
                self.add(check, "FAIL", "SQUARE_OFF_AFTER_SESSION", f"forced close committed {fill.at.time()}, after "
                         f"{limits.trading_end} even allowing the engine-clock tolerance", severity="P1",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
            elif fill.at.time() > limits.trading_end:
                self.add(check, "UNVERIFIABLE", "SQUARE_OFF_SESSION_BOUNDARY",
                         "forced close within the engine-clock tolerance of the session end",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
            if not stale:
                first_tick_by = datetime.combine(fill.at.date(), limits.square_off_time, tzinfo=IST) \
                    + SCHEDULER_TICK + self.clock_tol
                if fill.at > first_tick_by:
                    self.add(check, "UNRECONCILED", "SQUARE_OFF_NOT_FIRST_TICK",
                             f"forced close committed {fill.at.time()}, later than the first scheduler tick after "
                             f"{limits.square_off_time} could commit ({first_tick_by.time()}): review logs/alerts for "
                             "failed or missing cycles", index_id=fill.index_id, at=fill.at, evidence=evidence)
        self._check_missed_square_offs()
        if not self.evaluated[check]:
            self.add(check, "UNVERIFIABLE", "SQUARE_OFF_NOT_EXERCISED", "no forced close in range")

    def _check_missed_square_offs(self) -> None:
        check, limits = "SQUARE_OFF", self.limits
        span = self.x.manifest.get("range") or {}
        start, end = db_time(span.get("start")), db_time(span.get("end"))
        if start is None or end is None:
            self.add(check, "UNVERIFIABLE", "RANGE_UNKNOWN", "extraction range unknown: missed square-offs not checked")
            return
        restarts = sorted(db_time(a["created_at"]) for a in self.rows("alert_events")
                          if a.get("category") == "SYSTEM_RESTART")
        for index_id, points in self.timeline.items():
            intervals, opened = [], None
            for moment, qty, _side in points:
                if qty > 0 and opened is None:
                    opened = moment
                elif qty == 0 and opened is not None:
                    intervals.append((opened, moment))
                    opened = None
            if opened is not None:
                intervals.append((opened, None))
            for opened, closed in intervals:
                day = max(opened, start).date()
                last_day = (closed or end).date()
                while day <= last_day:
                    cutoff = datetime.combine(day, limits.square_off_time, tzinfo=IST)
                    deadline = datetime.combine(day, limits.trading_end, tzinfo=IST) + self.clock_tol
                    if (day.weekday() < 5 and opened < cutoff and start <= cutoff and end >= deadline
                            and (closed is None or closed > deadline)):
                        outage = [r for r in restarts if cutoff < r <= (closed or end)]
                        if outage:
                            self.observations[check]["missed_during_outage"] += 1
                            if closed is None:
                                self.add(check, "UNVERIFIABLE", "RECOVERY_NOT_IN_RANGE",
                                         f"square-off of {day} missed across a restart; no recovery fill in range",
                                         index_id=index_id, at=deadline)
                        else:
                            self.add(check, "FAIL", "SQUARE_OFF_MISSED",
                                     f"position still open after {limits.trading_end} on {day} with no restart "
                                     "in between", severity="P1", index_id=index_id, at=deadline)
                        break  # the first missed day per position; recovery is checked on the closing fill
                    day += timedelta(days=1)

    # -- 8e. blocked decisions agree with their recorded state
    def check_decision_state(self) -> None:
        check, limits = "DECISION_STATE", self.limits
        cooldown = timedelta(minutes=limits.cooldown_minutes)
        for decision in self.rows("paper_decision_events"):
            if decision.get("decision") != "BLOCKED":
                continue
            reason = decision.get("reason") or ""
            at = db_time(decision["created_at"])
            evidence = [ref("paper_decision_events", decision)]
            index_id = decision.get("index_id")

            def need(field_name: str, predicate, description: str) -> None:
                value = decision.get(field_name)
                if value is None:
                    self.add(check, "UNVERIFIABLE", "DECISION_FIELD_MISSING", f"{field_name} missing for {reason!r}",
                             index_id=index_id, at=at, evidence=evidence)
                elif not predicate(value):
                    self.add(check, "FAIL", "DECISION_STATE_MISMATCH",
                             f"blocked for {reason!r} but {field_name}={value!r} ({description})", severity="P1",
                             index_id=index_id, at=at, evidence=evidence)
                else:
                    self.evaluated[check] += 1

            if reason.startswith("Emergency kill switch is engaged"):
                need("kill_switch", bool, "the kill switch was not engaged")
            elif reason.startswith("Trading halted after"):
                need("consecutive_loss_halt", bool, "the halt was not set")
            elif reason == "Auto trading is disabled":
                need("auto_trading_enabled", lambda v: not bool(v), "auto trading was enabled")
            elif reason == "Max trades per day reached":
                need("entries_today", lambda v: int(v) >= limits.max_trades_per_day, "under the daily cap")
            elif reason == "Daily loss limit reached":
                need("realized_pnl_today", lambda v: float(v) <= -abs(limits.daily_loss_limit), "above the loss limit")
            elif reason.startswith("Past entry cutoff"):
                self._timed_decision(check, at.time() > limits.entry_cutoff, "decision committed before the cutoff",
                                     index_id, at, evidence)
            elif reason == "Outside configured trading hours":
                earliest = at - self.clock_tol
                inside = at.time() <= limits.trading_end and earliest.date() == at.date() \
                    and earliest.time() >= limits.trading_start
                self._timed_decision(check, not inside, "engine time was certainly inside trading hours",
                                     index_id, at, evidence)
            elif reason.startswith("Cooldown active until "):
                self._cooldown_decision(check, reason, cooldown, index_id, at, evidence)
            else:
                self.observations[check][f"other:{reason.split(' (')[0][:40]}"] += 1

    def _timed_decision(self, check: str, consistent: bool, why: str, index_id: str | None, at: datetime,
                        evidence: list[str]) -> None:
        self.evaluated[check] += 1
        if not consistent:
            self.add(check, "FAIL", "DECISION_TIME_MISMATCH", why, severity="P1", index_id=index_id, at=at,
                     evidence=evidence)

    def _cooldown_decision(self, check: str, reason: str, cooldown: timedelta, index_id: str | None, at: datetime,
                           evidence: list[str]) -> None:
        until_text = reason.removeprefix("Cooldown active until ").strip()
        kind, prior = self._last_exit_before(at)
        exact = None
        if kind == "fill":
            lo_x, hi_x = self._exit_engine_time(prior)
            exact = lo_x if lo_x == hi_x else None
            evidence = evidence + [ref("orders", prior.order)]
        elif kind == "baseline":
            exact = prior
        if exact is None:
            self.add(check, "UNVERIFIABLE", "COOLDOWN_SOURCE_UNKNOWN",
                     f"the last exit's engine time is not known ({kind})", index_id=index_id, at=at, evidence=evidence)
            return
        expected = (exact + cooldown).astimezone(IST).time().isoformat()
        self.evaluated[check] += 1
        if until_text != expected:
            self.add(check, "FAIL", "COOLDOWN_UNTIL_MISMATCH",
                     f"blocked until {until_text}, but last exit {exact.isoformat()} + {cooldown} = {expected}",
                     severity="P1", index_id=index_id, at=at, evidence=evidence)
        elif at - self.clock_tol >= exact + cooldown:
            self.add(check, "FAIL", "DECISION_TIME_MISMATCH", "decision's engine time was certainly after the cooldown",
                     severity="P1", index_id=index_id, at=at, evidence=evidence)

    # -- 8f. D1 exits at the exact SL/target level
    def check_exit_levels(self) -> None:
        check = "D1_EXIT_LEVEL"
        max_age = BAR_LENGTH * SIGNAL_FRESHNESS_BARS
        restarts = sorted(db_time(a["created_at"]) for a in self.rows("alert_events")
                          if a.get("category") == "SYSTEM_RESTART")
        last_entry: dict[str, Fill | None] = defaultdict(lambda: None)
        for fill in self.fills:
            if fill.is_entry:
                last_entry[fill.index_id] = fill
                continue
            entry, last_entry[fill.index_id] = last_entry[fill.index_id], None
            if fill.kind not in LEVEL_EXITS:
                continue
            evidence = [ref("orders", fill.order)]
            if self.captures is None:
                self.add(check, "UNVERIFIABLE", "SL_TP_NOT_RECONSTRUCTIBLE",
                         "SL/target levels are not persisted and there is no bar evidence to rebuild them; "
                         "only the supporting bar-range check (BARS) applies", index_id=fill.index_id, at=fill.at,
                         evidence=evidence)
                continue
            if entry is None:
                self.add(check, "UNVERIFIABLE", "ENTRY_OUTSIDE_RANGE", "the position's entry fill is not in range",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
                continue
            chosen = self.replayed.get(entry.order["id"])
            if chosen is None:
                self.add(check, "UNVERIFIABLE", "ENTRY_REPLAY_NOT_MATCHED",
                         "the entry's canonical replay did not match, so its levels cannot be rebuilt",
                         index_id=fill.index_id, at=fill.at, evidence=evidence + [ref("orders", entry.order)])
                continue
            snap = fill.group.member("snapshot") if fill.group else None
            bar_at = db_time(snap.get("last_event_at")) if snap else None
            if bar_at is not None and fill.at - (bar_at + BAR_LENGTH) > max_age:
                self.add(check, "UNVERIFIABLE", "LATE_EXIT",
                         "a late exit fills at the current price by design - no level to verify",
                         index_id=fill.index_id, at=fill.at, evidence=evidence)
                continue
            level = chosen.stop_loss if fill.kind == "EXIT_SL" else chosen.target
            price = float(fill.order["price"])
            if _same(level, price):
                self.evaluated[check] += 1
                continue
            restarted = any(entry.at < r < fill.at for r in restarts)
            self.add(check, "UNRECONCILED", "EXIT_NOT_AT_RECONSTRUCTED_LEVEL",
                     f"{fill.kind} filled at {price}, reconstructed level {level} (entry {entry.order['price']} at "
                     f"{chosen.timestamp.isoformat()})" + (" - levels re-derived after a restart" if restarted else ""),
                     index_id=fill.index_id, at=fill.at, evidence=evidence + [ref("orders", entry.order)])

    # -- run
    def run(self) -> dict[str, Any]:
        self.check_live_orders()
        self.build_groups()
        self.build_positions()
        self.check_max_open()
        self.check_duplicates()
        self.check_counters()
        self.check_audit()
        self.check_bars()
        self.check_replay()
        self.check_dashboard()
        self.check_entry_rules()
        self.check_halt()
        self.check_exit_rules()
        self.check_square_off()
        self.check_decision_state()
        self.check_exit_levels()
        return self.report()

    def report(self) -> dict[str, Any]:
        checks = {}
        for check in CHECKS:
            items = [f for f in self.findings if f.check == check]
            statuses = {f.status for f in items}
            if "FAIL" in statuses:
                status = "FAIL"
            elif "UNRECONCILED" in statuses:
                status = "UNRECONCILED"
            elif "UNVERIFIABLE" in statuses or not self.evaluated.get(check):
                status = "UNVERIFIABLE"
            else:
                status = "PASS"
            checks[check] = {"status": status, "evaluated": self.evaluated.get(check, 0),
                             "findings": len(items), "criteria": PROTOCOL_CRITERIA[check],
                             "observations": dict(sorted(self.observations[check].items()))}
        p0 = [f for f in self.findings if f.status == "FAIL" and f.severity == "P0"]
        return {
            "schema": SCHEMA,
            "inputs": {"db_extract_run": self.x.manifest.get("run_id"), "range": self.x.manifest.get("range"),
                       "bars": self.captures is not None, "status_samples": None if self.samples is None
                       else len(self.samples)},
            "parameters": {"group_tolerance_seconds": GROUP_TOLERANCE.total_seconds(),
                           "engine_clock_tolerance_seconds": self.clock_tol.total_seconds(),
                           "max_open_positions": self.max_open,
                           "status_lag_seconds": self.status_lag.total_seconds()},
            "checks": checks,
            "stop_campaign": bool(p0),
            "findings": [f.__dict__ for f in self.findings],
            "groups": {"total": len(self.groups),
                       "by_status": {s: sum(1 for g in self.groups if g.status == s)
                                     for s in ("RECONCILED", "INCOMPLETE", "UNRECONCILED", "INCONSISTENT")}},
            "fills": len(self.fills),
        }


def _num(value: Any) -> float:
    return float("nan") if isinstance(value, str) else float(value)


def _same(value: Any, price: float) -> bool:
    number = _num(value)
    return math.isfinite(number) and abs(number - price) <= MONEY_TOLERANCE * max(1.0, abs(price))


def _frame(bars: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """A captured window as the engine's DataFrame (invalid bars dropped by
    the engine's own rule, exactly as run_cycle does)."""
    if not bars:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    index = pd.DatetimeIndex([pd.Timestamp(key) for key in bars])
    frame = pd.DataFrame({
        "Open": [_num(b["open"]) for b in bars.values()], "High": [_num(b["high"]) for b in bars.values()],
        "Low": [_num(b["low"]) for b in bars.values()], "Close": [_num(b["close"]) for b in bars.values()],
        "Volume": [_num(b["volume"]) if b["volume"] is not None else 0.0 for b in bars.values()],
    }, index=index)
    return drop_invalid_bars(frame)


def reconcile(db_dir: Path, *, bars_dir: Path | None = None, status_dir: Path | None = None,
              max_open_positions: int | None = None) -> dict[str, Any]:
    captures = load_capture_windows(sorted(bars_dir.glob("bars_*.jsonl"))) if bars_dir else None
    samples, tampered = load_status_samples(status_dir) if status_dir else (None, 0)
    return Reconciler(load_extraction(db_dir), captures=captures, status_samples=samples,
                      tampered_samples=tampered, max_open_positions=max_open_positions).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="an extract.py output directory")
    parser.add_argument("--bars", type=Path, default=None)
    parser.add_argument("--status", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = reconcile(args.db, bars_dir=args.bars, status_dir=args.status)
    started = utc_now()
    with EvidenceFile(args.out, "reconcile", new_run_id(), started) as evidence:
        evidence.append(report)
        path = evidence.path
    for check, result in report["checks"].items():
        print(f"{check:14} {result['status']:13} evaluated={result['evaluated']} findings={result['findings']}")
    print(f"stop_campaign={report['stop_campaign']}  report={path}")
    return 0 if all(result["status"] == "PASS" for result in report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
