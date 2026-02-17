# Azure FinOps Playbook (Companion)

Use this document for deeper execution guidance. Keep runtime calls anchored to the v2 prompt and pull only needed sections from this playbook.

## 1) Standard Analysis Flow
1. Validate required inputs and scope.
2. Build paygo baseline.
3. Inventory current RI/SP commitments and utilization.
4. Model requested scenarios with consistent assumptions.
5. Layer MACC impact (if present): burn-down pace, shortfall/surplus risk.
6. Rank actions by value, confidence, and dependency.

## 2) Scenario Definitions
- `paygo`: no commitments.
- `ri_only`: optimize with RI options (1y/3y) and expected utilization.
- `sp_only`: compute/general SP commitment modeling.
- `ri_sp_hybrid`: RI applied first, SP covers eligible spillover, paygo remainder.
- `macc_3y` / `macc_5y`: scenario economics constrained by commitment burn-down outcomes.

## 3) KPI Set (Recommended)
- Total cost
- Savings vs paygo
- Effective discount percent
- Commitment coverage percent
- Commitment utilization percent
- Waste (unused commitment value)
- MACC projected gap/surplus
- Break-even month for each commitment strategy

## 4) Data Quality Checklist
- Date range complete and timezone aligned
- One currency across datasets
- Missing tag rate measured
- RI/SP inventory reconciled with amortized usage
- Price references validated for agreement type
- Advisor recommendations ingested and deduplicated against existing commitments

## 5) MACC Evaluation Heuristics
- Burn-down trend = cumulative eligible spend / elapsed term.
- Projected completion = trend extrapolated to term end.
- If projected shortfall risk is high, evaluate pacing options before aggressive unit-rate optimization.
- If projected surplus is very high, prioritize strongest unit-rate optimizations while preserving flexibility where volatility is high.

## 6) Output Guardrails
- Never hide missing data.
- Keep scenario assumptions explicit and comparable.
- Separate observed values from estimated values.
- Provide actionability: owner, dependency, priority, expected impact.

## 7) Optional Execution Snippets
### Example pseudo-metrics
- `effective_discount_pct = 1 - (scenario_cost / paygo_cost)`
- `coverage_gap = eligible_paygo_spend - covered_spend`
- `ri_utilization_pct = used_reserved_hours / total_reserved_hours`
- `sp_utilization_pct = consumed_commitment / purchased_commitment`

### Example decision thresholds
- Utilization below threshold (e.g., 80%) => investigate resize/exchange/scope changes.
- Coverage gap above threshold => evaluate additional commitment candidates.
- MACC behind schedule => review spend pacing and eligibility mix.

## 8) Handoff Contract for Orchestration
Downstream agents should receive:
- Final structured output schema from v2 prompt
- Raw assumptions register
- Data quality warnings
- Ranked action list with confidence
