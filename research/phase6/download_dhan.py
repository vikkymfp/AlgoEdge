"""Phase 6 research: download NIFTY 50 5-minute candles from Dhan.

Dhan is used ONLY as a historical-data source for research. This module never
places, modifies or reads orders, and nothing in src/ imports it.

    export DHAN_ACCESS_TOKEN=...        # required, never passed on the command line
    export DHAN_CLIENT_ID=...           # optional, sent as the client-id header
    PYTHONPATH=src:. python -m research.phase6.download_dhan --start 2025-01-01 --end 2025-06-30
    PYTHONPATH=src:. python -m research.phase6.download_dhan --validate-only

Output (git-ignored by the existing data/*.csv and data/*.json rules):
    data/phase6_nifty50_5m.csv            datetime,open,high,low,close,volume (IST)
    data/phase6_nifty50_5m_metadata.json

Nothing is ever interpolated: invalid candles are removed and counted, missing
intervals stay missing and are reported. A failed date chunk stops the dataset
from being written unless --allow-partial is given, and is always reported.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time as time_mod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# Dhan API contract (Dhan HQ API v2, "Historical Data" -> intraday candles).
# All API assumptions live here so they can be checked against Dhan's docs in
# one place; the network sandbox used to write this could not reach dhanhq.co.
# ---------------------------------------------------------------------------
DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
DHAN_MAX_DAYS_PER_REQUEST = 90  # Dhan limits one intraday request to 90 days
DHAN_INTERVALS = ("1", "5", "15", "25", "60")  # minutes
DHAN_REQUEST_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DHAN_RATE_LIMIT_CODES = frozenset({"DH-904"})  # "rate limit exceeded" - transient
ENV_ACCESS_TOKEN = "DHAN_ACCESS_TOKEN"
ENV_CLIENT_ID = "DHAN_CLIENT_ID"

# NIFTY 50 index: Dhan security ID 13 in the index segment.
DEFAULT_SYMBOL = "NIFTY50"
DEFAULT_SECURITY_ID = "13"
DEFAULT_EXCHANGE_SEGMENT = "IDX_I"
DEFAULT_INSTRUMENT = "INDEX"
DEFAULT_OUT = Path("data/phase6_nifty50_5m.csv")

SCHEMA = ("datetime", "open", "high", "low", "close", "volume")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


class DhanAPIError(Exception):
    def __init__(self, message: str, *, transient: bool, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.code = code


# ---------------------------------------------------------------- client


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any  # parsed JSON (dict) or raw text


Transport = Callable[[str, dict, dict, float], HttpResponse]


def requests_transport(url: str, headers: dict, payload: dict, timeout: float) -> HttpResponse:
    import requests

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as error:
        raise DhanAPIError(f"network error: {type(error).__name__}", transient=True) from error
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    return HttpResponse(response.status_code, body)


class DhanHistoricalClient:
    """Read-only client for Dhan's intraday historical candles."""

    def __init__(
        self,
        access_token: str,
        client_id: str | None = None,
        *,
        transport: Transport = requests_transport,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time_mod.sleep,
    ) -> None:
        if not access_token:
            raise ValueError(f"Dhan access token missing - set {ENV_ACCESS_TOKEN}")
        self._access_token = access_token
        self._client_id = client_id
        self._transport = transport
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout
        self._sleep = sleep

    @classmethod
    def from_env(cls, **kwargs) -> DhanHistoricalClient:
        return cls(os.environ.get(ENV_ACCESS_TOKEN, ""), os.environ.get(ENV_CLIENT_ID) or None, **kwargs)

    def __repr__(self) -> str:  # never expose credentials
        return "DhanHistoricalClient(access_token=***)"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "access-token": self._access_token}
        if self._client_id:
            headers["client-id"] = self._client_id
        return headers

    def intraday_candles(
        self, security_id: str, exchange_segment: str, instrument: str, interval: str,
        from_dt: datetime, to_dt: datetime,
    ) -> dict:
        payload = {
            "securityId": str(security_id), "exchangeSegment": exchange_segment, "instrument": instrument,
            "interval": str(interval), "oi": False,
            "fromDate": from_dt.strftime(DHAN_REQUEST_TIME_FORMAT),
            "toDate": to_dt.strftime(DHAN_REQUEST_TIME_FORMAT),
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._request_once(payload)
            except DhanAPIError as error:
                if not error.transient or attempt > self.max_retries:
                    raise
                self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _request_once(self, payload: dict) -> dict:
        response = self._transport(DHAN_INTRADAY_URL, self._headers(), payload, self.timeout)
        body = response.body
        code = body.get("errorCode") if isinstance(body, dict) else None
        if response.status != 200 or code:
            message = body.get("errorMessage") if isinstance(body, dict) else body
            transient = response.status in TRANSIENT_HTTP or code in DHAN_RATE_LIMIT_CODES
            raise DhanAPIError(
                f"HTTP {response.status} {code or ''} {message or ''}".strip(),
                transient=transient, status=response.status, code=code,
            )
        if not isinstance(body, dict):
            raise DhanAPIError("unexpected non-JSON response", transient=False, status=response.status)
        return body


# ---------------------------------------------------------------- chunking & download


@dataclass(frozen=True)
class Chunk:
    start: date
    end: date  # inclusive

    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def plan_chunks(start: date, end: date, max_days: int = DHAN_MAX_DAYS_PER_REQUEST) -> list[Chunk]:
    """Consecutive, non-overlapping, inclusive date ranges covering start..end."""
    if end < start:
        raise ValueError("end date is before start date")
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        chunks.append(Chunk(cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


@dataclass
class DownloadResult:
    raw: pd.DataFrame
    chunks: list[dict] = field(default_factory=list)
    failed_chunks: list[dict] = field(default_factory=list)


def response_to_frame(body: dict) -> pd.DataFrame:
    """Dhan returns parallel arrays; timestamps are epoch seconds (UTC)."""
    keys = ("timestamp", "open", "high", "low", "close", "volume")
    missing = [k for k in keys if k not in body]
    if missing and any(body.get(k) for k in keys):
        raise DhanAPIError(f"response missing fields: {missing}", transient=False)
    lengths = {len(body.get(k) or []) for k in keys}
    if len(lengths) > 1:
        raise DhanAPIError(f"response arrays have different lengths: {sorted(lengths)}", transient=False)
    return pd.DataFrame({k: list(body.get(k) or []) for k in keys})


def download(
    client: DhanHistoricalClient, start: date, end: date, *, security_id: str = DEFAULT_SECURITY_ID,
    exchange_segment: str = DEFAULT_EXCHANGE_SEGMENT, instrument: str = DEFAULT_INSTRUMENT, interval: str = "5",
    max_days: int = DHAN_MAX_DAYS_PER_REQUEST, log: Callable[[str], None] = print,
) -> DownloadResult:
    frames = []
    result = DownloadResult(raw=pd.DataFrame())
    for chunk in plan_chunks(start, end, max_days):
        from_dt = datetime.combine(chunk.start, time(0, 0))
        to_dt = datetime.combine(chunk.end, time(23, 59, 59))
        try:
            body = client.intraday_candles(security_id, exchange_segment, instrument, interval, from_dt, to_dt)
            frame = response_to_frame(body)
        except DhanAPIError as error:
            failure = {"chunk": chunk.label(), "error": str(error), "status": error.status, "code": error.code}
            result.failed_chunks.append(failure)
            log(f"FAILED chunk {chunk.label()}: {error}")
            continue
        frame["chunk"] = chunk.label()
        frames.append(frame)
        result.chunks.append({"chunk": chunk.label(), "rows": len(frame)})
        log(f"chunk {chunk.label()}: {len(frame)} candles")
    result.raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=[*SCHEMA[1:], "timestamp"])
    return result


# ---------------------------------------------------------------- normalization & validation


@dataclass
class CleanReport:
    input_rows: int = 0
    duplicate_count: int = 0
    conflicting_duplicate_count: int = 0
    invalid_row_count: int = 0
    invalid_examples: list[dict] = field(default_factory=list)


def _to_ist(values: pd.Series) -> pd.Series:
    """Epoch seconds (UTC) -> tz-aware Asia/Kolkata. Unparseable -> NaT."""
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.to_datetime(numeric, unit="s", utc=True, errors="coerce").dt.tz_convert(IST)


def invalid_candle_mask(df: pd.DataFrame) -> pd.Series:
    """Requirement 15: valid datetime, finite OHLC, and a consistent range."""
    o, h, low, c = (pd.to_numeric(df[k], errors="coerce") for k in ("open", "high", "low", "close"))
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(low) & np.isfinite(c)
    consistent = (h >= low) & (h >= o) & (h >= c) & (low <= o) & (low <= c)
    return ~(df["datetime"].notna() & finite & consistent)


def normalize(raw: pd.DataFrame) -> tuple[pd.DataFrame, CleanReport]:
    """Raw Dhan rows (timestamp + OHLCV, in download order) -> the Phase 6
    schema: IST datetimes, chronological, one row per timestamp, only valid
    candles. Duplicates keep the FIRST occurrence in download order (a stable
    sort), so the result never depends on anything but the input."""
    report = CleanReport(input_rows=len(raw))
    df = pd.DataFrame({
        "datetime": _to_ist(raw["timestamp"]) if len(raw) else pd.Series(dtype="datetime64[ns, Asia/Kolkata]"),
        **{k: pd.to_numeric(raw[k], errors="coerce") if len(raw) else pd.Series(dtype=float)
           for k in ("open", "high", "low", "close")},
        # Missing volume stays missing (never invented); index volume is 0 and only VWAP reads it.
        "volume": pd.to_numeric(raw["volume"], errors="coerce") if len(raw) else pd.Series(dtype=float),
    })

    invalid = invalid_candle_mask(df)
    report.invalid_row_count = int(invalid.sum())
    if invalid.any():
        examples = raw.loc[invalid.to_numpy()].head(5)
        report.invalid_examples = json.loads(examples.drop(columns=["chunk"], errors="ignore").to_json(orient="records"))
    df = df.loc[~invalid.to_numpy()]

    df = df.sort_values("datetime", kind="mergesort")
    dup = df["datetime"].duplicated(keep="first")
    report.duplicate_count = int(dup.sum())
    if dup.any():
        dups = df[df["datetime"].duplicated(keep=False)]
        distinct = dups.groupby("datetime")[["open", "high", "low", "close", "volume"]].nunique()
        report.conflicting_duplicate_count = int((distinct > 1).any(axis=1).sum())
    df = df.loc[~dup.to_numpy()].reset_index(drop=True)
    return df[list(SCHEMA)], report


@dataclass
class GapReport:
    interval_minutes: int
    trading_days: int
    expected_bars_per_day: int
    days_with_missing_bars: dict[str, int] = field(default_factory=dict)
    missing_bars_total: int = 0
    weekdays_without_data: list[str] = field(default_factory=list)
    bars_outside_session: int = 0
    off_grid_bars: int = 0
    max_intraday_gap_minutes: float | None = None
    suspected_timezone_offset: bool = False


def gap_report(df: pd.DataFrame, interval_minutes: int, requested: tuple[date, date] | None = None) -> GapReport:
    """Missing sessions and gaps against the NSE 09:15-15:30 grid. Nothing is
    filled in: a weekday with no candles is listed (it may be an exchange
    holiday - no holiday calendar is assumed), intraday holes are counted."""
    session_minutes = (SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
    expected = math.ceil(session_minutes / interval_minutes)
    report = GapReport(interval_minutes, 0, expected)
    if df.empty:
        if requested:
            report.weekdays_without_data = [d.isoformat() for d in pd.bdate_range(*requested).date]
        return report

    ts = pd.DatetimeIndex(df["datetime"]).tz_convert(IST)
    times = pd.Series(ts.time)
    in_session = (times >= SESSION_OPEN) & (times < SESSION_CLOSE)
    report.bars_outside_session = int((~in_session).sum())
    minutes_from_open = (ts.hour * 60 + ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
    report.off_grid_bars = int(((minutes_from_open % interval_minutes != 0) | (ts.second != 0)).sum())
    # A feed shifted by +/-05:30 puts most bars outside the session.
    report.suspected_timezone_offset = report.bars_outside_session > len(df) / 2

    dates = pd.Series(ts.date)
    counts = dates[in_session.to_numpy()].value_counts()
    report.trading_days = int(dates.nunique())
    for day, n in sorted(counts.items()):
        if n < expected:  # includes a last day still in progress when downloaded
            report.days_with_missing_bars[day.isoformat()] = int(expected - n)
    report.missing_bars_total = sum(report.days_with_missing_bars.values())

    gaps = []
    for _day, group in pd.Series(ts).groupby(dates.to_numpy()):
        if len(group) > 1:
            gaps.append(float(group.diff().dropna().dt.total_seconds().max() / 60))
    report.max_intraday_gap_minutes = max(gaps) if gaps else None

    span = requested or (min(dates), max(dates))
    present = set(dates)
    report.weekdays_without_data = [d.isoformat() for d in pd.bdate_range(span[0], span[1]).date if d not in present]
    return report


# ---------------------------------------------------------------- persistence


def write_dataset(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["datetime"] = out["datetime"].map(lambda t: t.isoformat())
    out.to_csv(path, index=False, float_format="%.6f")


def read_dataset(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in SCHEMA if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(IST)
    return df[list(SCHEMA)]


def metadata_path_for(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + "_metadata.json")


def strategy_smoke_check(df: pd.DataFrame) -> dict:
    """Runs the unchanged canonical strategy over the dataset (research use only)."""
    from fno_signals.config import INDEX_MAP, strategy_config_for
    from fno_signals.strategy import run as run_strategy

    frame = to_strategy_frame(df)
    results, events = run_strategy(frame, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")
    return {"bars_evaluated": len(results), "events": len(events),
            "entries": sum(1 for e in events if e.kind.startswith("ENTRY"))}


def to_strategy_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Phase 6 schema -> the OHLCV frame fno_signals.strategy.run() consumes."""
    frame = df.set_index(pd.DatetimeIndex(df["datetime"], name="Datetime"))
    frame = frame.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return frame[["Open", "High", "Low", "Close", "Volume"]].astype(float)


# ---------------------------------------------------------------- CLI


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def validate_existing(path: Path, interval_minutes: int) -> dict:
    df = read_dataset(path)
    as_raw = df.assign(timestamp=df["datetime"].map(lambda t: t.timestamp() if pd.notna(t) else np.nan))
    clean, report = normalize(as_raw.drop(columns=["datetime"]))
    gaps = gap_report(clean, interval_minutes)
    return {
        "file": str(path), "rows": len(df), "clean_rows": len(clean),
        "unparseable_or_invalid_rows": report.invalid_row_count, "duplicate_count": report.duplicate_count,
        "sorted": bool(df["datetime"].is_monotonic_increasing), "gaps": asdict(gaps),
        "strategy_smoke_check": strategy_smoke_check(clean) if len(clean) else None,
    }


def main(argv: list[str] | None = None, *, client: DhanHistoricalClient | None = None,
         now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    parser = argparse.ArgumentParser(description="Download Dhan intraday candles for Phase 6 research.")
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--security-id", default=DEFAULT_SECURITY_ID)
    parser.add_argument("--exchange-segment", default=DEFAULT_EXCHANGE_SEGMENT)
    parser.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    parser.add_argument("--interval", default="5", choices=DHAN_INTERVALS)
    parser.add_argument("--chunk-days", type=int, default=DHAN_MAX_DAYS_PER_REQUEST)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true", help="replace an existing dataset/metadata")
    parser.add_argument("--allow-partial", action="store_true",
                        help="write the dataset even if some chunks failed (they stay listed in metadata)")
    parser.add_argument("--validate-only", action="store_true", help="validate an existing CSV and exit")
    args = parser.parse_args(argv)
    interval_minutes = int(args.interval)
    metadata_path = args.metadata or metadata_path_for(args.out)

    if args.validate_only:
        if not args.out.exists():
            print(f"{args.out} does not exist", file=sys.stderr)
            return 2
        print(json.dumps(validate_existing(args.out, interval_minutes), indent=1, default=str))
        return 0

    if args.start is None or args.end is None:
        parser.error("--start and --end are required unless --validate-only")
    if args.chunk_days > DHAN_MAX_DAYS_PER_REQUEST:
        parser.error(f"--chunk-days cannot exceed Dhan's {DHAN_MAX_DAYS_PER_REQUEST}-day limit")
    existing = [p for p in (args.out, metadata_path) if p.exists()]
    if existing and not args.overwrite:
        print(f"Refusing to overwrite {', '.join(map(str, existing))} - pass --overwrite", file=sys.stderr)
        return 2

    try:
        client = client or DhanHistoricalClient.from_env(max_retries=args.max_retries)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    result = download(
        client, args.start, args.end, security_id=args.security_id, exchange_segment=args.exchange_segment,
        instrument=args.instrument, interval=args.interval, max_days=args.chunk_days,
    )
    clean, report = normalize(result.raw)
    gaps = gap_report(clean, interval_minutes, requested=(args.start, args.end))
    metadata = {
        "source": "Dhan HQ API v2 - intraday historical candles",
        "endpoint": DHAN_INTRADAY_URL,
        "instrument": {"symbol": args.symbol, "security_id": args.security_id, "instrument": args.instrument},
        "exchange": args.exchange_segment,
        "interval": f"{interval_minutes}m",
        "timezone": IST,
        "requested_start": args.start.isoformat(),
        "requested_end": args.end.isoformat(),
        "actual_first_timestamp": clean["datetime"].iloc[0].isoformat() if len(clean) else None,
        "actual_last_timestamp": clean["datetime"].iloc[-1].isoformat() if len(clean) else None,
        "downloaded_at": now().isoformat(timespec="seconds"),
        "row_count": len(clean),
        "raw_row_count": report.input_rows,
        "duplicate_count": report.duplicate_count,
        "conflicting_duplicate_count": report.conflicting_duplicate_count,
        "invalid_row_count": report.invalid_row_count,
        "invalid_examples": report.invalid_examples,
        "chunks": result.chunks,
        "failed_chunks": result.failed_chunks,
        "gaps": asdict(gaps),
        "complete": not result.failed_chunks,
    }

    if result.failed_chunks and not args.allow_partial:
        print(json.dumps({"failed_chunks": result.failed_chunks}, indent=1), file=sys.stderr)
        print("Dataset NOT written: some date ranges failed (re-run, or pass --allow-partial).", file=sys.stderr)
        return 1
    if clean.empty:
        print("No valid candles downloaded - nothing written.", file=sys.stderr)
        return 1
    write_dataset(clean, args.out)
    metadata["strategy_smoke_check"] = strategy_smoke_check(clean)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=1, default=str))
    print(f"wrote {len(clean)} candles to {args.out} and metadata to {metadata_path}")
    if result.failed_chunks:
        print(f"WARNING: partial dataset - {len(result.failed_chunks)} chunk(s) failed", file=sys.stderr)
    if gaps.suspected_timezone_offset:
        print("WARNING: most candles fall outside 09:15-15:30 IST - check the timestamp timezone", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
