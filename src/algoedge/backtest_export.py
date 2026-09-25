"""Builds Excel/PDF backtest reports from the exact payload
/api/backtest/run already computes - never recomputes, re-derives, or
invents a number. Every figure here is read straight from the same
BacktestMetrics/BacktestTrade data algoedge/backtest.py produces and
test_backtest.py already covers; this module is purely a formatting layer.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from algoedge.risk_manager import IST

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
POSITIVE_FONT = Font(color="0F7B3D")
NEGATIVE_FONT = Font(color="B3261E")


def _kpi_rows(metrics: dict) -> list[tuple[str, Any]]:
    return [
        ("Total trades", metrics.get("totalTrades")),
        ("Wins", metrics.get("wins")),
        ("Losses", metrics.get("losses")),
        ("Win rate (%)", metrics.get("winRate")),
        ("Profit factor", metrics.get("profitFactor")),
        ("Net points", metrics.get("netPoints")),
        ("Average trade (points)", metrics.get("averageTradePoints")),
        ("Expectancy (points)", metrics.get("expectancyPoints")),
        ("Largest win (points)", metrics.get("largestWinPoints")),
        ("Largest loss (points)", metrics.get("largestLossPoints")),
        ("Max consecutive losses", metrics.get("maxConsecutiveLosses")),
        ("Max drawdown (points)", metrics.get("maxDrawdownPoints")),
    ]


def _segments(payload: dict) -> list[tuple[str, dict]]:
    """Normalizes full-period vs split payloads into a uniform list of
    (label, segment) pairs so the rest of this module has one code path."""
    if payload.get("split"):
        order = [("train", "Train"), ("validation", "Validation"), ("out_of_sample", "Out-of-sample")]
        return [
            (label, payload["splits"][key])
            for key, label in order
            if payload.get("splits", {}).get(key) and payload["splits"][key].get("metrics") is not None
        ]
    return [("Full period", {"metrics": payload.get("metrics") or {}, "trades": payload.get("trades") or []})]


def _cumulative_points(trades: list[dict]) -> list[tuple[float, float]]:
    cumulative = 0.0
    series = [(0.0, 0.0)]
    for i, trade in enumerate(trades, start=1):
        cumulative += trade.get("points") or 0.0
        series.append((float(i), cumulative))
    return series


# ---------- Excel ----------


def _append_direction(sheet, direction: dict) -> None:
    sheet.append(["Trades", "Wins", "Losses", "Win rate (%)", "Net points"])
    for cell in sheet[sheet.max_row]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    sheet.append([
        direction.get("trades"), direction.get("wins"), direction.get("losses"),
        direction.get("win_rate"), direction.get("net_points"),
    ])


def _append_buckets(sheet, buckets: list[dict]) -> None:
    sheet.append(["Label", "Trades", "Win rate (%)", "Net points"])
    for cell in sheet[sheet.max_row]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for bucket in buckets:
        sheet.append([bucket.get("label"), bucket.get("trades"), bucket.get("win_rate"), bucket.get("net_points")])


def build_excel_report(payload: dict) -> bytes:
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary["A1"] = "AlgoEdge Backtest Report"
    summary["A1"].font = Font(bold=True, size=14)
    summary["A2"] = f"{payload.get('indexName', payload.get('indexId'))} · {payload.get('interval')} · {payload.get('period')} · {payload.get('candleCount')} candles"
    summary["A3"] = payload.get("disclaimer", "")
    summary["A3"].alignment = Alignment(wrap_text=True)
    summary.row_dimensions[3].height = 40
    summary.column_dimensions["A"].width = 34
    summary.column_dimensions["B"].width = 18

    row = 5
    for label, segment in _segments(payload):
        metrics = segment.get("metrics") or {}
        summary.cell(row=row, column=1, value=label).font = Font(bold=True, size=12)
        row += 1
        for kpi_label, value in _kpi_rows(metrics):
            summary.cell(row=row, column=1, value=kpi_label)
            cell = summary.cell(row=row, column=2, value=value)
            if isinstance(value, int | float) and any(
                kpi_label.startswith(prefix) for prefix in ("Net", "Average", "Expectancy", "Largest")
            ):
                cell.font = POSITIVE_FONT if value >= 0 else NEGATIVE_FONT
            row += 1
        row += 1

        ledger = wb.create_sheet(f"Trades - {label}"[:31])
        headers = [
            "Entry time", "Exit time", "Direction", "Entry price", "Exit price",
            "Exit reason", "Points", "Strike", "Option symbol",
        ]
        ledger.append(headers)
        for cell in ledger[1]:
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
        for trade in segment.get("trades") or []:
            ledger.append([
                trade.get("entryTime"), trade.get("exitTime"), trade.get("direction"),
                trade.get("entryPrice"), trade.get("exitPrice"), trade.get("exitReason"),
                trade.get("points"), trade.get("strike"), trade.get("optionSymbol"),
            ])
            points = trade.get("points") or 0
            points_cell = ledger.cell(row=ledger.max_row, column=7)
            points_cell.font = POSITIVE_FONT if points >= 0 else NEGATIVE_FONT
        for i, header in enumerate(headers, start=1):
            ledger.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)

        breakdown = wb.create_sheet(f"Breakdown - {label}"[:31])
        breakdown.append(["CALL performance"])
        _append_direction(breakdown, metrics.get("callPerformance") or {})
        breakdown.append([])
        breakdown.append(["PUT performance"])
        _append_direction(breakdown, metrics.get("putPerformance") or {})
        breakdown.append([])
        breakdown.append(["Time-of-day performance"])
        _append_buckets(breakdown, metrics.get("timeOfDayPerformance") or [])
        breakdown.append([])
        breakdown.append(["Market-regime performance"])
        _append_buckets(breakdown, metrics.get("marketRegimePerformance") or [])
        for column in ("A", "B", "C", "D", "E"):
            breakdown.column_dimensions[column].width = 22

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------- PDF ----------


def _table_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e6e8f0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _format_kpi(label: str, value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}" + ("%" if "%" in label else "")
    return str(value)


def _equity_chart_drawing(trades: list[dict]) -> Drawing | None:
    series = _cumulative_points(trades)
    if len(series) < 2:
        return None
    values = [point[1] for point in series]
    span = max(values) - min(values) or 1.0
    drawing = Drawing(430, 170)
    plot = LinePlot()
    plot.x, plot.y = 45, 25
    plot.height, plot.width = 120, 370
    plot.data = [series]
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = len(series) - 1
    plot.yValueAxis.valueMin = min(values) - span * 0.1
    plot.yValueAxis.valueMax = max(values) + span * 0.1
    plot.lines[0].strokeColor = colors.HexColor("#4f46e5")
    plot.lines[0].strokeWidth = 1.6
    drawing.add(plot)
    return drawing


def build_pdf_report(payload: dict) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, pageCompression=0,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("AlgoEdgeTitle", parent=styles["Title"], textColor=colors.HexColor("#0f1526"))
    heading_style = ParagraphStyle(
        "AlgoEdgeHeading", parent=styles["Heading2"], textColor=colors.HexColor("#0f1526"), spaceBefore=14,
    )
    body_style = styles["BodyText"]
    disclaimer_style = ParagraphStyle(
        "Disclaimer", parent=styles["BodyText"], textColor=colors.HexColor("#6b7280"), fontSize=8, leading=11,
    )

    story: list[Any] = [
        Paragraph("AlgoEdge Backtest Report", title_style),
        Paragraph(
            f"{payload.get('indexName', payload.get('indexId'))} &middot; "
            f"{payload.get('interval')} timeframe &middot; period {payload.get('period')} &middot; "
            f"{payload.get('candleCount')} candles",
            body_style,
        ),
        Paragraph(f"Generated {datetime.now(IST).strftime('%d %b %Y, %H:%M')}", body_style),
        Spacer(1, 8),
        Paragraph(payload.get("disclaimer", ""), disclaimer_style),
    ]

    for label, segment in _segments(payload):
        metrics = segment.get("metrics") or {}
        trades = segment.get("trades") or []
        story.append(Paragraph(label, heading_style))
        if metrics.get("totalTrades", 0) == 0:
            story.append(Paragraph("No trades were generated in this window.", body_style))
            continue

        kpi_data = [["Metric", "Value"]] + [
            [kpi_label, _format_kpi(kpi_label, value)] for kpi_label, value in _kpi_rows(metrics)
        ]
        kpi_table = Table(kpi_data, colWidths=[240, 130])
        kpi_table.setStyle(_table_style())
        story.append(kpi_table)
        story.append(Spacer(1, 10))

        drawing = _equity_chart_drawing(trades)
        if drawing is not None:
            story.append(Paragraph("Equity curve — cumulative points (not rupee P&amp;L)", body_style))
            story.append(drawing)
            story.append(Spacer(1, 10))

        call_perf = metrics.get("callPerformance") or {}
        put_perf = metrics.get("putPerformance") or {}
        direction_table = Table(
            [
                ["", "Trades", "Win rate (%)", "Net points"],
                ["CALL", call_perf.get("trades"), _pct(call_perf.get("win_rate")), _num(call_perf.get("net_points"))],
                ["PUT", put_perf.get("trades"), _pct(put_perf.get("win_rate")), _num(put_perf.get("net_points"))],
            ],
            colWidths=[90, 90, 100, 100],
        )
        direction_table.setStyle(_table_style())
        story.append(direction_table)
        story.append(Spacer(1, 10))

        time_of_day = metrics.get("timeOfDayPerformance") or []
        if time_of_day:
            story.append(Paragraph("Time-of-day performance", body_style))
            rows = [["Hour", "Trades", "Win rate (%)", "Net points"]] + [
                [bucket.get("label"), bucket.get("trades"), _pct(bucket.get("win_rate")), _num(bucket.get("net_points"))]
                for bucket in time_of_day
            ]
            tod_table = Table(rows, colWidths=[95, 95, 95, 95])
            tod_table.setStyle(_table_style())
            story.append(tod_table)
            story.append(Spacer(1, 10))

        regime = metrics.get("marketRegimePerformance") or []
        if regime:
            story.append(Paragraph(
                "Market-regime performance (price vs its own 50-period SMA — a simple proxy, not an "
                "authoritative regime classifier)", body_style,
            ))
            rows = [["Regime", "Trades", "Win rate (%)", "Net points"]] + [
                [bucket.get("label"), bucket.get("trades"), _pct(bucket.get("win_rate")), _num(bucket.get("net_points"))]
                for bucket in regime
            ]
            regime_table = Table(rows, colWidths=[95, 95, 95, 95])
            regime_table.setStyle(_table_style())
            story.append(regime_table)

    doc.build(story)
    return buffer.getvalue()
