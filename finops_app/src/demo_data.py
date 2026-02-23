"""Simulated Azure data for demonstration when live subscriptions lack RI / SP activity.

Generates realistic Advisor recommendations, RI/SP commitments, and Cost
Management query results based on the actual invoice CSV that was loaded.
"""
from __future__ import annotations

import hashlib
import random
import time
from datetime import date, timedelta
from typing import Any

import polars as pl

from .file_loader import existing_columns, load_invoice_file, safe_datetime_expr
from .logging_config import get_logger
from .models import AzureRecommendation, CommitmentItem

logger = get_logger(__name__)

# ── Realistic RI savings rates per service (based on published Azure pricing) ─
_SAVINGS_RATES: dict[str, tuple[float, float]] = {
    # (min_savings_pct, max_savings_pct) for 3-year RI vs PAYG
    "Virtual Machines": (0.40, 0.62),
    "SQL Database": (0.35, 0.55),
    "Azure Cosmos DB": (0.30, 0.50),
    "Azure Cache for Redis": (0.35, 0.55),
    "Azure Database for MySQL": (0.30, 0.48),
    "Azure Database for PostgreSQL": (0.30, 0.48),
    "API Management": (0.25, 0.42),
    "Azure App Service": (0.30, 0.50),
    "Storage": (0.20, 0.38),
    "Azure Kubernetes Service": (0.35, 0.55),
    "Backup": (0.15, 0.30),
    "Virtual Network": (0.10, 0.25),
    "Azure DNS": (0.10, 0.20),
}

_DEFAULT_RATE = (0.25, 0.45)

# ── SKU templates per service ──────────────────────────────────────────────────
_SKU_TEMPLATES: dict[str, list[str]] = {
    "Virtual Machines": ["Standard_D4s_v5", "Standard_E8s_v5", "Standard_B2ms", "Standard_D2s_v5", "Standard_F4s_v2"],
    "SQL Database": ["GP_Gen5_4", "GP_Gen5_8", "BC_Gen5_2", "GP_S_Gen5_2"],
    "Azure Cosmos DB": ["Standard_D4s", "Standard_D8s"],
    "API Management": ["Standard", "Premium"],
    "Storage": ["Standard_LRS", "Standard_GRS", "Premium_LRS"],
    "Azure App Service": ["P1v3", "P2v3", "S1", "B2"],
    "Azure Cache for Redis": ["C1 Standard", "C2 Standard", "P1 Premium"],
    "Virtual Network": ["VPN Gateway S2S", "Private Link"],
    "Backup": ["Standard", "Enhanced"],
    "Azure DNS": ["Standard"],
}

# ── Simulated additional "enterprise" services to bulk up the demo ─────────────
_EXTRA_SERVICES: list[dict[str, Any]] = [
    {"MeterCategory": "Virtual Machines", "region": "US East", "monthly_paygo": 8500.00, "consumed": "Microsoft.Compute"},
    {"MeterCategory": "Virtual Machines", "region": "US East 2", "monthly_paygo": 4200.00, "consumed": "Microsoft.Compute"},
    {"MeterCategory": "SQL Database", "region": "US East", "monthly_paygo": 3100.00, "consumed": "Microsoft.Sql"},
    {"MeterCategory": "Azure Cosmos DB", "region": "US East", "monthly_paygo": 1800.00, "consumed": "Microsoft.DocumentDb"},
    {"MeterCategory": "Azure Cache for Redis", "region": "US East 2", "monthly_paygo": 950.00, "consumed": "Microsoft.Cache"},
    {"MeterCategory": "Azure App Service", "region": "US East", "monthly_paygo": 620.00, "consumed": "Microsoft.Web"},
    {"MeterCategory": "Azure Kubernetes Service", "region": "US East", "monthly_paygo": 2400.00, "consumed": "Microsoft.ContainerService"},
    {"MeterCategory": "Azure Database for PostgreSQL", "region": "US East 2", "monthly_paygo": 780.00, "consumed": "Microsoft.DBforPostgreSQL"},
]


def _seed(file_path: str) -> int:
    """Deterministic seed so the same CSV always produces the same demo data."""
    return int(hashlib.md5(file_path.encode()).hexdigest()[:8], 16)


