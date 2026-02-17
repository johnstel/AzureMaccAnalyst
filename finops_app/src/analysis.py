from __future__ import annotations

import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from .logging_config import get_logger
from .models import AnalysisSummary, AzureRecommendation, CommitmentItem

logger = get_logger(__name__)

REQUIRED_COLUMNS = [
    "Date",
    "Cost",
    "Quantity",
    "BillingCurrency",
    "SubscriptionId",
    "SubscriptionName",
    "ServiceFamily",
    "MeterCategory",
    "MeterSubcategory",
    "MeterRegion",
    "ConsumedService",
    "Product",
    "PricingModel",
    "ChargeType",
    "ResourceGroup",
    "ResourceId",
    "ReservationId",
    "ReservationName",
    "Term",
]


def summarize_export(file_path: str) -> tuple[AnalysisSummary, dict[str, pl.DataFrame]]:
    logger.info("summarize_export: starting analysis of %s", file_path)
    t0 = time.perf_counter()
    path = Path(file_path)
    if not path.exists():
        logger.error("File not found: %s", file_path)
        raise FileNotFoundError(f"File not found: {file_path}")
    logger.debug("File size: %.2f MB", path.stat().st_size / (1024 * 1024))

    scan = pl.scan_csv(file_path, infer_schema_length=5000, ignore_errors=True)
    existing = _existing_columns(scan)
    selected = [column for column in REQUIRED_COLUMNS if column in existing]

    if not selected:
        logger.error("CSV has no expected Azure invoice columns. Found: %s", existing)
        raise ValueError("CSV file does not contain expected Azure invoice detail columns.")
    logger.debug("Selected %d of %d required columns: %s", len(selected), len(REQUIRED_COLUMNS), selected)

    typed = scan.select(selected).with_columns(
        [
            pl.col("Cost").cast(pl.Float64, strict=False) if "Cost" in selected else pl.lit(0.0).alias("Cost"),
            pl.col("Quantity").cast(pl.Float64, strict=False)
            if "Quantity" in selected
            else pl.lit(0.0).alias("Quantity"),
            pl.col("Date").str.to_date(strict=False) if "Date" in selected else pl.lit(None).alias("Date"),
        ]
    )

    # Coalesce empty primary columns to populated fallback columns.
    # Many Azure export formats leave SubscriptionName / ServiceFamily blank
    # while SubscriptionId / MeterCategory are always populated.
    coalesce_exprs: list[pl.Expr] = []
    if "SubscriptionName" in selected and "SubscriptionId" in selected:
        coalesce_exprs.append(
            pl.when(pl.col("SubscriptionName").is_null() | (pl.col("SubscriptionName").cast(pl.Utf8).str.strip_chars() == ""))
            .then(pl.col("SubscriptionId"))
            .otherwise(pl.col("SubscriptionName"))
            .alias("SubscriptionName")
        )
    if "ServiceFamily" in selected and "MeterCategory" in selected:
        coalesce_exprs.append(
            pl.when(pl.col("ServiceFamily").is_null() | (pl.col("ServiceFamily").cast(pl.Utf8).str.strip_chars() == ""))
            .then(pl.col("MeterCategory"))
            .otherwise(pl.col("ServiceFamily"))
            .alias("ServiceFamily")
        )
    if coalesce_exprs:
        typed = typed.with_columns(coalesce_exprs)

    summary_df = typed.select(
        [
            pl.min("Date").alias("period_start") if "Date" in selected else pl.lit(None).alias("period_start"),
            pl.max("Date").alias("period_end") if "Date" in selected else pl.lit(None).alias("period_end"),
            pl.col("BillingCurrency").drop_nulls().first().alias("currency")
            if "BillingCurrency" in selected
            else pl.lit("USD").alias("currency"),
            pl.sum("Cost").alias("total_cost"),
            pl.sum("Quantity").alias("total_quantity"),
            pl.len().alias("record_count"),
        ]
    ).collect(streaming=True)

    row = summary_df.to_dicts()[0]
    summary = AnalysisSummary(
        period_start=row.get("period_start") if isinstance(row.get("period_start"), date) else None,
        period_end=row.get("period_end") if isinstance(row.get("period_end"), date) else None,
        currency=row.get("currency") or "USD",
        total_cost=float(row.get("total_cost") or 0.0),
        total_quantity=float(row.get("total_quantity") or 0.0),
        record_count=int(row.get("record_count") or 0),
    )
    logger.info(
        "Invoice summary: period=%s to %s, cost=%.2f %s, records=%d",
        summary.period_start, summary.period_end, summary.total_cost,
        summary.currency, summary.record_count,
    )

    pivots = _build_summary_tables(typed, selected)
    elapsed = time.perf_counter() - t0
    logger.info("summarize_export completed in %.2fs, %d pivot tables built", elapsed, len(pivots))
    return summary, pivots


