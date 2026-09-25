"""Phase 6 research: data-quality validation for a 5-minute NIFTY 50 OHLC file.

    PYTHONPATH=src:. python -m research.phase6.validate_dataset data/master_5min.csv

Read-only with respect to the data: nothing is interpolated, fabricated,
resampled or rewritten. The input file is never modified. Writes a JSON report
and a per-session CSV next to the input (or to --out-dir) and prints a
summary. The report is the evidence for whether - and how - the file can be
used by the research harness; it does not decide that on its own.

Accepted layouts (column names are case-insensitive):
- one timestamp column: datetime / timestamp / date_time / time / date
- or separate `date` + `time` columns
- optional `start_time` / `end_time` columns (per-candle duration is then
  measured directly; otherwise it is inferred from the timestamp grid)
- open, high, low, close; volume is optional
Timestamps with a UTC offset are converted to Asia/Kolkata; naive timestamps
are taken as IST wall-clock time (reported as an assumption).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
BAR = pd.Timedelta(minutes=5)
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)  # last regular 5m bar starts 15:25
NORMAL_BARS = 75
MAX_EXAMPLES = 20

_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})\s*$")
_HAS_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})\s*$")
_TIME_CANDIDATES = ("datetime", "timestamp", "date_time", "start_time", "time", "date")


@dataclass
class QualityReport:
    file: str
    total_rows: int
    columns: list[str]
    timestamp_source: str
    timezone: dict
    first_timestamp: str | None
    last_timestamp: str | None
    trading_days: int
    valid_rows: int
    invalid_rows: int
    unparseable_timestamps: int
    missing_ohlc: dict
    invalid_ohlc: dict
    chronological: dict
    duplicates: dict
    duration: dict
    alignment: dict
    one_second_shifts: dict
    outside_session: dict
    gaps: dict
    session_length_distribution: dict
    abnormal_sessions: dict
    weekend_sessions: list
    weekdays_without_data: dict
    dates_requiring_investigation: list
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- loading


def _find(columns: dict[str, str], *names: str) -> str | None:
    return next((columns[n] for n in names if n in columns), None)


def load_raw(path: Path) -> tuple[pd.DataFrame, str]:
    """Returns the raw frame with a `ts_raw` string column and the name of the
    column(s) it came from. No row is dropped or altered here."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = {c.strip().lower(): c for c in df.columns}
    date_col, time_col = _find(columns, "date"), _find(columns, "time")
    single = _find(columns, "datetime", "timestamp", "date_time", "start_time")
    if single is not None:
        df["ts_raw"] = df[single].str.strip()
        source = single
    elif date_col and time_col:
        df["ts_raw"] = (df[date_col].str.strip() + " " + df[time_col].str.strip())
        source = f"{date_col} + {time_col}"
    elif date_col or time_col:
        df["ts_raw"] = df[date_col or time_col].str.strip()
        source = date_col or time_col
    else:
        raise ValueError(f"{path}: no timestamp column found (looked for {', '.join(_TIME_CANDIDATES)})")
    end_col = _find(columns, "end_time", "end", "close_time")
    if end_col:
        end = df[end_col].str.strip()
        # An end column holding only a time-of-day inherits the start's date.
        time_only = end.str.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?")
        date_part = df["ts_raw"].str.split(" ").str[0]
        df["end_raw"] = np.where(time_only, date_part + " " + end, end)
    for name in ("open", "high", "low", "close"):
        if name not in columns:
            raise ValueError(f"{path}: missing column '{name}'")
        df[f"_{name}"] = pd.to_numeric(df[columns[name]].replace("", np.nan), errors="coerce")
    return df, source


def parse_timestamps(raw: pd.Series) -> tuple[pd.Series, dict]:
    """Parses to tz-aware IST. Offsets present -> converted; naive -> IST."""
    has_offset = raw.str.contains(_HAS_OFFSET_RE, na=False)
    offsets = raw[has_offset].str.extract(_OFFSET_RE)[0].value_counts().to_dict()
    parsed = pd.Series(pd.NaT, index=raw.index, dtype=f"datetime64[ns, {IST}]")
    if has_offset.any():
        parsed[has_offset] = pd.to_datetime(raw[has_offset], utc=True, errors="coerce").dt.tz_convert(IST)
    if (~has_offset).any():
        naive = pd.to_datetime(raw[~has_offset], errors="coerce", format="mixed")
        parsed[~has_offset] = naive.dt.tz_localize(IST, ambiguous="NaT", nonexistent="NaT")
    info = {
        "rows_with_utc_offset": int(has_offset.sum()),
        "offsets_seen": {str(k): int(v) for k, v in offsets.items()},
        "naive_rows_assumed_ist": int((~has_offset).sum()),
        "reported_in": IST,
    }
    return parsed, info


# ---------------------------------------------------------------- checks


