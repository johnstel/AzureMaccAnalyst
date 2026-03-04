# Azure MACC Analyst App (Python)

A finance-friendly Streamlit application to:
- Process Azure invoice-detail CSV or Excel exports (large-file aware via Polars lazy scan)
- Connect to Azure securely with Microsoft sign-in
- Pull current Reserved Instances and Savings Plans inventory
- Pull Azure Advisor cost recommendations to support future purchase decisions
- Export a consolidated Excel workbook for reporting

## Why Python + Streamlit
- Fastest path to an intuitive UI for non-IT financial users
- Strong data tooling for large CSVs
- Easy Excel export workflow

## Security model
- Uses Microsoft Entra interactive browser authentication (`InteractiveBrowserCredential`)
- No password capture in the app
- Access controlled by Azure RBAC
- Token cache uses Azure Identity persistence options

## Authentication modes
1. User-context mode (default, no app registration required):
   - Uses existing signed-in context (Shared token cache / Azure CLI / Azure Developer CLI / Azure PowerShell)
   - Falls back to browser sign-in if needed
2. App-registration mode (optional):
   - Set `AZURE_CLIENT_ID` (and optionally `AZURE_TENANT_ID`) for stricter enterprise control

## Prerequisites
1. Python 3.11+
2. (Optional) Azure app registration — only needed for enterprise lockdown; the default user-context flow requires no app registration.
3. RBAC roles for signed-in users (assign at Root Management Group or Billing Account scope for full coverage):

| Role | Scope | Purpose |
|------|-------|---------|
| **Reader** | Root Management Group | Azure Advisor recommendations across all subscriptions |
| **Cost Management Reader** | Root Management Group | Cost Management query data per subscription |
| **Reservations Reader** | Tenant root (`/providers/Microsoft.Capacity`) | List Reserved Instance orders (API returns empty without this) |
| **Savings Plan Reader** or **Billing Account Reader** | Billing Account | List Savings Plans (API returns 403 without this) |

> **Note:** The Reservations and Savings Plans APIs are tenant/billing-scoped, not subscription-scoped. Without the correct roles, the API silently returns empty data (reservations) or an explicit 403 (savings plans).

## Setup
1. Create and activate a virtual environment
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Copy `.env.example` values into environment variables
   - `AZURE_CLIENT_ID` is optional
   - `AZURE_TENANT_ID` defaults to `common`

## Run
- From `finops_app` folder:
  - `streamlit run app.py`

## Single EXE deployment (recommended for non-technical users)
- Build once using: `./build_exe.ps1`
- Distribute: `dist/AzureMaccAnalyst.exe`
- End users launch by double-clicking the `.exe`
- See full details in `README-exe.md`

---

## Handling Large Excel Files (1 GB+)

Azure invoice exports are often delivered as multi-GB `.xlsx` files. The app uses a **tiered conversion strategy** that automatically picks the fastest safe approach for the machine it's running on.

### Conversion Tiers

| Tier | Engine | Speed | Peak Memory | When Used |
|------|--------|-------|-------------|-----------|
| 1 | **fastexcel** (calamine / Rust) → Parquet | 10–50× faster | ~2–4 GB for a 5 GB xlsx | `fastexcel` installed, file fits in ≤80% available RAM |
| 2 | **openpyxl streaming** → CSV → Parquet | Moderate | ~50 MB constant | `pyarrow` installed, but file too large for Tier 1 |
| 3 | **openpyxl streaming** → CSV | Slowest | ~50 MB constant | Fallback when `pyarrow` is not installed |

The app displays a **live progress bar** during conversion so users know exactly how far along the process is.

### Installing fastexcel for best performance

Both `fastexcel` and `pyarrow` are included in `requirements.txt` and installed by default. If you're working in a minimal environment:

```bash
pip install fastexcel pyarrow
```

That's it — the app detects them automatically on next run. No code or config changes needed.

### How it works

1. **First analysis of an `.xlsx` file**: the app converts it to an intermediate file (`.converted.parquet` or `.converted.csv`) saved next to the original.
2. **Subsequent runs**: the cached intermediate is reused instantly if it's newer than the source `.xlsx` — no re-conversion.
3. **Analysis**: Polars scans the intermediate file lazily (`pl.scan_parquet` or `pl.scan_csv`) with `.collect(streaming=True)`, keeping memory constant regardless of file size.

