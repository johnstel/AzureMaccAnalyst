"""Azure Retail Pricing API client.

The Azure Retail Prices API (https://prices.azure.com/api/retail/prices)
is **public** — no authentication required.  It returns pay-as-you-go,
Reserved Instance (1-yr / 3-yr), and Savings Plan (1-yr / 3-yr) prices
for any Azure SKU/region combination.

This module provides helpers to:
1.  Look up retail PAYG, RI-3yr, and SP-3yr prices for a given
    (service_name, sku, region) tuple.
2.  Batch-resolve prices for a list of Advisor recommendations so that
    the savings analysis can show *real* RI/SP costs instead of flat
    discount-rate assumptions.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .logging_config import get_logger

logger = get_logger(__name__)

_RETAIL_API = "https://prices.azure.com/api/retail/prices"
_PAGE_SIZE = 100  # max results per page
_MAX_PAGES = 5  # safety cap per query
_REQUEST_TIMEOUT = int(os.getenv("RETAIL_PRICING_TIMEOUT", "15"))

# ── Mapping from Advisor resource-provider segments to Retail API serviceName ─
_PROVIDER_TO_SERVICE: dict[str, str] = {
    "microsoft.compute": "Virtual Machines",
    "microsoft.sql": "SQL Database",
    "microsoft.dbformysql": "Azure Database for MySQL",
    "microsoft.dbforpostgresql": "Azure Database for PostgreSQL",
    "microsoft.storage": "Storage",
    "microsoft.web": "Azure App Service",
    "microsoft.containerservice": "Azure Kubernetes Service",
    "microsoft.cache": "Azure Cache for Redis",
    "microsoft.cosmosdb": "Azure Cosmos DB",
    "microsoft.documentdb": "Azure Cosmos DB",
    "microsoft.network": "Virtual Network",
    "microsoft.apimanagement": "API Management",
}

# Azure region display name → ARM location mapping
_REGION_DISPLAY: dict[str, str] = {
    "eastus": "US East",
    "eastus2": "US East 2",
    "westus": "US West",
    "westus2": "US West 2",
    "westus3": "US West 3",
    "centralus": "US Central",
    "northcentralus": "US North Central",
    "southcentralus": "US South Central",
    "westcentralus": "US West Central",
    "canadacentral": "CA Central",
    "canadaeast": "CA East",
    "uksouth": "UK South",
    "ukwest": "UK West",
    "northeurope": "EU North",
    "westeurope": "EU West",
    "francecentral": "FR Central",
    "germanywestcentral": "DE West Central",
    "norwayeast": "NO East",
    "swedencentral": "SE Central",
    "switzerlandnorth": "CH North",
    "australiaeast": "AU East",
    "australiasoutheast": "AU Southeast",
    "eastasia": "AP East",
    "southeastasia": "AP Southeast",
    "japaneast": "JA East",
    "japanwest": "JA West",
    "koreacentral": "KR Central",
    "koreasouth": "KR South",
    "centralindia": "IN Central",
    "southindia": "IN South",
    "westindia": "IN West",
    "brazilsouth": "BR South",
    "southafricanorth": "ZA North",
    "uaenorth": "AE North",
}


def _arm_region_to_display(arm_region: str) -> str:
    """Convert ARM location name (e.g. 'eastus') to Retail API display name."""
    return _REGION_DISPLAY.get(arm_region.lower().replace(" ", ""), arm_region)


def _build_filter(
    service_name: str | None = None,
    sku_name: str | None = None,
    arm_region: str | None = None,
    price_type: str | None = None,
    reservation_term: str | None = None,
) -> str:
    """Build an OData $filter string for the Retail Prices API."""
    parts: list[str] = []
    if service_name:
        parts.append(f"serviceName eq '{service_name}'")
    if sku_name:
        parts.append(f"skuName eq '{sku_name}'")
    if arm_region:
        display = _arm_region_to_display(arm_region)
        parts.append(f"armRegionName eq '{arm_region}'")
    if price_type:
        parts.append(f"priceType eq '{price_type}'")
    if reservation_term:
        parts.append(f"reservationTerm eq '{reservation_term}'")
    return " and ".join(parts)


def _query_retail_prices(odata_filter: str) -> list[dict[str, Any]]:
    """Execute a paginated query against the Retail Prices API."""
    results: list[dict[str, Any]] = []
    url = _RETAIL_API
    params: dict[str, str] = {
        "$filter": odata_filter,
    }

    for page in range(_MAX_PAGES):
        try:
            resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as ex:
            logger.warning("Retail pricing query failed (page %d): %s — filter: %s", page, ex, odata_filter)
            break

        items = data.get("Items", [])
        results.extend(items)
        next_page = data.get("NextPageLink")
        if not next_page or not items:
            break
        # NextPageLink is a full URL — use it directly
        url = next_page
        params = {}

    return results


def _sku_name_variations(sku_name: str) -> list[str]:
    """Generate alternative SKU name spellings for the Retail Prices API.

    The API is inconsistent — e.g. v3 VMs use ``D2s v3`` (spaces) while
    v5 VMs use ``Standard_D4s_v5`` (underscores + prefix).  This helper
    returns a list of candidate names to try *after* the original.
    """
    alts: list[str] = []
    # 1.  Standard_Xxx_v5 → Xxx v5  (strip prefix, underscores → spaces)
    if sku_name.startswith("Standard_"):
        alts.append(sku_name[len("Standard_"):].replace("_", " "))
    # 2.  Xxx v5 → Standard_Xxx_v5  (add prefix, spaces → underscores)
    if not sku_name.startswith("Standard_") and " " in sku_name:
        alts.append("Standard_" + sku_name.replace(" ", "_"))
    # 3.  Underscores ↔ spaces
    if "_" in sku_name:
        alt = sku_name.replace("_", " ")
        if alt not in alts and alt != sku_name:
            alts.append(alt)
    elif " " in sku_name:
        alt = sku_name.replace(" ", "_")
        if alt not in alts and alt != sku_name:
            alts.append(alt)
    return alts


def lookup_sku_prices(
    service_name: str,
    sku_name: str,
    arm_region: str,
) -> dict[str, float | None]:
    """Look up PAYG, RI-3yr, and SP-3yr hourly/unit prices for a SKU.

    Returns a dict with keys:
        payg_unit_price, ri_3yr_unit_price, sp_3yr_unit_price,
        payg_retail_price, ri_3yr_retail_price, sp_3yr_retail_price,
        ri_3yr_savings_pct, sp_3yr_savings_pct
    Any value may be None if price data is unavailable.
    """
    result: dict[str, float | None] = {
        "payg_unit_price": None,
        "ri_3yr_unit_price": None,
        "sp_3yr_unit_price": None,
        "payg_retail_price": None,
        "ri_3yr_retail_price": None,
        "sp_3yr_retail_price": None,
        "ri_3yr_savings_pct": None,
        "sp_3yr_savings_pct": None,
    }

    # --- Resolve effective SKU name (try alternatives if needed) ---
    effective_sku = sku_name
    payg_filter = _build_filter(
        service_name=service_name,
        sku_name=sku_name,
        arm_region=arm_region,
        price_type="Consumption",
    )
    payg_items = _query_retail_prices(payg_filter)

    if not payg_items:
        # Try alternative SKU name spellings (the Retail API is inconsistent)
        for alt_sku in _sku_name_variations(sku_name):
            alt_filter = _build_filter(
                service_name=service_name,
                sku_name=alt_sku,
                arm_region=arm_region,
                price_type="Consumption",
            )
            payg_items = _query_retail_prices(alt_filter)
            if payg_items:
                effective_sku = alt_sku
                logger.debug("SKU name retry succeeded: '%s' → '%s'", sku_name, alt_sku)
                break

    # Prefer non-spot, non-low-priority, primary meter
    payg_price = _pick_best_price(payg_items, prefer_type="Consumption")
    if payg_price is not None:
        result["payg_unit_price"] = payg_price.get("unitPrice")
        result["payg_retail_price"] = payg_price.get("retailPrice")

    # --- RI 3-Year price ---
    ri_filter = _build_filter(
        service_name=service_name,
        sku_name=effective_sku,
        arm_region=arm_region,
        price_type="Reservation",
        reservation_term="3 Years",
    )
    ri_items = _query_retail_prices(ri_filter)
    ri_price = _pick_best_price(ri_items, prefer_type="Reservation")
    if ri_price is not None:
        result["ri_3yr_unit_price"] = ri_price.get("unitPrice")
        result["ri_3yr_retail_price"] = ri_price.get("retailPrice")

    # --- SP 3-Year price ---
    sp_filter = _build_filter(
        service_name=service_name,
        sku_name=effective_sku,
        arm_region=arm_region,
        price_type="SavingsPlan",
        reservation_term="3 Years",
    )
    sp_items = _query_retail_prices(sp_filter)
    sp_price = _pick_best_price(sp_items, prefer_type="SavingsPlan")
    if sp_price is not None:
        result["sp_3yr_unit_price"] = sp_price.get("unitPrice")
        result["sp_3yr_retail_price"] = sp_price.get("retailPrice")

    # --- Compute savings percentages ---
    payg_retail = result.get("payg_retail_price")
    if payg_retail and payg_retail > 0:
        ri_retail = result.get("ri_3yr_retail_price")
        sp_retail = result.get("sp_3yr_retail_price")
        # Azure Retail Prices API returns Reservation / SavingsPlan prices
        # as the *total cost for the entire term* (e.g. 3 years), NOT as a
        # per-hour rate.  Normalise to hourly for comparison with PAYG.
        HOURS_3YR = 8760 * 3  # 26,280 hours in 3 years
        if ri_retail is not None:
            ri_hourly = ri_retail / HOURS_3YR
            result["ri_3yr_savings_pct"] = round((1 - ri_hourly / payg_retail) * 100, 1)
        if sp_retail is not None:
            sp_hourly = sp_retail / HOURS_3YR
            result["sp_3yr_savings_pct"] = round((1 - sp_hourly / payg_retail) * 100, 1)

    return result


def _pick_best_price(items: list[dict], prefer_type: str) -> dict | None:
    """Select the best price item from a list of Retail API results.

    Prefers items that:
    - Match the desired priceType
    - Are not Spot or Low Priority
    - Have a non-zero retail price
    """
    candidates = []
    for item in items:
        sku_lower = (item.get("skuName") or "").lower()
        # Skip spot, low-priority, DevTestPricing
        if "spot" in sku_lower or "low priority" in sku_lower:
            continue
        if item.get("priceType", "").lower() != prefer_type.lower():
            continue
        if (item.get("retailPrice") or 0) <= 0:
            continue
        candidates.append(item)

    if not candidates:
        return items[0] if items else None

    # Prefer items with 'unitOfMeasure' containing "Hour" for compute
    hourly = [c for c in candidates if "hour" in (c.get("unitOfMeasure") or "").lower()]
    if hourly:
        return hourly[0]
    return candidates[0]


# ─────────────────────────────────────────────────────────────────────────────
# Batch lookup for Advisor recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _extract_advisor_sku_info(rec: Any) -> dict[str, str]:
    """Extract service, SKU, and region from an Advisor recommendation."""
    raw = getattr(rec, "raw", None) or {}
    extended = (
        raw.get("extendedProperties")
        or raw.get("properties", {}).get("extendedProperties", {})
        or {}
    )
    rid = (getattr(rec, "resource_id", None) or "").lower()
    desc = (getattr(rec, "short_description", None) or "").lower()

    # Region
    region = (
        extended.get("region")
        or extended.get("location")
        or ""
    )

    # SKU name
    sku = (
        extended.get("sku")
        or extended.get("vmSize")
        or extended.get("currentSku")
        or ""
    )

    # Service name — derive from resource ID provider segment
    service_name = ""
    for provider, svc in _PROVIDER_TO_SERVICE.items():
        if provider in rid:
            service_name = svc
            break

    # If no provider match, try description keywords
    if not service_name:
        if "virtual machine" in desc or "vm" in desc:
            service_name = "Virtual Machines"
        elif "sql" in desc:
            service_name = "SQL Database"
        elif "storage" in desc:
            service_name = "Storage"
        elif "app service" in desc:
            service_name = "Azure App Service"
        elif "cosmos" in desc:
            service_name = "Azure Cosmos DB"
        elif "redis" in desc:
            service_name = "Azure Cache for Redis"
        elif "kubernetes" in desc or "aks" in desc:
            service_name = "Azure Kubernetes Service"
        elif "mysql" in desc:
            service_name = "Azure Database for MySQL"
        elif "postgres" in desc:
            service_name = "Azure Database for PostgreSQL"

    return {
        "service_name": service_name,
        "sku_name": sku,
        "arm_region": region,
    }


def batch_lookup_prices(
    recommendations: list[Any],
    max_workers: int = 3,
) -> dict[int, dict[str, float | None]]:
    """Look up retail prices for a list of Advisor recommendations.

    Returns a dict mapping recommendation index → price info dict.
    Only performs lookups where we have enough info (service + SKU + region).
    Uses threading for parallelism with a conservative worker count since
    the Retail API is public and rate limits are generous.
    """
    tasks: list[tuple[int, str, str, str]] = []
    for idx, rec in enumerate(recommendations):
        info = _extract_advisor_sku_info(rec)
        if info["service_name"] and info["sku_name"] and info["arm_region"]:
            tasks.append((idx, info["service_name"], info["sku_name"], info["arm_region"]))
        else:
            logger.debug(
                "Skipping retail price lookup for rec %d — missing info: service=%r, sku=%r, region=%r",
                idx, info["service_name"], info["sku_name"], info["arm_region"],
            )

    if not tasks:
        logger.info("No recommendations have sufficient info for retail price lookup.")
        return {}

    logger.info("Looking up retail prices for %d of %d recommendations…", len(tasks), len(recommendations))
    t0 = time.perf_counter()
    results: dict[int, dict[str, float | None]] = {}

    def _lookup(task: tuple[int, str, str, str]) -> tuple[int, dict[str, float | None]]:
        idx, svc, sku, region = task
        try:
            prices = lookup_sku_prices(svc, sku, region)
            return idx, prices
        except Exception as ex:
            logger.warning("Retail price lookup failed for %s/%s/%s: %s", svc, sku, region, ex)
            return idx, {}

    actual_workers = min(max_workers, len(tasks))
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {executor.submit(_lookup, t): t for t in tasks}
        for future in as_completed(futures):
            idx, prices = future.result()
            if prices:
                results[idx] = prices

    elapsed = time.perf_counter() - t0
    success_count = sum(1 for p in results.values() if p.get("payg_retail_price") is not None)
    logger.info(
        "Retail price lookup complete in %.2fs: %d/%d successful (of %d total recs)",
        elapsed, success_count, len(tasks), len(recommendations),
    )
    return results
