from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from algoedge.pnl import compute_live_realized_pnl


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


@dataclass
class DailySummary:
    day: date
    signals_total: int = 0
    signals_by_action: dict[str, int] = field(default_factory=dict)
    orders_placed: int = 0
    orders_live: int = 0
    orders_paper: int = 0
    orders_by_outcome: dict[str, int] = field(default_factory=dict)
    realized_pnl: float = 0.0
    realized_pnl_live: float = 0.0
    realized_pnl_paper: float = 0.0


def compute_daily_summary(
    orders_newest_first: list[dict[str, Any]],
    signals_newest_first: list[dict[str, Any]],
) -> list[DailySummary]:
    """Rolls up signals + orders + realized P&L per calendar day.

    P&L is attributed to the day a position CLOSED, not the day it opened -
    a trade opened on Monday and closed on Wednesday shows its realized
    P&L on Wednesday, matching how brokers report it. Days are returned
    newest first.
    """
    days: dict[date, DailySummary] = {}

    def get_day(day: date) -> DailySummary:
        return days.setdefault(day, DailySummary(day=day))

    for signal in signals_newest_first:
        day = _as_date(signal.get("createdAt"))
        if day is None:
            continue
        summary = get_day(day)
        summary.signals_total += 1
        action = signal.get("action") or "UNKNOWN"
        summary.signals_by_action[action] = summary.signals_by_action.get(action, 0) + 1

    for order in orders_newest_first:
        day = _as_date(order.get("createdAt"))
        if day is None:
            continue
        summary = get_day(day)
        summary.orders_placed += 1
        if order.get("live"):
            summary.orders_live += 1
        else:
            summary.orders_paper += 1
        outcome = order.get("outcome") or "UNKNOWN"
        summary.orders_by_outcome[outcome] = summary.orders_by_outcome.get(outcome, 0) + 1

    live_orders_oldest_first = list(reversed([order for order in orders_newest_first if order.get("live")]))
    _live_total, live_trades = compute_live_realized_pnl(live_orders_oldest_first)
    for trade in live_trades:
        day = _as_date(trade.closed_at)
        if day is None:
            continue
        summary = get_day(day)
        summary.realized_pnl_live += trade.pnl
        summary.realized_pnl += trade.pnl

    paper_orders = [order for order in orders_newest_first if not order.get("live")]
    for order in paper_orders:
        if order.get("realizedPnl") is None:
            continue
        day = _as_date(order.get("createdAt"))
        if day is None:
            continue
        summary = get_day(day)
        pnl = float(order.get("realizedPnl") or 0.0)
        summary.realized_pnl_paper += pnl
        summary.realized_pnl += pnl

    return sorted(days.values(), key=lambda summary: summary.day, reverse=True)


@dataclass
class PeriodSummary:
    """A weekly or monthly roll-up of DailySummary rows (spec §27's
    "Daily/Weekly/Monthly P&L" requirement) - built by summing already-
    computed daily summaries rather than re-deriving P&L-close-day
    attribution against raw orders a second time."""

    period_label: str  # "2026-W39" (ISO week) or "2026-09" (calendar month)
    period_start: date
    period_end: date
    signals_total: int = 0
    orders_placed: int = 0
    orders_live: int = 0
    orders_paper: int = 0
    realized_pnl: float = 0.0
    realized_pnl_live: float = 0.0
    realized_pnl_paper: float = 0.0


def _week_bounds(day: date) -> tuple[str, date, date]:
    iso_year, iso_week, _ = day.isocalendar()
    week_start = day - timedelta(days=day.isoweekday() - 1)
    week_end = week_start + timedelta(days=6)
    return f"{iso_year}-W{iso_week:02d}", week_start, week_end


def _month_bounds(day: date) -> tuple[str, date, date]:
    start = day.replace(day=1)
    next_month = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    end = next_month - timedelta(days=1)
    return f"{start.year}-{start.month:02d}", start, end


def aggregate_period_summary(daily_summaries: list[DailySummary], period: str) -> list[PeriodSummary]:
    """Groups DailySummary rows (any order, from compute_daily_summary)
    into weekly or monthly buckets. `period` must be "weekly" or "monthly"."""
    if period == "weekly":
        bounds_fn = _week_bounds
    elif period == "monthly":
        bounds_fn = _month_bounds
    else:
        raise ValueError(f"Unsupported period: {period!r} - expected 'weekly' or 'monthly'")

    buckets: dict[str, PeriodSummary] = {}
    for daily in daily_summaries:
        label, start, end = bounds_fn(daily.day)
        bucket = buckets.setdefault(label, PeriodSummary(period_label=label, period_start=start, period_end=end))
        bucket.signals_total += daily.signals_total
        bucket.orders_placed += daily.orders_placed
        bucket.orders_live += daily.orders_live
        bucket.orders_paper += daily.orders_paper
        bucket.realized_pnl += daily.realized_pnl
        bucket.realized_pnl_live += daily.realized_pnl_live
        bucket.realized_pnl_paper += daily.realized_pnl_paper

    return sorted(buckets.values(), key=lambda bucket: bucket.period_start, reverse=True)
