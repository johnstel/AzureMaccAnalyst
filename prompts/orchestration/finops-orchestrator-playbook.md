# FinOps Orchestrator Playbook

## Objective
Coordinate end-to-end analysis without overloading runtime context, especially for large Azure invoice-detail exports.

## Step 1: Intake + Contract Check
- Validate required fields from orchestrator runtime prompt.
- Confirm analysis period and scope IDs.
- Record run metadata: `analysis_id`, timestamp, user/request origin.

## Step 2: Schema Mapping
For invoice-detail layouts similar to:
- `Date`, `Cost`, `Quantity`, `BillingCurrency`
- `SubscriptionId`, `ResourceGroup`, `ResourceId`
- `MeterCategory`, `ServiceFamily`, `PricingModel`, `ChargeType`
- `ReservationId`, `ReservationName`, `Term`

Create field-map if provider export names differ.

## Step 3: Large File Strategy (10GB class)
- Use distributed engine (Fabric/Spark/SQL) and partition by date.
- Read only required columns for each sub-task.
- Materialize compact aggregates for analyst agents.
- Avoid passing raw row-level records into LLM context.
- Keep artifact lineage: source path + filter + hash + row counts.

## Step 4: Sub-Agent Sequence
1. Data profiler returns quality and schema report.
2. Baseline analyst computes paygo baseline.
3. Commitment analyst computes RI/SP/hybrid scenarios.
4. MACC analyst computes burn-down outcomes and risk.
5. Ranker issues prioritized action plan.

## Step 5: Reconciliation Checks
- `sum(scenario_cost_components)` should reconcile to scenario total.
- Paygo baseline should reconcile to filtered invoice-detail totals.
- RI/SP utilization values must be bounded [0,100].
- Currency must be consistent; no hidden FX conversion.

## Step 6: Final Pack
Produce one merged result using orchestrator output schema:
- executive summary
- scenario comparison
- MACC status
- actions with owners/dependencies
- traceability and assumptions

## Failure Modes
- Missing RI/SP inventory => return partial with explicit blocker.
- Mixed currencies => blocked unless conversion policy is provided.
- Incomplete period coverage => partial with confidence reduction.

## Performance Targets (Guidance)
- First quality signal quickly via sampled profile.
- Full scenario run built on aggregates, not raw rows.
- Re-runs should reuse prior aggregates for same input/date slice when valid.