def build_recommendation_summary(
    recommendations: list[AzureRecommendation],
    commitments: list[CommitmentItem],
) -> dict[str, Any]:
    annual_savings = sum(max(rec.annual_savings_estimate, 0.0) for rec in recommendations)
    open_commitments = [item for item in commitments if item.days_remaining is None or item.days_remaining > 0]
    expiring_90d = [item for item in commitments if item.days_remaining is not None and item.days_remaining <= 90]

    return {
        "advisor_cost_recommendation_count": len(recommendations),
        "advisor_estimated_annual_savings": annual_savings,
        "active_commitments": len(open_commitments),
        "commitments_expiring_within_90_days": len(expiring_90d),
    }


def commitment_rows(commitments: list[CommitmentItem]) -> list[dict[str, Any]]:
    return [
        {
            "Source": item.source,
            "Name": item.name,
            "Term": item.term,
            "State": item.state,
            "StartDate": item.start_date,
            "EndDate": item.end_date,
            "DaysRemaining": item.days_remaining,
            "Scope": item.scope,
        }
        for item in commitments
    ]


def recommendation_rows(recommendations: list[AzureRecommendation]) -> list[dict[str, Any]]:
    return [
        {
            "Category": item.category,
            "Impact": item.impact,
            "Description": item.short_description,
            "AnnualSavingsEstimate": item.annual_savings_estimate,
            "ResourceId": item.resource_id,
        }
        for item in recommendations
    ]


def summary_to_dict(summary: AnalysisSummary) -> dict[str, Any]:
    return asdict(summary)


