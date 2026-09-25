"""Phase 6 research: load a validated 5-minute candle CSV into SQL Server.

RESEARCH / BACKTEST DATA ONLY - writes research.phase6.historical_db's two
tables (historical_candle_loads, historical_candles) and nothing else.

    PYTHONPATH=src:. python -m research.phase6.load_historical_data data/master_5min.csv            # dry run
    PYTHONPATH=src:. python -m research.phase6.load_historical_data data/master_5min.csv --confirm  # real load

Without --confirm nothing touches the database: the file is read, validated
(research.phase6.validate_dataset - the same checks, not a copy), hashed,
the exclusion policy is applied and the expected insert is reported.

Data policy (baseline load):
- Every candle whose bar_start falls in historical_db.EXCLUDED_PERIODS
  (2015-06-22 .. 2015-11-13, inclusive, IST dates) is excluded.
- Outside that window the dataset is preserved exactly. A row the validator
  rejects there (unparseable time, missing/inconsistent OHLC, a duplicated
  timestamp, a timestamp off the 5-minute grid, a candle not exactly 5
  minutes long) is never imported and never repaired; by default its mere
  presence refuses the whole load (--allow-rejections skips such rows and
  records them in the load's validation_json).
- Nothing is filled, interpolated, resampled or fabricated; volume stays
  NULL when the source has none. The source file is only ever read.

The load is one transaction: the load record, every candle and the
post-insert verification queries (A-J) commit together or not at all.
Loading the same file content (same SHA-256) twice is a no-op.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, and_, func, insert, not_, or_, select
from sqlalchemy.exc import IntegrityError

from research.phase6 import historical_db as hdb
from research.phase6.validate_dataset import IST, QualityReport, validate_with_rows

DEFAULT_INDEX_ID = "nifty-50"
DEFAULT_TIMEFRAME = "5m"
DEFAULT_BATCH_SIZE = 5000
REJECTION_FLAGS = ("invalid", "duplicate", "off_grid", "bad_duration")


class LoadRefused(Exception):
    """The data does not meet the load policy - nothing was written."""


class LoadVerificationError(Exception):
    """A post-insert check failed - the whole load was rolled back."""

    def __init__(self, checks: dict):
        failed = {k: v for k, v in checks.items() if not v["ok"]}
        super().__init__(f"post-insert verification failed: {failed}")
        self.checks = checks


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _naive_ist(ts: pd.Timestamp) -> datetime:
    """IST wall-clock, no offset - the project's DATETIME convention."""
    return ts.tz_convert(IST).tz_localize(None).to_pydatetime()


def excluded_mask(ts: pd.Series) -> pd.Series:
    """True where bar_start's IST date is inside an excluded period."""
    dates = ts.dt.tz_convert(IST).dt.date
    mask = pd.Series(False, index=ts.index)
    for start, end in hdb.EXCLUDED_PERIODS:
        mask |= ts.notna() & (dates >= start) & (dates <= end)
    return mask


@dataclass
class LoadPlan:
    source: str
    file_path: str
    file_sha256: str
    index_id: str
    timeframe: str
    report: QualityReport
    source_rows: int
    excluded_rows: int
    rejected_rows: int
    rejected_by_reason: dict[str, int]
    rejected_examples: list[dict]
    candles: list[dict] = field(repr=False)

    @property
    def first_bar(self) -> datetime | None:
        return self.candles[0]["bar_start"] if self.candles else None

    @property
    def last_bar(self) -> datetime | None:
        return self.candles[-1]["bar_start"] if self.candles else None

    def policy(self) -> dict:
        return {
            "excluded_periods": [[a.isoformat(), b.isoformat()] for a, b in hdb.EXCLUDED_PERIODS],
            "excluded_rows": self.excluded_rows,
            "rejected_rows_outside_exclusion": self.rejected_rows,
            "rejected_by_reason": self.rejected_by_reason,
            "rows_to_insert": len(self.candles),
            "index_id": self.index_id, "timeframe": self.timeframe,
            "first_inserted_bar_ist": self.first_bar.isoformat() if self.first_bar else None,
            "last_inserted_bar_ist": self.last_bar.isoformat() if self.last_bar else None,
        }

    def load_record(self) -> dict:
        """historical_candle_loads row. Counts describe the WHOLE source file as
        validated; first/last bar describe what is actually inserted."""
        r = self.report
        validation = {
            "validator": "research.phase6.validate_dataset",
            "file": self.file_path, "source_rows": r.total_rows,
            "source_first_timestamp": r.first_timestamp, "source_last_timestamp": r.last_timestamp,
            "trading_days": r.trading_days, "timezone": r.timezone,
            "missing_ohlc": r.missing_ohlc,
            "invalid_ohlc": {k: v for k, v in r.invalid_ohlc.items() if k != "examples"},
            "duplicates": {k: v for k, v in r.duplicates.items() if k != "examples"},
            "gaps": {k: v for k, v in r.gaps.items() if k != "examples"},
            "misaligned": r.alignment["misaligned"], "one_second_shifts": r.one_second_shifts["count"],
            "session_length_distribution": r.session_length_distribution,
            "load_policy": self.policy(),
        }
        return {
            "source": self.source, "file_sha256": self.file_sha256,
            "row_count": r.total_rows, "valid_rows": r.valid_rows, "invalid_rows": r.invalid_rows,
            "duplicate_count": r.duplicates["duplicate_rows"], "gap_count": r.gaps["count"],
            "first_bar": self.first_bar, "last_bar": self.last_bar,
            "validation_json": json.dumps(validation, default=str),
        }

    def summary(self) -> dict:
        return {"source": self.source, "file": self.file_path, "sha256": self.file_sha256,
                "source_rows": self.source_rows, **self.policy(),
                "rejected_examples": self.rejected_examples}


