from __future__ import annotations

import time
from datetime import datetime
from io import BytesIO
from typing import Any

import pandas as pd
import polars as pl
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side, numbers
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from .logging_config import get_logger

logger = get_logger(__name__)

# ── Brand palette ──────────────────────────────────────────────────────────────
_BRAND_DARK = "1B3A5C"  # dark navy
_BRAND_MED = "2E75B6"   # azure-blue
_BRAND_LIGHT = "D6E4F0"  # light blue tint
_WHITE = "FFFFFF"
_LIGHT_GREY = "F2F2F2"
_RED = "C0392B"
_GREEN = "27AE60"
_ORANGE = "E67E22"

_HEADER_FONT = Font(name="Calibri", bold=True, color=_WHITE, size=11)
_HEADER_FILL = PatternFill(start_color=_BRAND_DARK, end_color=_BRAND_DARK, fill_type="solid")
_SUBHEADER_FILL = PatternFill(start_color=_BRAND_LIGHT, end_color=_BRAND_LIGHT, fill_type="solid")
_SUBHEADER_FONT = Font(name="Calibri", bold=True, size=11, color="000000")
_BODY_FONT = Font(name="Calibri", size=10)
_KPI_VALUE_FONT = Font(name="Calibri", bold=True, size=14, color=_BRAND_MED)
_KPI_LABEL_FONT = Font(name="Calibri", size=10, color="555555")
_TITLE_FONT = Font(name="Calibri", bold=True, size=18, color=_BRAND_DARK)
_SECTION_FONT = Font(name="Calibri", bold=True, size=13, color=_BRAND_DARK)
_THIN_BORDER = Border(
    bottom=Side(style="thin", color="CCCCCC"),
)
_CURRENCY_FMT = '#,##0.00'
_INT_FMT = '#,##0'


_PERCENT_FMT = '0.0%'
_PERCENT_DISPLAY_FMT = '#,##0.0'


def build_excel_workbook(
    summary: dict[str, Any],
    pivots: dict[str, pl.DataFrame],
    commitments: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    recommendation_summary: dict[str, Any],
    cost_query_rows: list[dict[str, Any]] | None = None,
    savings_analysis: dict[str, Any] | None = None,
) -> bytes:
    logger.info(
        "Building Excel workbook: %d pivots, %d commitments, %d recommendations, savings=%s",
        len(pivots), len(commitments), len(recommendations), bool(savings_analysis),
    )
    t0 = time.perf_counter()
    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)  # type: ignore[arg-type]

    _write_executive_summary(wb, summary, recommendation_summary, pivots)

    # RI / SP Savings sheets (placed right after Executive Summary)
    if savings_analysis and (
        savings_analysis.get("savings_by_category")
        or savings_analysis.get("top_opportunities")
    ):
        _write_ri_sp_savings(wb, savings_analysis, summary)
        _write_savings_by_region(wb, savings_analysis, summary)
        _write_top_opportunities(wb, savings_analysis, summary)

    _write_cost_breakdown(wb, pivots)
    _write_daily_trend(wb, pivots)
    _write_commitments(wb, commitments)
    _write_advisor(wb, recommendations)
    if cost_query_rows:
        _write_cost_query(wb, cost_query_rows)

    # Pivot-friendly data sheets (always last — raw data for ad-hoc analysis)
    if savings_analysis:
        _write_pivot_data(wb, savings_analysis)
    _write_summary_data(wb, pivots)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    content = output.getvalue()
    elapsed = time.perf_counter() - t0
    logger.info(
        "Excel workbook built in %.2fs: %d sheets, %d bytes",
        elapsed, len(wb.sheetnames), len(content),
    )
    return content


# ═══════════════════════════════════════════════════════════════════════════════
# Executive Summary
# ═══════════════════════════════════════════════════════════════════════════════

