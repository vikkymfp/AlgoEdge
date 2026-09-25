"""Tests for research.phase6.run --db (DB-backed candle input) - SQLite only.

    PYTHONPATH=src:. python -m pytest research/phase6/test_run_db.py -q
"""

import json
import re
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, insert

from research.phase6 import candle_source, historical_db
from research.phase6 import engine as engine_mod
from research.phase6 import run as run_mod

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|REPLACE|PRAGMA)\b", re.I)
GAP_LIMIT = pd.Timedelta(days=candle_source.DEFAULT_MAX_GAP_DAYS)
SEG1_DAYS = pd.bdate_range("2015-06-08", "2015-06-19")  # ends right before the excluded window
SEG2_DAYS = pd.bdate_range("2015-11-16", "2015-11-27")  # starts right after it


def candle_rows(days, *, source, load_id, seed):
    rng = np.random.default_rng(seed)
    rows = []
    price = 8000.0
    for day in days:
        first = datetime.fromisoformat(f"{day.date()} 09:15")
        for k in range(75):
            start = first + timedelta(minutes=5 * k)
            close = price + rng.normal(0, 8)
            rows.append({"index_id": "nifty-50", "timeframe": "5m", "bar_start": start,
                         "bar_end": start + timedelta(minutes=5), "open": price,
                         "high": max(price, close) + abs(rng.normal(0, 4)),
                         "low": min(price, close) - abs(rng.normal(0, 4)), "close": close, "volume": None,
                         "source": source, "load_id": load_id})
            price = close
    return rows


@pytest.fixture()
def research_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    historical_db.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(historical_db.HistoricalCandleLoad.__table__), [
            {"id": 1, "source": "master_5min.csv", "file_sha256": "a" * 64, "row_count": 0, "valid_rows": 0,
             "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0},
            {"id": 2, "source": "other.csv", "file_sha256": "b" * 64, "row_count": 0, "valid_rows": 0,
             "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0}])
        conn.execute(insert(historical_db.HistoricalCandle.__table__),
                     candle_rows(SEG1_DAYS, source="master_5min.csv", load_id=1, seed=1)
                     + candle_rows(SEG2_DAYS, source="master_5min.csv", load_id=1, seed=2)
                     # Same timestamps from another dataset: reading without source= would raise.
                     + candle_rows(SEG2_DAYS[:2], source="other.csv", load_id=2, seed=3))
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda _c, _cur, stmt, *_a: statements.append(stmt))
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: engine)
    yield engine, statements
    engine.dispose()


@pytest.fixture()
def spies(monkeypatch):
    """Records the frame handed to every strategy entry point used by run_index."""
    frames: list[tuple[str, pd.DatetimeIndex]] = []
    reader_calls: list[dict] = []

    def wrap(module, name):
        real = getattr(module, name)

        def spy(df, *args, **kwargs):
            frames.append((name, df.index))
            return real(df, *args, **kwargs)
        monkeypatch.setattr(module, name, spy)

    wrap(run_mod, "canonical_run")
    wrap(run_mod, "simulate")
    wrap(engine_mod, "simulate")  # production_splits calls it from inside engine.py

    real_run_index = run_mod.run_index
    run_index_frames: list[pd.DataFrame] = []

    def run_index_spy(label, df, interval, key):
        run_index_frames.append(df)
        return real_run_index(label, df, interval, key)
    monkeypatch.setattr(run_mod, "run_index", run_index_spy)

    real_reader = candle_source.load_db_segments

    def reader_spy(engine, **kwargs):
        reader_calls.append(kwargs)
        return real_reader(engine, **kwargs)
    monkeypatch.setattr(candle_source, "load_db_segments", reader_spy)
    return frames, run_index_frames, reader_calls


def run_db(tmp_path, *extra):
    return run_mod.main(["--db", "--indices", "nifty-50", "--out", str(tmp_path / "out"), *extra])


# ---------------- the DB path ----------------


def test_db_path_runs_each_segment_separately(research_db, spies, tmp_path, capsys) -> None:
    _engine, statements = research_db
    frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path) == 0

    # Loads the expected candles, in exactly two independent segments.
    assert [len(f) for f in run_index_frames] == [len(SEG1_DAYS) * 75, len(SEG2_DAYS) * 75]
    seg1, seg2 = run_index_frames
    assert seg1.index[0].isoformat() == "2015-06-08T09:15:00+05:30"
    assert seg1.index[-1].isoformat() == "2015-06-19T15:25:00+05:30"
    assert seg2.index[0].isoformat() == "2015-11-16T09:15:00+05:30"
    assert seg2.index[-1].isoformat() == "2015-11-27T15:25:00+05:30"
    assert str(seg1.index.tz) == str(seg2.index.tz) == "Asia/Kolkata"

    # The explicit source (and the interval) went to the reader.
    assert reader_calls == [{"index_id": "nifty-50", "timeframe": "5m", "start": None, "end": None,
                             "source": "master_5min.csv"}]

    # No strategy run ever saw bars from both sides of the gap.
    assert frames, "strategy entry points were not called"
    boundary = pd.Timestamp("2015-06-22", tz="Asia/Kolkata")
    for name, index in frames:
        assert (index < boundary).all() or (index >= boundary).all(), name
        assert (index.to_series().diff().dropna() <= GAP_LIMIT).all(), name

    # One report section per segment, no aggregate across them.
    out = capsys.readouterr().out
    assert "nifty-50 seg1 2015-06-08 09:15..2015-06-19 15:25: 750 bars" in out
    assert "nifty-50 seg2 2015-11-16 09:15..2015-11-27 15:25: 750 bars" in out
    report = json.loads((tmp_path / "out" / "db_master_5min_nifty-50.json").read_text())
    assert [r["label"] for r in report] == ["nifty-50 seg1 2015-06-08 09:15..2015-06-19 15:25",
                                            "nifty-50 seg2 2015-11-16 09:15..2015-11-27 15:25"]
    assert [r["quality"]["rows"] for r in report] == [750, 750]
    md = (tmp_path / "out" / "db_master_5min_nifty-50.md").read_text()
    assert md.count("## nifty-50 seg") == 2

    # The candle reader only ever issued SELECTs.
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert not any(WRITE_SQL.match(s) for s in statements)


