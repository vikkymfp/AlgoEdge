"""Tests for research.phase6.validate_dataset on synthetic files with known defects.

    PYTHONPATH=src:. python -m pytest research/phase6/test_validate_dataset.py -q
"""

import json

import pandas as pd
import pytest

from research.phase6 import validate_dataset as vd


def session(day: str, bars: int = 75, start: str = "09:15") -> list[pd.Timestamp]:
    first = pd.Timestamp(f"{day} {start}")
    return [first + pd.Timedelta(minutes=5 * k) for k in range(bars)]


def rows_for(stamps, price=100.0):
    return [{"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": price, "high": price + 2,
             "low": price - 2, "close": price + 1} for t in stamps]


def write(tmp_path, rows, name="master_5min.csv"):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


@pytest.fixture()
def clean_file(tmp_path):
    # Fri 2015-01-09, Mon 12th, Tue 13th: three normal sessions.
    stamps = session("2015-01-09") + session("2015-01-12") + session("2015-01-13")
    return write(tmp_path, rows_for(stamps))


def test_clean_file_has_no_findings(clean_file) -> None:
    report, sessions = vd.validate(clean_file)
    assert report.total_rows == report.valid_rows == 225 and report.invalid_rows == 0
    assert report.trading_days == 3
    assert report.first_timestamp == "2015-01-09T09:15:00+05:30"
    assert report.last_timestamp == "2015-01-13T15:25:00+05:30"
    assert report.chronological["is_sorted"] and report.duplicates["duplicate_rows"] == 0
    assert report.gaps["count"] == 0 and report.alignment["misaligned"] == 0
    assert report.one_second_shifts["count"] == 0 and report.outside_session["count"] == 0
    assert report.session_length_distribution == {"75": 3}
    assert report.duration["label_convention"].startswith("bar START")
    assert report.weekdays_without_data["count"] == 0
    assert report.dates_requiring_investigation == []
    assert report.timezone["naive_rows_assumed_ist"] == 225
    assert list(sessions["status"]) == ["normal"] * 3
    assert sessions.loc[0, "first_bar"] == "2015-01-09T09:15:00+05:30"
    assert sessions.loc[0, "last_bar"] == "2015-01-09T15:25:00+05:30"


