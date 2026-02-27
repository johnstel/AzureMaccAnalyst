# Changelog

All notable changes to Azure MACC Analyst are documented here.

---

## [1.1.3] — 2026-02-26

### Added
- **Retail pricing lookup** — Real RI/SP pricing from the Azure Retail Prices API with proper term-to-hourly normalisation (3-year RI prices divided by 26,280 hours)
- **SKU name retry logic** — Automatic SKU name variations (`D4s v5` ↔ `Standard_D4s_v5`) when querying retail prices
- **CSV RI detection** — Identify existing Reserved Instance and Savings Plan usage directly from invoice line items without Azure API access (`csv_ri_detection.py`)
- **Disclaimer** — All analysis output (app sidebar, page footer, and Excel sheets) includes a disclaimer stating estimates are for demonstrative purposes only with no guarantees
- **CHANGELOG.md** — This file
- **Comprehensive RBAC documentation** — All READMEs updated with required Azure roles (Reader, Cost Management Reader, Reservations Reader, Savings Plan Reader) and their scopes

### Fixed
- **Retail pricing unit mismatch** — Azure Retail Prices API returns RI/SP prices as total-cost-for-term (e.g., $20,292 for 3 years) but labels them `unitOfMeasure: "1 Hour"`. The tool now normalises these to actual hourly rates before comparing against PAYG prices.
- **Savings percentage calculation** — `analysis.py` now converts retail pricing percentages (e.g., `41.0`) to decimal fractions (`0.41`) before use in calculations
- **RBAC/authorisation failures are non-fatal** — 403 and other auth errors on individual subscriptions no longer crash the enrichment; the tool continues with remaining subscriptions and reports warnings
- **Savings analysis crash on mixed None/string MeterCategory** — Fixed `TypeError` when invoice data contains null meter categories

### Changed
- Excel workbook sheet names updated: `Summary` → `Executive Summary`, added `Strategy Comparison` and per-strategy savings sheets (`Savings – RI Only`, `Savings – SP Only`, `Savings – Hybrid`)

---

## [1.1.2] — 2026-02-25

### Fixed
- RBAC/authorisation failures are now non-fatal with explicit user warnings
- Continue processing other subscriptions after RBAC failures
- Fix savings analysis crash caused by mixed None/string MeterCategory values
- Includes all v1.1.1 improvements

---

## [1.1.1] — 2026-02-24

### Fixed
- Excel date parsing improvements for various invoice export formats
- Performance improvements for large file processing

---

## [1.1.0] — 2026-02-20

### Added
- Azure Advisor cost recommendations integration
- Savings Plans inventory listing
- Cost Management query per subscription
- Parallel Azure API calls with adaptive worker sizing and early-exit pagination
- Adaptive throttling for Azure API rate limits

---

## [1.0.0] — 2026-02-15

### Added
- Initial release
- Invoice analysis (CSV and Excel) with Polars lazy scan
- Azure sign-in via Microsoft Entra interactive browser authentication
- Reserved Instances inventory listing
- Excel workbook export with branded sheets
- Tiered large Excel file conversion (fastexcel → openpyxl streaming → CSV)
- PyInstaller desktop EXE packaging
