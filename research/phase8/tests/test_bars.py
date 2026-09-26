"""Phase 8.0: market-bar evidence capture (synthetic yfinance-shaped frames)."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from algoedge import auto_trader
from research.phase8.tools import bars
from research.phase8.tools.common import IST, read_jsonl

FIVE = timedelta(minutes=5)


def window(start="2026-10-01 09:15", n=10, overrides=None, drop=()):
    index = pd.date_range(start, periods=n, freq="5min", tz="Asia/Kolkata")
    closes = [100.0 + i for i in range(n)]
    frame = pd.DataFrame({"Open": closes, "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes],
                          "Close": closes, "Volume": [0.0] * n}, index=index)
    for ts, values in (overrides or {}).items():
        for column, value in values.items():
            frame.loc[pd.Timestamp(ts, tz="Asia/Kolkata"), column] = value
    return frame.drop(index=[pd.Timestamp(ts, tz="Asia/Kolkata") for ts in drop])


def ist(text):
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def test_a_normal_completed_window() -> None:
    frame = window(n=10)  # 09:15 .. 10:00
    result = bars.analyze(frame, ist("2026-10-01T10:05:30"), FIVE)
    summary = result["summary"]
    assert summary["bar_count"] == 10 and summary["forming_bar"] is None
    assert summary["latest_completed_bar"] == "2026-10-01T10:00:00+05:30"
    assert summary["latest_completed_age_seconds"] == 30.0
    assert (summary["stale"], summary["missing_bars"], summary["invalid_bars"]) == (False, [], [])
    first = result["bars"][0]
    assert first == {"bar_start": "2026-10-01T09:15:00+05:30", "open": 100.0, "high": 101.0, "low": 99.0,
                     "close": 100.0, "volume": 0.0, "state": "completed", "valid": True, "invalid_reasons": []}


def test_the_forming_bar_is_identified_with_the_engines_rule() -> None:
    frame = window(n=11)  # last bar 10:05 is still forming at 10:07
    observed = ist("2026-10-01T10:07:00")
    result = bars.analyze(frame, observed, FIVE)
    assert result["summary"]["forming_bar"] == "2026-10-01T10:05:00+05:30"
    assert result["bars"][-1]["state"] == "forming"
    assert result["summary"]["latest_completed_bar"] == "2026-10-01T10:00:00+05:30"
    engine_completed = auto_trader._completed_bars(frame, FIVE, observed)
    ours = [b["bar_start"] for b in result["bars"] if b["state"] == "completed"]
    assert ours == [ts.isoformat() for ts in engine_completed.index]


def test_a_stale_window_during_the_session() -> None:
    result = bars.analyze(window(n=10), ist("2026-10-01T10:20:00"), FIVE)  # latest bar closed 10:05
    summary = result["summary"]
    assert summary["stale"] is True and "900s ago" in summary["stale_reason"]
    assert summary["missing_bars"] == ["2026-10-01T10:05:00+05:30", "2026-10-01T10:10:00+05:30",
                                       "2026-10-01T10:15:00+05:30"]


def test_staleness_is_not_applicable_outside_session_hours() -> None:
    result = bars.analyze(window(n=10), ist("2026-10-01T16:00:00"), FIVE)
    assert result["summary"]["stale"] is None


def test_a_missing_bar_is_reported_never_filled() -> None:
    frame = window(n=10, drop=["2026-10-01 09:40"])
    result = bars.analyze(frame, ist("2026-10-01T10:05:30"), FIVE)
    assert result["summary"]["missing_bars"] == ["2026-10-01T09:40:00+05:30"]
    assert "2026-10-01T09:40:00+05:30" not in [b["bar_start"] for b in result["bars"]]
    assert len(result["bars"]) == 9


def test_malformed_ohlc_is_recorded_as_invalid_with_its_raw_values() -> None:
    frame = window(n=10, overrides={"2026-10-01 09:30": {"Close": float("nan")},
                                    "2026-10-01 09:35": {"High": 50.0, "Low": 60.0}})
    result = bars.analyze(frame, ist("2026-10-01T10:05:30"), FIVE)
    by_start = {b["bar_start"]: b for b in result["bars"]}
    nan_bar, inverted = by_start["2026-10-01T09:30:00+05:30"], by_start["2026-10-01T09:35:00+05:30"]
    assert nan_bar["close"] == "NaN" and not nan_bar["valid"] and nan_bar["invalid_reasons"] == ["Close not finite"]
    assert (inverted["high"], inverted["low"]) == (50.0, 60.0) and inverted["invalid_reasons"] == ["High < Low"]
    assert result["summary"]["invalid_bars"] == ["2026-10-01T09:30:00+05:30", "2026-10-01T09:35:00+05:30"]


def test_a_delayed_bar_is_measured_from_its_close() -> None:
    ledger = bars.IndexLedger()
    first_seen = window(n=9)  # 09:15 .. 09:55; the 10:00 bar is late
    first = bars.capture_index("nifty-50", fetch=lambda *a, **k: first_seen, interval="5m",
                               observed_at_fn=lambda: ist("2026-10-01T10:05:30"), ledger=ledger, run_id="r", seq=0)
    assert all(a["delay_seconds"] is None for a in first["summary"]["newly_completed"])  # unknown on first capture
    later = window(n=11)  # 10:00 and 10:05 arrive together at 10:12
    second = bars.capture_index("nifty-50", fetch=lambda *a, **k: later, interval="5m",
                                observed_at_fn=lambda: ist("2026-10-01T10:12:00"), ledger=ledger, run_id="r", seq=1)
    arrivals = {a["bar_start"]: a for a in second["summary"]["newly_completed"]}
    assert arrivals["2026-10-01T10:00:00+05:30"]["delay_seconds"] == 420.0
    assert arrivals["2026-10-01T10:00:00+05:30"]["delayed"] is True
    assert arrivals["2026-10-01T10:00:00+05:30"]["beyond_freshness"] is False
    assert arrivals["2026-10-01T10:05:00+05:30"]["delayed"] is False  # closed 10:10, seen 10:12
    assert arrivals["2026-10-01T10:00:00+05:30"]["resolution_seconds"] == 390.0


def test_a_fetch_failure_is_recorded_not_replaced() -> None:
    def failing(*_a, **_k):
        raise RuntimeError("No data returned for ^NSEI")

    record = bars.capture_index("nifty-50", fetch=failing, interval="5m",
                                observed_at_fn=lambda: ist("2026-10-01T10:05:30"), ledger=bars.IndexLedger(),
                                run_id="r", seq=0)
    assert record["fetch"]["ok"] is False and record["fetch"]["error"]["type"] == "RuntimeError"
    assert record["window"] is None and "summary" not in record


def test_capture_uses_the_engines_provider_arguments() -> None:
    seen = []

    def fetch(ticker, period, interval):
        seen.append((ticker, period, interval))
        return window(n=3)

    for index_id in ("nifty-50", "sensex", "bank-nifty"):
        bars.capture_index(index_id, fetch=fetch, interval="5m", observed_at_fn=lambda: ist("2026-10-01T10:00:00"),
                           ledger=bars.IndexLedger(), run_id="r", seq=0)
    assert seen == [("^NSEI", "5d", "5m"), ("^BSESN", "5d", "5m"), ("^NSEBANK", "5d", "5m")]
    assert bars.INDEX_CHOICE == auto_trader._INDEX_CHOICE


def test_delta_captures_rebuild_every_window_exactly(tmp_path) -> None:
    frames = iter([window(n=10), window(n=11),
                   window(n=11, overrides={"2026-10-01 09:20": {"Close": 999.0}})])  # a completed bar revised
    fetched = []

    def fetch(*_a, **_k):
        frame = next(frames)
        fetched.append(frame)
        return frame

    class Clock:
        def __init__(self):
            self.times = [ist("2026-10-01T10:05:30"), ist("2026-10-01T10:10:30"), ist("2026-10-01T10:11:30")]
            self.i = 0

        def __call__(self):
            return self.times[min(self.i, 2)]

        def sleep(self, _seconds):
            self.i += 1

    fake = Clock()
    path = bars.run(tmp_path, indices=["nifty-50"], captures=3, every=60, fetch=fetch, clock=fake,
                    sleep=fake.sleep, run_id="r1")
    records = read_jsonl(path)
    assert [r["window"]["full"] for r in records] == [True, False, False]
    assert len(records[1]["window"]["bars"]) == 1  # only the new bar is stored
    assert records[2]["window"]["revised_completed_bars"][0]["after"]["close"] == 999.0
    rebuilt = bars.load_capture_windows([path])["nifty-50"]
    assert len(rebuilt) == 3
    for capture, frame in zip(rebuilt, fetched):
        assert [b["close"] for b in capture.bars.values()] == frame["Close"].tolist()

    tampered = tmp_path / "bars_tampered.jsonl"
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["window"]["bars"][0]["close"] = 1.0
    tampered.write_text("\n".join([lines[0], json.dumps(record), lines[2]]) + "\n")
    with pytest.raises(ValueError, match="does not match its sha256"):
        bars.load_capture_windows([tampered])


def test_unknown_indices_are_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        bars.run(tmp_path, indices=["dow-jones"], captures=1)


def test_observed_time_is_recorded_in_utc_and_ist() -> None:
    record = bars.capture_index("nifty-50", fetch=lambda *a, **k: window(n=2), interval="5m",
                                observed_at_fn=lambda: datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc),
                                ledger=bars.IndexLedger(), run_id="r", seq=0)
    assert record["observed_at"] == {"utc": "2026-10-01T04:30:00+00:00", "ist": "2026-10-01T10:00:00+05:30"}
    assert (record["source"], record["timezone"]) == (bars.SOURCE, "Asia/Kolkata")