def build_savings_analysis(
    file_path: str,
    recommendations: list[AzureRecommendation],
    augmented_annual_paygo: float | None = None,
) -> dict[str, Any]:
    """Build a Pay-Go vs RI/SP savings analysis from invoice data + Advisor recs.

    Args:
        file_path: Path to the invoice CSV.
        recommendations: Advisor cost recommendations.
        augmented_annual_paygo: If supplied (e.g. from demo mode), overrides the
            annual PAYG run-rate derived from the invoice CSV.  This is needed
            when the recommendation set covers services beyond what appears in
            the CSV (e.g. simulated enterprise workloads).

    Returns a dict with keys:
      - pricing_model_summary: list[dict] – spend by PricingModel
      - paygo_total: float – total OnDemand spend in the period
      - period_days: int
      - annual_paygo_run_rate: float
      - three_year_paygo: float
      - savings_by_category: list[dict] – per-MeterCategory savings breakdown
      - savings_by_region: list[dict] – per-MeterRegion savings breakdown
      - top_opportunities: list[dict] – ranked Advisor recs with savings %
      - kpi: dict – headline KPI block (total_3yr_savings, savings_pct, etc.)
    """
    logger.info(
        "build_savings_analysis: file=%s, recs=%d, augmented_paygo=%s",
        file_path, len(recommendations), augmented_annual_paygo,
    )
    t0 = time.perf_counter()
    path = Path(file_path)
    if not path.exists():
        logger.error("File not found for savings analysis: %s", file_path)
        raise FileNotFoundError(file_path)

    scan = pl.scan_csv(file_path, infer_schema_length=5000, ignore_errors=True)
    existing = _existing_columns(scan)

    cost_col = "Cost" if "Cost" in existing else None
    if cost_col is None:
        logger.warning("No 'Cost' column found — returning empty savings")
        return _empty_savings()

    cols_needed = ["Cost"]
    for c in ("PricingModel", "MeterCategory", "MeterSubcategory", "MeterRegion",
              "ConsumedService", "Product", "Date"):
        if c in existing:
            cols_needed.append(c)

    lf = scan.select(cols_needed).with_columns(
        pl.col("Cost").cast(pl.Float64, strict=False),
    )
    if "Date" in cols_needed:
        lf = lf.with_columns(pl.col("Date").str.to_date(strict=False))

    df = lf.collect(streaming=True)

    # ── Pricing model breakdown ────────────────────────────────────────────────
    if "PricingModel" in df.columns:
        pm_summary = (
            df.group_by("PricingModel")
            .agg(pl.sum("Cost").alias("TotalCost"), pl.len().alias("Count"))
            .sort("TotalCost", descending=True)
            .to_dicts()
        )
    else:
        pm_summary = [{"PricingModel": "Unknown", "TotalCost": df["Cost"].sum(), "Count": len(df)}]

    total_cost = df["Cost"].sum() or 0.0
    paygo_total = sum(
        r["TotalCost"] for r in pm_summary
        if str(r.get("PricingModel", "")).lower() in ("ondemand", "on demand", "payg", "pay-as-you-go", "")
    ) or total_cost

    # Period length → annualise
    if "Date" in df.columns and df["Date"].drop_nulls().len() > 0:
        dates = df["Date"].drop_nulls()
        period_start = dates.min()
        period_end = dates.max()
        period_days = max((period_end - period_start).days, 1)  # type: ignore[operator]
    else:
        period_days = 30  # fallback

    annual_paygo_from_csv = paygo_total / period_days * 365

    # If an augmented annual PAYGO was provided (demo mode with extra services),
    # use the larger of the two so savings never exceed the PAYG base.
    if augmented_annual_paygo and augmented_annual_paygo > annual_paygo_from_csv:
        annual_paygo = augmented_annual_paygo
    else:
        annual_paygo = annual_paygo_from_csv

    three_year_paygo = annual_paygo * 3

    # ── Advisor recs → savings projection ──────────────────────────────────────
    total_annual_savings = sum(max(r.annual_savings_estimate, 0) for r in recommendations)
    # Cap savings so they can never exceed total PAYG (defensive)
    total_annual_savings = min(total_annual_savings, annual_paygo)
    three_year_savings = total_annual_savings * 3
    ri_three_year_cost = three_year_paygo - three_year_savings
    savings_pct = (three_year_savings / three_year_paygo * 100) if three_year_paygo > 0 else 0.0

    # ── Savings by MeterCategory ───────────────────────────────────────────────
    # Map Advisor recs to categories (best-effort from short description & resource id)
    cat_savings: dict[str, float] = {}
    for rec in recommendations:
        cat = _guess_category(rec, df)
        cat_savings[cat] = cat_savings.get(cat, 0.0) + max(rec.annual_savings_estimate, 0)

    # Current PAYG cost per MeterCategory
    if "MeterCategory" in df.columns:
        cat_costs = dict(
            df.filter(
                pl.col("PricingModel").is_in(["OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", ""])
                if "PricingModel" in df.columns
                else pl.lit(True)
            )
            .group_by("MeterCategory")
            .agg(pl.sum("Cost"))
            .iter_rows()
        )
    else:
        cat_costs = {"All Services": paygo_total}

    savings_by_category = []
    all_cats = set(list(cat_costs.keys()) + list(cat_savings.keys()))
    for cat in sorted(all_cats):
        csv_paygo_annual = cat_costs.get(cat, 0.0) / period_days * 365
        ann_sav = cat_savings.get(cat, 0.0)

        # When a category has recommendations but no/tiny invoice cost
        # (e.g. demo-simulated services), derive implied PAYG from the
        # savings assuming a ~40% average RI savings rate.
        if ann_sav > 0 and csv_paygo_annual < ann_sav:
            implied_paygo_annual = ann_sav / 0.40  # conservative estimate
            paygo_annual = max(csv_paygo_annual, implied_paygo_annual)
        else:
            paygo_annual = csv_paygo_annual

        paygo_3yr = paygo_annual * 3
        sav_3yr = ann_sav * 3
        ri_cost = paygo_3yr - sav_3yr
        pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
        pct_of_total = (sav_3yr / three_year_savings * 100) if three_year_savings > 0 else 0.0
        savings_by_category.append({
            "Resource Type": cat,
            "Current 3-Yr Cost": round(paygo_3yr, 2),
            "RI/SP 3-Yr Cost": round(ri_cost, 2),
            "Net Savings (3-Yr)": round(sav_3yr, 2),
            "Savings %": round(pct, 1),
            "Annual Savings": round(ann_sav, 2),
            "Monthly Savings": round(ann_sav / 12, 2),
            "% of Total Savings": round(pct_of_total, 1),
        })

    savings_by_category.sort(key=lambda x: x["Net Savings (3-Yr)"], reverse=True)

    # ── Savings by Region ──────────────────────────────────────────────────────
    savings_by_region = _build_region_savings(df, recommendations, period_days, three_year_savings)

    # ── Top Opportunities (ranked Advisor recs) ────────────────────────────────
    top_opps = []
    for rank, rec in enumerate(
        sorted(recommendations, key=lambda r: r.annual_savings_estimate, reverse=True), start=1
    ):
        ann = max(rec.annual_savings_estimate, 0)
        sav_3 = ann * 3
        # Try to extract SKU / region from the raw advisory data
        raw = rec.raw or {}
        extended = raw.get("extendedProperties", raw.get("properties", {}).get("extendedProperties", {})) or {}
        sku = extended.get("sku", extended.get("vmSize", extended.get("currentSku", rec.short_description[:60])))
        region = extended.get("region", extended.get("location", "—"))
        resource_type = _guess_category(rec, df)

        # Derive implied PAYG from savings + typical savings rate
        # Try to parse the savings % from the description (e.g. "save ~42%")
        import re
        pct_match = re.search(r"~(\d+)%", rec.short_description or "")
        if pct_match:
            sav_rate = int(pct_match.group(1)) / 100
        else:
            sav_rate = 0.40  # default assumption
        sav_rate = max(sav_rate, 0.01)

        implied_paygo_3yr = sav_3 / sav_rate if sav_3 > 0 else 0
        implied_ri_3yr = implied_paygo_3yr - sav_3

        top_opps.append({
            "Rank": rank,
            "Resource Type": resource_type,
            "SKU": sku,
            "Region": region,
            "Current 3-Yr Cost": round(implied_paygo_3yr, 2),
            "RI/SP 3-Yr Cost": round(implied_ri_3yr, 2),
            "Net Savings (3-Yr)": round(sav_3, 2),
            "Savings %": round(sav_rate * 100, 1),
            "Annual Savings": round(ann, 2),
            "Description": rec.short_description,
        })

    kpi = {
        "total_3yr_savings": round(three_year_savings, 2),
        "savings_pct": round(savings_pct, 1),
        "total_recommendations": len(recommendations),
        "current_3yr_spend": round(three_year_paygo, 2),
        "ri_sp_3yr_cost": round(ri_three_year_cost, 2),
        "resource_categories": len(savings_by_category),
        "annual_savings": round(total_annual_savings, 2),
        "monthly_savings": round(total_annual_savings / 12, 2),
    }

    elapsed = time.perf_counter() - t0
    logger.info(
        "Savings analysis complete in %.2fs: 3yr_savings=%.2f, savings_pct=%.1f%%, categories=%d, regions=%d, opportunities=%d",
        elapsed, three_year_savings, savings_pct, len(savings_by_category), len(savings_by_region), len(top_opps),
    )

    return {
        "pricing_model_summary": pm_summary,
        "paygo_total": paygo_total,
        "period_days": period_days,
        "annual_paygo_run_rate": round(annual_paygo, 2),
        "three_year_paygo": round(three_year_paygo, 2),
        "savings_by_category": savings_by_category,
        "savings_by_region": savings_by_region,
        "top_opportunities": top_opps,
        "kpi": kpi,
    }