def _write_executive_summary(
    wb: Workbook,
    summary: dict[str, Any],
    rec_summary: dict[str, Any],
    pivots: dict[str, pl.DataFrame],
) -> None:
    ws = wb.create_sheet("Executive Summary")
    ws.sheet_properties.tabColor = _BRAND_DARK

    # Title block
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = "Azure Cost & Commitment Analysis"
    c.font = _TITLE_FONT
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:H2")
    ws["A2"].value = f"Generated {datetime.now():%B %d, %Y at %H:%M}  |  Period: {summary.get('period_start', 'N/A')} to {summary.get('period_end', 'N/A')}"
    ws["A2"].font = Font(name="Calibri", italic=True, size=10, color="666666")
    ws.row_dimensions[2].height = 20

    # ── KPI cards row ──────────────────────────────────────────────────────────
    row = 4
    kpis = [
        ("Total Spend", f"{summary.get('currency', 'USD')} {summary.get('total_cost', 0):,.2f}"),
        ("Line Items", f"{summary.get('record_count', 0):,}"),
        ("Active Commitments", str(rec_summary.get("active_commitments", "—"))),
        ("Expiring ≤90 Days", str(rec_summary.get("commitments_expiring_within_90_days", "—"))),
        ("Advisor Recommendations", str(rec_summary.get("advisor_cost_recommendation_count", "—"))),
        ("Est. Annual Savings", f"{summary.get('currency', 'USD')} {rec_summary.get('advisor_estimated_annual_savings', 0):,.2f}"),
    ]

    for col_idx, (label, value) in enumerate(kpis, start=1):
        cell_label = ws.cell(row=row, column=col_idx, value=label)
        cell_label.font = _KPI_LABEL_FONT
        cell_label.alignment = Alignment(horizontal="center")

        cell_value = ws.cell(row=row + 1, column=col_idx, value=value)
        cell_value.font = _KPI_VALUE_FONT
        cell_value.alignment = Alignment(horizontal="center")

        # Light card background
        for r in (row, row + 1):
            ws.cell(row=r, column=col_idx).fill = PatternFill(
                start_color=_LIGHT_GREY, end_color=_LIGHT_GREY, fill_type="solid"
            )
        ws.column_dimensions[get_column_letter(col_idx)].width = 22

    ws.row_dimensions[row].height = 18
    ws.row_dimensions[row + 1].height = 28

    # ── Top 10 Service Families table ──────────────────────────────────────────
    tbl_row = row + 4
    ws.cell(row=tbl_row, column=1, value="Top 10 — Cost by Service").font = _SECTION_FONT
    tbl_row += 1
    if "cost_by_service_family" in pivots:
        df = pivots["cost_by_service_family"].to_pandas().head(10)
        _write_branded_table(ws, df, start_row=tbl_row, start_col=1, currency_cols=["TotalCost"])
        _add_bar_chart(ws, tbl_row, len(df), 1, 2, anchor=f"D{tbl_row}", title="Cost by Service")

    # ── Top 10 Subscriptions table ─────────────────────────────────────────────
    tbl_row2 = tbl_row + 14
    ws.cell(row=tbl_row2, column=1, value="Top 10 — Cost by Subscription").font = _SECTION_FONT
    tbl_row2 += 1
    if "cost_by_subscription" in pivots:
        df2 = pivots["cost_by_subscription"].to_pandas().head(10)
        _write_branded_table(ws, df2, start_row=tbl_row2, start_col=1, currency_cols=["TotalCost"])

    # Print setup
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Cost Breakdown
# ═══════════════════════════════════════════════════════════════════════════════

