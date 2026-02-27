# Azure MACC Analyst

> A desktop tool and agentic workspace for analyzing Azure cost optimization and commitment economics — Reserved Instances, Savings Plans, and Microsoft Azure Consumption Commitment (MACC) scenarios.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## Quick Start — Desktop App (no Python required)

1. **Download** the latest release zip from [`releases/`](releases/).
2. **Extract** the zip to any folder.
3. **Double-click** `AzureMaccAnalyst.exe`.
4. Your browser opens to the dashboard — sign in to Azure and start analyzing.

> **Requirements:** Windows 10/11, internet access, and a Microsoft account with the required Azure RBAC roles (Reader, Cost Management Reader, Reservations Reader, Savings Plan Reader). See [Desktop Distribution Guide](finops_app/README-exe.md) for details.

See [Desktop Distribution Guide](finops_app/README-exe.md) for full details, troubleshooting, and enterprise deployment.

---

## What It Does

| Capability | Description |
|---|---|
| **Invoice Analysis** | Process Azure invoice-detail CSV or Excel exports (large-file aware via Polars lazy scan) |
| **Large Excel Support** | Tiered conversion: fastexcel (Rust, 10–50× faster) → openpyxl streaming → Parquet/CSV intermediate with live progress bar |
| **Azure Enrichment** | Pull current Reserved Instances, Savings Plans, and cost data via Microsoft sign-in |
| **Advisor Recommendations** | Surface Azure Advisor cost recommendations to support future purchase decisions |
| **Retail Pricing** | Real RI/SP pricing from the Azure Retail Prices API with proper term-to-hourly normalisation |
| **CSV RI Detection** | Identify existing RI and SP usage directly from invoice line items |
| **Excel Reporting** | Export a consolidated Excel workbook with summary, optimization, commitments, and savings strategies |
| **Disclaimer** | All estimates clearly marked as demonstrative/approximation — no guarantees |

## Analysis Scenarios

- Current-state spend baseline
- RI-only optimization
- Savings Plan-only optimization
- Hybrid RI + Savings Plan optimization
- MACC-constrained plans (3-year, 5-year)
- Future growth sensitivity (low / base / high)

---

## Project Structure

```
AzureMaccAnalyst/
├── finops_app/          # Streamlit desktop app (Python source)
│   ├── app.py           # Main app entry point
│   ├── src/             # Core modules (auth, analysis, export)
│   ├── build_exe.ps1    # Build script for desktop EXE
│   └── requirements.txt
├── agents/              # Agent definitions and instructions
├── prompts/             # Reusable prompts by domain (FinOps, orchestration)
├── data/                # Source data, assumptions, and exports
├── analysis/            # Baseline and scenario outputs
├── docs/                # Architecture notes and glossary
├── releases/            # Downloadable binary (Git LFS)
└── .github/workflows/   # CI/CD (future)
```

## Running from Source

```bash
cd finops_app
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
streamlit run app.py
```

> **Large Excel files?** The app handles multi-GB `.xlsx` exports automatically — see [Handling Large Excel Files](finops_app/README.md#handling-large-excel-files-1-gb) in the app README for full details. For best performance, ensure `fastexcel` and `pyarrow` are installed (both are in `requirements.txt` by default).

## Building the Desktop EXE

```powershell
cd finops_app
powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Output: dist/AzureMaccAnalyst.zip
```

## Security Model

- **Microsoft Entra** interactive browser authentication — no passwords stored in the app
- Access controlled by **Azure RBAC**:
  - `Reader` + `Cost Management Reader` at Root Management Group scope
  - `Reservations Reader` at tenant/billing scope
  - `Savings Plan Reader` or `Billing Account Reader` at billing account scope
- Optional: set `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` for enterprise app-registration lockdown
- See [finops_app/README.md](finops_app/README.md) for full auth and RBAC details

## License

This project is licensed under the [MIT License](LICENSE).
