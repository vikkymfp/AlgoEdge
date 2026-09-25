"""Tests for research.phase6.candle_source - SQLite only.

    PYTHONPATH=src:. python -m pytest research/phase6/test_candle_source.py -q
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, insert

from research.phase6 import candle_source as cs
from research.phase6 import historical_db as hdb

REPO = Path(__file__).resolve().parents[2]
WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|REPLACE|UPSERT|GRANT|"
                       r"REVOKE|EXEC|EXECUTE|ATTACH|DETACH|VACUUM|PRAGMA)\b", re.IGNORECASE)


def session_rows(day: str, count: int = 75, *, source="master_5min.csv", index_id="nifty-50", timeframe="5m",
                 load_id=1, base=8000.0):
    start = datetime.fromisoformat(f"{day} 09:15")
    return [{"index_id": index_id, "timeframe": timeframe, "bar_start": start + timedelta(minutes=5 * k),
             "bar_end": start + timedelta(minutes=5 * (k + 1)), "open": base + k, "high": base + k + 3,
             "low": base + k - 3, "close": base + k + 1, "volume": None, "source": source, "load_id": load_id}
            for k in range(count)]


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'candles.db'}")
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad.__table__), [
            {"id": 1, "source": "master_5min.csv", "file_sha256": "a" * 64, "row_count": 0, "valid_rows": 0,
             "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0},
            {"id": 2, "source": "other.csv", "file_sha256": "b" * 64, "row_count": 0, "valid_rows": 0,
             "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0}])
    yield engine
    engine.dispose()


def insert_candles(engine, rows):
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle.__table__), rows)


@pytest.fixture()
def gapped(engine):
    # Fri 2015-06-19, then the excluded window, then Mon 2015-11-16 and Tue 17th.
    insert_candles(engine, session_rows("2015-06-19") + session_rows("2015-11-16") + session_rows("2015-11-17"))
    return engine


@pytest.fixture()
def statements(engine):
    seen: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda _c, _cur, stmt, *_a: seen.append(stmt))
    return seen


# ---------------- A-H: shape, order, types, timezone ----------------


def test_schema_order_and_types(gapped) -> None:
    df = cs.read_candles(gapped)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]  # A
    assert len(df) == 225  # B
    assert df.index.is_monotonic_increasing  # C
    assert df.index.is_unique  # D
    assert all(df[c].dtype == np.float64 for c in df.columns)  # E
    assert df["Volume"].isna().all()  # F: NULL volume stays missing
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.name == "Datetime"


def test_naive_ist_is_localized_not_converted_from_utc(gapped) -> None:
    df = cs.read_candles(gapped)
    assert str(df.index.tz) == "Asia/Kolkata"  # G
    first = df.index[0]
    assert first.isoformat() == "2015-06-19T09:15:00+05:30"  # H: wall-clock preserved
    assert first != pd.Timestamp("2015-06-19 09:15", tz="UTC")  # would be 14:45 IST if read as UTC
    assert first.tz_convert("UTC").isoformat() == "2015-06-19T03:45:00+00:00"


def test_values_round_trip_exactly(gapped) -> None:
    df = cs.read_candles(gapped)
    first = df.iloc[0]
    assert (first.Open, first.High, first.Low, first.Close) == (8000.0, 8003.0, 7997.0, 8001.0)


def test_non_null_volume_is_kept(engine) -> None:
    rows = session_rows("2016-01-04", 2)
    rows[1]["volume"] = 1234.0
    insert_candles(engine, rows)
    assert cs.read_candles(engine)["Volume"].tolist()[1] == 1234.0
    assert np.isnan(cs.read_candles(engine)["Volume"].tolist()[0])


# ---------------- I-M: filters ----------------


def test_start_and_end_filters_are_inclusive_ist(gapped) -> None:
    df = cs.read_candles(gapped, start=datetime(2015, 11, 16, 9, 15), end=datetime(2015, 11, 16, 15, 25))
    assert len(df) == 75  # I + J
    assert df.index[0].isoformat() == "2015-11-16T09:15:00+05:30"
    assert df.index[-1].isoformat() == "2015-11-16T15:25:00+05:30"
    aware = cs.read_candles(gapped, start=pd.Timestamp("2015-11-17 03:45", tz="UTC"))  # = 09:15 IST
    assert aware.index[0].isoformat() == "2015-11-17T09:15:00+05:30" and len(aware) == 75


def test_index_timeframe_and_source_filters(engine) -> None:
    insert_candles(engine, session_rows("2016-01-04", 5)
                   + session_rows("2016-01-04", 3, index_id="bank-nifty")
                   + session_rows("2016-01-04", 4, timeframe="15m")
                   + session_rows("2016-01-05", 2, source="other.csv", load_id=2))
    assert len(cs.read_candles(engine, index_id="bank-nifty")) == 3  # K
    assert len(cs.read_candles(engine, timeframe="15m")) == 4  # L
    assert len(cs.read_candles(engine, source="master_5min.csv")) == 5  # M
    assert len(cs.read_candles(engine, source="other.csv")) == 2
    assert len(cs.read_candles(engine)) == 7  # both sources, no overlapping timestamps


# ---------------- N-Q: segmentation ----------------


def test_multi_month_gap_splits_into_segments(gapped) -> None:
    segments = cs.load_db_segments(gapped)
    assert len(segments) == 2  # N
    assert segments[0].index[-1].isoformat() == "2015-06-19T15:25:00+05:30"
    assert segments[1].index[0].isoformat() == "2015-11-16T09:15:00+05:30"
    assert all(str(s.index.tz) == "Asia/Kolkata" for s in segments)


def test_segmentation_keeps_every_row_and_value(gapped) -> None:
    df = cs.read_candles(gapped)
    segments = cs.contiguous_segments(df)
    pd.testing.assert_frame_equal(pd.concat(segments), df)  # O + Q: same rows, same values, same order
    segments[0].iloc[0, 0] = -1.0  # independent copies
    assert df.iloc[0, 0] == 8000.0


def test_normal_sessions_and_weekends_stay_one_segment(engine) -> None:
    # Thu, Fri, then Mon: an ordinary weekend (and a 4-day holiday weekend) is not a data hole.
    insert_candles(engine, session_rows("2016-03-23") + session_rows("2016-03-28") + session_rows("2016-03-29"))
    segments = cs.load_db_segments(engine)
    assert len(segments) == 1 and len(segments[0]) == 225  # P


def test_max_gap_threshold_is_configurable(gapped) -> None:
    df = cs.read_candles(gapped)
    assert len(cs.contiguous_segments(df, max_gap_days=0.5)) == 3  # overnight Mon->Tue also split
    assert len(cs.contiguous_segments(df, max_gap_days=200)) == 1


def test_no_candles_are_fabricated(engine) -> None:
    rows = session_rows("2016-01-04", 10)
    del rows[3:6]  # a 20-minute hole inside the session
    insert_candles(engine, rows)
    df = cs.read_candles(engine)
    assert len(df) == 7  # Q
    assert [t.strftime("%H:%M") for t in df.index] == ["09:15", "09:20", "09:25", "09:45", "09:50", "09:55", "10:00"]
    assert len(cs.contiguous_segments(df)) == 1


def test_known_missing_days_are_documented() -> None:
    assert [d.isoformat() for d in cs.KNOWN_MISSING_TRADING_DAYS] == ["2015-01-16", "2025-03-20", "2025-03-21"]
    assert cs.DEFAULT_MAX_GAP_DAYS == 7.0


# ---------------- empty and invalid ----------------


def test_empty_result_has_the_same_schema(engine) -> None:
    df = cs.read_candles(engine)
    assert df.empty and list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert str(df.index.tz) == "Asia/Kolkata" and df.index.name == "Datetime"
    assert all(df[c].dtype == np.float64 for c in df.columns)
    assert cs.contiguous_segments(df) == [] and cs.load_db_segments(engine) == []


def test_duplicate_timestamps_fail_instead_of_being_deduplicated(engine) -> None:
    insert_candles(engine, session_rows("2016-01-04", 3)
                   + session_rows("2016-01-04", 2, source="other.csv", load_id=2))
    with pytest.raises(cs.CandleSourceError, match="duplicate bar_start"):
        cs.read_candles(engine)
    assert len(cs.read_candles(engine, source="master_5min.csv")) == 3  # one source is clean


def test_unparseable_timestamp_fails_clearly(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO historical_candles (index_id, timeframe, bar_start, bar_end, open, high, low, close, "
            "source, load_id) VALUES ('nifty-50', '5m', 'garbage', 'garbage', 1, 2, 0.5, 1.5, 'x.csv', 1)")
    with pytest.raises((cs.CandleSourceError, ValueError)):
        cs.read_candles(engine)


def test_unordered_frame_is_refused_by_segmentation() -> None:
    idx = pd.DatetimeIndex(["2016-01-04 09:20", "2016-01-04 09:15"]).tz_localize("Asia/Kolkata")
    with pytest.raises(cs.CandleSourceError):
        cs.contiguous_segments(pd.DataFrame({c: [1.0, 1.0] for c in cs.OHLCV}, index=idx))


def test_engine_is_required() -> None:
    with pytest.raises(ValueError, match="engine"):
        cs.read_candles(None)


# ---------------- R-T: read-only and isolation ----------------


def test_reader_issues_select_statements_only(gapped, statements) -> None:
    cs.read_candles(gapped)
    cs.read_candles(gapped, start=datetime(2015, 11, 16), end=datetime(2015, 11, 17), source="master_5min.csv")
    cs.load_db_segments(gapped)
    cs.read_candles(gapped, index_id="none")  # empty result path
    assert statements, "no SQL captured"
    offending = [s for s in statements if WRITE_SQL.match(s) or not s.lstrip().upper().startswith("SELECT")]
    assert offending == []  # R


def test_guard_would_catch_a_write(engine, statements) -> None:
    insert_candles(engine, session_rows("2016-01-04", 1))
    assert any(WRITE_SQL.match(s) for s in statements)  # proves the listener sees writes


def test_reader_does_not_import_production_persistence() -> None:
    code = ("import sys, research.phase6.candle_source\n"
            "print(sorted(m for m in ('algoedge.db', 'algoedge.models') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": f"{REPO / 'src'}:{REPO}", "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "[]"  # S


def test_nothing_under_src_imports_research() -> None:
    pattern = re.compile(r"^\s*(from|import)\s+research\b", re.MULTILINE)
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "src").rglob("*.py") if pattern.search(p.read_text())]
    assert offenders == []  # T
