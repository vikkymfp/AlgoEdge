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
BEGINNING = datetime(1970, 1, 1, tzinfo=IST)  # "before the range" in a position timeline

CHECKS = ("LIVE_ORDERS", "P1_GROUPING", "TRANSITIONS", "R1_MAX_OPEN", "C2_DUPLICATES", "R2_COUNTERS",
          "PNL", "AUDIT", "BARS", "S1_REPLAY", "DASHBOARD")


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
                 max_open_positions: int | None = None, status_lag: timedelta = timedelta(seconds=65)) -> None:
        self.x = extraction
        self.captures = captures
        self.samples = status_samples
        self.tampered_samples = tampered_samples
        self.limits = RiskLimits()
        self.max_open = max_open_positions if max_open_positions is not None else self.limits.max_open_positions
        self.status_lag = status_lag
        self.findings: list[Finding] = []
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
            if chosen is None or chosen.kind != fill.kind or chosen.timestamp != pd.Timestamp(bar_at) \
                    or not _same(chosen.underlying_price, float(fill.order["price"])):
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
                             "findings": len(items)}
        p0 = [f for f in self.findings if f.status == "FAIL" and f.severity == "P0"]
        return {
            "schema": SCHEMA,
            "inputs": {"db_extract_run": self.x.manifest.get("run_id"), "range": self.x.manifest.get("range"),
                       "bars": self.captures is not None, "status_samples": None if self.samples is None
                       else len(self.samples)},
            "parameters": {"group_tolerance_seconds": GROUP_TOLERANCE.total_seconds(),
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
