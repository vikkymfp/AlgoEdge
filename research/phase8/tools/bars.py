"""Read-only market-bar evidence capture for the Phase 8.1 campaign.

Fetches the paper engine's own market data - the same provider function
(`fno_signals.main.fetch_underlying_data`), tickers, period and interval the
engine uses - and records, per index and capture:

- every bar's start timestamp and OHLCV, exactly as returned (a NaN stays
  "NaN"; nothing is repaired, interpolated or filled);
- when it was observed (collector clock), fetch success/failure and latency;
- whether the last bar is still forming, which bars are invalid (the
  engine's own `invalid_bar_mask` rule), which 5-minute slots are missing;
- whether the data is stale by the engine's freshness rule, and how late
  each newly completed bar arrived.

To keep a 30-session campaign small, a capture stores the full window only
the first time; later captures store the bars that are new or revised plus
a SHA-256 of the full window, so `load_capture_windows()` can rebuild and
verify every window exactly.

The time the ENGINE used a bar is not observable here - it comes from the
database evidence (fill/decision `created_at`) and is joined by the
reconciler.

    PYTHONPATH=src:. python -m research.phase8.tools.bars --out research/phase8/evidence/<campaign>/bars
"""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import pandas as pd

from algoedge.auto_trader import SIGNAL_FRESHNESS_BARS
from algoedge.market_pulse import TIMEFRAMES
from fno_signals.config import INDEX_MAP
from fno_signals.main import fetch_underlying_data
from fno_signals.strategy import invalid_bar_mask
from research.phase8.tools.common import (
    IST,
    Clock,
    EvidenceFile,
    canonical_json,
    describe_error,
    encode_number,
    new_run_id,
    read_jsonl,
    sha256_text,
    stamp,
    utc_now,
)

SCHEMA = "phase8.bars.v1"
SOURCE = "yfinance via fno_signals.main.fetch_underlying_data"
# The paper engine's index ids and their strategy index (auto_trader._INDEX_CHOICE).
INDEX_CHOICE = {"nifty-50": 1, "bank-nifty": 2, "sensex": 3}
INDEX_TICKERS = {index_id: INDEX_MAP[choice].ticker for index_id, choice in INDEX_CHOICE.items()}
BAR_LENGTHS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}
SESSION_OPEN = dtime(9, 15)  # IST, first bar start (NSE/BSE cash session)
SESSION_CLOSE = dtime(15, 30)  # IST, the last bar ends here
PRICE_COLUMNS = ("Open", "High", "Low", "Close")

FetchFn = Callable[..., pd.DataFrame]


def as_ist(timestamp: Any) -> pd.Timestamp:
    """The engine's convention (auto_trader._as_ist): naive means IST."""
    ts = pd.Timestamp(timestamp)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def invalid_reasons(row: pd.Series) -> list[str]:
    reasons = []
    values = {}
    for column in PRICE_COLUMNS:
        value = row.get(column)
        try:
            number = float(value)
        except (TypeError, ValueError):
            reasons.append(f"{column} not numeric")
            continue
        if not math.isfinite(number):
            reasons.append(f"{column} not finite")
        elif number <= 0:
            reasons.append(f"{column} not positive")
        values[column] = number
    if "High" in values and "Low" in values and math.isfinite(values["High"]) \
            and math.isfinite(values["Low"]) and values["High"] < values["Low"]:
        reasons.append("High < Low")
    return reasons


def expected_slots(day: Any, bar_length: timedelta, until: datetime | None) -> list[pd.Timestamp]:
    """Bar starts of a regular session day (09:15 up to the bar ending at
    15:30), limited to bars completed by `until` when given."""
    start = pd.Timestamp(datetime.combine(day, SESSION_OPEN), tz=IST)
    close = pd.Timestamp(datetime.combine(day, SESSION_CLOSE), tz=IST)
    slots = []
    slot = start
    while slot + bar_length <= close:
        if until is None or slot + bar_length <= until:
            slots.append(slot)
        slot += bar_length
    return slots


def in_session(moment: datetime) -> bool:
    local = moment.astimezone(IST)
    return local.weekday() < 5 and SESSION_OPEN <= local.time() <= SESSION_CLOSE


@dataclass
class IndexLedger:
    """What previous captures of one index already saw (for delta storage,
    revision detection and bar-arrival latency)."""

    bars: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_seen: set[str] = field(default_factory=set)
    last_observed: datetime | None = None