def generate_demo_data(
    file_path: str,
) -> dict[str, Any]:
    """Return a dict with keys: recommendations, commitments, cost_query_rows, sub_ids.

    All data is synthetic but *grounded* in the real CSV: the actual services and
    their PAYG costs are preserved; additional enterprise services are layered on
    to demonstrate a realistic RI/SP opportunity set.
    """
    logger.info("Generating demo data from %s", file_path)
    t0 = time.perf_counter()
    rng = random.Random(_seed(file_path))

    # ── Read real invoice services ─────────────────────────────────────────────
    scan = load_invoice_file(file_path)
    existing = existing_columns(scan)

    cols = [c for c in ("MeterCategory", "MeterRegion", "Cost", "ConsumedService", "Date") if c in existing]
    lf = scan.select(cols).with_columns(
        pl.col("Cost").cast(pl.Float64, strict=False) if "Cost" in cols else pl.lit(0.0).alias("Cost"),
    )
    if "Date" in cols:
        lf = lf.with_columns(safe_datetime_expr("Date", scan))
    df = lf.collect(streaming=True)

    # Period info
    if "Date" in df.columns:
        dates = df["Date"].drop_nulls()
        period_days = max((dates.max() - dates.min()).days, 1) if len(dates) > 0 else 30  # type: ignore[operator]
    else:
        period_days = 30

    # Group real services
    _CATEGORY_TO_PROVIDER = {
        "API Management": "Microsoft.ApiManagement",
        "Virtual Network": "Microsoft.Network",
        "Storage": "Microsoft.Storage",
        "Azure DNS": "Microsoft.Network",
        "Backup": "Microsoft.DataProtection",
        "Azure App Service": "Microsoft.Web",
        "Virtual Machines": "Microsoft.Compute",
        "SQL Database": "Microsoft.Sql",
        "Azure Cosmos DB": "Microsoft.DocumentDb",
        "Azure Cache for Redis": "Microsoft.Cache",
        "Azure Kubernetes Service": "Microsoft.ContainerService",
        "Azure Database for PostgreSQL": "Microsoft.DBforPostgreSQL",
        "Azure Database for MySQL": "Microsoft.DBforMySQL",
    }

    real_services: list[dict[str, Any]] = []
    if "MeterCategory" in df.columns:
        # Get the first ConsumedService per MeterCategory if available
        consumed_map: dict[str, str] = {}
        if "ConsumedService" in df.columns:
            for r in df.group_by("MeterCategory").agg(pl.col("ConsumedService").drop_nulls().first()).iter_rows(named=True):
                if r.get("ConsumedService"):
                    consumed_map[r["MeterCategory"]] = r["ConsumedService"]

        for row in (
            df.group_by(["MeterCategory", "MeterRegion"] if "MeterRegion" in df.columns else ["MeterCategory"])
            .agg(pl.sum("Cost").alias("TotalCost"))
            .sort("TotalCost", descending=True)
            .iter_rows(named=True)
        ):
            cat = row["MeterCategory"]
            monthly = row["TotalCost"] / period_days * 30
            consumed = consumed_map.get(cat, "") or _CATEGORY_TO_PROVIDER.get(cat, "Microsoft.Compute")
            real_services.append({
                "MeterCategory": cat,
                "region": row.get("MeterRegion") or "US East",
                "monthly_paygo": monthly,
                "consumed": consumed,
            })

    # Combine real + extra (extra simulate a larger environment)
    all_services = real_services + _EXTRA_SERVICES

    # ── Generate Advisor recommendations ───────────────────────────────────────
    recommendations: list[AzureRecommendation] = []
    for svc in all_services:
        cat = svc["MeterCategory"]
        region = svc["region"] or "US East"
        monthly = svc["monthly_paygo"]
        if monthly < 0.01:
            continue

        lo, hi = _SAVINGS_RATES.get(cat, _DEFAULT_RATE)
        savings_pct = rng.uniform(lo, hi)
        annual_savings = monthly * 12 * savings_pct
        skus = _SKU_TEMPLATES.get(cat, ["Standard"])
        sku = rng.choice(skus)
        num_units = max(1, int(monthly / rng.uniform(50, 500)))

        rec_id = f"/subscriptions/demo-sub-01/providers/{svc.get('consumed', 'Microsoft.Compute')}/recommendations/{hashlib.md5(f'{cat}-{sku}-{region}'.encode()).hexdigest()[:12]}"

        recommendations.append(AzureRecommendation(
            resource_id=rec_id,
            category="Cost",
            impact=rng.choice(["High", "High", "Medium", "Medium", "Low"]),
            short_description=f"Purchase Reserved Instances for {cat} ({sku}) in {region} — save ~{savings_pct*100:.0f}%",
            annual_savings_estimate=round(annual_savings, 2),
            raw={
                "extendedProperties": {
                    "sku": sku,
                    "region": region,
                    "vmSize": sku if cat == "Virtual Machines" else "",
                    "quantity": str(num_units),
                    "savingsAmount": str(round(annual_savings, 2)),
                    "annualSavingsAmount": str(round(annual_savings, 2)),
                    "savingsCurrency": "USD",
                    "term": "P3Y",
                    "lookbackPeriod": "Last30Days",
                    "currentSku": sku,
                },
                "properties": {"category": "Cost", "impact": "High"},
            },
        ))

    # ── Generate commitments (some RIs, some SPs) ─────────────────────────────
    commitments: list[CommitmentItem] = []
    today = date.today()

    # A few active RIs
    ri_samples = [
        ("Virtual Machines – Standard_D4s_v5", "P3Y", -400, "Active"),
        ("SQL Database – GP_Gen5_4", "P1Y", -180, "Active"),
        ("Azure Cosmos DB – Standard_D4s", "P3Y", -700, "Active"),
        ("Virtual Machines – Standard_B2ms", "P1Y", -60, "Expiring"),  # expiring soon
        ("Azure App Service – P1v3", "P1Y", -340, "Active"),
    ]
    for name, term, start_offset, state in ri_samples:
        start = today + timedelta(days=start_offset)
        years = 3 if "P3Y" in term else 1
        end = start + timedelta(days=365 * years)
        days_rem = (end - today).days
        if days_rem < 0:
            continue
        # Mark items expiring within 90 days
        actual_state = "Succeeded" if days_rem > 90 else "Succeeded"
        commitments.append(CommitmentItem(
            source="Reservation",
            item_id=f"demo-ri-{hashlib.md5(name.encode()).hexdigest()[:8]}",
            name=name,
            term=term,
            scope="Shared",
            state=actual_state,
            start_date=str(start),
            end_date=str(end),
            days_remaining=days_rem,
            details={"provisioningState": actual_state, "displayName": name},
        ))

    # A few Savings Plans
    sp_samples = [
        ("Compute Savings Plan – $500/hr", "P3Y", -200, "Active"),
        ("Compute Savings Plan – $150/hr", "P1Y", -50, "Active"),
    ]
    for name, term, start_offset, state in sp_samples:
        start = today + timedelta(days=start_offset)
        years = 3 if "P3Y" in term else 1
        end = start + timedelta(days=365 * years)
        days_rem = (end - today).days
        commitments.append(CommitmentItem(
            source="SavingsPlan",
            item_id=f"demo-sp-{hashlib.md5(name.encode()).hexdigest()[:8]}",
            name=name,
            term=term,
            scope="Shared",
            state="Succeeded",
            start_date=str(start),
            end_date=str(end),
            days_remaining=days_rem,
            details={"provisioningState": "Succeeded", "displayName": name},
        ))

    # ── Simulated Cost Management query rows ───────────────────────────────────
    cost_query_rows: list[dict[str, Any]] = []
    for svc in all_services:
        if svc["monthly_paygo"] < 0.01:
            continue
        cost_query_rows.append({
            "ServiceName": svc["MeterCategory"],
            "Cost": round(svc["monthly_paygo"] * period_days / 30, 2),
            "Currency": "USD",
        })

    # Total annual PAYG across all services (real invoice + simulated extra)
    full_annual_paygo = sum(s["monthly_paygo"] * 12 for s in all_services if s["monthly_paygo"] > 0)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Demo data generated in %.2fs: %d recommendations, %d commitments, %d cost rows, annual_paygo=%.2f",
        elapsed, len(recommendations), len(commitments), len(cost_query_rows), full_annual_paygo,
    )

    return {
        "recommendations": recommendations,
        "commitments": commitments,
        "cost_query_rows": cost_query_rows,
        "sub_ids": ["demo-sub-01"],
        "full_annual_paygo": round(full_annual_paygo, 2),
    }
