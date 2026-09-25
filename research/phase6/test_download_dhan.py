"""Mocked tests for research.phase6.download_dhan - no Dhan credentials and
no network: every request goes to an in-process fake transport.

    PYTHONPATH=src:. python -m pytest research/phase6/test_download_dhan.py -q
"""

import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from research.phase6 import data as data_mod
from research.phase6 import download_dhan as dd

TOKEN = "test-token-not-real"


def session_epochs(day: date, interval: int = 5) -> list[int]:
    """Epoch seconds (UTC) of every NSE bar start 09:15..15:25 IST on `day`."""
    start = pd.Timestamp(f"{day} 09:15", tz="Asia/Kolkata")
    return [int((start + pd.Timedelta(minutes=interval * k)).timestamp()) for k in range(375 // interval)]


def candles_for(days: list[date], seed: int = 0) -> dict:
    epochs = [e for d in days for e in session_epochs(d)]
    rng = np.random.default_rng(seed)
    close = 24000 + np.cumsum(rng.normal(0, 10, len(epochs)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 3
    low = np.minimum(open_, close) - 3
    return {"timestamp": epochs, "open": open_.tolist(), "high": high.tolist(), "low": low.tolist(),
            "close": close.tolist(), "volume": [0] * len(epochs)}


class FakeDhan:
    """Serves weekdays in the requested range; scripted failures per call."""

    def __init__(self, failures: dict[int, list[dd.HttpResponse | Exception]] | None = None,
                 mutate=None):
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.failures = failures or {}
        self.mutate = mutate
        self.attempts: dict[str, int] = {}

    def __call__(self, url, headers, payload, timeout):
        assert url == dd.DHAN_INTRADAY_URL
        self.calls.append(payload)
        self.headers.append(headers)
        key = payload["fromDate"]
        n = self.attempts.get(key, 0)
        self.attempts[key] = n + 1
        chunk_index = len({p["fromDate"] for p in self.calls}) - 1
        scripted = self.failures.get(chunk_index, [])
        if n < len(scripted):
            outcome = scripted[n]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        start = datetime.strptime(payload["fromDate"], dd.DHAN_REQUEST_TIME_FORMAT).date()
        end = datetime.strptime(payload["toDate"], dd.DHAN_REQUEST_TIME_FORMAT).date()
        body = candles_for(list(pd.bdate_range(start, end).date), seed=start.toordinal())
        if self.mutate:
            body = self.mutate(body, payload)
        return dd.HttpResponse(200, body)


def client(transport, **kw) -> dd.DhanHistoricalClient:
    kw.setdefault("sleep", lambda _s: None)
    return dd.DhanHistoricalClient(TOKEN, "1000000001", transport=transport, **kw)


def run_cli(tmp_path, *args, transport=None, **kw):
    out = tmp_path / "phase6_nifty50_5m.csv"
    argv = ["--out", str(out), *args]
    code = dd.main(argv, client=client(transport or FakeDhan(), **kw),
                   now=lambda: datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
    return code, out, dd.metadata_path_for(out)


# ---------------- successful download ----------------


def test_successful_download_writes_schema_csv_and_metadata(tmp_path) -> None:
    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-25")
    assert code == 0
    header = out.read_text().splitlines()[0]
    assert header == "datetime,open,high,low,close,volume"
    df = dd.read_dataset(out)
    assert len(df) == 5 * 75
    assert str(df["datetime"].dt.tz) == "Asia/Kolkata"

    meta = json.loads(meta_path.read_text())
    assert meta["source"].startswith("Dhan")
    assert meta["instrument"] == {"symbol": "NIFTY50", "security_id": "13", "instrument": "INDEX"}
    assert meta["exchange"] == "IDX_I" and meta["interval"] == "5m"
    assert meta["requested_start"] == "2026-09-21" and meta["requested_end"] == "2026-09-25"
    assert meta["actual_first_timestamp"] == "2026-09-21T09:15:00+05:30"
    assert meta["actual_last_timestamp"] == "2026-09-25T15:25:00+05:30"
    assert meta["downloaded_at"] == "2026-09-25T12:00:00+00:00"
    assert meta["row_count"] == 375
    assert meta["duplicate_count"] == 0 and meta["invalid_row_count"] == 0
    assert meta["failed_chunks"] == [] and meta["complete"] is True
    assert meta["strategy_smoke_check"]["bars_evaluated"] == 375


def test_request_payload_matches_the_dhan_contract() -> None:
    fake = FakeDhan()
    dd.download(client(fake), date(2026, 9, 21), date(2026, 9, 22), log=lambda _m: None)
    assert fake.calls == [{
        "securityId": "13", "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "5", "oi": False,
        "fromDate": "2026-09-21 00:00:00", "toDate": "2026-09-22 23:59:59",
    }]
    assert fake.headers[0]["access-token"] == TOKEN
    assert fake.headers[0]["client-id"] == "1000000001"


# ---------------- chunking ----------------


def test_plan_chunks_is_contiguous_non_overlapping_and_capped() -> None:
    chunks = dd.plan_chunks(date(2026, 1, 1), date(2026, 7, 19), max_days=90)
    assert [c.label() for c in chunks] == [
        "2026-01-01..2026-03-31", "2026-04-01..2026-06-29", "2026-06-30..2026-07-19",
    ]
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert (b.start - a.end).days == 1
    assert all((c.end - c.start).days + 1 <= 90 for c in chunks)
    with pytest.raises(ValueError):
        dd.plan_chunks(date(2026, 2, 1), date(2026, 1, 1))


def test_multiple_chunks_are_all_downloaded_and_merged(tmp_path) -> None:
    fake = FakeDhan()
    code, out, meta_path = run_cli(tmp_path, "--start", "2026-01-01", "--end", "2026-07-19", transport=fake)
    assert code == 0
    assert len(fake.calls) == 3
    df = dd.read_dataset(out)
    expected_days = len(pd.bdate_range("2026-01-01", "2026-07-19"))
    assert len(df) == expected_days * 75
    meta = json.loads(meta_path.read_text())
    assert [c["chunk"] for c in meta["chunks"]] == [
        "2026-01-01..2026-03-31", "2026-04-01..2026-06-29", "2026-06-30..2026-07-19",
    ]


def test_chunk_days_above_the_dhan_limit_is_rejected(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run_cli(tmp_path, "--start", "2026-01-01", "--end", "2026-07-19", "--chunk-days", "120")


# ---------------- duplicates, ordering, determinism ----------------


def _raw(rows):
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def test_duplicate_candles_are_removed_keeping_the_first_occurrence() -> None:
    t0, t1 = session_epochs(date(2026, 9, 21))[:2]
    raw = _raw([
        (t0, 100, 102, 99, 101, 0),
        (t1, 101, 103, 100, 102, 0),
        (t0, 100, 102, 99, 101, 0),  # exact duplicate
        (t1, 101, 109, 100, 108, 0),  # conflicting duplicate - first wins
    ])
    clean, report = dd.normalize(raw)
    assert len(clean) == 2
    assert report.duplicate_count == 2
    assert report.conflicting_duplicate_count == 1
    assert clean.loc[1, "close"] == 102


def test_overlapping_chunk_responses_are_deduplicated(tmp_path) -> None:
    def overlap(body, payload):
        # Every response also repeats the first bar of the NEXT calendar day.
        start = datetime.strptime(payload["toDate"], dd.DHAN_REQUEST_TIME_FORMAT).date()
        extra = candles_for([pd.Timestamp(start) + pd.offsets.BDay(1)])
        return {k: body[k] + extra[k][:1] for k in body}

    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-14", "--end", "2026-09-25",
                                   "--chunk-days", "5", transport=FakeDhan(mutate=overlap))
    assert code == 0
    meta = json.loads(meta_path.read_text())
    assert meta["duplicate_count"] >= 1
    df = dd.read_dataset(out)
    assert df["datetime"].is_unique and df["datetime"].is_monotonic_increasing


def test_ordering_is_chronological_and_deterministic() -> None:
    body = candles_for([date(2026, 9, 21), date(2026, 9, 22)])
    raw = _raw(list(zip(*[body[k] for k in ("timestamp", "open", "high", "low", "close", "volume")], strict=True)))
    shuffled = raw.sample(frac=1.0, random_state=7).reset_index(drop=True)
    a, _ = dd.normalize(shuffled)
    b, _ = dd.normalize(raw.iloc[::-1].reset_index(drop=True))
    assert a["datetime"].is_monotonic_increasing
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(a, dd.normalize(shuffled)[0])


# ---------------- invalid candles ----------------


@pytest.mark.parametrize("row", [
    ("t", 100, 99, 101, 100, 0),  # high < low
    ("t", 105, 104, 99, 101, 0),  # high < open
    ("t", 100, 102, 99, 103, 0),  # high < close
    ("t", 98, 102, 99, 101, 0),  # low > open
    ("t", 100, 102, 99, 98.5, 0),  # low > close
    ("t", np.nan, 102, 99, 101, 0),  # missing open
    ("t", 100, np.inf, 99, 101, 0),  # non-finite high
    ("bad", 100, 102, 99, 101, 0),  # unparseable timestamp
])
def test_invalid_candles_are_removed_and_counted(row) -> None:
    t0, t1 = session_epochs(date(2026, 9, 21))[:2]
    bad = (t1 if row[0] == "t" else "not-a-time", *row[1:])
    clean, report = dd.normalize(_raw([(t0, 100, 102, 99, 101, 0), bad]))
    assert len(clean) == 1 and report.invalid_row_count == 1
    assert report.invalid_examples


def test_invalid_rows_are_reported_in_metadata(tmp_path) -> None:
    def corrupt(body, _payload):
        body["high"][3] = body["low"][3] - 1
        return body

    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21",
                                   transport=FakeDhan(mutate=corrupt))
    assert code == 0
    meta = json.loads(meta_path.read_text())
    assert meta["invalid_row_count"] == 1 and meta["row_count"] == 74
    assert meta["gaps"]["days_with_missing_bars"] == {"2026-09-21": 1}  # removed, never repaired


# ---------------- missing candles / sessions ----------------


def test_missing_candles_are_preserved_not_interpolated(tmp_path) -> None:
    def drop_bars(body, _payload):
        keep = [i for i in range(len(body["timestamp"])) if i not in (10, 11, 12)]
        return {k: [body[k][i] for i in keep] for k in body}

    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21",
                                   transport=FakeDhan(mutate=drop_bars))
    assert code == 0
    df = dd.read_dataset(out)
    assert len(df) == 72  # nothing filled in
    gaps = json.loads(meta_path.read_text())["gaps"]
    assert gaps["days_with_missing_bars"] == {"2026-09-21": 3}
    assert gaps["missing_bars_total"] == 3
    assert gaps["max_intraday_gap_minutes"] == 20.0


def test_missing_trading_sessions_are_listed() -> None:
    days = [date(2026, 9, 21), date(2026, 9, 23)]  # the 22nd (a Tuesday) is absent
    raw = _raw(list(zip(*[candles_for(days)[k] for k in ("timestamp", "open", "high", "low", "close", "volume")],
                        strict=True)))
    clean, _ = dd.normalize(raw)
    gaps = dd.gap_report(clean, 5, requested=(date(2026, 9, 21), date(2026, 9, 23)))
    assert gaps.weekdays_without_data == ["2026-09-22"]
    assert gaps.trading_days == 2 and gaps.missing_bars_total == 0


# ---------------- API errors and retries ----------------


def test_transient_errors_are_retried_with_backoff() -> None:
    sleeps = []
    fake = FakeDhan(failures={0: [
        dd.HttpResponse(429, {"errorCode": "DH-904", "errorMessage": "Too many requests"}),
        dd.DhanAPIError("network error: ConnectionError", transient=True),
        dd.HttpResponse(503, "Service Unavailable"),
    ]})
    result = dd.download(client(fake, sleep=sleeps.append, backoff_seconds=1.0),
                         date(2026, 9, 21), date(2026, 9, 21), log=lambda _m: None)
    assert result.failed_chunks == []
    assert len(result.raw) == 75
    assert len(fake.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_rate_limit_error_code_is_transient_even_with_http_200() -> None:
    fake = FakeDhan(failures={0: [dd.HttpResponse(200, {"errorCode": "DH-904", "errorMessage": "rate"})]})
    result = dd.download(client(fake), date(2026, 9, 21), date(2026, 9, 21), log=lambda _m: None)
    assert result.failed_chunks == [] and len(fake.calls) == 2


def test_exhausted_retries_fail_the_chunk_and_are_reported() -> None:
    fake = FakeDhan(failures={1: [dd.HttpResponse(500, "boom")] * 10})
    result = dd.download(client(fake, max_retries=2), date(2026, 9, 1), date(2026, 9, 30), max_days=10,
                         log=lambda _m: None)
    assert [f["chunk"] for f in result.failed_chunks] == ["2026-09-11..2026-09-20"]
    assert result.failed_chunks[0]["status"] == 500
    assert fake.attempts["2026-09-11 00:00:00"] == 3  # 1 try + 2 retries
    assert len(result.chunks) == 2  # the other chunks still downloaded


def test_non_transient_error_is_not_retried() -> None:
    fake = FakeDhan(failures={0: [dd.HttpResponse(401, {"errorCode": "DH-901", "errorMessage": "Invalid token"})]})
    result = dd.download(client(fake), date(2026, 9, 21), date(2026, 9, 21), log=lambda _m: None)
    assert len(fake.calls) == 1
    assert result.failed_chunks[0]["code"] == "DH-901"


def test_a_failed_chunk_blocks_the_write_unless_allow_partial(tmp_path) -> None:
    failing = {1: [dd.HttpResponse(400, {"errorCode": "DH-905", "errorMessage": "Input exception"})]}
    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-01", "--end", "2026-09-30", "--chunk-days", "10",
                                   transport=FakeDhan(failures=failing))
    assert code == 1
    assert not out.exists() and not meta_path.exists()

    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-01", "--end", "2026-09-30", "--chunk-days", "10",
                                   "--allow-partial", transport=FakeDhan(failures=failing))
    assert code == 0
    meta = json.loads(meta_path.read_text())
    assert meta["complete"] is False
    assert meta["failed_chunks"][0]["chunk"] == "2026-09-11..2026-09-20"
    assert set(meta["gaps"]["weekdays_without_data"]) >= {"2026-09-14", "2026-09-18"}


def test_malformed_response_fails_the_chunk() -> None:
    fake = FakeDhan(mutate=lambda body, _p: {**body, "close": body["close"][:-1]})
    result = dd.download(client(fake), date(2026, 9, 21), date(2026, 9, 21), log=lambda _m: None)
    assert "different lengths" in result.failed_chunks[0]["error"]


# ---------------- timezone ----------------


def test_epoch_timestamps_are_normalized_to_ist() -> None:
    utc_epoch = int(pd.Timestamp("2026-09-21 03:45:00", tz="UTC").timestamp())
    clean, _ = dd.normalize(_raw([(utc_epoch, 100, 101, 99, 100, 0)]))
    assert clean.loc[0, "datetime"].isoformat() == "2026-09-21T09:15:00+05:30"


def test_a_timezone_shifted_feed_is_flagged() -> None:
    # Candles whose epochs are IST wall-clock values mislabelled as UTC land
    # 5h30m late (14:45-20:55 IST) - most fall outside the session.
    shift = 5 * 3600 + 1800
    body = candles_for([date(2026, 9, 21)])
    body["timestamp"] = [t + shift for t in body["timestamp"]]
    raw = _raw(list(zip(*[body[k] for k in ("timestamp", "open", "high", "low", "close", "volume")], strict=True)))
    clean, _ = dd.normalize(raw)
    assert dd.gap_report(clean, 5).suspected_timezone_offset is True
    assert dd.gap_report(dd.normalize(_raw([(t - shift, 1, 2, 0.5, 1.5, 0) for t in body["timestamp"]]))[0], 5) \
        .suspected_timezone_offset is False


# ---------------- existing-file protection & credentials ----------------


def test_existing_dataset_is_not_overwritten_without_flag(tmp_path) -> None:
    out = tmp_path / "phase6_nifty50_5m.csv"
    out.write_text("sentinel")
    fake = FakeDhan()
    code, out, _meta = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21", transport=fake)
    assert code == 2
    assert out.read_text() == "sentinel"
    assert fake.calls == []  # refused before any request

    code, out, _meta = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21", "--overwrite")
    assert code == 0 and out.read_text().startswith("datetime,")


def test_existing_metadata_alone_also_blocks_the_write(tmp_path) -> None:
    dd.metadata_path_for(tmp_path / "phase6_nifty50_5m.csv").write_text("{}")
    code, out, _meta = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21")
    assert code == 2 and not out.exists()


def test_missing_token_is_refused_and_credentials_never_leak(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(dd.ENV_ACCESS_TOKEN, raising=False)
    code = dd.main(["--out", str(tmp_path / "x.csv"), "--start", "2026-09-21", "--end", "2026-09-21"])
    assert code == 2

    monkeypatch.setenv(dd.ENV_ACCESS_TOKEN, TOKEN)
    monkeypatch.setenv(dd.ENV_CLIENT_ID, "1000000001")
    from_env = dd.DhanHistoricalClient.from_env(transport=FakeDhan())
    assert TOKEN not in repr(from_env)

    code, out, meta_path = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-21")
    assert code == 0
    assert TOKEN not in out.read_text() and TOKEN not in meta_path.read_text()
    assert "1000000001" not in meta_path.read_text()


# ---------------- validate-only & integration ----------------


def test_validate_only_reports_on_an_existing_file(tmp_path, capsys) -> None:
    code, out, _meta = run_cli(tmp_path, "--start", "2026-09-21", "--end", "2026-09-22")
    capsys.readouterr()
    code = dd.main(["--out", str(out), "--validate-only"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rows"] == report["clean_rows"] == 150
    assert report["duplicate_count"] == 0 and report["unparseable_or_invalid_rows"] == 0 and report["sorted"]
    assert report["strategy_smoke_check"]["bars_evaluated"] == 150


def test_validate_only_on_a_missing_file_fails(tmp_path) -> None:
    assert dd.main(["--out", str(tmp_path / "nope.csv"), "--validate-only"]) == 2


def test_research_loader_reads_the_dhan_dataset_for_the_canonical_strategy(tmp_path) -> None:
    from fno_signals.config import INDEX_MAP, strategy_config_for
    from fno_signals.strategy import run as run_strategy

    code, out, _meta = run_cli(tmp_path, "--start", "2026-06-01", "--end", "2026-07-31")
    assert code == 0
    frame = data_mod.load_csv(out)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert str(frame.index.tz) == "Asia/Kolkata" and frame.index.is_monotonic_increasing
    pd.testing.assert_frame_equal(frame, dd.to_strategy_frame(dd.read_dataset(out)), check_names=False)

    results, events = run_strategy(frame, strategy_config_for(INDEX_MAP[1]), underlying_label="NIFTY 50")
    assert len(results) == len(frame)
    assert any(e.kind.startswith("ENTRY") for e in events)
    assert data_mod.quality_report(frame, "5m").missing_bars_total == 0


def test_research_runner_accepts_the_dhan_csv(tmp_path) -> None:
    from research.phase6 import run as run_mod

    code, out, _meta = run_cli(tmp_path, "--start", "2026-06-01", "--end", "2026-07-31")
    results_dir = tmp_path / "results"
    assert run_mod.main(["--csv", str(out), "--indices", "nifty-50", "--out", str(results_dir)]) == 0
    report = (results_dir / "phase6_nifty50_5m_nifty-50.md").read_text()
    assert "### Baseline" in report and "| baseline |" in report