def analyze(frame: pd.DataFrame, observed_at: datetime, bar_length: timedelta,
            ledger: IndexLedger | None = None) -> dict[str, Any]:
    """Classifies one fetched window. Never alters or drops a bar."""
    max_age = bar_length * SIGNAL_FRESHNESS_BARS
    observed = pd.Timestamp(observed_at).tz_convert(IST)
    mask = invalid_bar_mask(frame) if len(frame) else pd.Series(dtype=bool)
    bars: list[dict[str, Any]] = []
    for position, (index, row) in enumerate(frame.iterrows()):
        start = as_ist(index)
        reasons = invalid_reasons(row)
        if bool(mask.iloc[position]) != bool(reasons):  # our reasons must agree with the engine's rule
            reasons.append("classification disagrees with invalid_bar_mask")
        bars.append({
            "bar_start": start.isoformat(),
            "open": encode_number(row.get("Open")), "high": encode_number(row.get("High")),
            "low": encode_number(row.get("Low")), "close": encode_number(row.get("Close")),
            "volume": encode_number(row.get("Volume")),
            # The engine's _completed_bars rule: only a bar whose start + length
            # is after `now` is still forming.
            "state": "forming" if start + bar_length > observed else "completed",
            "valid": not reasons, "invalid_reasons": reasons,
        })

    starts = [pd.Timestamp(bar["bar_start"]) for bar in bars]
    present = set(starts)
    missing: list[str] = []
    off_grid: list[str] = []
    for day in sorted({ts.date() for ts in starts}):
        until = observed if day == observed.date() else None
        grid = expected_slots(day, bar_length, until)
        missing += [slot.isoformat() for slot in grid if slot not in present]
        full_grid = set(expected_slots(day, bar_length, None))
        off_grid += [ts.isoformat() for ts in starts if ts.date() == day and ts not in full_grid]

    completed = [bar for bar in bars if bar["state"] == "completed"]
    latest = completed[-1] if completed else None
    latest_close = pd.Timestamp(latest["bar_start"]) + bar_length if latest else None
    age = (observed - latest_close).total_seconds() if latest_close is not None else None
    if not in_session(observed_at):
        stale, stale_reason = None, "outside session hours (not applicable)"
    elif latest_close is None or latest_close.date() != observed.date():
        stale, stale_reason = True, "no completed bar from today"
    elif age > max_age.total_seconds():
        stale, stale_reason = True, f"latest completed bar closed {age:.0f}s ago (> {max_age.total_seconds():.0f}s)"
    else:
        stale, stale_reason = False, None

    arrivals: list[dict[str, Any]] = []
    if ledger is not None:
        for bar in completed:
            key = bar["bar_start"]
            if key in ledger.completed_seen:
                continue
            close_at = pd.Timestamp(key) + bar_length
            if ledger.last_observed is None:
                arrivals.append({"bar_start": key, "delay_seconds": None,
                                 "note": "first capture of this run - arrival time unknown"})
            else:
                delay = (observed - close_at).total_seconds()
                arrivals.append({
                    "bar_start": key, "delay_seconds": round(delay, 3),
                    # Upper bound: the bar may have been available any time since the previous capture.
                    "resolution_seconds": round((observed_at - ledger.last_observed).total_seconds(), 3),
                    "delayed": delay > bar_length.total_seconds(),
                    "beyond_freshness": delay > max_age.total_seconds(),
                })

    return {
        "bars": bars,
        "summary": {
            "bar_count": len(bars),
            "forming_bar": bars[-1]["bar_start"] if bars and bars[-1]["state"] == "forming" else None,
            "latest_completed_bar": latest["bar_start"] if latest else None,
            "latest_completed_age_seconds": None if age is None else round(age, 3),
            "stale": stale, "stale_reason": stale_reason,
            "freshness_window_seconds": max_age.total_seconds(),
            "invalid_bars": [bar["bar_start"] for bar in bars if not bar["valid"]],
            "missing_bars": missing, "off_grid_bars": off_grid,
            "newly_completed": arrivals,
        },
    }


def _window_digest(bars: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_json(bars))