def plan_load(path: Path, *, source_name: str | None = None, index_id: str = DEFAULT_INDEX_ID,
              timeframe: str = DEFAULT_TIMEFRAME) -> LoadPlan:
    """Reads, validates and filters the file. Never writes anything."""
    file_sha256 = sha256_file(path)
    report, _sessions, rows = validate_with_rows(path)
    excluded = excluded_mask(rows["ts"])
    flags = rows[list(REJECTION_FLAGS)].astype(bool)
    rejected = ~excluded & flags.any(axis=1)
    keep = rows[~excluded & ~rejected].sort_values("ts", kind="mergesort")

    source = source_name or path.name
    candles = [
        {"index_id": index_id, "timeframe": timeframe,
         "bar_start": _naive_ist(r.ts), "bar_end": _naive_ist(r.end),
         "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
         "volume": None, "source": source}
        for r in keep.itertuples(index=False)
    ]
    reasons = {flag: int((~excluded & flags[flag]).sum()) for flag in REJECTION_FLAGS}
    examples = rows[rejected].head(10)
    return LoadPlan(
        source=source, file_path=str(path), file_sha256=file_sha256, index_id=index_id, timeframe=timeframe,
        report=report, source_rows=len(rows), excluded_rows=int(excluded.sum()),
        rejected_rows=int(rejected.sum()), rejected_by_reason=reasons,
        rejected_examples=[{"line": int(r.row), "ts_raw": r.ts_raw,
                            "reasons": [f for f in REJECTION_FLAGS if getattr(r, f)]}
                           for r in examples.itertuples(index=False)],
        candles=candles,
    )


# ---------------------------------------------------------------- database


@dataclass
class LoadResult:
    load_id: int
    already_loaded: bool
    inserted: int
    checks: dict = field(default_factory=dict)


def find_existing_load(engine: Engine, file_sha256: str) -> int | None:
    loads = hdb.HistoricalCandleLoad.__table__
    with engine.connect() as conn:
        return conn.execute(select(loads.c.id).where(loads.c.file_sha256 == file_sha256)).scalar()


def verify_load(conn, load_id: int, plan: LoadPlan) -> dict:
    """Checks A-J, run inside the load transaction before it commits."""
    c = hdb.HistoricalCandle.__table__
    mine = c.c.load_id == load_id

    def scalar(stmt):
        return conn.execute(stmt).scalar()

    count = scalar(select(func.count()).select_from(c).where(mine))
    window_hits = 0
    for start, end in hdb.EXCLUDED_PERIODS:
        lo = datetime.combine(start, datetime.min.time())
        hi = datetime.combine(end, datetime.min.time()) + timedelta(days=1)
        window_hits += scalar(select(func.count()).select_from(c).where(
            c.c.index_id == plan.index_id, c.c.timeframe == plan.timeframe, c.c.bar_start >= lo, c.c.bar_start < hi))
    dup_groups = scalar(select(func.count()).select_from(
        select(c.c.bar_start).where(mine).group_by(c.c.index_id, c.c.timeframe, c.c.bar_start)
        .having(func.count() > 1).subquery()))
    bad_ohlc = scalar(select(func.count()).select_from(c).where(mine, not_(and_(
        c.c.high >= c.c.low, c.c.high >= c.c.open, c.c.high >= c.c.close,
        c.c.low <= c.c.open, c.c.low <= c.c.close))))
    checks = {
        "A_count": (count, len(plan.candles)),
        "B_min_bar_start": (scalar(select(func.min(c.c.bar_start)).where(mine)), plan.first_bar),
        "C_max_bar_start": (scalar(select(func.max(c.c.bar_start)).where(mine)), plan.last_bar),
        "D_distinct_bar_start": (scalar(select(func.count(func.distinct(c.c.bar_start))).where(mine)),
                                 len(plan.candles)),
        "E_duplicate_groups": (dup_groups, 0),
        "F_null_ohlc": (scalar(select(func.count()).select_from(c).where(mine, or_(
            c.c.open.is_(None), c.c.high.is_(None), c.c.low.is_(None), c.c.close.is_(None)))), 0),
        "G_invalid_ohlc": (bad_ohlc, 0),
        "H_wrong_index_or_timeframe": (scalar(select(func.count()).select_from(c).where(
            mine, or_(c.c.index_id != plan.index_id, c.c.timeframe != plan.timeframe))), 0),
        "I_rows_in_excluded_period": (window_hits, 0),
        "J_source_rows_with_other_load_id": (scalar(select(func.count()).select_from(c).where(
            c.c.source == plan.source, c.c.load_id != load_id)), 0),
        "J_rows_with_this_load_id": (count, len(plan.candles)),
        "volume_not_null": (scalar(select(func.count()).select_from(c).where(mine, c.c.volume.is_not(None))), 0),
    }
    return {k: {"value": _plain(v), "expected": _plain(e), "ok": v == e} for k, (v, e) in checks.items()}


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value


