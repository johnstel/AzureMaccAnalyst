"""Unified file loader for Azure invoice exports (CSV and Excel).

Detects file format, reads data into a Polars LazyFrame, and normalises
column names so the rest of the application can work with a single
canonical PascalCase schema regardless of the source format.

Large-file strategy
───────────────────
• CSV  – ``pl.scan_csv`` returns a truly lazy frame; Polars streams chunks
  off disk and never loads the full file into memory.
• Excel – openpyxl's *read_only* mode streams rows one at a time.  We pipe
  them into a temporary CSV file on disk, then hand that file to
  ``pl.scan_csv`` so the downstream pipeline is equally lazy / streaming.
  Peak memory is just the CSV write-buffer (a few KB), not the whole sheet.
"""
from __future__ import annotations

import atexit
import csv
import datetime
import os
import tempfile
from pathlib import Path
from typing import Any

import polars as pl

from .logging_config import get_logger

logger = get_logger(__name__)

# ── Column-name normalisation ──────────────────────────────────────────────────
# Azure exports ship in at least two flavours:
#   • Cost Management CSV  → PascalCase (e.g. "SubscriptionName", "Cost")
#   • MCA / Partner portal → camelCase  (e.g. "subscriptionName", "costInBillingCurrency")
#
# We map *everything* to the PascalCase names the app already uses.

# Step 1: camelCase → PascalCase direct equivalents
_CAMEL_TO_PASCAL: dict[str, str] = {
    "invoiceid":              "InvoiceId",
    "billingaccountid":       "BillingAccountId",
    "billingaccountname":     "BillingAccountName",
    "billingprofileid":       "BillingProfileId",
    "billingprofilename":     "BillingProfileName",
    "invoicesectionid":       "InvoiceSectionId",
    "invoicesectionname":     "InvoiceSectionName",
    "date":                   "Date",
    "quantity":               "Quantity",
    "billingcurrency":        "BillingCurrency",
    "subscriptionid":         "SubscriptionId",
    "subscriptionname":       "SubscriptionName",
    "servicefamily":          "ServiceFamily",
    "metercategory":          "MeterCategory",
    "metersubcategory":       "MeterSubcategory",
    "meterregion":            "MeterRegion",
    "meterid":                "MeterId",
    "metername":              "MeterName",
    "consumedservice":        "ConsumedService",
    "product":                "Product",
    "productorderid":         "ProductOrderId",
    "productordername":       "ProductOrderName",
    "pricingmodel":           "PricingModel",
    "chargetype":             "ChargeType",
    "resourcegroup":          "ResourceGroup",
    "resourcegroupname":      "ResourceGroup",   # alternate name → same target
    "resourceid":             "ResourceId",
    "resourcelocation":       "ResourceLocation",
    "location":               "Location",
    "reservationid":          "ReservationId",
    "reservationname":        "ReservationName",
    "term":                   "Term",
    "publishertype":          "PublisherType",
    "publisherid":            "PublisherId",
    "publishername":          "PublisherName",
    "effectiveprice":         "EffectivePrice",
    "unitofmeasure":          "UnitOfMeasure",
    "frequency":              "Frequency",
    "unitprice":              "UnitPrice",
    "paygprice":              "PayGPrice",
    "provider":               "Provider",
    "benefitid":              "BenefitId",
    "benefitname":            "BenefitName",
    "tags":                   "Tags",
    "additionalinfo":         "AdditionalInfo",
    "serviceinfo1":           "ServiceInfo1",
    "serviceinfo2":           "ServiceInfo2",
    "costcenter":             "CostCenter",
    "isazurecrediteligible":  "IsAzureCreditEligible",
    "costallocationrulename": "CostAllocationRuleName",
    # Cost columns (see Step 2 below for fallback logic)
    "cost":                   "Cost",
    "costinbillingcurrency":  "CostInBillingCurrency",
    "costinpricingcurrency":  "CostInPricingCurrency",
    "costinusd":              "CostInUsd",
    "paygcostinbillingcurrency": "PayGCostInBillingCurrency",
    "paygcostinusd":          "PayGCostInUsd",
    "exchangeratepricingtobilling": "ExchangeRatePricingToBilling",
}

# Step 2: If there is no "Cost" column after renaming, fall back to one of these
# (first match wins, in priority order).
_COST_FALLBACKS = [
    "CostInBillingCurrency",
    "CostInUsd",
    "CostInPricingCurrency",
]


def _normalise_column_name(raw: str) -> str:
    """Map a raw column header to its canonical PascalCase name.

    Strategy:
      1. Strip whitespace and any cell-reference artefacts (e.g. "invoiceIdA1:BK4").
      2. Lower-case → look up in the mapping dict.
      3. If not found, try treating the raw string as already correct PascalCase.
    """
    cleaned = raw.strip()
    # Some corrupt exports append cell ranges like "invoiceIdA1:BK4"
    if ":" in cleaned:
        cleaned = cleaned.split(":")[0]
    # Strip trailing cell-ref junk: e.g. "invoiceIdA1" → "invoiceId"
    import re
    cleaned = re.sub(r"[A-Z]+\d+$", "", cleaned)  # remove e.g. "A1"
    if not cleaned:
        cleaned = raw.strip()  # fall back to original if nothing left

    key = cleaned.lower()
    return _CAMEL_TO_PASCAL.get(key, cleaned)