def _examples(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    out = frame[columns].head(MAX_EXAMPLES).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].map(lambda t: t.isoformat() if pd.notna(t) else None)
    return json.loads(out.to_json(orient="records"))


def validate(path: Path) -> tuple[QualityReport, pd.DataFrame]:
    report, sessions, _rows = validate_with_rows(path)
    return report, sessions


def validate_with_rows(path: Path) -> tuple[QualityReport, pd.DataFrame, pd.DataFrame]:
    """Same checks as validate(), plus the per-row verdicts behind them - one
    row per input line (file order), with the parsed IST `ts`/`end`, OHLC and
    boolean flags: `invalid` (unparseable time, missing or inconsistent OHLC),
    `duplicate` (every copy of a repeated timestamp), `off_grid` (not on a
    5-minute :00 boundary) and `bad_duration` (end - start != 5 minutes, only
    when the file has an end column). Consumers (e.g. load_historical_data)
    use these flags so validation logic lives in one place."""
    raw, source = load_raw(path)
    ts, tz_info = parse_timestamps(raw["ts_raw"])
    df = pd.DataFrame({
        "row": np.arange(len(raw)) + 2,  # 1-based file line (header is line 1)
        "ts_raw": raw["ts_raw"], "ts": ts,
        "open": raw["_open"], "high": raw["_high"], "low": raw["_low"], "close": raw["_close"],
    })
    notes = []
    if tz_info["naive_rows_assumed_ist"]:
        notes.append("Timestamps without a UTC offset were taken as IST wall-clock time.")

    # 3-4. missing / invalid OHLC
    ohlc = df[["open", "high", "low", "close"]]
    missing = {c: int(ohlc[c].isna().sum()) for c in ohlc}
    finite = np.isfinite(ohlc).all(axis=1)
    o, h, lo, c = (df[k] for k in ("open", "high", "low", "close"))
    rules = {
        "high_below_low": finite & (h < lo),
        "high_below_open": finite & (h < o),
        "high_below_close": finite & (h < c),
        "low_above_open": finite & (lo > o),
        "low_above_close": finite & (lo > c),
        "non_positive_price": finite & (ohlc <= 0).any(axis=1),
        "non_finite_value": ~finite & ohlc.notna().all(axis=1),
    }
    bad_relation = pd.concat(rules.values(), axis=1).any(axis=1)
    unparseable = df["ts"].isna()
    invalid = unparseable | ohlc.isna().any(axis=1) | bad_relation
    df["invalid"] = invalid

    # Everything below works on rows with a usable timestamp; OHLC-invalid rows
    # still count for timing checks (a bad price does not move the clock).
    timed = df.loc[~unparseable].copy()
    local = timed["ts"]

    # 1. ordering
    backwards = local.diff() < pd.Timedelta(0)
    chronological = {
        "is_sorted": bool(local.is_monotonic_increasing),
        "backward_steps": int(backwards.sum()),
        "examples": _examples(timed.assign(previous=local.shift())[backwards.to_numpy()], ["row", "previous", "ts"]),
    }

    # 2. duplicates
    dup_mask = local.duplicated(keep=False)
    dup_groups = timed[dup_mask.to_numpy()].groupby("ts")
    conflicting = int((dup_groups[["open", "high", "low", "close"]].nunique(dropna=False) > 1).any(axis=1).sum())
    duplicates = {
        "duplicate_rows": int(local.duplicated(keep="first").sum()),
        "timestamps_involved": int(dup_groups.ngroups),
        "conflicting_values": conflicting,
        "examples": _examples(timed[dup_mask.to_numpy()], ["row", "ts", "open", "high", "low", "close"]),
    }

    # 10-11. alignment and one-second shifts
    seconds = local.dt.second
    minute_off = (local.dt.minute % 5) != 0
    off_grid = minute_off | (seconds != 0)
    near_boundary = (seconds == 59) | (seconds == 1)
    alignment = {
        "aligned_to_5m": int((~off_grid).sum()),
        "misaligned": int(off_grid.sum()),
        "misaligned_minute": int(minute_off.sum()),
        "nonzero_seconds": int((seconds != 0).sum()),
        "examples": _examples(timed[off_grid.to_numpy()], ["row", "ts_raw", "ts"]),
    }
    one_second = {
        "count": int(near_boundary.sum()),
        "at_59_seconds": int((seconds == 59).sum()),
        "at_01_seconds": int((seconds == 1).sum()),
        "examples": _examples(timed[near_boundary.to_numpy()], ["row", "ts_raw", "ts"]),
    }

    # 5. candle duration
    if "end_raw" in raw:
        end, _ = parse_timestamps(raw.loc[timed.index, "end_raw"])
        dur = (end - local)
        df["end"] = end.reindex(df.index)
        df["bad_duration"] = (dur != BAR).reindex(df.index, fill_value=True)
        counts = dur.dt.total_seconds().value_counts(dropna=False).sort_index()
        wrong = dur != BAR
        duration = {
            "measured_from": "start/end columns",
            "distribution_seconds": {str(k): int(v) for k, v in counts.items()},
            "not_exactly_5m": int(wrong.sum()),
            "end_inclusive_4m59s": int((dur == BAR - pd.Timedelta(seconds=1)).sum()),
            "examples": _examples(timed.assign(end=end)[wrong.to_numpy()], ["row", "ts", "end"]),
        }
    else:
        firsts = local.groupby(local.dt.date).min().dt.time.value_counts().head(5)
        duration = {
            "measured_from": "no end column - inferred from the timestamp grid",
            "session_first_bar_times": {str(k): int(v) for k, v in firsts.items()},
            "label_convention": (
                "bar START time (first bar 09:15)" if firsts.index[:1].tolist() == [SESSION_OPEN]
                else "unclear - first bars are not 09:15; check whether timestamps label bar END"
            ) if len(firsts) else None,
        }

    # 6. gaps (within a day; the overnight step is not a gap)
    order = timed.sort_values("ts", kind="mergesort").drop_duplicates("ts")
    step = order["ts"].diff()
    same_day = order["ts"].dt.date == order["ts"].shift().dt.date
    gap = same_day & (step != BAR)
    gap_minutes = (step[gap].dt.total_seconds() / 60).round(3)
    gaps = {
        "count": int(gap.sum()),
        "missing_bars_implied": int(((step[gap] / BAR).clip(lower=1) - 1).round().sum()),
        "distribution_minutes": {str(k): int(v) for k, v in gap_minutes.value_counts().sort_index().items()},
        "examples": _examples(order.assign(previous=order["ts"].shift(), minutes=step.dt.total_seconds() / 60)
                              [gap.to_numpy()], ["row", "previous", "ts", "minutes"]),
    }

    # 7-9, 12. sessions
    order["date"] = order["ts"].dt.date
    t = order["ts"].dt.time
    in_session = (t >= SESSION_OPEN) & (t < SESSION_CLOSE)
    outside_session = {
        "count": int((~in_session).sum()),
        "dates": sorted({d.isoformat() for d in order.loc[~in_session.to_numpy(), "date"]})[:MAX_EXAMPLES],
    }
    sessions = order.groupby("date").agg(
        bars=("ts", "size"), first_bar=("ts", "min"), last_bar=("ts", "max"), invalid_rows=("invalid", "sum"),
    )
    sessions["bars_in_regular_session"] = in_session.groupby(order["date"].to_numpy()).sum()
    sessions["weekday"] = [pd.Timestamp(d).day_name() for d in sessions.index]
    sessions["status"] = np.select(
        [pd.Index([pd.Timestamp(d).weekday() >= 5 for d in sessions.index]),
         sessions["bars"] < NORMAL_BARS, sessions["bars"] > NORMAL_BARS],
        ["weekend", "short", "long"], default="normal",
    )
    length_distribution = {str(k): int(v) for k, v in sessions["bars"].value_counts().sort_index().items()}
    abnormal = sessions[sessions["status"] != "normal"]

    def session_rows(frame):
        return [{"date": d.isoformat(), "bars": int(r.bars), "first": r.first_bar.strftime("%H:%M:%S"),
                 "last": r.last_bar.strftime("%H:%M:%S"), "status": r.status} for d, r in frame.iterrows()]

    abnormal_sessions = {
        "short": int((sessions["status"] == "short").sum()),
        "long": int((sessions["status"] == "long").sum()),
        "list": session_rows(abnormal),
    }
    weekend = session_rows(sessions[sessions["status"] == "weekend"])

    missing_weekdays = []
    if len(sessions):
        present = set(sessions.index)
        missing_weekdays = [d.isoformat() for d in pd.bdate_range(min(present), max(present)).date
                            if d not in present]
    by_year: dict[str, int] = {}
    for d in missing_weekdays:
        by_year[d[:4]] = by_year.get(d[:4], 0) + 1

    # Dates to look at: anything abnormal, plus dates with timing defects.
    flagged: dict[str, set[str]] = {}

    def flag(dates, reason):
        for d in dates:
            flagged.setdefault(d.isoformat() if hasattr(d, "isoformat") else str(d), set()).add(reason)

    flag(abnormal.index[abnormal["status"] == "short"], "fewer than 75 bars")
    flag(abnormal.index[abnormal["status"] == "long"], "more than 75 bars")
    flag(abnormal.index[abnormal["status"] == "weekend"], "weekend session")
    flag(order.loc[gap.to_numpy(), "date"], "intraday gap != 5m")
    flag(timed.loc[off_grid.to_numpy(), "ts"].dt.date, "off the 5m grid")
    flag(timed.loc[dup_mask.to_numpy(), "ts"].dt.date, "duplicate timestamps")
    flag(order.loc[~in_session.to_numpy(), "date"], "bars outside 09:15-15:30")
    flag(df.loc[invalid & ~unparseable, "ts"].dt.date, "invalid OHLC")
    investigation = [{"date": d, "reasons": sorted(r)} for d, r in sorted(flagged.items())]

    report = QualityReport(
        file=str(path), total_rows=len(df), columns=list(raw.columns.drop(
            [c for c in raw.columns if c.startswith("_") or c in ("ts_raw", "end_raw")])),
        timestamp_source=source, timezone=tz_info,
        first_timestamp=local.min().isoformat() if len(local) else None,
        last_timestamp=local.max().isoformat() if len(local) else None,
        trading_days=len(sessions), valid_rows=int((~invalid).sum()), invalid_rows=int(invalid.sum()),
        unparseable_timestamps=int(unparseable.sum()),
        missing_ohlc=missing,
        invalid_ohlc={**{k: int(v.sum()) for k, v in rules.items()},
                      "rows": int(bad_relation.sum()),
                      "examples": _examples(df[bad_relation.to_numpy()], ["row", "ts", "open", "high", "low", "close"])},
        chronological=chronological, duplicates=duplicates, duration=duration, alignment=alignment,
        one_second_shifts=one_second, outside_session=outside_session, gaps=gaps,
        session_length_distribution=length_distribution, abnormal_sessions=abnormal_sessions,
        weekend_sessions=weekend,
        weekdays_without_data={"count": len(missing_weekdays), "by_year": by_year,
                               "dates": missing_weekdays,
                               "note": "exchange holidays or missing data - no holiday calendar is assumed"},
        dates_requiring_investigation=investigation, notes=notes,
    )
    session_table = sessions.reset_index().rename(columns={"index": "date"})
    session_table["first_bar"] = session_table["first_bar"].map(lambda x: x.isoformat())
    session_table["last_bar"] = session_table["last_bar"].map(lambda x: x.isoformat())
    df["duplicate"] = dup_mask.reindex(df.index, fill_value=False)
    df["off_grid"] = off_grid.reindex(df.index, fill_value=False)
    if "end" not in df:
        df["end"] = pd.Series(pd.NaT, index=df.index, dtype=f"datetime64[ns, {IST}]")
        df["bad_duration"] = False
    return report, session_table, df