def _write_cost_breakdown(wb: Workbook, pivots: dict[str, pl.DataFrame]) -> None:
    ws = wb.create_sheet("Cost Breakdown")
    ws.sheet_properties.tabColor = _BRAND_MED
    r = 1
    for key, title in [
        ("cost_by_service_family", "Cost by Service / Meter Category"),
        ("cost_by_subscription", "Cost by Subscription"),
        ("cost_by_charge_type", "Cost by Charge Type"),
    ]:
        if key not in pivots:
            continue
        ws.cell(row=r, column=1, value=title).font = _SECTION_FONT
        r += 1
        df = pivots[key].to_pandas()
        r = _write_branded_table(ws, df, start_row=r, start_col=1, currency_cols=["TotalCost"])
        r += 2
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Daily Cost Trend
# ═══════════════════════════════════════════════════════════════════════════════

def _write_daily_trend(wb: Workbook, pivots: dict[str, pl.DataFrame]) -> None:
    if "cost_by_day" not in pivots:
        return
    ws = wb.create_sheet("Daily Trend")
    ws.sheet_properties.tabColor = _BRAND_MED
    df = pivots["cost_by_day"].to_pandas().sort_values("Date")
    ws.cell(row=1, column=1, value="Daily Cost Trend").font = _SECTION_FONT
    end_row = _write_branded_table(ws, df, start_row=2, start_col=1, currency_cols=["TotalCost"])

    # Line chart
    chart = LineChart()
    chart.title = "Daily Spend"
    chart.style = 10
    chart.y_axis.title = "Cost"
    chart.x_axis.title = "Date"
    chart.width = 28
    chart.height = 14
    data_ref = Reference(ws, min_col=2, min_row=2, max_row=end_row - 1)
    cat_ref = Reference(ws, min_col=1, min_row=3, max_row=end_row - 1)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cat_ref)
    series = chart.series[0]
    series.graphicalProperties.line.solidFill = _BRAND_MED  # type: ignore[union-attr]
    ws.add_chart(chart, f"D2")
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Commitments Inventory
# ═══════════════════════════════════════════════════════════════════════════════

def _write_commitments(wb: Workbook, commitments: list[dict[str, Any]]) -> None:
    ws = wb.create_sheet("Commitments")
    ws.sheet_properties.tabColor = _BRAND_MED
    ws.cell(row=1, column=1, value="Reserved Instances & Savings Plans Inventory").font = _SECTION_FONT
    if not commitments:
        ws.cell(row=3, column=1, value="No commitment data available. Sign in and fetch Azure data to populate.").font = Font(
            name="Calibri", italic=True, size=10, color="888888"
        )
        return
    df = pd.DataFrame(commitments)
    _write_branded_table(ws, df, start_row=2, start_col=1)

    # Conditional highlight for expiring within 90 days
    if "DaysRemaining" in df.columns:
        col_idx = list(df.columns).index("DaysRemaining") + 1
        for row_idx in range(3, 3 + len(df)):
            cell = ws.cell(row=row_idx, column=col_idx)
            try:
                val = int(cell.value) if cell.value is not None else None
            except (TypeError, ValueError):
                val = None
            if val is not None and val <= 90:
                cell.fill = PatternFill(start_color="FDEBD0", end_color="FDEBD0", fill_type="solid")
                cell.font = Font(name="Calibri", bold=True, size=10, color=_RED)
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Advisor Recommendations
# ═══════════════════════════════════════════════════════════════════════════════

def _write_advisor(wb: Workbook, recommendations: list[dict[str, Any]]) -> None:
    ws = wb.create_sheet("Advisor Recommendations")
    ws.sheet_properties.tabColor = _BRAND_MED
    ws.cell(row=1, column=1, value="Azure Advisor — Cost Optimization Recommendations").font = _SECTION_FONT
    if not recommendations:
        ws.cell(row=3, column=1, value="No recommendations available. Sign in and fetch Azure data to populate.").font = Font(
            name="Calibri", italic=True, size=10, color="888888"
        )
        return
    df = pd.DataFrame(recommendations)
    _write_branded_table(ws, df, start_row=2, start_col=1, currency_cols=["AnnualSavingsEstimate"])

    # Color-code impact
    if "Impact" in df.columns:
        col_idx = list(df.columns).index("Impact") + 1
        impact_colors = {"high": _RED, "medium": _ORANGE, "low": _GREEN}
        for row_idx in range(3, 3 + len(df)):
            cell = ws.cell(row=row_idx, column=col_idx)
            color = impact_colors.get(str(cell.value).lower())
            if color:
                cell.font = Font(name="Calibri", bold=True, size=10, color=color)
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Azure Cost Query
# ═══════════════════════════════════════════════════════════════════════════════

