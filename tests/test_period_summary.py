from datetime import date

import pytest

from algoedge.daily_summary import DailySummary, aggregate_period_summary


def make_daily(day, signals=1, orders=1, live=1, paper=0, pnl=0.0, pnl_live=0.0, pnl_paper=0.0):
    return DailySummary(
        day=day, signals_total=signals, orders_placed=orders, orders_live=live, orders_paper=paper,
        realized_pnl=pnl, realized_pnl_live=pnl_live, realized_pnl_paper=pnl_paper,
    )


def test_weekly_groups_days_in_the_same_iso_week() -> None:
    # 2026-09-21 (Mon) through 2026-09-27 (Sun) is ISO week 39.
    days = [
        make_daily(date(2026, 9, 21), pnl=100.0),
        make_daily(date(2026, 9, 23), pnl=50.0),
    ]

    result = aggregate_period_summary(days, "weekly")

    assert len(result) == 1
    assert result[0].period_label == "2026-W39"
    assert result[0].period_start == date(2026, 9, 21)
    assert result[0].period_end == date(2026, 9, 27)
    assert result[0].realized_pnl == pytest.approx(150.0)


def test_weekly_separates_days_in_different_weeks() -> None:
    days = [make_daily(date(2026, 9, 20)), make_daily(date(2026, 9, 21))]  # Sun (wk38) vs Mon (wk39)

    result = aggregate_period_summary(days, "weekly")

    labels = {bucket.period_label for bucket in result}
    assert labels == {"2026-W38", "2026-W39"}


def test_monthly_groups_days_in_the_same_calendar_month() -> None:
    days = [make_daily(date(2026, 9, 1), pnl=100.0), make_daily(date(2026, 9, 30), pnl=25.0)]

    result = aggregate_period_summary(days, "monthly")

    assert len(result) == 1
    assert result[0].period_label == "2026-09"
    assert result[0].period_start == date(2026, 9, 1)
    assert result[0].period_end == date(2026, 9, 30)
    assert result[0].realized_pnl == pytest.approx(125.0)


def test_monthly_handles_december_year_rollover() -> None:
    result = aggregate_period_summary([make_daily(date(2026, 12, 15))], "monthly")

    assert result[0].period_end == date(2026, 12, 31)


def test_monthly_separates_different_months() -> None:
    days = [make_daily(date(2026, 8, 31)), make_daily(date(2026, 9, 1))]

    result = aggregate_period_summary(days, "monthly")

    labels = {bucket.period_label for bucket in result}
    assert labels == {"2026-08", "2026-09"}


def test_sums_all_fields_not_just_pnl() -> None:
    days = [
        make_daily(date(2026, 9, 1), signals=2, orders=3, live=2, paper=1, pnl_live=100.0, pnl_paper=10.0),
        make_daily(date(2026, 9, 2), signals=1, orders=1, live=0, paper=1, pnl_live=0.0, pnl_paper=5.0),
    ]

    result = aggregate_period_summary(days, "monthly")

    bucket = result[0]
    assert bucket.signals_total == 3
    assert bucket.orders_placed == 4
    assert bucket.orders_live == 2
    assert bucket.orders_paper == 2
    assert bucket.realized_pnl_live == pytest.approx(100.0)
    assert bucket.realized_pnl_paper == pytest.approx(15.0)


def test_results_sorted_newest_first() -> None:
    days = [make_daily(date(2026, 7, 1)), make_daily(date(2026, 9, 1)), make_daily(date(2026, 8, 1))]

    result = aggregate_period_summary(days, "monthly")

    assert [bucket.period_label for bucket in result] == ["2026-09", "2026-08", "2026-07"]


def test_empty_input_returns_empty_list() -> None:
    assert aggregate_period_summary([], "weekly") == []
    assert aggregate_period_summary([], "monthly") == []


def test_unsupported_period_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported period"):
        aggregate_period_summary([make_daily(date(2026, 9, 1))], "yearly")