def _normalise_columns(columns: list[str]) -> dict[str, str]:
    """Return a mapping of original → normalised names for a list of columns."""
    mapping: dict[str, str] = {}
    seen: set[str] = set()
    for col in columns:
        norm = _normalise_column_name(col)
        if norm in seen:
            # Duplicate after normalisation — keep the first one
            continue
        mapping[col] = norm
        seen.add(norm)
    return mapping


# ── File loading ───────────────────────────────────────────────────────────────

_EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".xlsb"}
_CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}


def _max_excel_size_mb() -> int:
    try:
        return max(1, int(os.getenv("AZURE_MAX_EXCEL_MB", "8192")))
    except ValueError:
        return 8192


def _block_oversized_excel() -> bool:
    raw = os.getenv("AZURE_BLOCK_OVERSIZED_EXCEL", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def load_invoice_file(file_path: str) -> pl.LazyFrame:
    """Load an Azure invoice export and return a normalised Polars LazyFrame.

    Supports CSV and Excel (.xlsx / .xls) files.  Column names are mapped to
    the canonical PascalCase schema.  If no ``Cost`` column exists, the best
    available alternative (``CostInBillingCurrency`` → ``CostInUsd``) is
    aliased as ``Cost``.
    """
    path = Path(file_path)
    if not path.exists():
        logger.error("File not found: %s", file_path)
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    logger.info("Loading invoice file: %s (format: %s, size: %.2f MB)",
                file_path, ext, path.stat().st_size / (1024 * 1024))

    if ext in _EXCEL_EXTENSIONS:
        size_mb = path.stat().st_size / (1024 * 1024)
        max_excel_mb = _max_excel_size_mb()
        if size_mb > max_excel_mb:
            msg = (
                f"Excel file is {size_mb:,.1f} MB, above configured threshold {max_excel_mb} MB. "
                "Conversion may run CPU-intensive for a while."
            )
            if _block_oversized_excel():
                logger.warning("%s Blocking is enabled. (%s)", msg, file_path)
                raise ValueError(
                    msg
                    + " Oversized Excel blocking is enabled. Set AZURE_BLOCK_OVERSIZED_EXCEL=false to allow conversion."
                )
            logger.warning("%s Continuing conversion. (%s)", msg, file_path)
        lf = _load_excel(file_path)
    elif ext in _CSV_EXTENSIONS or ext == "":
        lf = _load_csv(file_path)
    else:
        # Try CSV as a last resort
        logger.warning("Unknown extension '%s' — attempting CSV parse", ext)
        lf = _load_csv(file_path)

    # ── Normalise column names ─────────────────────────────────────────────
    raw_columns = lf.collect_schema().names()
    col_map = _normalise_columns(raw_columns)
    rename_needed = {k: v for k, v in col_map.items() if k != v}
    if rename_needed:
        logger.debug("Renaming %d columns: %s", len(rename_needed),
                      {k: v for k, v in list(rename_needed.items())[:10]})
        lf = lf.rename(rename_needed)

    # ── Ensure a "Cost" column exists ──────────────────────────────────────
    current_cols = set(lf.collect_schema().names())
    if "Cost" not in current_cols:
        for fallback in _COST_FALLBACKS:
            if fallback in current_cols:
                logger.info("No 'Cost' column found — using '%s' as Cost", fallback)
                lf = lf.with_columns(pl.col(fallback).alias("Cost"))
                break
        else:
            logger.warning("No Cost or fallback cost column found in file")

    final_cols = lf.collect_schema().names()
    logger.info("Loaded %d columns. Canonical columns present: %s",
                len(final_cols),
                [c for c in final_cols if c[0].isupper()][:15])

    return lf


def _load_csv(file_path: str) -> pl.LazyFrame:
    """Load a CSV with Polars lazy scanning (memory-safe for multi-GB files)."""
    return pl.scan_csv(file_path, infer_schema_length=5000, ignore_errors=True)


# ── Temp-file registry (cleaned up on process exit) ───────────────────────────
_TEMP_FILES: list[str] = []


def _register_temp(path: str) -> None:
    """Track a temp file so it can be cleaned up at exit."""
    _TEMP_FILES.append(path)


def _cleanup_temps() -> None:
    for p in _TEMP_FILES:
        try:
            os.remove(p)
        except OSError:
            pass


atexit.register(_cleanup_temps)


def _excel_cell_to_csv(value):
    """Convert an Excel cell value to a CSV-friendly string.

    datetime objects keep their full timestamp (``YYYY-MM-DDTHH:MM:SS``) so
    that downstream pivots can group by any time granularity.  Plain date
    objects are written as ``YYYY-MM-DD``.
    """
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.isoformat()          # e.g. 2026-01-15T14:30:00
    if isinstance(value, datetime.date):
        return value.isoformat()           # e.g. 2026-01-15
    return value


def _stream_excel_to_csv(file_path: str, csv_path: str) -> tuple[int, float]:
    """Stream an Excel workbook into CSV.

    Returns a tuple of ``(row_count, elapsed_seconds)``.
    """
    import time as _time
    from openpyxl import load_workbook

    t0 = _time.perf_counter()
    wb = load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        wb.close()
        raise ValueError(f"Excel file has no active sheet: {file_path}")

    headers: list[str] | None = None
    row_count = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for row in ws.iter_rows(values_only=True):
            if headers is None:
                headers = [str(h) if h is not None else f"_col{i}" for i, h in enumerate(row)]
                writer.writerow(headers)
                continue
            writer.writerow([_excel_cell_to_csv(v) for v in row])
            row_count += 1

    wb.close()
    if headers is None:
        raise ValueError(f"Excel file is empty: {file_path}")

    return row_count, (_time.perf_counter() - t0)


def convert_excel_to_csv(file_path: str, output_path: str | None = None, *, overwrite: bool = False) -> str:
    """Convert an Excel file to CSV and return the CSV path.

    Conversion is streaming and suitable for large workbooks. By default, the
    CSV is created next to the source workbook using ``<stem>.converted.csv``.
    """
    source = Path(file_path)
    if source.suffix.lower() not in _EXCEL_EXTENSIONS:
        raise ValueError(f"Expected an Excel file ({', '.join(sorted(_EXCEL_EXTENSIONS))}): {file_path}")

    target = Path(output_path) if output_path else source.with_suffix(".converted.csv")

    if target.exists() and not overwrite:
        try:
            if target.stat().st_size > 0 and target.stat().st_mtime >= source.stat().st_mtime:
                logger.info("Reusing existing converted CSV: %s", str(target))
                return str(target)
        except OSError:
            pass

    logger.info("Converting Excel to CSV: %s -> %s", str(source), str(target))
    row_count, elapsed = _stream_excel_to_csv(str(source), str(target))
    size_mb = target.stat().st_size / (1024 * 1024)
    logger.info(
        "Excel conversion complete in %.1fs: %d rows, %.1f MB CSV (%s)",
        elapsed,
        row_count,
        size_mb,
        str(target),
    )
    return str(target)


def _load_excel(file_path: str) -> pl.LazyFrame:
    """Stream an Excel file to a temporary CSV, then return a lazy scan.

    This keeps peak memory low even for multi-GB workbooks:
      1. openpyxl ``read_only=True`` streams rows one at a time.
      2. Each row is immediately written to a temp CSV via Python's csv module.
      3. The temp CSV is handed to ``pl.scan_csv`` for lazy / streaming reads.
      4. The temp file is deleted when the process exits (via ``atexit``).
    """
    logger.info("Streaming Excel → temp CSV: %s", file_path)

    # Create a temp CSV next to the original so it's on the same volume
    #   (avoids cross-drive copies).  Falls back to system temp dir.
    parent = Path(file_path).parent
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix=".xlconv_", dir=str(parent))
    except OSError:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix=".xlconv_")
    os.close(fd)
    _register_temp(tmp_path)

    row_count, elapsed = _stream_excel_to_csv(file_path, tmp_path)
    tmp_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
    logger.info(
        "Excel → CSV conversion done in %.1fs: %d data rows, %.1f MB temp file (%s)",
        elapsed, row_count, tmp_size_mb, tmp_path,
    )

    # Now scan the temp CSV lazily — same streaming path as native CSVs
    return pl.scan_csv(tmp_path, infer_schema_length=5000, ignore_errors=True)