def _write_cost_query(wb: Workbook, cost_query_rows: list[dict[str, Any]]) -> None:
    ws = wb.create_sheet("Azure Cost Query")
    ws.sheet_properties.tabColor = _BRAND_MED
    ws.cell(row=1, column=1, value="Azure Cost Management — Actual Cost by Service").font = _SECTION_FONT
    df = pd.DataFrame(cost_query_rows)
    _write_branded_table(ws, df, start_row=2, start_col=1, currency_cols=["Cost"])
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# RI / SP Savings Analysis  (matches reference RI_RECOMMENDATIONS_Executive.xlsx)
# ═══════════════════════════════════════════════════════════════════════════════

def _write_ri_sp_savings(
    wb: Workbook,
    savings: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    ws = wb.create_sheet("RI SP Savings Analysis")
    ws.sheet_properties.tabColor = _BRAND_DARK

    kpi = savings.get("kpi", {})
    ccy = summary.get("currency", "USD")

    # ── Title block ────────────────────────────────────────────────────────────
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = "Reserved Instance / Savings Plan — Recommendations"
    c.font = _TITLE_FONT
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:H2")
    ws["A2"].value = (
        f"Generated {datetime.now():%B %d, %Y}  |  "
        f"Period: {summary.get('period_start', 'N/A')} to {summary.get('period_end', 'N/A')}  |  "
        f"Based on {savings.get('period_days', 0)}-day run rate extrapolated to 3 years"
    )
    ws["A2"].font = Font(name="Calibri", italic=True, size=10, color="666666")

    # ── KPI row 1  (headline metrics) ──────────────────────────────────────────
    row = 4
    headline_kpis = [
        ("Total 3-Year Savings", f"{ccy} {kpi.get('total_3yr_savings', 0):,.2f}"),
        ("Savings %", f"{kpi.get('savings_pct', 0):.1f}%"),
        ("Total Recommendations", f"{kpi.get('total_recommendations', 0):,}"),
    ]
    for col_idx, (label, value) in enumerate(headline_kpis, start=1):
        cell_l = ws.cell(row=row, column=col_idx, value=label)
        cell_l.font = _KPI_LABEL_FONT
        cell_l.alignment = Alignment(horizontal="center")
        cell_v = ws.cell(row=row + 1, column=col_idx, value=value)
        cell_v.font = Font(name="Calibri", bold=True, size=16, color=_GREEN)
        cell_v.alignment = Alignment(horizontal="center")
        for r in (row, row + 1):
            ws.cell(row=r, column=col_idx).fill = PatternFill(
                start_color=_LIGHT_GREY, end_color=_LIGHT_GREY, fill_type="solid"
            )
        ws.column_dimensions[get_column_letter(col_idx)].width = 26

    # ── KPI row 2  (context metrics) ──────────────────────────────────────────
    row2 = row + 3
    context_kpis = [
        ("Current 3-Yr Spend (PAYG)", f"{ccy} {kpi.get('current_3yr_spend', 0):,.2f}"),
        ("RI/SP 3-Yr Cost", f"{ccy} {kpi.get('ri_sp_3yr_cost', 0):,.2f}"),
        ("Resource Categories", f"{kpi.get('resource_categories', 0)}"),
    ]
    for col_idx, (label, value) in enumerate(context_kpis, start=1):
        cell_l = ws.cell(row=row2, column=col_idx, value=label)
        cell_l.font = _KPI_LABEL_FONT
        cell_l.alignment = Alignment(horizontal="center")
        cell_v = ws.cell(row=row2 + 1, column=col_idx, value=value)
        cell_v.font = _KPI_VALUE_FONT
        cell_v.alignment = Alignment(horizontal="center")
        for r in (row2, row2 + 1):
            ws.cell(row=r, column=col_idx).fill = PatternFill(
                start_color=_LIGHT_GREY, end_color=_LIGHT_GREY, fill_type="solid"
            )

    # ── Savings breakdown by Resource Type ─────────────────────────────────────
    tbl_row = row2 + 3
    ws.cell(row=tbl_row, column=1, value="Savings Breakdown by Resource Type").font = _SECTION_FONT
    tbl_row += 1

    cats = savings.get("savings_by_category", [])
    if cats:
        df = pd.DataFrame(cats)
        end = _write_branded_table(
            ws, df, start_row=tbl_row, start_col=1,
            currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)", "Annual Savings", "Monthly Savings"],
        )
        # Add bar chart for net savings
        _add_bar_chart(
            ws, tbl_row, len(df), label_col=1, value_col=4,
            anchor=f"J{tbl_row}", title="Net 3-Year Savings by Resource Type",
        )

    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Savings by Region