def capture_index(index_id: str, *, fetch: FetchFn, interval: str, observed_at_fn: Clock,
                  ledger: IndexLedger, run_id: str, seq: int) -> dict[str, Any]:
    ticker = INDEX_TICKERS[index_id]
    period, yf_interval = TIMEFRAMES[interval]
    bar_length = BAR_LENGTHS[interval]
    requested_at = observed_at_fn()
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema": SCHEMA, "run_id": run_id, "seq": seq, "index_id": index_id, "ticker": ticker,
        "source": SOURCE, "interval": yf_interval, "period": period, "timezone": "Asia/Kolkata",
        "requested_at": stamp(requested_at),
    }
    try:
        frame = fetch(ticker, period=period, interval=yf_interval)
        error = None
    except Exception as failure:  # noqa: BLE001 - any provider failure is evidence, recorded as-is
        frame, error = None, describe_error(failure)
    observed_at = observed_at_fn()
    record["observed_at"] = stamp(observed_at)
    record["fetch"] = {"ok": error is None, "latency_seconds": round(time.monotonic() - started, 6),
                       "error": error}
    if frame is None:
        record["window"] = None
        return record
    record["index_tz"] = str(getattr(frame.index, "tz", None))
    analysis = analyze(frame, observed_at, bar_length, ledger)
    bars = analysis["bars"]
    current = {bar["bar_start"]: bar for bar in bars}
    first = not ledger.bars
    changed = [bar for key, bar in current.items() if ledger.bars.get(key) != bar]
    revised = [{"bar_start": key, "before": ledger.bars[key], "after": current[key]}
               for key in current if key in ledger.bars and ledger.bars[key] != current[key]
               and ledger.bars[key]["state"] == "completed"]
    record["window"] = {
        "bar_count": len(bars), "first_bar": bars[0]["bar_start"] if bars else None,
        "last_bar": bars[-1]["bar_start"] if bars else None, "sha256": _window_digest(bars),
        "full": first, "bars": bars if first else changed,
        "removed": sorted(key for key in ledger.bars if key not in current),
        "revised_completed_bars": revised,
    }
    record["summary"] = analysis["summary"]
    ledger.bars = current
    ledger.completed_seen |= {bar["bar_start"] for bar in bars if bar["state"] == "completed"}
    ledger.last_observed = observed_at
    return record


def run(out_dir: Path, *, indices: Iterable[str] = tuple(INDEX_TICKERS), interval: str = "5m",
        every: float = 60.0, captures: int = 0, fetch: FetchFn = fetch_underlying_data,
        clock: Clock = utc_now, sleep: Callable[[float], None] = time.sleep,
        run_id: str | None = None) -> Path:
    index_ids = list(indices)
    unknown = [index_id for index_id in index_ids if index_id not in INDEX_TICKERS]
    if unknown:
        raise ValueError(f"unknown index ids: {unknown}")
    if interval not in BAR_LENGTHS or interval not in TIMEFRAMES:
        raise ValueError(f"unsupported interval {interval!r}")
    run_id = run_id or new_run_id()
    started = clock()
    ledgers = {index_id: IndexLedger() for index_id in index_ids}
    with EvidenceFile(out_dir, "bars", run_id, started) as evidence:
        seq = 0
        try:
            while captures == 0 or seq < captures:
                wait = started.timestamp() + seq * every - clock().timestamp()
                if wait > 0:
                    sleep(wait)
                for index_id in index_ids:
                    evidence.append(capture_index(index_id, fetch=fetch, interval=interval,
                                                  observed_at_fn=clock, ledger=ledgers[index_id],
                                                  run_id=run_id, seq=seq))
                seq += 1
        except KeyboardInterrupt:
            pass
        return evidence.path


@dataclass(frozen=True)
class CaptureWindow:
    index_id: str
    observed_at: datetime
    bars: dict[str, dict[str, Any]]  # bar_start ISO -> bar, as fetched at observed_at


def load_capture_windows(paths: Iterable[Path]) -> dict[str, list[CaptureWindow]]:
    """Rebuilds every captured window from delta records, verifying each
    against its stored SHA-256. A mismatch raises - evidence that cannot be
    reproduced exactly must not be used."""
    windows: dict[str, list[CaptureWindow]] = {}
    for path in sorted(paths):
        state: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for record in read_jsonl(path):
            if record.get("schema") != SCHEMA or record.get("window") is None:
                continue
            key = (record["run_id"], record["index_id"])
            window = record["window"]
            bars = {} if window["full"] else dict(state.get(key, {}))
            for removed in window["removed"]:
                bars.pop(removed, None)
            for bar in window["bars"]:
                bars[bar["bar_start"]] = bar
            ordered = [bars[k] for k in sorted(bars, key=pd.Timestamp)]
            if _window_digest(ordered) != window["sha256"]:
                raise ValueError(f"{path.name}: window seq {record['seq']} of {record['index_id']} "
                                 "does not match its sha256")
            state[key] = bars
            windows.setdefault(record["index_id"], []).append(CaptureWindow(
                record["index_id"], datetime.fromisoformat(record["observed_at"]["utc"]),
                {k: bars[k] for k in sorted(bars, key=pd.Timestamp)},
            ))
    for captures in windows.values():
        captures.sort(key=lambda capture: capture.observed_at)
    return windows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--indices", nargs="+", default=list(INDEX_TICKERS))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--every", type=float, default=60.0, help="seconds between captures")
    parser.add_argument("--captures", type=int, default=0, help="0 = until interrupted")
    args = parser.parse_args(argv)
    print(run(args.out, indices=args.indices, interval=args.interval, every=args.every, captures=args.captures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