# ---------------------------------------------------------------- CLI


def summary(report: QualityReport) -> str:
    r = report
    lines = [
        f"File: {r.file}",
        f"Rows: {r.total_rows} (valid {r.valid_rows}, invalid {r.invalid_rows}; unparseable timestamps "
        f"{r.unparseable_timestamps})",
        f"Range: {r.first_timestamp} -> {r.last_timestamp}   trading days: {r.trading_days}",
        f"Timezone: {r.timezone}",
        f"Sorted: {r.chronological['is_sorted']} (backward steps {r.chronological['backward_steps']})",
        f"Duplicates: {r.duplicates['duplicate_rows']} rows ({r.duplicates['conflicting_values']} conflicting)",
        f"Missing OHLC: {r.missing_ohlc}",
        f"Invalid OHLC rows: {r.invalid_ohlc['rows']}",
        f"Intraday gaps != 5m: {r.gaps['count']} (implied missing bars {r.gaps['missing_bars_implied']})",
        f"Off 5m grid: {r.alignment['misaligned']}; one-second shifts: {r.one_second_shifts['count']}",
        f"Bars outside 09:15-15:30: {r.outside_session['count']}",
        f"Duration: {r.duration}",
        f"Session lengths (bars -> days): {r.session_length_distribution}",
        f"Short sessions: {r.abnormal_sessions['short']}, long: {r.abnormal_sessions['long']}, "
        f"weekend: {len(r.weekend_sessions)}, weekdays without data: {r.weekdays_without_data['count']}",
        f"Dates requiring investigation: {len(r.dates_requiring_investigation)}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a 5-minute OHLC dataset (read-only).")
    parser.add_argument("path", type=Path)
    parser.add_argument("--out-dir", type=Path, default=None, help="defaults to the input file's directory")
    args = parser.parse_args(argv)
    if not args.path.exists():
        print(f"{args.path} does not exist", file=sys.stderr)
        return 2
    report, sessions = validate(args.path)
    out_dir = args.out_dir or args.path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.path.stem
    report_path = out_dir / f"{stem}_quality.json"
    sessions_path = out_dir / f"{stem}_sessions.csv"
    report_path.write_text(json.dumps(asdict(report), indent=1, default=str))
    sessions.to_csv(sessions_path, index=False)
    print(summary(report))
    print(f"\nwrote {report_path} and {sessions_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
