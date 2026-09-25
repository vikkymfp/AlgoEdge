"""Tests for research.phase6.load_historical_data - synthetic CSVs + SQLite.
No SQL Server and no real master_5min.csv needed.

    PYTHONPATH=src:. python -m pytest research/phase6/test_load_historical_data.py -q
"""

import hashlib
import json
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError

from research.phase6 import historical_db as hdb
from research.phase6 import load_historical_data as lhd

HEADER = "start_time,open,high,low,close,end_time"


def bars(day: str, count: int = 3, start: str = "09:15") -> list[str]:
    """CSV lines in the master_5min.csv layout (+05:30 offsets, 5m candles)."""
    first = pd.Timestamp(f"{day} {start}")
    lines = []
    for k in range(count):
        s = first + pd.Timedelta(minutes=5 * k)
        e = s + pd.Timedelta(minutes=5)
        price = 8000 + 10 * k
        lines.append(f"{s:%Y-%m-%d %H:%M:%S}+05:30,{price},{price + 5},{price - 5},{price + 1},"
                     f"{e:%Y-%m-%d %H:%M:%S}+05:30")
    return lines


def write_csv(tmp_path, lines, name="master_5min.csv"):
    path = tmp_path / name
    path.write_text("\n".join([HEADER, *lines]) + "\n")
    return path


@pytest.fixture()
def boundary_csv(tmp_path):
    # Outside / first excluded day / off-grid bar inside / last excluded day / outside.
    lines = (bars("2015-06-19") + bars("2015-06-22")
             + ["2015-06-22 10:00:01+05:30,8000,8005,7995,8001,2015-06-22 10:05:01+05:30"]
             + bars("2015-11-13") + bars("2015-11-16"))
    return write_csv(tmp_path, lines)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
    hdb.create_research_schema(engine)  # so refusal tests can inspect empty tables
    yield engine
    engine.dispose()


def table_rows(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(table)).mappings().all()


CANDLES = hdb.HistoricalCandle.__table__
LOADS = hdb.HistoricalCandleLoad.__table__


# ---------------- parsing, mapping, timestamps ----------------


def test_rows_are_mapped_exactly(boundary_csv) -> None:
    plan = lhd.plan_load(boundary_csv)
    assert plan.source == "master_5min.csv"
    first = plan.candles[0]
    assert first == {
        "index_id": "nifty-50", "timeframe": "5m",
        "bar_start": datetime(2015, 6, 19, 9, 15), "bar_end": datetime(2015, 6, 19, 9, 20),
        "open": 8000.0, "high": 8005.0, "low": 7995.0, "close": 8001.0, "volume": None,
        "source": "master_5min.csv",
    }
    assert all(c["volume"] is None for c in plan.candles)
    assert all(c["bar_start"].tzinfo is None and c["bar_end"].tzinfo is None for c in plan.candles)


def test_utc_offsets_are_stored_as_ist_wall_clock(tmp_path) -> None:
    path = write_csv(tmp_path, ["2016-01-04T03:45:00Z,1,2,0.5,1.5,2016-01-04T03:50:00Z"])
    [candle] = lhd.plan_load(path).candles
    assert candle["bar_start"] == datetime(2016, 1, 4, 9, 15)
    assert candle["bar_end"] == datetime(2016, 1, 4, 9, 20)


def test_source_name_override(boundary_csv) -> None:
    assert lhd.plan_load(boundary_csv, source_name="master_5min.csv").source == "master_5min.csv"
    renamed = boundary_csv.rename(boundary_csv.with_name("ee257eb8-master_5min.csv"))
    assert lhd.plan_load(renamed).source == "ee257eb8-master_5min.csv"


def test_sha256_is_of_the_exact_bytes(boundary_csv) -> None:
    assert lhd.sha256_file(boundary_csv) == hashlib.sha256(boundary_csv.read_bytes()).hexdigest()
    assert lhd.plan_load(boundary_csv).file_sha256 == hashlib.sha256(boundary_csv.read_bytes()).hexdigest()


# ---------------- exclusion policy ----------------


def test_contaminated_period_is_excluded_inclusively(boundary_csv) -> None:
    plan = lhd.plan_load(boundary_csv)
    starts = [c["bar_start"].date().isoformat() for c in plan.candles]
    assert sorted(set(starts)) == ["2015-06-19", "2015-11-16"]
    assert plan.excluded_rows == 7  # 3 + off-grid bar on 06-22, 3 on 11-13
    assert plan.rejected_rows == 0  # the off-grid bar is inside the window: excluded, not rejected
    assert len(plan.candles) == 6
    assert plan.source_rows == 13


