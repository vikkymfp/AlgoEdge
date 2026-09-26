"""Read-only status/alert collector for the Phase 8.1 campaign.

Polls the running dashboard with GET only - /api/auto-trading/status and
/api/alerts - and appends one timestamped, self-hashed record per sample to
an append-only evidence file. It never calls a trading or mutating endpoint
(the only paths it can request are STATUS_PATH and ALERTS_PATH).

A failed request, a non-JSON body or a missing field is recorded as such:
`ok` is False and `missing_fields` names what was absent. A missing value is
never replaced by a default.

    PYTHONPATH=src:. python -m research.phase8.tools.collector --out research/phase8/evidence/<campaign>/status
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from research.phase8.tools.common import (
    Clock,
    EvidenceFile,
    Transport,
    exchange,
    new_run_id,
    stamp,
    urllib_transport,
    utc_now,
    validate_base_url,
)

SCHEMA = "phase8.status.v1"
STATUS_PATH = "/api/auto-trading/status"
ALERTS_PATH = "/api/alerts"
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_BASE_URL = "http://127.0.0.1:5173"

# Fields of /api/auto-trading/status the protocol needs (UI1-UI3, R1, R2).
STATUS_FIELDS = (
    "enabled", "killSwitch", "killSwitchReason", "tradesToday", "entriesToday",
    "realizedPnlToday", "realizedPnlTodayUnit", "consecutiveLosses",
    "consecutiveLossHalt", "lastExitAt", "limits", "accounts", "scheduler",
)
LIMIT_FIELDS = (
    "dailyLossLimit", "dailyLossLimitUnit", "maxTradesPerDay", "maxTradesPerDayCounts",
    "maxOpenPositions", "maxQuantity", "tradingStart", "tradingEnd", "entryCutoff",
    "squareOffTime", "maxConsecutiveLosses", "cooldownMinutes",
)
ACCOUNT_FIELDS = ("indexName", "cash", "quantity", "averagePrice", "side")


def extract_status(body: Any) -> tuple[dict[str, Any], list[str]]:
    """The protocol's fields from a status body, and the dotted names of any
    that are absent. A field that is present with a null value (e.g. the
    side of a flat account) is a real value and is kept as null."""
    missing: list[str] = []
    if not isinstance(body, dict):
        return {}, ["<body is not a JSON object>"]
    extracted: dict[str, Any] = {}
    for name in STATUS_FIELDS:
        if name in body:
            extracted[name] = body[name]
        else:
            missing.append(name)
    limits = body.get("limits")
    if isinstance(limits, dict):
        missing += [f"limits.{name}" for name in LIMIT_FIELDS if name not in limits]
    elif "limits" in body:
        missing.append("limits.<not an object>")
    accounts = body.get("accounts")
    if isinstance(accounts, dict):
        for index_id, account in sorted(accounts.items()):
            if not isinstance(account, dict):
                missing.append(f"accounts.{index_id}.<not an object>")
                continue
            missing += [f"accounts.{index_id}.{name}" for name in ACCOUNT_FIELDS if name not in account]
    elif "accounts" in body:
        missing.append("accounts.<not an object>")
    extracted["openPositions"] = _open_positions(accounts)
    return extracted, missing


def _open_positions(accounts: Any) -> dict[str, Any]:
    """Accounts holding a position right now. Only counted when every
    account reports a numeric quantity - otherwise the count is None with
    the reason, never a partial number."""
    if not isinstance(accounts, dict):
        return {"count": None, "indexIds": None, "reason": "accounts unavailable"}
    holding = []
    for index_id, account in sorted(accounts.items()):
        quantity = account.get("quantity") if isinstance(account, dict) else None
        if isinstance(quantity, bool) or not isinstance(quantity, int | float):
            return {"count": None, "indexIds": None, "reason": f"{index_id}: quantity not numeric"}
        if quantity > 0:
            holding.append(index_id)
    return {"count": len(holding), "indexIds": holding, "reason": None}


def collect_sample(transport: Transport, base_url: str, *, run_id: str, seq: int, timeout: float,
                   alerts_limit: int, clock: Clock = utc_now) -> dict[str, Any]:
    collected_at = clock()
    status = exchange(transport, "GET", base_url, STATUS_PATH, timeout, clock)
    alerts = exchange(transport, "GET", base_url, f"{ALERTS_PATH}?limit={int(alerts_limit)}", timeout, clock)

    extracted: dict[str, Any] = {}
    missing: list[str] = []
    problems: list[str] = []
    if status.error is not None:
        problems.append(f"status request failed: {status.error['type']}")
    elif status.http_status != 200:
        problems.append(f"status HTTP {status.http_status}")
    elif status.body_text is not None:
        problems.append("status body is not JSON")
    else:
        extracted, missing = extract_status(status.body_json)
        if missing:
            problems.append(f"{len(missing)} status field(s) missing")

    alert_rows = None
    if alerts.error is not None:
        problems.append(f"alerts request failed: {alerts.error['type']}")
    elif alerts.http_status != 200:
        problems.append(f"alerts HTTP {alerts.http_status}")
    elif not isinstance(alerts.body_json, dict) or not isinstance(alerts.body_json.get("alerts"), list):
        problems.append("alerts body has no 'alerts' list")
    else:
        alert_rows = alerts.body_json["alerts"]

    return {
        "schema": SCHEMA, "run_id": run_id, "seq": seq, "collected_at": stamp(collected_at),
        "ok": not problems, "problems": problems,
        "status": {"exchange": status.to_record(), "extracted": extracted, "missing_fields": missing},
        "alerts": {"exchange": alerts.to_record(), "rows": alert_rows},
    }


def run(out_dir: Path, *, base_url: str = DEFAULT_BASE_URL, interval: float = DEFAULT_INTERVAL_SECONDS,
        samples: int = 0, timeout: float = 10.0, alerts_limit: int = 200, allow_non_loopback: bool = False,
        transport: Transport = urllib_transport, clock: Clock = utc_now,
        sleep: Callable[[float], None] = time.sleep, run_id: str | None = None) -> Path:
    """Samples every `interval` seconds on a fixed schedule (a slow sample
    never shifts later ones; a sample that starts late records how late).
    `samples=0` runs until interrupted."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    base = validate_base_url(base_url, allow_non_loopback=allow_non_loopback)
    run_id = run_id or new_run_id()
    started: datetime = clock()
    with EvidenceFile(out_dir, "status", run_id, started) as evidence:
        seq = 0
        try:
            while samples == 0 or seq < samples:
                due = started.timestamp() + seq * interval
                wait = due - clock().timestamp()
                if wait > 0:
                    sleep(wait)
                late = max(0.0, clock().timestamp() - due)
                record = collect_sample(transport, base, run_id=run_id, seq=seq, timeout=timeout,
                                        alerts_limit=alerts_limit, clock=clock)
                record["late_seconds"] = round(late, 3)
                record["interval_seconds"] = interval
                evidence.append(record)
                seq += 1
        except KeyboardInterrupt:
            pass
        return evidence.path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="evidence directory for status samples")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--samples", type=int, default=0, help="0 = until interrupted")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--alerts-limit", type=int, default=200)
    parser.add_argument("--allow-non-loopback", action="store_true")
    args = parser.parse_args(argv)
    path = run(args.out, base_url=args.base_url, interval=args.interval, samples=args.samples,
               timeout=args.timeout, alerts_limit=args.alerts_limit, allow_non_loopback=args.allow_non_loopback)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
