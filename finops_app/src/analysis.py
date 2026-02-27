from __future__ import annotations

import os
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

import polars as pl

from .file_loader import existing_columns, load_invoice_file, safe_datetime_expr
from .logging_config import get_logger
from .models import AnalysisSummary, AzureRecommendation, CommitmentItem

logger = get_logger(__name__)

# Azure Compute Savings Plans cover these service categories.
# Everything else (SQL DB, Cosmos DB, Redis, etc.) is RI-only.
SP_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({
    "Virtual Machines",
    "Azure App Service",
    "Azure Kubernetes Service",
    "Container Instances",
    "Azure VMware Solution",
    "Azure Functions",
    "Functions",           # billing-CSV MeterCategory alias
})

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

    scan = load_invoice_file(file_path)
    existing = existing_columns(scan)
    selected = [column for column in REQUIRED_COLUMNS if column in existing]

    if not selected:
        logger.error("File has no expected Azure invoice columns. Found: %s", existing)
        raise ValueError("File does not contain expected Azure invoice detail columns.")
    logger.debug("Selected %d of %d required columns: %s", len(selected), len(REQUIRED_COLUMNS), selected)

    typed = scan.select(selected).with_columns(
        [
            pl.col("Cost").cast(pl.Float64, strict=False) if "Cost" in selected else pl.lit(0.0).alias("Cost"),
            pl.col("Quantity").cast(pl.Float64, strict=False)
            if "Quantity" in selected
            else pl.lit(0.0).alias("Quantity"),
            safe_datetime_expr("Date", scan) if "Date" in selected else pl.lit(None).alias("Date"),
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
        period_start=row.get("period_start") if isinstance(row.get("period_start"), (date, datetime)) else None,
        period_end=row.get("period_end") if isinstance(row.get("period_end"), (date, datetime)) else None,
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
    retail_prices: dict[int, dict[str, float | None]] | None = None,
) -> dict[str, Any]:
    """Build a Pay-Go vs RI/SP savings analysis from invoice data + Advisor recs.

    Args:
        file_path: Path to the invoice CSV.
        recommendations: Advisor cost recommendations.
        augmented_annual_paygo: If supplied (e.g. from demo mode), overrides the
            annual PAYG run-rate derived from the invoice CSV.
        retail_prices: Optional dict mapping recommendation index → retail price
            info from the Azure Retail Prices API.  When provided, real RI/SP
            discount percentages are used instead of flat assumptions.

    Returns a dict with keys:
      - pricing_model_summary, paygo_total, period_days, annual_paygo_run_rate, three_year_paygo
      - ri_analysis: dict with savings_by_category, savings_by_region, top_opportunities, kpi
      - sp_analysis: dict with savings_by_category, savings_by_region, top_opportunities, kpi
      - has_retail_prices: bool — whether real lookup data was available
    """
    logger.info(
        "build_savings_analysis: file=%s, recs=%d, augmented_paygo=%s",
        file_path, len(recommendations), augmented_annual_paygo,
    )
    t0 = time.perf_counter()

    scan = load_invoice_file(file_path)
    existing = existing_columns(scan)

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
        lf = lf.with_columns(safe_datetime_expr("Date", scan))

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

    # ── Per-recommendation RI vs SP discount rates ───────────────────────────────
    # Standard Azure discount rates (3-Year commitments) used as fallback:
    #   Reserved Instances: ~40% savings — locked to specific SKU + region
    #   Savings Plans:      ~30% savings — flexible across SKU/region
    RI_DISCOUNT_RATE = float(os.getenv("RI_DISCOUNT_RATE", "0.40"))
    SP_DISCOUNT_RATE = float(os.getenv("SP_DISCOUNT_RATE", "0.30"))

    has_retail = bool(retail_prices)

    # For each rec, resolve effective RI / SP discount rate.
    # When the Retail Prices API returned data for a rec, use those real
    # percentages; otherwise fall back to the flat assumptions above.
    rec_ri_rate: list[float] = []
    rec_sp_rate: list[float] = []
    for i, _rec in enumerate(recommendations):
        if retail_prices and i in retail_prices:
            rp = retail_prices[i]
            ri_pct = rp.get("ri_3yr_savings_pct")
            sp_pct = rp.get("sp_3yr_savings_pct")
            # ri/sp_3yr_savings_pct is a percentage (e.g. 41.0 for 41%);
            # convert to a decimal fraction to match RI/SP_DISCOUNT_RATE.
            rec_ri_rate.append(ri_pct / 100 if ri_pct and ri_pct > 0 else RI_DISCOUNT_RATE)
            rec_sp_rate.append(sp_pct / 100 if sp_pct and sp_pct > 0 else SP_DISCOUNT_RATE)
        else:
            rec_ri_rate.append(RI_DISCOUNT_RATE)
            rec_sp_rate.append(SP_DISCOUNT_RATE)

    total_annual_savings = sum(max(r.annual_savings_estimate, 0) for r in recommendations)
    total_annual_savings = min(total_annual_savings, annual_paygo)

    # ── Pre-compute per-recommendation data ────────────────────────────────────
    import re as _re

    rec_data: list[dict[str, Any]] = []
    for i, rec in enumerate(recommendations):
        ann_sav = max(rec.annual_savings_estimate, 0)

        # Try to parse savings % from Advisor description (e.g. "save ~42%")
        pct_match = _re.search(r"~(\d+)%", rec.short_description or "")
        advisor_sav_rate = int(pct_match.group(1)) / 100 if pct_match else 0.40
        advisor_sav_rate = max(advisor_sav_rate, 0.01)

        implied_paygo_3yr = (ann_sav * 3) / advisor_sav_rate if ann_sav > 0 else 0

        ri_rate = rec_ri_rate[i]
        sp_rate = rec_sp_rate[i]
        ri_3yr_cost = implied_paygo_3yr * (1 - ri_rate)
        sp_3yr_cost = implied_paygo_3yr * (1 - sp_rate)

        cat = _guess_category(rec, df)
        raw = rec.raw or {}
        extended = raw.get("extendedProperties",
                           raw.get("properties", {}).get("extendedProperties", {})) or {}
        sku = extended.get("sku", extended.get("vmSize",
              extended.get("currentSku", rec.short_description[:60])))
        region = extended.get("region", extended.get("location", "Unspecified")) or "Unspecified"

        rec_data.append({
            "index": i,
            "rec": rec,
            "ann_sav": ann_sav,
            "implied_paygo_3yr": implied_paygo_3yr,
            "ri_rate": ri_rate,
            "sp_rate": sp_rate,
            "ri_3yr_cost": ri_3yr_cost,
            "sp_3yr_cost": sp_3yr_cost,
            "ri_3yr_sav": implied_paygo_3yr - ri_3yr_cost,
            "sp_3yr_sav": implied_paygo_3yr - sp_3yr_cost,
            "category": cat,
            "region": region,
            "sku": sku,
            "advisor_sav_rate": advisor_sav_rate,
            "has_retail": has_retail and i in (retail_prices or {}),
        })

    total_ri_3yr_sav = sum(d["ri_3yr_sav"] for d in rec_data)
    total_sp_3yr_sav = sum(d["sp_3yr_sav"] for d in rec_data)

    # ── Invoice PAYG costs (shared lookups) ────────────────────────────────────
    if "MeterCategory" in df.columns:
        _cat_csv: dict[str, float] = {}
        for meter_category, total_cost_val in (
            df.filter(
                pl.col("PricingModel").is_in(
                    ["OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", ""]
                )
                if "PricingModel" in df.columns
                else pl.lit(True)
            )
            .group_by("MeterCategory")
            .agg(pl.sum("Cost"))
            .iter_rows()
        ):
            _cat_csv[str(meter_category or "Unspecified")] = float(total_cost_val or 0.0)
    else:
        _cat_csv = {"All Services": paygo_total}

    _region_csv: dict[str, float] = {}
    if "MeterRegion" in df.columns:
        for row in (
            df.filter(
                pl.col("PricingModel").is_in(
                    ["OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", ""]
                )
                if "PricingModel" in df.columns
                else pl.lit(True)
            )
            .group_by("MeterRegion")
            .agg(pl.sum("Cost"))
            .iter_rows()
        ):
            _region_csv[str(row[0] or "Unspecified")] = float(row[1] or 0.0)

    # ── Build one savings variant (RI or SP) ───────────────────────────────────
    def _build_variant(variant: str) -> dict[str, Any]:
        is_ri = variant == "ri"
        cost_key = "ri_3yr_cost" if is_ri else "sp_3yr_cost"
        sav_key = "ri_3yr_sav" if is_ri else "sp_3yr_sav"
        rate_key = "ri_rate" if is_ri else "sp_rate"
        flat_rate = RI_DISCOUNT_RATE if is_ri else SP_DISCOUNT_RATE
        label = "Reserved Instance" if is_ri else "Savings Plan"
        total_variant_sav = total_ri_3yr_sav if is_ri else total_sp_3yr_sav

        # ─── Savings by Category ──────────────────────────────────
        cat_rec_sav: dict[str, float] = {}
        cat_rec_paygo: dict[str, float] = {}
        for d in rec_data:
            c = d["category"]
            cat_rec_sav[c] = cat_rec_sav.get(c, 0) + d[sav_key]
            cat_rec_paygo[c] = cat_rec_paygo.get(c, 0) + d["implied_paygo_3yr"]

        all_cats = set(list(_cat_csv.keys()) + list(cat_rec_sav.keys()))
        savings_by_category: list[dict[str, Any]] = []
        for cat in sorted(all_cats):
            csv_paygo_3yr = _cat_csv.get(cat, 0) / period_days * 365 * 3
            rec_paygo_3yr = cat_rec_paygo.get(cat, 0)
            rec_sav_3yr = cat_rec_sav.get(cat, 0)

            paygo_3yr = max(csv_paygo_3yr, rec_paygo_3yr)
            if rec_sav_3yr > 0:
                sav_3yr = rec_sav_3yr
                commitment_cost = paygo_3yr - sav_3yr
            else:
                # No recommendations — don't assume flat savings
                sav_3yr = 0.0
                commitment_cost = paygo_3yr

            pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
            pct_total = (sav_3yr / total_variant_sav * 100) if total_variant_sav > 0 else 0.0

            savings_by_category.append({
                "Resource Type": cat,
                "Current 3-Yr Cost": round(paygo_3yr, 2),
                "Commitment 3-Yr Cost": round(commitment_cost, 2),
                "Savings (3-Yr)": round(sav_3yr, 2),
                "Savings %": round(pct, 1),
                "Annual Savings": round(sav_3yr / 3, 2),
                "Monthly Savings": round(sav_3yr / 36, 2),
                "% of Total Savings": round(pct_total, 1),
            })
        savings_by_category.sort(key=lambda x: x["Savings (3-Yr)"], reverse=True)

        # ─── Savings by Region ────────────────────────────────────
        rgn_rec_sav: dict[str, float] = {}
        rgn_rec_paygo: dict[str, float] = {}
        for d in rec_data:
            r = d["region"]
            rgn_rec_sav[r] = rgn_rec_sav.get(r, 0) + d[sav_key]
            rgn_rec_paygo[r] = rgn_rec_paygo.get(r, 0) + d["implied_paygo_3yr"]

        all_regions = set(list(_region_csv.keys()) + list(rgn_rec_sav.keys()))
        savings_by_region: list[dict[str, Any]] = []
        for rgn in sorted(all_regions):
            csv_paygo_3yr = _region_csv.get(rgn, 0) / period_days * 365 * 3
            rec_paygo_3yr = rgn_rec_paygo.get(rgn, 0)
            rec_sav_3yr = rgn_rec_sav.get(rgn, 0)

            paygo_3yr = max(csv_paygo_3yr, rec_paygo_3yr)
            if rec_sav_3yr > 0:
                sav_3yr = rec_sav_3yr
            else:
                sav_3yr = 0.0
            commitment_cost = paygo_3yr - sav_3yr

            pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
            pct_total = (sav_3yr / total_variant_sav * 100) if total_variant_sav > 0 else 0.0

            savings_by_region.append({
                "Region": rgn,
                "Current 3-Yr Cost": round(paygo_3yr, 2),
                "Commitment 3-Yr Cost": round(commitment_cost, 2),
                "Savings (3-Yr)": round(sav_3yr, 2),
                "Savings %": round(pct, 1),
                "Annual Savings": round(sav_3yr / 3, 2),
                "% of Total Savings": round(pct_total, 1),
            })
        savings_by_region.sort(key=lambda x: x["Savings (3-Yr)"], reverse=True)

        # ─── Top Opportunities ────────────────────────────────────
        sorted_recs = sorted(rec_data, key=lambda d: d[sav_key], reverse=True)
        top_opps: list[dict[str, Any]] = []
        for rank, d in enumerate(sorted_recs, start=1):
            top_opps.append({
                "Rank": rank,
                "Resource Type": d["category"],
                "SKU": d["sku"],
                "Region": d["region"],
                "Current 3-Yr Cost": round(d["implied_paygo_3yr"], 2),
                "Commitment 3-Yr Cost": round(d[cost_key], 2),
                "Savings (3-Yr)": round(d[sav_key], 2),
                "Savings %": round(d[rate_key] * 100, 1),
                "Annual Savings": round(d[sav_key] / 3, 2),
                "Description": d["rec"].short_description,
                "Pricing Source": "Retail API" if d["has_retail"] else "Estimated",
            })

        # ─── KPI ──────────────────────────────────────────────────
        variant_cost = three_year_paygo - total_variant_sav
        variant_pct = (total_variant_sav / three_year_paygo * 100) if three_year_paygo > 0 else 0.0

        # Weighted-average discount rate across all recs (by 3-yr PAYG weight)
        total_paygo_weight = sum(d["implied_paygo_3yr"] for d in rec_data)
        if total_paygo_weight > 0:
            weighted_rate = sum(
                d[rate_key] * d["implied_paygo_3yr"] for d in rec_data
            ) / total_paygo_weight
        else:
            weighted_rate = flat_rate

        n_retail = sum(1 for d in rec_data if d["has_retail"])

        kpi: dict[str, Any] = {
            "label": label,
            "total_3yr_savings": round(total_variant_sav, 2),
            "savings_pct": round(variant_pct, 1),
            "total_recommendations": len(recommendations),
            "current_3yr_spend": round(three_year_paygo, 2),
            "commitment_3yr_cost": round(variant_cost, 2),
            "discount_rate": round(weighted_rate, 4),
            "discount_rate_source": "retail" if n_retail == len(rec_data) else (
                "mixed" if n_retail > 0 else "estimated"
            ),
            "resource_categories": len(savings_by_category),
            "annual_savings": round(total_variant_sav / 3, 2) if total_variant_sav else 0,
            "monthly_savings": round(total_variant_sav / 36, 2) if total_variant_sav else 0,
            "retail_prices_used": n_retail,
        }

        return {
            "savings_by_category": savings_by_category,
            "savings_by_region": savings_by_region,
            "top_opportunities": top_opps,
            "kpi": kpi,
        }

    ri_analysis = _build_variant("ri")
    sp_analysis = _build_variant("sp")

    # ── Hybrid variant: SP for compute-eligible, RI for everything else ────────
    def _build_hybrid_variant() -> dict[str, Any]:
        """SP rates for compute-eligible services + RI rates for the rest."""

        def _hkeys(d: dict) -> tuple[str, str, str]:
            if d["category"] in SP_ELIGIBLE_CATEGORIES:
                return "sp_3yr_cost", "sp_3yr_sav", "sp_rate"
            return "ri_3yr_cost", "ri_3yr_sav", "ri_rate"

        total_hybrid_sav = sum(d[_hkeys(d)[1]] for d in rec_data)

        # ── Savings by Category ───────────────────────────────────
        cat_rec_sav: dict[str, float] = {}
        cat_rec_paygo: dict[str, float] = {}
        for d in rec_data:
            _, sk, _ = _hkeys(d)
            c = d["category"]
            cat_rec_sav[c] = cat_rec_sav.get(c, 0) + d[sk]
            cat_rec_paygo[c] = cat_rec_paygo.get(c, 0) + d["implied_paygo_3yr"]

        all_cats = set(list(_cat_csv.keys()) + list(cat_rec_sav.keys()))
        savings_by_category: list[dict[str, Any]] = []
        for cat in sorted(all_cats):
            csv_paygo_3yr = _cat_csv.get(cat, 0) / period_days * 365 * 3
            rec_paygo_3yr = cat_rec_paygo.get(cat, 0)
            rec_sav_3yr = cat_rec_sav.get(cat, 0)
            paygo_3yr = max(csv_paygo_3yr, rec_paygo_3yr)
            if rec_sav_3yr > 0:
                sav_3yr = rec_sav_3yr
            else:
                # No recommendations — don't assume flat savings
                sav_3yr = 0.0
            commitment_cost = paygo_3yr - sav_3yr
            pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
            pct_total = (sav_3yr / total_hybrid_sav * 100) if total_hybrid_sav > 0 else 0.0
            commitment_type = "SP" if cat in SP_ELIGIBLE_CATEGORIES else "RI"
            savings_by_category.append({
                "Resource Type": cat,
                "Commitment": commitment_type,
                "Current 3-Yr Cost": round(paygo_3yr, 2),
                "Commitment 3-Yr Cost": round(commitment_cost, 2),
                "Savings (3-Yr)": round(sav_3yr, 2),
                "Savings %": round(pct, 1),
                "Annual Savings": round(sav_3yr / 3, 2),
                "Monthly Savings": round(sav_3yr / 36, 2),
                "% of Total Savings": round(pct_total, 1),
            })
        savings_by_category.sort(key=lambda x: x["Savings (3-Yr)"], reverse=True)

        # ── Savings by Region ─────────────────────────────────────
        rgn_rec_sav: dict[str, float] = {}
        rgn_rec_paygo: dict[str, float] = {}
        for d in rec_data:
            _, sk, _ = _hkeys(d)
            r = d["region"]
            rgn_rec_sav[r] = rgn_rec_sav.get(r, 0) + d[sk]
            rgn_rec_paygo[r] = rgn_rec_paygo.get(r, 0) + d["implied_paygo_3yr"]

        all_regions = set(list(_region_csv.keys()) + list(rgn_rec_sav.keys()))
        savings_by_region: list[dict[str, Any]] = []
        for rgn in sorted(all_regions):
            csv_paygo_3yr = _region_csv.get(rgn, 0) / period_days * 365 * 3
            rec_paygo_3yr = rgn_rec_paygo.get(rgn, 0)
            rec_sav_3yr = rgn_rec_sav.get(rgn, 0)
            paygo_3yr = max(csv_paygo_3yr, rec_paygo_3yr)
            if rec_sav_3yr > 0:
                sav_3yr = rec_sav_3yr
            else:
                sav_3yr = 0.0
            commitment_cost = paygo_3yr - sav_3yr
            pct = (sav_3yr / paygo_3yr * 100) if paygo_3yr > 0 else 0.0
            pct_total = (sav_3yr / total_hybrid_sav * 100) if total_hybrid_sav > 0 else 0.0
            savings_by_region.append({
                "Region": rgn,
                "Current 3-Yr Cost": round(paygo_3yr, 2),
                "Commitment 3-Yr Cost": round(commitment_cost, 2),
                "Savings (3-Yr)": round(sav_3yr, 2),
                "Savings %": round(pct, 1),
                "Annual Savings": round(sav_3yr / 3, 2),
                "% of Total Savings": round(pct_total, 1),
            })
        savings_by_region.sort(key=lambda x: x["Savings (3-Yr)"], reverse=True)

        # ── Top Opportunities ─────────────────────────────────────
        sorted_recs = sorted(rec_data, key=lambda d: d[_hkeys(d)[1]], reverse=True)
        top_opps: list[dict[str, Any]] = []
        for rank, d in enumerate(sorted_recs, start=1):
            ck, sk, rk = _hkeys(d)
            ctype = "SP" if d["category"] in SP_ELIGIBLE_CATEGORIES else "RI"
            top_opps.append({
                "Rank": rank,
                "Resource Type": d["category"],
                "Commitment": ctype,
                "SKU": d["sku"],
                "Region": d["region"],
                "Current 3-Yr Cost": round(d["implied_paygo_3yr"], 2),
                "Commitment 3-Yr Cost": round(d[ck], 2),
                "Savings (3-Yr)": round(d[sk], 2),
                "Savings %": round(d[rk] * 100, 1),
                "Annual Savings": round(d[sk] / 3, 2),
                "Description": d["rec"].short_description,
                "Pricing Source": "Retail API" if d["has_retail"] else "Estimated",
            })

        # ── KPI ───────────────────────────────────────────────────
        hybrid_cost = three_year_paygo - total_hybrid_sav
        hybrid_pct = (total_hybrid_sav / three_year_paygo * 100) if three_year_paygo > 0 else 0.0
        total_paygo_wt = sum(d["implied_paygo_3yr"] for d in rec_data)
        if total_paygo_wt > 0:
            weighted_rate = sum(
                d[_hkeys(d)[2]] * d["implied_paygo_3yr"] for d in rec_data
            ) / total_paygo_wt
        else:
            weighted_rate = RI_DISCOUNT_RATE
        n_retail = sum(1 for d in rec_data if d["has_retail"])
        n_sp = sum(1 for d in rec_data if d["category"] in SP_ELIGIBLE_CATEGORIES)

        return {
            "savings_by_category": savings_by_category,
            "savings_by_region": savings_by_region,
            "top_opportunities": top_opps,
            "kpi": {
                "label": "Hybrid (SP + RI)",
                "total_3yr_savings": round(total_hybrid_sav, 2),
                "savings_pct": round(hybrid_pct, 1),
                "total_recommendations": len(recommendations),
                "current_3yr_spend": round(three_year_paygo, 2),
                "commitment_3yr_cost": round(hybrid_cost, 2),
                "discount_rate": round(weighted_rate, 4),
                "discount_rate_source": "retail" if n_retail == len(rec_data) else (
                    "mixed" if n_retail > 0 else "estimated"
                ),
                "resource_categories": len(savings_by_category),
                "annual_savings": round(total_hybrid_sav / 3, 2) if total_hybrid_sav else 0,
                "monthly_savings": round(total_hybrid_sav / 36, 2) if total_hybrid_sav else 0,
                "retail_prices_used": n_retail,
                "sp_eligible_recs": n_sp,
                "ri_only_recs": len(rec_data) - n_sp,
            },
        }

    hybrid_analysis = _build_hybrid_variant()

    # ── Logging ────────────────────────────────────────────────────────────────
    three_year_savings = total_annual_savings * 3
    savings_pct = (three_year_savings / three_year_paygo * 100) if three_year_paygo > 0 else 0.0

    elapsed = time.perf_counter() - t0
    total_hybrid_sav_log = hybrid_analysis["kpi"]["total_3yr_savings"]
    logger.info(
        "Savings analysis complete in %.2fs: ri_3yr_sav=%.2f, sp_3yr_sav=%.2f, "
        "hybrid_3yr_sav=%.2f, categories=%d, retail_hits=%d/%d",
        elapsed, total_ri_3yr_sav, total_sp_3yr_sav, total_hybrid_sav_log,
        len(ri_analysis["savings_by_category"]),
        sum(1 for d in rec_data if d["has_retail"]), len(rec_data),
    )

    return {
        "pricing_model_summary": pm_summary,
        "paygo_total": paygo_total,
        "period_days": period_days,
        "annual_paygo_run_rate": round(annual_paygo, 2),
        "three_year_paygo": round(three_year_paygo, 2),
        "ri_analysis": ri_analysis,
        "sp_analysis": sp_analysis,
        "hybrid_analysis": hybrid_analysis,
        "has_retail_prices": has_retail,
        # Legacy keys for backward compat — default to RI data
        "savings_by_category": ri_analysis["savings_by_category"],
        "savings_by_region": ri_analysis["savings_by_region"],
        "top_opportunities": ri_analysis["top_opportunities"],
        "kpi": {
            **ri_analysis["kpi"],
            "ri_3yr_cost": ri_analysis["kpi"]["commitment_3yr_cost"],
            "ri_3yr_savings": ri_analysis["kpi"]["total_3yr_savings"],
            "sp_3yr_cost": sp_analysis["kpi"]["commitment_3yr_cost"],
            "sp_3yr_savings": sp_analysis["kpi"]["total_3yr_savings"],
            "ri_sp_3yr_cost": ri_analysis["kpi"]["commitment_3yr_cost"],
            "ri_discount_rate": RI_DISCOUNT_RATE,
            "sp_discount_rate": SP_DISCOUNT_RATE,
        },
    }


def _empty_savings() -> dict[str, Any]:
    _empty_variant: dict[str, Any] = {
        "savings_by_category": [],
        "savings_by_region": [],
        "top_opportunities": [],
        "kpi": {
            "label": "", "total_3yr_savings": 0, "savings_pct": 0,
            "total_recommendations": 0, "current_3yr_spend": 0,
            "commitment_3yr_cost": 0, "discount_rate": 0,
            "resource_categories": 0, "annual_savings": 0, "monthly_savings": 0,
            "retail_prices_used": 0,
        },
    }
    return {
        "pricing_model_summary": [],
        "paygo_total": 0,
        "period_days": 0,
        "annual_paygo_run_rate": 0,
        "three_year_paygo": 0,
        "ri_analysis": _empty_variant,
        "sp_analysis": _empty_variant,
        "hybrid_analysis": _empty_variant,
        "has_retail_prices": False,
        # Legacy keys
        "savings_by_category": [],
        "savings_by_region": [],
        "top_opportunities": [],
        "kpi": {
            "total_3yr_savings": 0, "savings_pct": 0, "total_recommendations": 0,
            "current_3yr_spend": 0, "ri_sp_3yr_cost": 0, "resource_categories": 0,
            "annual_savings": 0, "monthly_savings": 0,
            "ri_3yr_cost": 0, "ri_3yr_savings": 0,
            "sp_3yr_cost": 0, "sp_3yr_savings": 0,
            "ri_discount_rate": 0, "sp_discount_rate": 0,
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
    ri_discount_rate: float = 0.40,
    sp_discount_rate: float = 0.30,
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

        ri_rgn_3yr = paygo_3yr * (1 - ri_discount_rate)
        sp_rgn_3yr = paygo_3yr * (1 - sp_discount_rate)

        rows.append({
            "Region": rgn,
            "Current 3-Yr Cost": round(paygo_3yr, 2),
            "RI 3-Yr Cost": round(ri_rgn_3yr, 2),
            "RI Savings (3-Yr)": round(paygo_3yr - ri_rgn_3yr, 2),
            "SP 3-Yr Cost": round(sp_rgn_3yr, 2),
            "SP Savings (3-Yr)": round(paygo_3yr - sp_rgn_3yr, 2),
            "Advisor Savings (3-Yr)": round(sav_3yr, 2),
            "Savings %": round(pct, 1),
            "Annual Savings": round(ann_sav, 2),
            "% of Total Savings": round(pct_of_total, 1),
        })

    rows.sort(key=lambda x: x["Advisor Savings (3-Yr)"], reverse=True)
    return rows


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
    elif "PricingModel" in selected:
        tables["cost_by_charge_type"] = (
            typed.group_by("PricingModel")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .rename({"PricingModel": "ChargeType"})
            .collect(streaming=True)
        )
    else:
        tables["cost_by_charge_type"] = typed.select(
            [
                pl.lit("Unspecified").alias("ChargeType"),
                pl.sum("Cost").alias("TotalCost"),
            ]
        ).collect(streaming=True)

    if "Date" in selected:
        tables["cost_by_day"] = (
            typed.with_columns(pl.col("Date").dt.date().alias("Day"))
            .filter(pl.col("Day").is_not_null())
            .group_by("Day")
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("Day")
            .rename({"Day": "Date"})
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