def test_no_candles_are_fabricated_and_gaps_stay_gaps(tmp_path) -> None:
    lines = bars("2016-01-04", count=6)
    del lines[2:4]  # a 15-minute hole
    plan = lhd.plan_load(write_csv(tmp_path, lines))
    starts = [c["bar_start"].strftime("%H:%M") for c in plan.candles]
    assert starts == ["09:15", "09:20", "09:35", "09:40"]


# ---------------- rejected rows ----------------


@pytest.mark.parametrize("bad_line, reason, count", [
    ("2016-01-04 10:00:00+05:30,100,99,101,100,2016-01-04 10:05:00+05:30", "invalid", 1),  # high < low
    ("2016-01-04 10:00:00+05:30,100,,99,100,2016-01-04 10:05:00+05:30", "invalid", 1),  # missing high
    ("2016-01-04 10:00:01+05:30,100,102,99,101,2016-01-04 10:05:01+05:30", "off_grid", 1),
    ("2016-01-04 10:00:00+05:30,100,102,99,101,2016-01-04 10:25:00+05:30", "bad_duration", 1),
    ("2016-01-04 09:20:00+05:30,100,102,99,101,2016-01-04 09:25:00+05:30", "duplicate", 2),
])
def test_rows_failing_validation_are_never_imported(tmp_path, engine, bad_line, reason, count) -> None:
    plan = lhd.plan_load(write_csv(tmp_path, [*bars("2016-01-04"), bad_line]))
    assert plan.rejected_by_reason[reason] == count
    assert plan.rejected_rows == count
    assert len(plan.candles) == 4 - count

    with pytest.raises(lhd.LoadRefused):
        lhd.execute_load(engine, plan)
    assert table_rows(engine, LOADS) == [] and table_rows(engine, CANDLES) == []

    result = lhd.execute_load(engine, plan, allow_rejections=True)
    assert result.inserted == len(plan.candles)
    policy = json.loads(table_rows(engine, LOADS)[0]["validation_json"])["load_policy"]
    assert policy["rejected_by_reason"][reason] == count


# ---------------- the load itself ----------------


def test_successful_load_writes_metadata_candles_and_passes_every_check(boundary_csv, engine) -> None:
    plan = lhd.plan_load(boundary_csv)
    result = lhd.execute_load(engine, plan)
    assert result.already_loaded is False and result.inserted == 6
    assert all(check["ok"] for check in result.checks.values())
    assert set(result.checks) >= {"A_count", "B_min_bar_start", "C_max_bar_start", "D_distinct_bar_start",
                                  "E_duplicate_groups", "F_null_ohlc", "G_invalid_ohlc",
                                  "H_wrong_index_or_timeframe", "I_rows_in_excluded_period",
                                  "J_source_rows_with_other_load_id", "J_rows_with_this_load_id"}

    [load] = table_rows(engine, LOADS)
    assert load["id"] == result.load_id
    assert load["source"] == "master_5min.csv" and load["file_sha256"] == plan.file_sha256
    assert (load["row_count"], load["valid_rows"], load["invalid_rows"]) == (13, 13, 0)
    assert load["duplicate_count"] == 0 and load["gap_count"] == plan.report.gaps["count"]
    assert load["first_bar"] == datetime(2015, 6, 19, 9, 15) and load["last_bar"] == datetime(2015, 11, 16, 9, 25)
    validation = json.loads(load["validation_json"])
    assert validation["load_policy"]["excluded_rows"] == 7
    assert validation["load_policy"]["excluded_periods"] == [["2015-06-22", "2015-11-13"]]

    candles = table_rows(engine, CANDLES)
    assert len(candles) == 6
    assert {c["load_id"] for c in candles} == {result.load_id}
    assert all(c["volume"] is None for c in candles)
    assert all(c["index_id"] == "nifty-50" and c["timeframe"] == "5m" for c in candles)
    stored = sorted((c["bar_start"], c["bar_end"], c["open"], c["high"], c["low"], c["close"]) for c in candles)
    planned = sorted((c["bar_start"], c["bar_end"], c["open"], c["high"], c["low"], c["close"]) for c in plan.candles)
    assert stored == planned


def test_loading_the_same_file_twice_is_a_no_op(boundary_csv, engine) -> None:
    plan = lhd.plan_load(boundary_csv)
    first = lhd.execute_load(engine, plan)
    second = lhd.execute_load(engine, lhd.plan_load(boundary_csv))
    assert second.already_loaded is True and second.load_id == first.load_id and second.inserted == 0
    assert len(table_rows(engine, LOADS)) == 1 and len(table_rows(engine, CANDLES)) == 6