def test_start_and_end_are_passed_through_and_applied(research_db, spies, tmp_path) -> None:
    _frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path, "--start", "2015-11-16", "--end", "2015-11-20 15:25") == 0
    assert reader_calls[0]["start"] == pd.Timestamp("2015-11-16")
    assert reader_calls[0]["end"] == pd.Timestamp("2015-11-20 15:25")
    assert [len(f) for f in run_index_frames] == [5 * 75]  # one segment, only the requested week


def test_db_source_is_configurable(research_db, spies, tmp_path) -> None:
    _frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path, "--db-source", "other.csv") == 0
    assert reader_calls[0]["source"] == "other.csv"
    assert [len(f) for f in run_index_frames] == [2 * 75]
    assert (tmp_path / "out" / "db_other_nifty-50.md").exists()


def test_no_segments_fails_clearly(research_db, spies, tmp_path, capsys) -> None:
    assert run_db(tmp_path, "--db-source", "missing.csv") == 1
    assert "No candles in historical_candles" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# ---------------- argument validation ----------------


@pytest.mark.parametrize("extra, message", [
    (["--indices", "nifty-50", "bank-nifty"], "exactly one --indices"),
    (["--indices", "nifty-50", "--csv", "x.csv"], "--csv"),
    (["--indices", "nifty-50", "--synthetic"], "--synthetic"),
    (["--indices", "nifty-50", "--null", "3"], "--null"),
])
def test_db_rejects_conflicting_arguments(monkeypatch, capsys, extra, message) -> None:
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: pytest.fail("engine created"))
    with pytest.raises(SystemExit) as exit_info:
        run_mod.main(["--db", *extra])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_db_without_indices_uses_all_indices_and_is_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: pytest.fail("engine created"))
    with pytest.raises(SystemExit):
        run_mod.main(["--db"])  # default --indices is all three
    assert "exactly one --indices" in capsys.readouterr().err