def execute_load(engine: Engine, plan: LoadPlan, *, allow_rejections: bool = False,
                 batch_size: int = DEFAULT_BATCH_SIZE) -> LoadResult:
    """The real write. Atomic: load record + candles + verification in one
    transaction. Returns without writing if this file was already loaded."""
    if plan.rejected_rows and not allow_rejections:
        raise LoadRefused(f"{plan.rejected_rows} row(s) outside the excluded period fail validation "
                          f"{plan.rejected_by_reason} - nothing written (see --allow-rejections)")
    if not plan.candles:
        raise LoadRefused("no candles to insert")
    hdb.create_research_schema(engine)
    existing = find_existing_load(engine, plan.file_sha256)
    if existing is not None:
        return LoadResult(existing, True, 0)
    loads = hdb.HistoricalCandleLoad.__table__
    candles = hdb.HistoricalCandle.__table__
    try:
        with engine.begin() as conn:
            load_id = conn.execute(insert(loads).values(**plan.load_record())).inserted_primary_key[0]
            for start in range(0, len(plan.candles), batch_size):
                batch = [{**row, "load_id": load_id} for row in plan.candles[start:start + batch_size]]
                conn.execute(insert(candles), batch)  # executemany (fast_executemany on SQL Server)
            checks = verify_load(conn, load_id, plan)
            if not all(check["ok"] for check in checks.values()):
                raise LoadVerificationError(checks)
    except IntegrityError:
        # A concurrent run may have registered the same file first.
        existing = find_existing_load(engine, plan.file_sha256)
        if existing is not None:
            return LoadResult(existing, True, 0)
        raise
    return LoadResult(load_id, False, len(plan.candles), checks)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None, *, engine_factory: Callable[[], Engine] = hdb.research_engine) -> int:
    parser = argparse.ArgumentParser(description="Load a validated 5m candle CSV into the research tables.")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--source-name", help="value stored in `source` (default: the file name)")
    parser.add_argument("--index-id", default=DEFAULT_INDEX_ID)
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--dry-run", action="store_true", help="validate and report only (the default)")
    parser.add_argument("--confirm", action="store_true", help="actually write to SQL Server")
    parser.add_argument("--allow-rejections", action="store_true",
                        help="skip (and record) rows outside the excluded period that fail validation")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    if not args.csv.is_file():
        print(f"{args.csv} does not exist", file=sys.stderr)
        return 2
    started = time.perf_counter()
    plan = plan_load(args.csv, source_name=args.source_name, index_id=args.index_id, timeframe=args.timeframe)
    summary = plan.summary()

    if args.dry_run or not args.confirm:
        mode = "DRY RUN" if args.dry_run else "DRY RUN (no --confirm given)"
        print(f"{mode} - SQL Server was not contacted; nothing was written.")
        print(json.dumps({**summary, "elapsed_seconds": round(time.perf_counter() - started, 2)}, indent=1))
        if plan.rejected_rows and not args.allow_rejections:
            print(f"NOTE: a real load would be REFUSED - {plan.rejected_rows} rejected row(s) outside the "
                  "excluded period.", file=sys.stderr)
        return 0

    try:
        result = execute_load(engine_factory(), plan, allow_rejections=args.allow_rejections,
                              batch_size=args.batch_size)
    except (LoadRefused, LoadVerificationError) as error:
        print(f"LOAD FAILED - nothing written: {error}", file=sys.stderr)
        return 1
    if sha256_file(args.csv) != plan.file_sha256:  # the source must never change underneath a load
        print("WARNING: the source file changed while loading", file=sys.stderr)
        return 1
    if result.already_loaded:
        print(f"Already loaded: {plan.source} (sha256 {plan.file_sha256}) is load_id {result.load_id} "
              "- nothing inserted.")
        return 0
    print(json.dumps({
        "source_file": plan.file_path, "source": plan.source, "sha256": plan.file_sha256,
        "source_rows": plan.source_rows, "excluded_contaminated_rows": plan.excluded_rows,
        "rejected_rows_skipped": plan.rejected_rows, "inserted": result.inserted,
        "first_inserted": plan.first_bar, "last_inserted": plan.last_bar, "load_id": result.load_id,
        "elapsed_seconds": round(time.perf_counter() - started, 2), "checks": result.checks,
    }, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