### Configuration

Set these in your environment or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_EXCEL_FAST_PATH` | `true` | Enable/disable the fastexcel (Rust) Tier 1 path. Set `false` to always use streaming. |
| `AZURE_MAX_EXCEL_MB` | `8192` | Warning threshold for very large Excel files (in MB). |
| `AZURE_BLOCK_OVERSIZED_EXCEL` | `false` | `true` = block files above threshold; `false` = warn and continue. |

---

## Reliability / Performance Tuning

Operators can tune the Azure Retail Prices API lookup behaviour without any code changes by setting these environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `RETAIL_PRICING_TIMEOUT` | `30` | Per-request HTTP timeout in seconds for the Azure Retail Prices API. Increase on high-latency networks. |
| `RETAIL_PRICING_MAX_WORKERS` | `3` | Max parallel threads used by the **Advisor-path** retail price batch lookup. Reduce if you see transient network errors on large recommendation sets. |
| `CSV_RI_MAX_PRICE_WORKERS` | `2` | Max parallel threads used by the **CSV RI-detection path** retail price lookups. A lower default reduces network pressure on large billing exports. |

These knobs can also be combined with existing variables like `CSV_RI_MIN_ANNUAL_COST` (default: `100`) and `RI_DISCOUNT_RATE` / `SP_DISCOUNT_RATE` to control analysis thresholds.

### Performance expectations (5 GB .xlsx, ~10M rows)

| Scenario | Conversion Time | Intermediate Size | Analysis Time |
|----------|----------------|-------------------|---------------|
| Tier 1 (fastexcel → Parquet) | ~2–5 min | ~500 MB–1 GB | seconds |
| Tier 2 (openpyxl → CSV → Parquet) | ~30–60 min | ~500 MB–1 GB | seconds |
| Tier 3 (openpyxl → CSV) | ~30–60 min | ~3–5 GB | ~10–30 sec |
| Cached re-analysis (any tier) | **0 sec** | reused | seconds |

> **Recommendation:** For recurring analysis of the same file, the first conversion is a one-time cost. Subsequent runs are near-instant.

### Memory safety

The app checks available system RAM before attempting Tier 1:
- **File < 1 GB compressed**: always tries fastexcel (low risk)
- **File ≥ 1 GB compressed**: estimates decompressed size (~4× the `.xlsx` file size) and only uses fastexcel if it fits within 80% of available RAM
- If fastexcel fails or is skipped, the app automatically falls back to openpyxl streaming with constant ~50 MB memory

---

## Key features
- **Retail pricing lookup** — Fetches real RI/SP pricing from the Azure Retail Prices API and normalizes term-based prices to hourly rates for accurate savings calculations
- **CSV-based RI detection** — Identifies existing Reserved Instance and Savings Plan usage directly from invoice line items, even without Azure API access
- **Disclaimer** — All analysis output (app UI and Excel) includes a disclaimer that savings estimates are for demonstrative purposes only

## Typical flow
1. Sign in to Azure from the sidebar.
2. Analyze an invoice-detail CSV or Excel file (local export).
3. Fetch Azure enrichment (RI, SP, Advisor recommendations).
4. Generate and download Excel workbook.

## Output workbook sheets
- `Executive Summary` — High-level KPIs and period overview
- `Optimization` — Cost optimization recommendations from Azure Advisor
- `Strategy Comparison` — Side-by-side RI vs SP vs Hybrid savings strategies
- `Savings – RI Only` / `Savings – SP Only` / `Savings – Hybrid` — Detailed savings analysis per strategy
- `cost_by_service_family` — Spend breakdown by Azure service family
- `cost_by_subscription` — Spend breakdown by subscription
- `cost_by_charge_type` — Spend breakdown by charge type
- `cost_by_day` — Daily spend trend
- `Commitments` — Current RI and SP inventory
- `Advisor` — Full Advisor recommendations list
- `AzureCostQuery` — Cost Management API data (if available)

## Notes for very large exports (10 GB+)
- The tiered conversion strategy handles files up to 8 GB by default.
- For files beyond that, adjust `AZURE_MAX_EXCEL_MB` upward.
- For production at very large scale, consider pre-converting to CSV or using staged aggregates in Fabric/Spark and pointing the app to summarised datasets.