@pytest.fixture()
def defective(tmp_path):
    day1 = session("2015-01-09")
    day2 = [t for t in session("2015-01-12") if t.strftime("%H:%M") not in ("10:00", "10:05", "10:10")]
    day3 = session("2015-01-13") + [pd.Timestamp("2015-01-13 15:30")]  # one extra bar
    saturday = session("2015-01-17", bars=12)  # special weekend session
    rows = rows_for(day1) + rows_for(day2) + rows_for(day3) + rows_for(saturday)

    rows.append(rows_for([pd.Timestamp("2015-01-09 09:20")])[0])  # exact duplicate, out of order
    conflict = rows_for([pd.Timestamp("2015-01-09 09:25")], price=500)[0]
    rows.append(conflict)  # conflicting duplicate
    rows.append({"datetime": "2015-01-14 09:17:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5})  # off grid
    rows.append({"datetime": "2015-01-14 10:04:59", "open": 1, "high": 2, "low": 0.5, "close": 1.5})  # 1s shift
    rows.append({"datetime": "not a time", "open": 1, "high": 2, "low": 0.5, "close": 1.5})
    rows.append({"datetime": "2015-01-14 11:00:00", "open": 100, "high": 99, "low": 101, "close": 100})
    rows.append({"datetime": "2015-01-14 11:05:00", "open": 100, "high": 102, "low": 99, "close": ""})
    rows.append({"datetime": "2015-01-14 18:15:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5})  # evening
    return write(tmp_path, rows)


def test_defects_are_each_detected(defective) -> None:
    report, sessions = vd.validate(defective)
    assert report.total_rows == 75 + 72 + 76 + 12 + 8
    assert report.unparseable_timestamps == 1
    assert report.missing_ohlc["close"] == 1
    assert report.invalid_ohlc["high_below_low"] == 1 and report.invalid_ohlc["rows"] == 1
    assert report.invalid_rows == 3  # unparseable + missing close + high<low
    assert report.valid_rows == report.total_rows - 3

    assert report.chronological["is_sorted"] is False
    assert report.chronological["backward_steps"] == 1  # 2015-01-17 -> 2015-01-09
    assert report.duplicates["duplicate_rows"] == 2
    assert report.duplicates["conflicting_values"] == 1

    assert report.alignment["misaligned"] == 2  # 09:17 and 10:04:59
    assert report.alignment["misaligned_minute"] == 2
    assert report.one_second_shifts == {**report.one_second_shifts, "count": 1, "at_59_seconds": 1}

    assert report.gaps["count"] >= 1
    assert report.gaps["missing_bars_implied"] >= 3
    assert "20.0" in report.gaps["distribution_minutes"]  # the 09:55 -> 10:15 hole

    assert report.outside_session["count"] == 2  # 15:30 bar and the 18:15 bar
    assert report.abnormal_sessions["long"] == 1 and report.abnormal_sessions["short"] >= 1
    assert [s["date"] for s in report.weekend_sessions] == ["2015-01-17"]
    assert report.weekdays_without_data["dates"] == ["2015-01-15", "2015-01-16"]

    flagged = {d["date"]: d["reasons"] for d in report.dates_requiring_investigation}
    assert "intraday gap != 5m" in flagged["2015-01-12"]
    assert "more than 75 bars" in flagged["2015-01-13"]
    assert "weekend session" in flagged["2015-01-17"]
    assert "duplicate timestamps" in flagged["2015-01-09"]
    assert {"off the 5m grid", "invalid OHLC", "bars outside 09:15-15:30"} <= set(flagged["2015-01-14"])
    assert set(sessions.columns) >= {"date", "bars", "first_bar", "last_bar", "status", "bars_in_regular_session"}


def test_input_file_is_never_modified(defective) -> None:
    before = defective.read_bytes()
    vd.validate(defective)
    assert defective.read_bytes() == before


def test_utc_offsets_are_converted_to_ist(tmp_path) -> None:
    rows = [{"timestamp": "2015-01-09T03:45:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            {"timestamp": "2015-01-09T09:20:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
    report, _ = vd.validate(write(tmp_path, rows))
    assert report.timezone["rows_with_utc_offset"] == 2
    assert report.first_timestamp == "2015-01-09T09:15:00+05:30"
    assert report.gaps["count"] == 0 and report.chronological["is_sorted"]


def test_separate_date_and_time_columns_and_no_volume(tmp_path) -> None:
    stamps = session("2015-01-09", bars=3)
    rows = [{"Date": t.strftime("%Y-%m-%d"), "Time": t.strftime("%H:%M"),
             "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5} for t in stamps]
    report, _ = vd.validate(write(tmp_path, rows))
    assert report.timestamp_source == "Date + Time"
    assert report.total_rows == report.valid_rows == 3


def test_start_end_columns_measure_each_candle(tmp_path) -> None:
    stamps = session("2015-01-09", bars=4)
    rows = [{"start_time": t.isoformat(sep=" "), "end_time": (t + pd.Timedelta(minutes=5)).isoformat(sep=" "),
             "open": 1, "high": 2, "low": 0.5, "close": 1.5} for t in stamps]
    rows[1]["end_time"] = (stamps[1] + pd.Timedelta(minutes=4, seconds=59)).isoformat(sep=" ")  # inclusive end
    rows[2]["end_time"] = (stamps[2] + pd.Timedelta(minutes=25)).isoformat(sep=" ")  # a 25-minute candle
    report, _ = vd.validate(write(tmp_path, rows))
    d = report.duration
    assert d["measured_from"] == "start/end columns"
    assert d["not_exactly_5m"] == 2 and d["end_inclusive_4m59s"] == 1
    assert d["distribution_seconds"] == {"299.0": 1, "300.0": 2, "1500.0": 1}


def test_end_labelled_bars_are_called_out(tmp_path) -> None:
    report, _ = vd.validate(write(tmp_path, rows_for(session("2015-01-09", start="09:20"))))
    assert "unclear" in report.duration["label_convention"]
    assert report.outside_session["count"] == 1  # the 15:30 bar


def test_cli_writes_report_and_session_table(defective, tmp_path, capsys) -> None:
    out_dir = tmp_path / "reports"
    assert vd.main([str(defective), "--out-dir", str(out_dir)]) == 0
    report = json.loads((out_dir / "master_5min_quality.json").read_text())
    assert report["total_rows"] == 243
    sessions = pd.read_csv(out_dir / "master_5min_sessions.csv")
    assert len(sessions) == report["trading_days"]
    assert "Dates requiring investigation" in capsys.readouterr().out


def test_cli_missing_file(tmp_path) -> None:
    assert vd.main([str(tmp_path / "nope.csv")]) == 2
