from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests
from azure.core.credentials import TokenCredential

from .logging_config import get_logger
from .models import AzureRecommendation, CommitmentItem

logger = get_logger(__name__)

_ARM_SCOPE = "https://management.azure.com/.default"
_ARM_BASE = "https://management.azure.com"
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiter  —  prevents thundering-herd 429 errors
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimiter:
    """Adaptive rate limiter that dynamically adjusts concurrency on throttling.

    Starts with an initial concurrency level and minimum inter-request interval.
    When Azure returns 429 (throttled), concurrency is halved and the interval
    increases.  After a streak of consecutive successes the limiter gradually
    restores concurrency and reduces the interval back toward their initial
    values.

    This prevents the "thundering herd" problem where all threads keep
    hammering the API after a 429, making the situation worse.
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        min_interval: float = 0.5,
        *,
        recovery_streak: int = 10,
    ):
        self._initial_concurrent = max_concurrent
        self._initial_interval = min_interval
        self._max_concurrent = max_concurrent
        self._min_interval = min_interval
        self._recovery_streak = recovery_streak

        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._last_time = 0.0
        self._consecutive_ok = 0
        self._active = 0  # currently active slots
        self._throttle_count = 0  # lifetime throttle events

    def acquire(self) -> None:
        self._semaphore.acquire()
        with self._lock:
            self._active += 1
            now = time.monotonic()
            wait = self._last_time + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_time = time.monotonic()

    def release(self) -> None:
        with self._lock:
            self._active = max(self._active - 1, 0)
        self._semaphore.release()

    # ── Feedback signals ──────────────────────────────────────────────────
    def report_success(self) -> None:
        """Call after a successful API response (non-429)."""
        with self._lock:
            self._consecutive_ok += 1
            if (
                self._consecutive_ok >= self._recovery_streak
                and self._max_concurrent < self._initial_concurrent
            ):
                old_c = self._max_concurrent
                self._max_concurrent = min(self._max_concurrent + 1, self._initial_concurrent)
                self._min_interval = max(
                    self._min_interval * 0.85,
                    self._initial_interval,
                )
                self._consecutive_ok = 0  # reset streak counter
                # Add a permit back to the semaphore
                try:
                    self._semaphore.release()
                except ValueError:
                    pass  # semaphore at max — harmless
                logger.info(
                    "Adaptive throttle: recovering concurrency %d → %d "
                    "(interval %.2fs) after %d consecutive successes",
                    old_c, self._max_concurrent,
                    self._min_interval, self._recovery_streak,
                )

    def report_throttle(self) -> None:
        """Call when a 429 response is received."""
        with self._lock:
            self._throttle_count += 1
            self._consecutive_ok = 0
            old_c = self._max_concurrent
            old_i = self._min_interval

            # Halve concurrency (floor 1)
            new_concurrent = max(self._max_concurrent // 2, 1)
            # Increase interval by 50%
            new_interval = self._min_interval * 1.5

            # Drain excess permits from the semaphore
            drained = 0
            for _ in range(old_c - new_concurrent):
                acquired = self._semaphore.acquire(blocking=False)
                if acquired:
                    drained += 1
                else:
                    break  # no more available permits

            self._max_concurrent = new_concurrent
            self._min_interval = new_interval

            logger.warning(
                "Adaptive throttle: reducing concurrency %d → %d, "
                "interval %.2fs → %.2fs (throttle event #%d, drained %d permits)",
                old_c, new_concurrent,
                old_i, new_interval,
                self._throttle_count, drained,
            )

    @property
    def current_concurrency(self) -> int:
        return self._max_concurrent

    @property
    def current_interval(self) -> float:
        return self._min_interval

    @property
    def throttle_count(self) -> int:
        return self._throttle_count


# Shared rate limiter for Cost Management queries (most throttle-sensitive)
_cost_mgmt_limiter = _RateLimiter(
    max_concurrent=int(os.getenv("AZURE_COST_MAX_CONCURRENT", "3")),
    min_interval=float(os.getenv("AZURE_COST_MIN_INTERVAL", "0.6")),
)

# Shared rate limiter for Advisor API (less aggressive but still throttled)
_advisor_limiter = _RateLimiter(
    max_concurrent=int(os.getenv("AZURE_ADVISOR_MAX_CONCURRENT", "5")),
    min_interval=float(os.getenv("AZURE_ADVISOR_MIN_INTERVAL", "0.3")),
)


def is_throttling_exception(ex: Exception) -> bool:
    if isinstance(ex, requests.HTTPError):
        status = ex.response.status_code if ex.response is not None else None
        return status == 429
    msg = str(ex).lower()
    return "throttl" in msg or "too many requests" in msg or "http 429" in msg


def is_authorization_exception(ex: Exception) -> bool:
    if isinstance(ex, requests.HTTPError):
        status = ex.response.status_code if ex.response is not None else None
        return status in {401, 403}
    msg = str(ex).lower()
    return (
        "forbidden" in msg
        or "unauthorized" in msg
        or "permission" in msg
        or "access denied" in msg
        or "http 401" in msg
        or "http 403" in msg
    )


def _api_version(name: str, default: str) -> str:
    return os.getenv(name, default)


def _max_retries() -> int:
    try:
        return max(0, int(os.getenv("AZURE_HTTP_MAX_RETRIES", "7")))
    except ValueError:
        return 7


def _retry_base_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("AZURE_HTTP_RETRY_BASE_SECONDS", "1.5")))
    except ValueError:
        return 1.5


def _default_max_workers(num_subscriptions: int) -> int:
    """Calculate optimal worker pool size based on subscription count.
    
    Rules:
    - Up to 10 subs: 4 workers (default)
    - 11-50 subs: 8 workers
    - 51-150 subs: 12 workers
    - 150+ subs: min(16, num_subscriptions // 15)
    
    Can be overridden with AZURE_PARALLEL_WORKERS env var.
    """
    env_override = os.getenv("AZURE_PARALLEL_WORKERS")
    if env_override:
        try:
            return max(1, int(env_override))
        except ValueError:
            pass
    
    if num_subscriptions <= 10:
        return 4
    elif num_subscriptions <= 50:
        return 8
    elif num_subscriptions <= 150:
        return 12
    else:
        return min(16, max(12, num_subscriptions // 15))



def _auth_header(credential: TokenCredential) -> dict[str, str]:
    logger.debug("Acquiring token for scope %s", _ARM_SCOPE)
    token = credential.get_token(_ARM_SCOPE).token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _request_with_retries(
    method: str,
    credential: TokenCredential,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 60,
    rate_limiter: _RateLimiter | None = None,
) -> requests.Response:
    attempts = _max_retries() + 1
    base_delay = _retry_base_seconds()

    for attempt in range(1, attempts + 1):
        # Honour rate limiter if provided (acquire before each attempt)
        if rate_limiter:
            rate_limiter.acquire()
        try:
            response = requests.request(
                method,
                url,
                headers=_auth_header(credential),
                params=params,
                json=json_body,
                timeout=timeout,
            )
            response.raise_for_status()
            # Notify limiter of successful request
            if rate_limiter:
                rate_limiter.report_success()
            return response
        except requests.HTTPError as ex:
            status = ex.response.status_code if ex.response is not None else None
            is_retryable = status in _RETRYABLE_STATUS_CODES

            # Notify limiter of throttling so it can reduce concurrency
            if status == 429 and rate_limiter:
                rate_limiter.report_throttle()

            if not is_retryable or attempt >= attempts:
                if status == 429:
                    logger.warning(
                        "%s %s is being throttled by Azure and retries are exhausted (%d attempts).",
                        method,
                        url,
                        attempts,
                    )
                raise
            retry_after_header = ex.response.headers.get("Retry-After") if ex.response is not None else None
            retry_after: float | None = None
            if retry_after_header:
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    retry_after = None
            if retry_after is not None and retry_after > 0:
                delay = retry_after
            else:
                delay = base_delay * (2 ** (attempt - 1))
            # Add jitter (0-50% of delay) to spread retries across threads
            jitter = delay * random.uniform(0, 0.5)
            delay += jitter
            reason = "throttled by Azure" if status == 429 else f"HTTP {status}"
            logger.warning(
                "%s %s failed (%s, attempt %d/%d). Retrying in %.1fs",
                method,
                url,
                reason,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
        except (requests.Timeout, requests.ConnectionError) as ex:
            if attempt >= attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            jitter = delay * random.uniform(0, 0.5)
            delay += jitter
            logger.warning(
                "%s %s failed with %s (attempt %d/%d). Retrying in %.1fs",
                method,
                url,
                type(ex).__name__,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
        finally:
            if rate_limiter:
                rate_limiter.release()


def _get(credential: TokenCredential, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    logger.debug("GET %s params=%s", url, params)
    t0 = time.perf_counter()
    response = _request_with_retries("GET", credential, url, params=params, timeout=60)
    elapsed = time.perf_counter() - t0
    logger.debug("GET %s -> %s (%.2fs)", url, response.status_code, elapsed)
    return response.json()


def _get_all_pages(
    credential: TokenCredential,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    allow_partial_on_error: bool = False,
    exit_on_empty_first_page: bool = False,
    rate_limiter: _RateLimiter | None = None,
) -> list[dict[str, Any]]:
    """Follow nextLink pagination and collect all items from 'value' arrays.
    
    Args:
        exit_on_empty_first_page: If True, stop pagination if first page returns 0 items.
                                  Useful for Advisor queries where empty first page means no results.
        rate_limiter: Optional adaptive rate limiter for throttle-sensitive APIs.
    """
    all_items: list[dict[str, Any]] = []
    next_url: str | None = url
    next_params = params
    page = 0
    t0 = time.perf_counter()
    while next_url:
        page += 1
        logger.debug("GET page %d: %s params=%s", page, next_url, next_params)
        try:
            response = _request_with_retries(
                "GET", credential, next_url, params=next_params, timeout=60,
                rate_limiter=rate_limiter,
            )
        except requests.RequestException as ex:
            if allow_partial_on_error and all_items:
                logger.warning(
                    "Paginated GET %s failed on page %d after collecting %d items. Returning partial results. Error: %s",
                    url,
                    page,
                    len(all_items),
                    ex,
                )
                break
            raise
        logger.debug("GET page %d -> %s", page, response.status_code)
        payload = response.json()
        items = payload.get("value", [])
        all_items.extend(items)
        logger.debug("Page %d returned %d items (running total: %d)", page, len(items), len(all_items))
        next_url = payload.get("nextLink")
        next_params = None  # nextLink contains query string already
        
        # Early exit optimization: if first page is empty and we're not expecting pagination, stop
        if exit_on_empty_first_page and page == 1 and len(items) == 0:
            logger.debug("First page returned 0 items; stopping pagination early")
            break
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
    try:
        plans = _get_all_pages(credential, url, params={"api-version": api_version})
    except requests.HTTPError as ex:
        status = ex.response.status_code if ex.response is not None else None
        if status == 403:
            logger.warning(
                "Savings Plans API returned 403 Forbidden. This typically means the signed-in identity "
                "does not have BillingBenefits read permissions for savings plans. Continuing without savings plans data."
            )
            return []
        raise
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
    all_recs = _get_all_pages(
        credential,
        url,
        params={"api-version": api_version},
        allow_partial_on_error=True,
        exit_on_empty_first_page=True,  # Optimization: stop pagination if first page is empty
        rate_limiter=_advisor_limiter,
    )
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


def list_advisor_cost_recommendations_parallel(
    credential: TokenCredential,
    subscription_ids: list[str],
    max_workers: int | None = None,
) -> list[AzureRecommendation]:
    """Fetch Advisor recommendations for multiple subscriptions in parallel.
    
    Args:
        credential: Azure credential
        subscription_ids: List of subscription IDs to query
        max_workers: Maximum number of concurrent threads. If None, auto-calculated based on
                     subscription count. Can be overridden with AZURE_PARALLEL_WORKERS env var.
    
    Returns:
        Flattened list of all recommendations from all subscriptions
    """
    if max_workers is None:
        max_workers = _default_max_workers(len(subscription_ids))
    
    all_recs: list[AzureRecommendation] = []
    
    def fetch_advisor_for_subscription(sid: str) -> tuple[str, list[AzureRecommendation]]:
        """Fetch recommendations for one subscription. Returns (subscription_id, recommendations)."""
        try:
            recs = list_advisor_cost_recommendations(credential, sid)
            return (sid, recs)
        except Exception as ex:
            logger.warning("Failed Advisor query for subscription %s: %s", sid, ex)
            return (sid, [])
    
    logger.info("Fetching Advisor recommendations for %d subscriptions (max %d parallel workers)", len(subscription_ids), max_workers)
    t0 = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_advisor_for_subscription, sid): sid for sid in subscription_ids}
        
        for future in as_completed(futures):
            try:
                sid, recs = future.result()
                all_recs.extend(recs)
                logger.debug("Advisor fetch completed for subscription %s: %d recommendations", sid, len(recs))
            except Exception as ex:
                sid = futures[future]
                logger.error("Unexpected error fetching Advisor for subscription %s: %s", sid, ex)
    
    elapsed = time.perf_counter() - t0
    logger.info("Parallel Advisor fetch completed: %d total recommendations in %.2fs", len(all_recs), elapsed)
    return all_recs


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
    response = _request_with_retries(
        "POST",
        credential,
        url,
        params={"api-version": api_version},
        json_body=body,
        timeout=120,
        rate_limiter=_cost_mgmt_limiter,
    )
    elapsed = time.perf_counter() - t0
    logger.debug("POST %s -> %s (%.2fs)", url, response.status_code, elapsed)
    payload = response.json()

    properties = payload.get("properties", {})
    columns = [c.get("name") for c in properties.get("columns", [])]
    rows = properties.get("rows", [])
    shaped = [dict(zip(columns, row)) for row in rows]
    logger.info("Cost query returned %d rows for scope %s", len(shaped), scope)
    return shaped


def query_cost_by_service_parallel(
    credential: TokenCredential,
    subscription_ids: list[str],
    start_date: str,
    end_date: str,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    """Query cost by service for multiple subscriptions in parallel.
    
    Uses a lower concurrency cap than Advisor queries because the Cost
    Management API has stricter rate limits (~30 requests/minute/tenant).
    The rate limiter further spaces requests to avoid 429 bursts.
    
    Args:
        credential: Azure credential
        subscription_ids: List of subscription IDs to query
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        max_workers: Maximum number of concurrent threads. If None, auto-calculated
                     with a lower ceiling than Advisor.
    
    Returns:
        Flattened list of all cost query rows from all subscriptions
    """
    if max_workers is None:
        # Cost Management is throttle-sensitive: use half the default workers, capped at 5
        max_workers = min(5, _default_max_workers(len(subscription_ids)))
    
    all_rows: list[dict[str, Any]] = []
    
    def fetch_cost_for_subscription(sid: str) -> tuple[str, list[dict[str, Any]]]:
        """Query cost for one subscription. Returns (subscription_id, rows)."""
        try:
            rows = query_cost_by_service(
                credential=credential,
                scope=f"/subscriptions/{sid}",
                start_date=f"{start_date}T00:00:00Z",
                end_date=f"{end_date}T23:59:59Z",
            )
            return (sid, rows)
        except Exception as ex:
            logger.warning("Cost query failed for subscription %s: %s", sid, ex)
            return (sid, [])
    
    logger.info("Running Cost Management queries for %d subscriptions (max %d parallel workers)", len(subscription_ids), max_workers)
    t0 = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_cost_for_subscription, sid): sid for sid in subscription_ids}
        
        for future in as_completed(futures):
            try:
                sid, rows = future.result()
                all_rows.extend(rows)
                logger.debug("Cost query completed for subscription %s: %d rows", sid, len(rows))
            except Exception as ex:
                sid = futures[future]
                logger.error("Unexpected error querying cost for subscription %s: %s", sid, ex)
    
    elapsed = time.perf_counter() - t0
    logger.info("Parallel cost queries completed: %d total rows in %.2fs", len(all_rows), elapsed)
    return all_rows


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
