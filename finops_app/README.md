# Azure MACC Analyst App (Python)

A finance-friendly Streamlit application to:
- Process Azure invoice-detail CSV exports (large-file aware via Polars lazy scan)
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
3. RBAC roles for signed-in users:
   - `Cost Management Reader` (or equivalent at required scope)
   - Reader access on resources/subscriptions for Advisor visibility

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

## Typical flow
1. Sign in to Azure from the sidebar.
2. Analyze an invoice-detail CSV (local export).
3. Fetch Azure enrichment (RI, SP, Advisor recommendations).
4. Generate and download Excel workbook.

## Output workbook sheets
- `Summary`
- `Optimization`
- `cost_by_service_family`
- `cost_by_subscription`
- `cost_by_charge_type`
- `cost_by_day`
- `Commitments`
- `Advisor`
- `AzureCostQuery` (if available)

## Notes for very large exports (10GB+)
- Current implementation uses Polars lazy scanning to avoid full in-memory loading.
- For production at very large scale, use staged aggregates in Fabric/Spark and point the app to summarized datasets.
