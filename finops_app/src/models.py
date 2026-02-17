from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class AnalysisSummary:
    period_start: date | None
    period_end: date | None
    currency: str
    total_cost: float
    total_quantity: float
    record_count: int


@dataclass
class AzureRecommendation:
    resource_id: str
    category: str
    impact: str
    short_description: str
    annual_savings_estimate: float
    raw: dict[str, Any]


@dataclass
class CommitmentItem:
    source: str
    item_id: str
    name: str
    term: str
    scope: str
    state: str
    start_date: str
    end_date: str
    days_remaining: int | None
    details: dict[str, Any]