def existing_columns(lf: pl.LazyFrame) -> set[str]:
    """Return the set of column names in a LazyFrame."""
    return set(lf.collect_schema().names())


def safe_datetime_expr(col_name: str, lf: pl.LazyFrame) -> pl.Expr:
    """Return a Polars expression that casts *col_name* to Datetime.

    The full timestamp is preserved so that downstream pivots can use any
    time granularity (hour, day, month, etc.).

    * Already Datetime → returned as-is.
    * Date → cast to Datetime (midnight).
    * String → parsed with ISO-8601 (``%Y-%m-%dT%H:%M:%S`` then ``%Y-%m-%d``).
    """
    dtype = lf.collect_schema()[col_name]
    if dtype == pl.Date:
        return pl.col(col_name).cast(pl.Datetime("us"), strict=False)
    if dtype in (pl.Datetime, pl.Datetime("ms"), pl.Datetime("us"), pl.Datetime("ns")):
        return pl.col(col_name)
    if str(dtype).startswith("Datetime"):
        return pl.col(col_name)
    # String column — try full ISO timestamp first, then date-only
    return (
        pl.col(col_name)
        .str.to_datetime("%Y-%m-%dT%H:%M:%S", strict=False)
        .fill_null(
            pl.col(col_name)
            .str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
        )
        .fill_null(
            pl.col(col_name)
            .str.to_datetime("%Y-%m-%d", strict=False)
        )
    )