# ═══════════════════════════════════════════════════════════════════════════════

def _write_savings_by_region(
    wb: Workbook,
    savings: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    ws = wb.create_sheet("By Region")
    ws.sheet_properties.tabColor = _BRAND_MED

    ws.cell(row=1, column=1, value="RI / SP Savings — By Region").font = _SECTION_FONT
    regions = savings.get("savings_by_region", [])
    if not regions:
        ws.cell(row=3, column=1, value="No region-level data available.").font = Font(
            name="Calibri", italic=True, size=10, color="888888"
        )
        ws.sheet_view.showGridLines = False
        return

    df = pd.DataFrame(regions)
    end = _write_branded_table(
        ws, df, start_row=2, start_col=1,
        currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)", "Annual Savings"],
    )
    _add_bar_chart(
        ws, header_row=2, data_count=len(df), label_col=1, value_col=4,
        anchor=f"I2", title="Net 3-Year Savings by Region",
    )
    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Top Opportunities
# ═══════════════════════════════════════════════════════════════════════════════

def _write_top_opportunities(
    wb: Workbook,
    savings: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    ws = wb.create_sheet("Top Opportunities")
    ws.sheet_properties.tabColor = _BRAND_MED

    ws.cell(row=1, column=1, value="RI / SP — Top Opportunities Ranked by Savings").font = _SECTION_FONT
    opps = savings.get("top_opportunities", [])
    if not opps:
        ws.cell(row=3, column=1, value="No recommendation data available.").font = Font(
            name="Calibri", italic=True, size=10, color="888888"
        )
        ws.sheet_view.showGridLines = False
        return

    df = pd.DataFrame(opps)
    # Drop Description column from display if too wide
    display_cols = [c for c in df.columns if c != "Description"]
    display_df = df[display_cols]
    end = _write_branded_table(
        ws, display_df, start_row=2, start_col=1,
        currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)", "Annual Savings"],
    )

    # Color-code savings % column
    if "Savings %" in display_df.columns:
        sav_col_idx = list(display_df.columns).index("Savings %") + 1
        for row_idx in range(3, 3 + len(display_df)):
            cell = ws.cell(row=row_idx, column=sav_col_idx)
            try:
                val = float(cell.value) if cell.value is not None else 0
            except (TypeError, ValueError):
                val = 0
            if val >= 40:
                cell.font = Font(name="Calibri", bold=True, size=10, color=_GREEN)
            elif val >= 20:
                cell.font = Font(name="Calibri", bold=True, size=10, color=_ORANGE)

    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Pivot Data  (flat detail — one row per recommendation, ready for Excel Pivot)
# ═══════════════════════════════════════════════════════════════════════════════