def test_small_batches_insert_everything(boundary_csv, engine) -> None:
    result = lhd.execute_load(engine, lhd.plan_load(boundary_csv), batch_size=2)
    assert result.inserted == 6 and len(table_rows(engine, CANDLES)) == 6


def test_a_failed_candle_insert_rolls_back_the_load_record_too(boundary_csv, engine) -> None:
    plan = lhd.plan_load(boundary_csv)
    plan.candles[-1]["open"] = None  # NOT NULL violation in the last batch
    with pytest.raises(IntegrityError):
        lhd.execute_load(engine, plan, batch_size=2)
    assert table_rows(engine, LOADS) == [] and table_rows(engine, CANDLES) == []


def test_a_failed_verification_rolls_everything_back(boundary_csv, engine, monkeypatch) -> None:
    monkeypatch.setattr(lhd, "verify_load", lambda *_a: {"A_count": {"value": 0, "expected": 6, "ok": False}})
    with pytest.raises(lhd.LoadVerificationError):
        lhd.execute_load(engine, lhd.plan_load(boundary_csv))
    assert table_rows(engine, LOADS) == [] and table_rows(engine, CANDLES) == []


def test_a_different_file_with_the_same_source_name_cannot_overwrite(tmp_path, engine) -> None:
    first = write_csv(tmp_path, bars("2016-01-04"))
    lhd.execute_load(engine, lhd.plan_load(first))
    changed = tmp_path / "changed" / "master_5min.csv"
    changed.parent.mkdir()
    changed.write_text("\n".join([HEADER, *bars("2016-01-04"), *bars("2016-01-05")]) + "\n")
    with pytest.raises(IntegrityError):  # same (index, timeframe, bar_start, source) already present
        lhd.execute_load(engine, lhd.plan_load(changed))
    assert len(table_rows(engine, LOADS)) == 1 and len(table_rows(engine, CANDLES)) == 3


def test_verification_detects_rows_in_the_excluded_period(boundary_csv, engine) -> None:
    plan = lhd.plan_load(boundary_csv)
    result = lhd.execute_load(engine, plan)
    with engine.begin() as conn:
        conn.execute(CANDLES.insert(), [{**plan.candles[0], "bar_start": datetime(2015, 7, 1, 9, 15),
                                         "source": "other.csv", "load_id": result.load_id}])
        checks = lhd.verify_load(conn, result.load_id, plan)
    assert checks["I_rows_in_excluded_period"] == {"value": 1, "expected": 0, "ok": False}
    assert checks["A_count"]["ok"] is False


# ---------------- CLI ----------------


def _no_db():
    raise AssertionError("the database must not be contacted")


@pytest.mark.parametrize("flags", [["--dry-run"], [], ["--dry-run", "--confirm"]])
def test_dry_run_and_missing_confirm_never_touch_the_database(boundary_csv, capsys, flags) -> None:
    before = boundary_csv.read_bytes()
    assert lhd.main([str(boundary_csv), *flags], engine_factory=_no_db) == 0
    out = capsys.readouterr().out
    assert "nothing was written" in out
    report = json.loads(out[out.index("{"):])
    assert report["rows_to_insert"] == 6 and report["excluded_rows"] == 7
    assert report["sha256"] == hashlib.sha256(before).hexdigest()
    assert boundary_csv.read_bytes() == before


def test_confirmed_load_and_repeat_via_cli(boundary_csv, engine, capsys) -> None:
    before = boundary_csv.read_bytes()
    assert lhd.main([str(boundary_csv), "--confirm"], engine_factory=lambda: engine) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["inserted"] == 6 and out["excluded_contaminated_rows"] == 7 and out["load_id"] == 1
    assert lhd.main([str(boundary_csv), "--confirm"], engine_factory=lambda: engine) == 0
    assert "Already loaded" in capsys.readouterr().out
    assert boundary_csv.read_bytes() == before
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(CANDLES)).scalar() == 6


def test_cli_refuses_a_load_with_rejected_rows(tmp_path, engine, capsys) -> None:
    path = write_csv(tmp_path, [*bars("2016-01-04"), "2016-01-04 10:00:00+05:30,100,99,101,100,"
                                                       "2016-01-04 10:05:00+05:30"])
    assert lhd.main([str(path), "--confirm"], engine_factory=lambda: engine) == 1
    assert "LOAD FAILED" in capsys.readouterr().err
    assert table_rows(engine, LOADS) == []


def test_cli_missing_file(tmp_path) -> None:
    assert lhd.main([str(tmp_path / "nope.csv")], engine_factory=_no_db) == 2
