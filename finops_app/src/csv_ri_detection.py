"""Detect Reserved-Instance (RI) and Savings-Plan (SP) opportunities from CSV billing data.

Instead of relying on Azure Advisor, this module inspects invoice/usage rows to
find resources that are paid PAYG and are eligible for commitment discounts.
It groups them by SKU + region, computes the annualised PAYG spend, then
queries the Azure Retail Prices API to obtain real RI / SP pricing.

The result is a list of `AzureRecommendation` objects that can be fed directly
into `build_savings_analysis()` just like Advisor recommendations.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import polars as pl

from .file_loader import existing_columns, load_invoice_file, safe_datetime_expr
from .logging_config import get_logger
from .models import AzureRecommendation
from .retail_pricing import lookup_sku_prices

logger = get_logger(__name__)

# ── RI-eligible meter categories and how they map to Retail API service names ──
_CATEGORY_TO_SERVICE: dict[str, str] = {
    "Virtual Machines": "Virtual Machines",
    "SQL Database": "SQL Database",
    "Azure Cosmos DB": "Azure Cosmos DB",
    "Redis Cache": "Azure Cache for Redis",
    "Azure Data Explorer": "Azure Data Explorer",
    "Azure App Service": "Azure App Service",
    "Azure Database for MySQL": "Azure Database for MySQL",
    "Azure Database for PostgreSQL": "Azure Database for PostgreSQL",
    "Azure Database for MariaDB": "Azure Database for MariaDB",
}

# Meter regions that appear in Azure CSVs mapped to ARM region names (for the
# Retail Prices API).  The CSV uses display names like "US Central"; the API
# expects ARM names like "centralus".
_CSV_REGION_TO_ARM: dict[str, str] = {
    "us central": "centralus",
    "us east": "eastus",
    "us east 2": "eastus2",
    "us west": "westus",
    "us west 2": "westus2",
    "us west 3": "westus3",
    "us north central": "northcentralus",
    "us south central": "southcentralus",
    "us west central": "westcentralus",
    "eu west": "westeurope",
    "eu north": "northeurope",
    "uk south": "uksouth",
    "uk west": "ukwest",
    "ca central": "canadacentral",
    "ca east": "canadaeast",
    "fr central": "francecentral",
    "de west central": "germanywestcentral",
    "no east": "norwayeast",
    "se central": "swedencentral",
    "ch north": "switzerlandnorth",
    "au east": "australiaeast",
    "au southeast": "australiasoutheast",
    "ap east": "eastasia",
    "ap southeast": "southeastasia",
    "ja east": "japaneast",
    "ja west": "japanwest",
    "kr central": "koreacentral",
    "kr south": "koreasouth",
    "in central": "centralindia",
    "in south": "southindia",
    "in west": "westindia",
    "br south": "brazilsouth",
    "za north": "southafricanorth",
    "ae north": "uaenorth",
}


def _csv_region_to_arm(meter_region: str | None) -> str:
    """Convert a CSV meterRegion display name to an ARM region identifier."""
    if not meter_region:
        return ""
    key = meter_region.strip().lower()
    if key in _CSV_REGION_TO_ARM:
        return _CSV_REGION_TO_ARM[key]
    # Fallback: try removing spaces and lower-casing (e.g. 'EastUS' → 'eastus')
    collapsed = re.sub(r"\s+", "", key)
    return collapsed


# ---------------------------------------------------------------------------
# VM SKU extraction from additionalInfo JSON
# ---------------------------------------------------------------------------

def _extract_vm_sku_from_additional_info(info_str: str | None) -> str:
    """Parse the additionalInfo JSON to extract VM SKU (ServiceType / VMSize)."""
    if not info_str:
        return ""
    try:
        info = json.loads(info_str)
        return info.get("ServiceType") or info.get("VMSize") or ""
    except (json.JSONDecodeError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# SKU extraction for SQL Database
# ---------------------------------------------------------------------------

def _extract_sql_sku(product: str, meter_sub: str, meter_name: str) -> str:
    """Derive a Retail-API-friendly SKU name for SQL Database from CSV columns.

    Examples:
      product = "SQL Database Single/Elastic Pool Hyperscale - Premium Series Compute - 1 vCore - US Central"
      → "vCore"  (vCore-based pricing, looked up by serviceName + armRegionName)

      product = "SQL Database Single Standard - S1"
      → "S1" (DTU-based)
    """
    # DTU tiers: S0, S1, S2, etc.
    dtu_match = re.search(r"\b(S\d+|P\d+|B)\b", product)
    if dtu_match:
        return dtu_match.group(1)
    # vCore-based: return the tier description for grouping
    if "vcore" in meter_name.lower():
        # Extract tier info from meterSubCategory
        # e.g. "SQL Database Single/Elastic Pool Hyperscale - Premium Series Compute"
        return meter_sub.strip()
    return meter_sub.strip() or product.strip()


# ---------------------------------------------------------------------------
# SKU extraction for App Service
# ---------------------------------------------------------------------------

def _extract_app_service_sku(meter_name: str, meter_sub: str) -> str:
    """Extract App Service plan SKU from meter columns.

    meter_name examples: "P3 v3 App", "P1 v2 App", "S1 App", "B1 App"
    """
    # Match patterns like P3v3, P1v2, S1, B1, F1
    sku_match = re.search(r"\b(P\d+\s*v?\d*|S\d+|B\d+|F\d+|P0v3)\b", meter_name, re.IGNORECASE)
    if sku_match:
        raw = sku_match.group(1).replace(" ", " ")
        return raw
    return meter_name.strip()


# ---------------------------------------------------------------------------
# SKU extraction for Cosmos DB
# ---------------------------------------------------------------------------

def _extract_cosmos_sku(meter_name: str, meter_sub: str, product: str) -> str:
    """Extract Cosmos DB SKU identifier."""
    # Group by provisioned RU vs serverless
    if "serverless" in meter_sub.lower() or "serverless" in product.lower():
        return "Serverless RUs"
    if "ru" in meter_name.lower():
        return "Provisioned RUs"
    return meter_name.strip()


# ---------------------------------------------------------------------------
# SKU extraction for Redis Cache
# ---------------------------------------------------------------------------

def _extract_redis_sku(meter_name: str, product: str) -> str:
    """Extract Redis Cache tier (P1, C0, etc.)."""
    sku_match = re.search(r"\b(P\d+|C\d+)\b", meter_name)
    if sku_match:
        return sku_match.group(1)
    return meter_name.strip()


# ---------------------------------------------------------------------------
# Main: detect RI opportunities from billing CSV
# ---------------------------------------------------------------------------

def detect_ri_opportunities(
    file_path: str,
    *,
    min_annual_cost: float | None = None,
    lookup_prices: bool = True,
    max_price_workers: int = 3,
) -> list[AzureRecommendation]:
    """Scan an Azure billing CSV and return synthetic RI/SP recommendations.

    Args:
        file_path:  Path to the billing CSV / Excel.
        min_annual_cost:  Minimum annualised PAYG cost to include a SKU.
                          Defaults to $100 (env: CSV_RI_MIN_ANNUAL_COST).
        lookup_prices:  Whether to call the Retail Prices API for each
                        detected opportunity.
        max_price_workers:  Max parallel threads for retail price lookups.

    Returns:
        A list of `AzureRecommendation` objects, one per detected SKU+region,
        with `annual_savings_estimate` populated from either real retail
        pricing or flat discount assumptions.  The `raw` dict on each rec
        carries extra metadata for downstream use.
    """
    if min_annual_cost is None:
        min_annual_cost = float(os.getenv("CSV_RI_MIN_ANNUAL_COST", "100"))

    logger.info("detect_ri_opportunities: file=%s, min_annual=$%.0f, lookup=%s",
                file_path, min_annual_cost, lookup_prices)
    t0 = time.perf_counter()

    scan = load_invoice_file(file_path)
    existing = existing_columns(scan)

    needed = ["Cost", "MeterCategory"]
    for c in ("MeterSubcategory", "MeterRegion", "Product", "MeterName",
              "ConsumedService", "Date", "PricingModel"):
        if c in existing:
            needed.append(c)
    # additionalInfo for VM SKU extraction
    has_additional = "AdditionalInfo" in existing
    if has_additional:
        needed.append("AdditionalInfo")

    if "Cost" not in existing or "MeterCategory" not in existing:
        logger.warning("CSV missing Cost or MeterCategory — cannot detect RI opportunities")
        return []

    lf = scan.select(needed).with_columns(
        pl.col("Cost").cast(pl.Float64, strict=False),
    )
    if "Date" in needed:
        lf = lf.with_columns(safe_datetime_expr("Date", scan))

    df = lf.collect(streaming=True)

    # ── Determine invoice period ────────────────────────────────────────────
    if "Date" in df.columns and df["Date"].drop_nulls().len() > 0:
        dates = df["Date"].drop_nulls()
        period_days = max((dates.max() - dates.min()).days, 1)  # type: ignore[operator]
    else:
        period_days = 30

    # ── Filter to RI-eligible categories and PAYG-only rows ────────────────
    ri_categories = list(_CATEGORY_TO_SERVICE.keys())
    mask = pl.col("MeterCategory").is_in(ri_categories)

    # If PricingModel exists, only take OnDemand / PAYG rows (exclude already-reserved)
    if "PricingModel" in df.columns:
        payg_models = ["OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", ""]
        mask = mask & pl.col("PricingModel").is_in(payg_models)

    ri_df = df.filter(mask)
    if ri_df.is_empty():
        logger.info("No RI-eligible PAYG rows found in CSV")
        return []

    logger.info("RI-eligible PAYG rows: %d / %d total", len(ri_df), len(df))

    # ── Extract SKU for each row ────────────────────────────────────────────
    sku_list: list[str] = []
    for row in ri_df.iter_rows(named=True):
        cat = row.get("MeterCategory", "")
        meter_name = str(row.get("MeterName", ""))
        meter_sub = str(row.get("MeterSubcategory", ""))
        product = str(row.get("Product", ""))
        additional = str(row.get("AdditionalInfo", "")) if has_additional else ""

        if cat == "Virtual Machines":
            sku = _extract_vm_sku_from_additional_info(additional) or meter_sub
        elif cat == "SQL Database":
            sku = _extract_sql_sku(product, meter_sub, meter_name)
        elif cat == "Azure App Service":
            sku = _extract_app_service_sku(meter_name, meter_sub)
        elif cat == "Azure Cosmos DB":
            sku = _extract_cosmos_sku(meter_name, meter_sub, product)
        elif cat == "Redis Cache":
            sku = _extract_redis_sku(meter_name, product)
        else:
            sku = meter_sub or meter_name or product
        sku_list.append(sku or "Unknown")

    ri_df = ri_df.with_columns(pl.Series("_sku", sku_list))

    # Also normalise the region column
    region_list = [_csv_region_to_arm(str(r)) for r in ri_df["MeterRegion"].to_list()] if "MeterRegion" in ri_df.columns else [""] * len(ri_df)
    ri_df = ri_df.with_columns(pl.Series("_arm_region", region_list))

    # ── Group by (category, sku, region) ────────────────────────────────────
    grouped = (
        ri_df.group_by(["MeterCategory", "_sku", "_arm_region"])
        .agg([
            pl.sum("Cost").alias("period_cost"),
            pl.len().alias("row_count"),
        ])
        .sort("period_cost", descending=True)
    )

    # Annualise and filter by minimum
    annualisation_factor = 365 / period_days
    opportunities: list[dict[str, Any]] = []
    for row in grouped.iter_rows(named=True):
        annual_cost = float(row["period_cost"]) * annualisation_factor
        if annual_cost < min_annual_cost:
            continue
        opportunities.append({
            "category": row["MeterCategory"],
            "sku": row["_sku"],
            "arm_region": row["_arm_region"],
            "annual_payg_cost": round(annual_cost, 2),
            "period_cost": round(float(row["period_cost"]), 2),
            "row_count": int(row["row_count"]),
            "service_name": _CATEGORY_TO_SERVICE.get(row["MeterCategory"], row["MeterCategory"]),
        })

    if not opportunities:
        logger.info("No RI-eligible SKUs exceed minimum annual cost threshold ($%.0f)", min_annual_cost)
        return []

    logger.info("Found %d RI-eligible SKU groups above $%.0f/year", len(opportunities), min_annual_cost)

    # ── Optional: look up real prices from Azure Retail API ────────────────
    RI_DISCOUNT_RATE = float(os.getenv("RI_DISCOUNT_RATE", "0.40"))
    SP_DISCOUNT_RATE = float(os.getenv("SP_DISCOUNT_RATE", "0.30"))

    if lookup_prices:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _price_lookup(opp: dict) -> dict:
            """Enhance an opportunity dict with retail pricing."""
            svc = opp["service_name"]
            sku = opp["sku"]
            region = opp["arm_region"]
            if not (svc and sku and region):
                return opp
            try:
                prices = lookup_sku_prices(svc, sku, region)
                if prices and prices.get("payg_retail_price") is not None:
                    opp["retail_prices"] = prices
                    ri_pct = prices.get("ri_3yr_savings_pct")
                    sp_pct = prices.get("sp_3yr_savings_pct")
                    # Use retail-derived savings % if available
                    if ri_pct and ri_pct > 0:
                        opp["ri_savings_pct"] = ri_pct / 100
                    if sp_pct and sp_pct > 0:
                        opp["sp_savings_pct"] = sp_pct / 100
            except Exception as ex:
                logger.debug("Retail price lookup failed for %s/%s/%s: %s", svc, sku, region, ex)
            return opp

        logger.info("Looking up retail prices for %d CSV-detected SKUs…", len(opportunities))
        t_price = time.perf_counter()
        workers = min(max_price_workers, len(opportunities))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_price_lookup, opp): opp for opp in opportunities}
            for future in as_completed(futures):
                future.result()  # results mutate opp in-place
        price_hits = sum(1 for o in opportunities if "retail_prices" in o)
        logger.info("Retail price lookup done in %.2fs: %d/%d successful",
                     time.perf_counter() - t_price, price_hits, len(opportunities))

    # ── Build AzureRecommendation objects ──────────────────────────────────
    recs: list[AzureRecommendation] = []
    for opp in opportunities:
        annual = opp["annual_payg_cost"]
        ri_rate = opp.get("ri_savings_pct", RI_DISCOUNT_RATE)
        sp_rate = opp.get("sp_savings_pct", SP_DISCOUNT_RATE)
        # Use the RI savings as default estimate (most common commitment type)
        est_savings = annual * ri_rate

        has_retail = "retail_prices" in opp
        desc = (
            f"CSV-detected: {opp['category']} {opp['sku']} in {opp['arm_region']} — "
            f"~{ri_rate * 100:.0f}% RI savings"
            + (" (Retail API)" if has_retail else " (estimated)")
        )

        recs.append(AzureRecommendation(
            resource_id=f"/csv-detected/{opp['category']}/{opp['sku']}/{opp['arm_region']}",
            category="Cost",
            impact="High" if annual > 5000 else "Medium" if annual > 1000 else "Low",
            short_description=desc,
            annual_savings_estimate=round(est_savings, 2),
            raw={
                "source": "csv_detection",
                "extendedProperties": {
                    "region": opp["arm_region"],
                    "sku": opp["sku"],
                    "annualPaygCost": annual,
                    "riSavingsPct": ri_rate,
                    "spSavingsPct": sp_rate,
                    "periodCost": opp["period_cost"],
                    "periodDays": period_days,
                    "rowCount": opp["row_count"],
                },
                "retailPrices": opp.get("retail_prices"),
            },
        ))

    elapsed = time.perf_counter() - t0
    total_annual = sum(o["annual_payg_cost"] for o in opportunities)
    logger.info(
        "detect_ri_opportunities complete in %.2fs: %d opportunities, "
        "$%.0f annualised PAYG eligible for commitment discounts",
        elapsed, len(recs), total_annual,
    )
    return recs