def _write_pivot_data(wb: Workbook, savings: dict[str, Any]) -> None:
    ws = wb.create_sheet("Pivot Data")
    ws.sheet_properties.tabColor = "4472C4"

    ws.cell(row=1, column=1, value="RI / SP Savings — Pivot Data").font = _SECTION_FONT
    ws.cell(row=2, column=1, value=(
        "This sheet contains one row per recommendation with all detail columns. "
        "Insert an Excel PivotTable on this range to slice by Resource Type, Region, SKU, etc."
    )).font = Font(name="Calibri", italic=True, size=9, color="666666")

    opps = savings.get("top_opportunities", [])
    cats = savings.get("savings_by_category", [])
    regions = savings.get("savings_by_region", [])
    kpi = savings.get("kpi", {})

    # ── Opportunity detail table (main pivot source) ───────────────────────────
    if opps:
        # Build a rich flat table with extra computed columns
        rows: list[dict[str, Any]] = []
        for opp in opps:
            rows.append({
                "Rank": opp.get("Rank"),
                "Resource Type": opp.get("Resource Type", ""),
                "SKU": opp.get("SKU", ""),
                "Region": opp.get("Region", ""),
                "Current 3-Yr Cost": opp.get("Current 3-Yr Cost", 0),
                "RI/SP 3-Yr Cost": opp.get("RI/SP 3-Yr Cost", 0),
                "Net Savings (3-Yr)": opp.get("Net Savings (3-Yr)", 0),
                "Savings %": opp.get("Savings %", 0),
                "Annual Savings": opp.get("Annual Savings", 0),
                "Monthly Savings": round(opp.get("Annual Savings", 0) / 12, 2),
                "% of Total Savings": (
                    round(opp.get("Net Savings (3-Yr)", 0) / kpi.get("total_3yr_savings", 1) * 100, 1)
                    if kpi.get("total_3yr_savings", 0) > 0 else 0
                ),
                "Description": opp.get("Description", ""),
            })
        df_opp = pd.DataFrame(rows)
        tbl_start = 4
        ws.cell(row=tbl_start - 1, column=1, value="Recommendation Detail").font = _SECTION_FONT
        tbl_end = _write_branded_table(
            ws, df_opp, start_row=tbl_start, start_col=1,
            currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)",
                           "Annual Savings", "Monthly Savings"],
        )

        # Add an Excel Table object so user can one-click "Insert PivotTable"
        last_col_letter = get_column_letter(len(df_opp.columns))
        table_ref = f"A{tbl_start}:{last_col_letter}{tbl_end - 1}"
        tbl = Table(displayName="SavingsDetail", ref=table_ref)
        tbl.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showFirstColumn=False,
            showLastColumn=False, showRowStripes=True, showColumnStripes=False,
        )
        ws.add_table(tbl)

    # ── Category summary beneath ───────────────────────────────────────────────
    if cats:
        gap = (tbl_end + 2) if opps else 4
        ws.cell(row=gap, column=1, value="Summary by Resource Type").font = _SECTION_FONT
        df_cat = pd.DataFrame(cats)
        _write_branded_table(
            ws, df_cat, start_row=gap + 1, start_col=1,
            currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)",
                           "Annual Savings", "Monthly Savings"],
        )

    # ── Region summary beneath ─────────────────────────────────────────────────
    if regions:
        # Find the next free row
        last_used = ws.max_row + 2
        ws.cell(row=last_used, column=1, value="Summary by Region").font = _SECTION_FONT
        df_rgn = pd.DataFrame(regions)
        _write_branded_table(
            ws, df_rgn, start_row=last_used + 1, start_col=1,
            currency_cols=["Current 3-Yr Cost", "RI/SP 3-Yr Cost", "Net Savings (3-Yr)",
                           "Annual Savings"],
        )

    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Summary Data  (all invoice-level pivots stacked for ad-hoc analysis)