def test_start_end_require_db(capsys) -> None:
    with pytest.raises(SystemExit):
        run_mod.main(["--synthetic", "--start", "2020-01-01"])
    assert "only supported with --db" in capsys.readouterr().err


# ---------------- non-DB behaviour unchanged ----------------


def test_synthetic_path_is_unchanged_and_never_touches_the_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: pytest.fail("engine created"))
    monkeypatch.setattr(candle_source, "load_db_segments", lambda *a, **k: pytest.fail("reader called"))
    assert run_mod.main(["--synthetic", "--indices", "nifty-50", "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "synthetic_5m.json").read_text())
    assert [r["label"] for r in report] == ["nifty-50"]
    assert "SYNTHETIC random-walk data" in (tmp_path / "synthetic_5m.md").read_text()


def test_null_benchmark_path_is_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(historical_db, "research_engine", lambda *a, **k: pytest.fail("engine created"))
    assert run_mod.main(["--null", "1", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "null_benchmark_5m.md").read_text().startswith("# Null benchmark - 1 random walks")


# ---------------- --start / --end date semantics ----------------


def test_date_only_start_is_the_start_of_that_day(research_db, spies, tmp_path) -> None:
    _frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path, "--start", "2015-11-17") == 0
    assert reader_calls[0]["start"] == pd.Timestamp("2015-11-17 00:00:00")
    [segment] = run_index_frames
    assert segment.index[0].isoformat() == "2015-11-17T09:15:00+05:30"  # the whole of the 17th is included
    assert len(segment) == 9 * 75  # 17th .. 27th


def test_date_only_end_includes_the_whole_day(research_db, spies, tmp_path) -> None:
    _frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path, "--start", "2015-11-16", "--end", "2015-11-20") == 0
    assert reader_calls[0]["end"] == pd.Timestamp("2015-11-20 23:59:59")
    [segment] = run_index_frames
    assert segment.index[-1].isoformat() == "2015-11-20T15:25:00+05:30"  # through the last bar of the day
    assert len(segment) == 5 * 75


@pytest.mark.parametrize("end, last_bar, bars", [
    ("2015-11-20 12:00", "2015-11-20T12:00:00+05:30", 4 * 75 + 34),  # inclusive at the exact bar
    ("2015-11-20T00:00", "2015-11-19T15:25:00+05:30", 4 * 75),  # explicit midnight stays midnight
    ("2015-11-20 15:25", "2015-11-20T15:25:00+05:30", 5 * 75),
])
def test_explicit_datetime_end_is_exact(research_db, spies, tmp_path, end, last_bar, bars) -> None:
    _frames, run_index_frames, reader_calls = spies
    assert run_db(tmp_path, "--start", "2015-11-16", "--end", end) == 0
    assert reader_calls[0]["end"] == pd.Timestamp(end)
    [segment] = run_index_frames
    assert segment.index[-1].isoformat() == last_bar
    assert len(segment) == bars


def test_parse_end_unit() -> None:
    assert run_mod._parse_end("2015-12-31") == pd.Timestamp("2015-12-31 23:59:59")
    assert run_mod._parse_end(" 2015-12-31 ") == pd.Timestamp("2015-12-31 23:59:59")
    assert run_mod._parse_end("2015-12-31 15:25") == pd.Timestamp("2015-12-31 15:25")
    assert run_mod._parse_end("2015-12-31T00:00:00") == pd.Timestamp("2015-12-31 00:00")
    aware = run_mod._parse_end("2015-12-31T09:55:00Z")  # explicit, timezone-aware: kept exactly
    assert aware == pd.Timestamp("2015-12-31 09:55", tz="UTC")