def _empty_savings() -> dict[str, Any]:
    return {
        "pricing_model_summary": [],
        "paygo_total": 0,
        "period_days": 0,
        "annual_paygo_run_rate": 0,
        "three_year_paygo": 0,
        "savings_by_category": [],
        "savings_by_region": [],
        "top_opportunities": [],
        "kpi": {
            "total_3yr_savings": 0, "savings_pct": 0, "total_recommendations": 0,
            "current_3yr_spend": 0, "ri_sp_3yr_cost": 0, "resource_categories": 0,
            "annual_savings": 0, "monthly_savings": 0,
        },
    }


def _guess_category(rec: AzureRecommendation, df: pl.DataFrame) -> str:
    """Best-effort map an Advisor rec to a MeterCategory from the invoice data."""
    desc = (rec.short_description or "").lower()
    rid = (rec.resource_id or "").lower()

    # Map known resource providers to common MeterCategory names
    provider_map = {
        "microsoft.compute": "Virtual Machines",
        "microsoft.sql": "SQL Database",
        "microsoft.dbformysql": "Azure Database for MySQL",
        "microsoft.dbforpostgresql": "Azure Database for PostgreSQL",
        "microsoft.storage": "Storage",
        "microsoft.web": "Azure App Service",
        "microsoft.network": "Virtual Network",
        "microsoft.apimanagement": "API Management",
        "microsoft.containerservice": "Azure Kubernetes Service",
        "microsoft.cache": "Azure Cache for Redis",
        "microsoft.cosmosdb": "Azure Cosmos DB",
        "microsoft.documentdb": "Azure Cosmos DB",
        "microsoft.dataprotection": "Backup",
        "microsoft.recoveryservices": "Backup",
    }

    for provider, category in provider_map.items():
        if provider in rid:
            return category

    # Fallback: try matching description keywords to MeterCategory values in the data
    if "MeterCategory" in df.columns:
        cats = df["MeterCategory"].unique().to_list()
        for cat in cats:
            if cat and str(cat).lower() in desc:
                return str(cat)

    return rec.category or "Other"


