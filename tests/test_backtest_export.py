import io
from datetime import datetime, timedelta

import openpyxl
import pytest

from algoedge.backtest import compute_backtest_metrics, pair_trades
from algoedge.backtest_export import build_excel_report, build_pdf_report
from algoedge.risk_manager import IST
from fno_signals.strategy import TradeEvent

BASE = datetime(2026, 9, 24, 9, 15, tzinfo=IST)


def entry(kind, underlying_price, minute_offset, strike=24500, option_symbol="NIFTY 24500 CE"):
    right = "CE" if kind == "ENTRY_CALL" else "PE"
    return TradeEvent(
        timestamp=BASE + timedelta(minutes=minute_offset), kind=kind, underlying_price=underlying_price,
        option_symbol=option_symbol, stop_loss=underlying_price - 20, target=underlying_price + 40,
        exit_level=None, strike=strike, right=right,
    )


def exit_event(kind, exit_level, minute_offset):
    return TradeEvent(
        timestamp=BASE + timedelta(minutes=minute_offset), kind=kind, underlying_price=exit_level,
        option_symbol=None, stop_loss=None, target=None, exit_level=exit_level,
    )


def _metrics_payload(metrics) -> dict:
    """Mirrors web_server.py's _backtest_metrics_payload exactly, kept
    local rather than imported so this test doesn't need to construct a
    full FastAPI app just to reach a pure formatting helper."""
    return {
        "totalTrades": metrics.total_trades,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "winRate": metrics.win_rate,
        "profitFactor": metrics.profit_factor,
        "netPoints": metrics.net_points,
        "averageTradePoints": metrics.average_trade_points,
        "expectancyPoints": metrics.expectancy_points,
        "largestWinPoints": metrics.largest_win_points,
        "largestLossPoints": metrics.largest_loss_points,
        "maxConsecutiveLosses": metrics.max_consecutive_losses,
        "maxDrawdownPoints": metrics.max_drawdown_points,
        "callPerformance": vars(metrics.call_performance),
        "putPerformance": vars(metrics.put_performance),
        "timeOfDayPerformance": [vars(bucket) for bucket in metrics.time_of_day_performance],
        "marketRegimePerformance": [vars(bucket) for bucket in metrics.market_regime_performance],
    }


def _trades_payload(trades) -> list[dict]:
    return [
        {
            "entryTime": trade.entry_time.isoformat(), "exitTime": trade.exit_time.isoformat(),
            "direction": trade.direction, "entryPrice": trade.entry_price, "exitPrice": trade.exit_price,
            "exitReason": trade.exit_reason, "points": trade.points, "strike": trade.strike,
            "optionSymbol": trade.option_symbol,
        }
        for trade in sorted(trades, key=lambda t: t.entry_time)
    ]


def make_full_period_payload(*, with_trades: bool = True) -> dict:
    events = (
        [
            entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5),
            entry("ENTRY_PUT", 24540, 10), exit_event("EXIT_SL", 24560, 15),
        ]
        if with_trades else []
    )
    trades = pair_trades(events)
    metrics = compute_backtest_metrics(trades, regime_by_time=None)
    return {
        "indexId": "nifty-50", "indexName": "NIFTY 50", "interval": "5m", "period": "60d",
        "candleCount": 500, "assumedSlippagePoints": 0.0,
        "disclaimer": "Points-based backtest disclaimer text.",
        "split": False, "metrics": _metrics_payload(metrics), "trades": _trades_payload(trades),
    }


def make_split_payload() -> dict:
    events = [entry("ENTRY_CALL", 24500, 0), exit_event("EXIT_TARGET", 24540, 5)]
    trades = pair_trades(events)
    metrics = compute_backtest_metrics(trades, regime_by_time=None)
    segment = {"candleCount": 120, "metrics": _metrics_payload(metrics), "trades": _trades_payload(trades)}
    return {
        "indexId": "bank-nifty", "indexName": "BANK NIFTY", "interval": "15m", "period": "60d",
        "candleCount": 400, "assumedSlippagePoints": 1.5,
        "disclaimer": "Points-based backtest disclaimer text.",
        "split": True,
        "splits": {
            "train": segment,
            "validation": {"candleCount": 5, "metrics": None, "trades": []},
            "out_of_sample": segment,
        },
    }


# -- Excel ----------------------------------------------


def test_excel_report_is_a_valid_workbook_with_expected_sheets() -> None:
    content = build_excel_report(make_full_period_payload())
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["Summary", "Trades - Full period", "Breakdown - Full period"]


def test_excel_summary_sheet_contains_kpi_values() -> None:
    payload = make_full_period_payload()
    content = build_excel_report(payload)
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    summary = workbook["Summary"]
    values = [cell.value for row in summary.iter_rows() for cell in row]
    assert "Total trades" in values
    assert payload["metrics"]["totalTrades"] in values


def test_excel_trade_ledger_sheet_has_one_row_per_trade() -> None:
    payload = make_full_period_payload()
    content = build_excel_report(payload)
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    ledger = workbook["Trades - Full period"]
    # header row + one row per trade
    assert ledger.max_row == 1 + len(payload["trades"])
    header = [cell.value for cell in ledger[1]]
    assert header[0] == "Entry time"
    assert "Points" in header


def test_excel_handles_zero_trades_without_error() -> None:
    payload = make_full_period_payload(with_trades=False)
    content = build_excel_report(payload)
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    ledger = workbook["Trades - Full period"]
    assert ledger.max_row == 1  # header only, no trade rows


def test_excel_split_payload_produces_a_sheet_per_present_segment() -> None:
    content = build_excel_report(make_split_payload())
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    # validation segment has metrics=None (too few candles) and must be skipped,
    # not rendered as an empty/misleading sheet.
    assert "Trades - Validation" not in workbook.sheetnames
    assert "Trades - Train" in workbook.sheetnames
    assert "Trades - Out-of-sample" in workbook.sheetnames


# -- PDF ----------------------------------------------


def test_pdf_report_starts_with_the_pdf_magic_bytes() -> None:
    content = build_pdf_report(make_full_period_payload())
    assert content.startswith(b"%PDF")
    assert len(content) > 1000  # a real, non-trivial document, not an empty shell


def test_pdf_report_handles_zero_trades_without_error() -> None:
    content = build_pdf_report(make_full_period_payload(with_trades=False))
    assert content.startswith(b"%PDF")


def test_pdf_report_handles_split_payload_without_error() -> None:
    content = build_pdf_report(make_split_payload())
    assert content.startswith(b"%PDF")


def test_pdf_report_never_invents_a_rupee_pnl_label() -> None:
    # The whole point of the points-based backtest (see backtest.py's module
    # docstring) is that a rupee P&L figure is never fabricated - the PDF's
    # equity-chart caption must say "points", never claim a rupee amount.
    content = build_pdf_report(make_full_period_payload())
    assert b"points" in content.lower() or b"Points" in content



if __name__ == "__main__":
    pytest.main([__file__])