# ═══════════════════════════════════════════════════════════════════════════════

def _write_summary_data(wb: Workbook, pivots: dict[str, pl.DataFrame]) -> None:
    ws = wb.create_sheet("Summary Data")
    ws.sheet_properties.tabColor = "4472C4"

    ws.cell(row=1, column=1, value="Invoice Summary Pivots — Raw Data").font = _SECTION_FONT
    ws.cell(row=2, column=1, value=(
        "All pivot tables from the invoice analysis in one sheet. "
        "Use these as data sources for your own PivotTables or charts."
    )).font = Font(name="Calibri", italic=True, size=9, color="666666")

    pivot_defs = [
        ("cost_by_service_family", "Cost by Service / Meter Category"),
        ("cost_by_subscription", "Cost by Subscription"),
        ("cost_by_charge_type", "Cost by Charge Type"),
        ("cost_by_pricing_model", "Cost by Pricing Model"),
        ("cost_by_region", "Cost by Region"),
        ("cost_by_day", "Cost by Day"),
    ]

    r = 4
    for key, title in pivot_defs:
        if key not in pivots:
            continue
        df = pivots[key].to_pandas()
        if df.empty:
            continue
        ws.cell(row=r, column=1, value=title).font = _SECTION_FONT
        r += 1
        r = _write_branded_table(ws, df, start_row=r, start_col=1, currency_cols=["TotalCost"])
        r += 2  # gap between sections

    if r == 4:
        ws.cell(row=4, column=1, value="No pivot data available. Analyze an invoice CSV first.").font = Font(
            name="Calibri", italic=True, size=10, color="888888"
        )

    ws.sheet_view.showGridLines = False


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _write_branded_table(
    ws: Worksheet,
    df: pd.DataFrame,
    start_row: int,
    start_col: int,
    currency_cols: list[str] | None = None,
) -> int:
    """Write a DataFrame as a formatted table and return the next available row."""
    currency_cols = currency_cols or []

    # Header row
    for col_offset, col_name in enumerate(df.columns):
        cell = ws.cell(row=start_row, column=start_col + col_offset, value=col_name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(start_col + col_offset)].width = max(len(str(col_name)) + 6, 14)

    # Data rows
    for row_offset, (_, data_row) in enumerate(df.iterrows()):
        is_alt = row_offset % 2 == 1
        for col_offset, col_name in enumerate(df.columns):
            cell = ws.cell(row=start_row + 1 + row_offset, column=start_col + col_offset, value=data_row[col_name])
            cell.font = _BODY_FONT
            cell.border = _THIN_BORDER
            if is_alt:
                cell.fill = PatternFill(start_color=_LIGHT_GREY, end_color=_LIGHT_GREY, fill_type="solid")
            if col_name in currency_cols:
                cell.number_format = _CURRENCY_FMT
                cell.alignment = Alignment(horizontal="right")
            elif isinstance(data_row[col_name], (int, float)):
                cell.number_format = _INT_FMT

    # Auto-fit column widths (approximate)
    for col_offset, col_name in enumerate(df.columns):
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if len(df) else 0,
        )
        ws.column_dimensions[get_column_letter(start_col + col_offset)].width = min(max(max_len + 4, 14), 50)

    return start_row + 1 + len(df)


def _add_bar_chart(
    ws: Worksheet,
    header_row: int,
    data_count: int,
    label_col: int,
    value_col: int,
    anchor: str,
    title: str,
) -> None:
    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = title
    chart.width = 20
    chart.height = 12
    data_ref = Reference(ws, min_col=value_col, min_row=header_row, max_row=header_row + data_count)
    cat_ref = Reference(ws, min_col=label_col, min_row=header_row + 1, max_row=header_row + data_count)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cat_ref)
    series = chart.series[0]
    series.graphicalProperties.solidFill = _BRAND_MED  # type: ignore[union-attr]
    ws.add_chart(chart, anchor)