def _build_region_savings(
    df: pl.DataFrame,
    recommendations: list[AzureRecommendation],
    period_days: int,
    total_3yr_savings: float,
) -> list[dict[str, Any]]:
    """Build savings breakdown by MeterRegion."""
    if "MeterRegion" not in df.columns:
        return []

    # Region cost from invoice
    region_costs: dict[str, float] = {}
    for row in (
        df.filter(
            pl.col("PricingModel").is_in(["OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", ""])
            if "PricingModel" in df.columns
            else pl.lit(True)
        )
        .group_by("MeterRegion")
        .agg(pl.sum("Cost"))
        .iter_rows()
    ):
        region_costs[str(row[0] or "Unspecified")] = row[1]

    # Advisor recs → region (best-effort from raw properties)
    region_savings: dict[str, float] = {}
    for rec in recommendations:
        raw = rec.raw or {}
        extended = raw.get("extendedProperties", raw.get("properties", {}).get("extendedProperties", {})) or {}
        region = extended.get("region", extended.get("location", "Unspecified")) or "Unspecified"
        region_savings[region] = region_savings.get(region, 0.0) + max(rec.annual_savings_estimate, 0)

    all_regions = set(list(region_costs.keys()) + list(region_savings.keys()))
    rows = []
    for rgn in sorted(all_regions):
        csv_paygo_ann = region_costs.get(rgn, 0.0) / period_days * 365
        ann_sav = region_savings.get(rgn, 0.0)

        # Derive implied PAYG when savings exceed CSV costs (demo/extra services)
        if ann_sav > 0 and csv_paygo_ann < ann_sav:
            paygo_ann = ann_sav / 0.40
        else:
            paygo_ann = csv_paygo_ann

        paygo_3yr = paygo_ann * 3
        sav_3yr = ann_sav * 3
        ri_cost = paygo_3yr - sav_3yr
        pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
        pct_of_total = (sav_3yr / total_3yr_savings * 100) if total_3yr_savings > 0 else 0.0
        rows.append({
            "Region": rgn,
            "Current 3-Yr Cost": round(paygo_3yr, 2),
            "RI/SP 3-Yr Cost": round(ri_cost, 2),
            "Net Savings (3-Yr)": round(sav_3yr, 2),
            "Savings %": round(pct, 1),
            "Annual Savings": round(ann_sav, 2),
            "% of Total Savings": round(pct_of_total, 1),
        })

    rows.sort(key=lambda x: x["Net Savings (3-Yr)"], reverse=True)
    return rows


def _existing_columns(scan: pl.LazyFrame) -> set[str]:
    return set(scan.collect_schema().names())


def _build_summary_tables(typed: pl.LazyFrame, selected: list[str]) -> dict[str, pl.DataFrame]:
    tables: dict[str, pl.DataFrame] = {}

    if "ServiceFamily" in selected:
        tables["cost_by_service_family"] = (
            typed.group_by("ServiceFamily")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .collect(streaming=True)
        )

    if "SubscriptionName" in selected:
        tables["cost_by_subscription"] = (
            typed.group_by("SubscriptionName")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .collect(streaming=True)
        )

    if "ChargeType" in selected:
        tables["cost_by_charge_type"] = (
            typed.group_by("ChargeType")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .collect(streaming=True)
        )

    if "Date" in selected:
        tables["cost_by_day"] = (
            typed.group_by("Date")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("Date")
            .collect(streaming=True)
        )

    if "PricingModel" in selected:
        tables["cost_by_pricing_model"] = (
            typed.group_by("PricingModel")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .collect(streaming=True)
        )

    if "MeterRegion" in selected:
        tables["cost_by_region"] = (
            typed.group_by("MeterRegion")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .collect(streaming=True)
        )

    return tables
