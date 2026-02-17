from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from azure.core.credentials import TokenCredential

from .logging_config import get_logger
from .models import AzureRecommendation, CommitmentItem

logger = get_logger(__name__)

_ARM_SCOPE = "https://management.azure.com/.default"
_ARM_BASE = "https://management.azure.com"


def _api_version(name: str, default: str) -> str:
    return os.getenv(name, default)


def _auth_header(credential: TokenCredential) -> dict[str, str]:
    logger.debug("Acquiring token for scope %s", _ARM_SCOPE)
    token = credential.get_token(_ARM_SCOPE).token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(credential: TokenCredential, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    logger.debug("GET %s params=%s", url, params)
    t0 = time.perf_counter()
    response = requests.get(url, headers=_auth_header(credential), params=params, timeout=60)
    elapsed = time.perf_counter() - t0
    logger.debug("GET %s -> %s (%.2fs)", url, response.status_code, elapsed)
    response.raise_for_status()
    return response.json()


def _get_all_pages(credential: TokenCredential, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Follow nextLink pagination and collect all items from 'value' arrays."""
    all_items: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params = params
    page = 0
    t0 = time.perf_counter()
    while next_url:
        page += 1
        logger.debug("GET page %d: %s params=%s", page, next_url, next_params)
        response = requests.get(next_url, headers=_auth_header(credential), params=next_params, timeout=60)
        logger.debug("GET page %d -> %s", page, response.status_code)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("value", [])
        all_items.extend(items)
        logger.debug("Page %d returned %d items (running total: %d)", page, len(items), len(all_items))
        next_url = payload.get("nextLink")
        next_params = None  # nextLink contains query string already
    elapsed = time.perf_counter() - t0
    logger.info("Paginated GET %s completed: %d pages, %d items, %.2fs", url, page, len(all_items), elapsed)
    return all_items


def list_subscriptions(credential: TokenCredential) -> list[dict[str, Any]]:
    logger.info("Listing subscriptions")
    url = f"{_ARM_BASE}/subscriptions"
    subs = _get_all_pages(credential, url, params={"api-version": "2022-12-01"})
    logger.info("Found %d subscriptions", len(subs))
    return subs


def list_reservations(credential: TokenCredential) -> list[CommitmentItem]:
    logger.info("Listing reservation orders")
    api_version = _api_version("AZURE_API_VERSION_RESERVATIONS", "2022-11-01")
    url = f"{_ARM_BASE}/providers/Microsoft.Capacity/reservationOrders"
    orders = _get_all_pages(credential, url, params={"api-version": api_version})
    logger.info("Found %d reservation orders", len(orders))

    items: list[CommitmentItem] = []
    for order in orders:
        properties = order.get("properties", {})
        expiry = properties.get("expiryDate") or properties.get("expiryDateTime")
        start = (
            properties.get("effectiveDateTime")
            or properties.get("appliedScopeProperties", {}).get("displayName")
            or properties.get("purchaseDate")
        )
        days_remaining = _days_remaining(expiry)
        items.append(
            CommitmentItem(
                source="reservation",
                item_id=order.get("id", ""),
                name=order.get("name", ""),
                term=properties.get("term", ""),
                scope=str(properties.get("billingScopeId") or properties.get("appliedScopes", "")),
                state=properties.get("provisioningState", "unknown"),
                start_date=str(start or ""),
                end_date=str(expiry or ""),
                days_remaining=days_remaining,
                details=order,
            )
        )
    return items


def list_savings_plans(credential: TokenCredential) -> list[CommitmentItem]:
    logger.info("Listing savings plans")
    api_version = _api_version("AZURE_API_VERSION_SAVINGS_PLANS", "2022-11-01")
    url = f"{_ARM_BASE}/providers/Microsoft.BillingBenefits/savingsPlans"
    plans = _get_all_pages(credential, url, params={"api-version": api_version})
    logger.info("Found %d savings plans", len(plans))

    items: list[CommitmentItem] = []
    for plan in plans:
        properties = plan.get("properties", {})
        expiry = properties.get("endDateTime")
        start = properties.get("startDateTime")
        days_remaining = _days_remaining(expiry)
        items.append(
            CommitmentItem(
                source="savings_plan",
                item_id=plan.get("id", ""),
                name=plan.get("name", ""),
                term=properties.get("term", ""),
                scope=str(properties.get("appliedScopeProperties", "")),
                state=properties.get("provisioningState", "unknown"),
                start_date=str(start or ""),
                end_date=str(expiry or ""),
                days_remaining=days_remaining,
                details=plan,
            )
        )
    return items


def list_advisor_cost_recommendations(
    credential: TokenCredential,
    subscription_id: str,
) -> list[AzureRecommendation]:
    logger.info("Listing Advisor cost recommendations for subscription %s", subscription_id)
    api_version = _api_version("AZURE_API_VERSION_ADVISOR", "2023-01-01")
    url = f"{_ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations"
    all_recs = _get_all_pages(credential, url, params={"api-version": api_version})
    logger.debug("Raw Advisor results: %d (all categories)", len(all_recs))

    recs: list[AzureRecommendation] = []
    for row in all_recs:
        properties = row.get("properties", {})
        category = properties.get("category", "")
        if str(category).lower() != "cost":
            continue

        short_desc = properties.get("shortDescription", {})
        extended = properties.get("extendedProperties", {})

        annual_savings = _extract_annual_savings(extended)
        recs.append(
            AzureRecommendation(
                resource_id=row.get("id", ""),
                category=category,
                impact=str(properties.get("impact", "")),
                short_description=str(short_desc.get("problem", "")),
                annual_savings_estimate=annual_savings,
                raw=row,
            )
        )
    logger.info("Advisor cost recommendations for %s: %d", subscription_id, len(recs))
    return recs


def query_cost_by_service(
    credential: TokenCredential,
    scope: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    logger.info("Querying cost by service: scope=%s, start=%s, end=%s", scope, start_date, end_date)
    api_version = _api_version("AZURE_API_VERSION_COST_QUERY", "2024-08-01")
    url = f"{_ARM_BASE}{scope}/providers/Microsoft.CostManagement/query"
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start_date, "to": end_date},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
        },
    }

    t0 = time.perf_counter()
    response = requests.post(
        url,
        headers=_auth_header(credential),
        params={"api-version": api_version},
        json=body,
        timeout=120,
    )
    elapsed = time.perf_counter() - t0
    logger.debug("POST %s -> %s (%.2fs)", url, response.status_code, elapsed)
    response.raise_for_status()
    payload = response.json()

    properties = payload.get("properties", {})
    columns = [c.get("name") for c in properties.get("columns", [])]
    rows = properties.get("rows", [])
    shaped = [dict(zip(columns, row)) for row in rows]
    logger.info("Cost query returned %d rows for scope %s", len(shaped), scope)
    return shaped


def _extract_annual_savings(extended: dict[str, Any]) -> float:
    for key, value in extended.items():
        key_l = key.lower()
        if "annual" in key_l and "saving" in key_l:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    for key, value in extended.items():
        if "saving" in key.lower():
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _days_remaining(end_date: str | None) -> int | None:
    if not end_date:
        return None
    try:
        parsed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        delta = parsed - datetime.now(timezone.utc)
        return max(delta.days, 0)
    except ValueError:
        return None
