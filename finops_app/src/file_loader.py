"""Unified file loader for Azure invoice exports (CSV and Excel).

Detects file format, reads data into a Polars LazyFrame, and normalises
column names so the rest of the application can work with a single
canonical PascalCase schema regardless of the source format.

Large-file strategy
───────────────────
• CSV  – ``pl.scan_csv`` returns a truly lazy frame; Polars streams chunks
  off disk and never loads the full file into memory.
• Excel – Three conversion tiers are attempted in order:
    1. **fastexcel (calamine)** — Rust-based parser, 10-50× faster than
       openpyxl.  Used automatically when ``python-fastexcel`` is installed
       *and* estimated in-memory size fits comfortably in RAM.  Can be
       disabled via ``AZURE_EXCEL_FAST_PATH=false``.
    2. **openpyxl streaming** — Pure-Python row-by-row reader
       (``read_only=True``).  Constant ~50 MB peak memory regardless of
       file size.  Always available as the safe fallback.
  The intermediate file is written as **Parquet** (Snappy-compressed) when
  ``pyarrow`` is available, otherwise as plain CSV.  Parquet is ~3-5×
  smaller and much faster for ``pl.scan_parquet()`` on subsequent reads.
• Progress callback — both conversion paths accept an optional
  ``progress_callback(rows_done, total_rows_estimate)`` so the UI can
  show a progress bar.
"""
from __future__ import annotations

import atexit
import csv
import datetime
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

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


def _use_fast_path() -> bool:
    """Return True if the fastexcel/calamine fast-path is enabled (default: true)."""
    raw = os.getenv("AZURE_EXCEL_FAST_PATH", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _has_fastexcel() -> bool:
    """Return True if the ``fastexcel`` package is importable."""
    try:
        import fastexcel as _fe  # noqa: F401
        return True
    except ImportError:
        return False


def _has_pyarrow() -> bool:
    """Return True if ``pyarrow`` is importable (needed for Parquet output)."""
    try:
        import pyarrow as _pa  # noqa: F401
        return True
    except ImportError:
        return False


# Type alias for progress callbacks: (rows_done, total_rows_estimate) -> None
ProgressCallback = Callable[[int, int], None]


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


def _stream_excel_to_csv(
    file_path: str,
    csv_path: str,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[int, float]:
    """Stream an Excel workbook into CSV via openpyxl (constant-memory).

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

    # openpyxl exposes max_row in read_only mode (may be approximate)
    total_estimate = ws.max_row or 0
    if total_estimate > 1:
        total_estimate -= 1  # exclude header row

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
            if progress and row_count % 50_000 == 0:
                progress(row_count, max(total_estimate, row_count))

    wb.close()
    if headers is None:
        raise ValueError(f"Excel file is empty: {file_path}")

    # Final progress update
    if progress:
        progress(row_count, row_count)

    return row_count, (_time.perf_counter() - t0)


def _convert_via_fastexcel(
    file_path: str,
    output_path: str,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[int, float]:
    """Fast-path: use calamine (via fastexcel) to read Excel, write Parquet or CSV.

    fastexcel reads the entire sheet into memory but does so in Rust, which
    is 10-50× faster than openpyxl's pure-Python XML parser.

    Returns ``(row_count, elapsed_seconds)``.
    """
    import time as _time
    import fastexcel

    t0 = _time.perf_counter()
    excel_file = fastexcel.read_excel(file_path)
    sheet = excel_file.load_sheet_by_idx(0)
    # fastexcel returns an Arrow-backed table; convert to Polars DataFrame
    df = pl.from_arrow(sheet.to_arrow())
    row_count = len(df)

    if progress:
        progress(row_count, row_count)

    if output_path.endswith(".parquet"):
        df.write_parquet(output_path, compression="snappy")
    else:
        df.write_csv(output_path)

    return row_count, (_time.perf_counter() - t0)


def _csv_to_parquet(csv_path: str, parquet_path: str) -> float:
    """Convert a CSV file to Parquet using Polars streaming.

    Returns the parquet file size in MB.
    """
    df = pl.scan_csv(csv_path, infer_schema_length=5000, ignore_errors=True).collect(streaming=True)
    df.write_parquet(parquet_path, compression="snappy")
    return os.path.getsize(parquet_path) / (1024 * 1024)


def _choose_intermediate_ext() -> str:
    """Return '.parquet' if pyarrow is available, else '.csv'."""
    return ".parquet" if _has_pyarrow() else ".csv"


def convert_excel_to_csv(
    file_path: str,
    output_path: str | None = None,
    *,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> str:
    """Convert an Excel file to an optimised intermediate format and return its path.

    Conversion strategy (in order of preference):
      1. **fastexcel** → Parquet  (fastest, needs ~2-4 GB RAM for a 5 GB xlsx)
      2. **openpyxl streaming** → CSV → Parquet  (constant memory, slower)
      3. **openpyxl streaming** → CSV  (fallback if pyarrow unavailable)

    By default the output is created next to the source workbook as
    ``<stem>.converted.parquet`` (or ``.converted.csv``).  If the output
    already exists and is newer than the source, it is reused.
    """
    source = Path(file_path)
    if source.suffix.lower() not in _EXCEL_EXTENSIONS:
        raise ValueError(f"Expected an Excel file ({', '.join(sorted(_EXCEL_EXTENSIONS))}): {file_path}")

    intermediate_ext = _choose_intermediate_ext()

    if output_path:
        target = Path(output_path)
    else:
        target = source.with_suffix(f".converted{intermediate_ext}")

    # Reuse cached conversion if still valid
    if target.exists() and not overwrite:
        try:
            if target.stat().st_size > 0 and target.stat().st_mtime >= source.stat().st_mtime:
                logger.info("Reusing existing converted file: %s", str(target))
                return str(target)
        except OSError:
            pass

    source_mb = source.stat().st_size / (1024 * 1024)

    # ── Tier 1: fastexcel (calamine) fast-path ─────────────────────────────
    if _use_fast_path() and _has_fastexcel():
        # Heuristic: an xlsx uncompresses to ~3-5× its file size in memory.
        # Allow fast-path if estimated memory is below 80% of available RAM
        # or unconditionally for files under 1 GB compressed.
        estimated_mem_gb = (source_mb * 4) / 1024
        avail_mem_gb = _available_memory_gb()
        if source_mb < 1024 or (avail_mem_gb > 0 and estimated_mem_gb < avail_mem_gb * 0.8):
            logger.info(
                "Using fastexcel (calamine) fast-path for %.1f MB xlsx "
                "(est. %.1f GB RAM, %.1f GB available)",
                source_mb, estimated_mem_gb, avail_mem_gb,
            )
            try:
                row_count, elapsed = _convert_via_fastexcel(
                    str(source), str(target), progress=progress,
                )
                size_mb = target.stat().st_size / (1024 * 1024)
                logger.info(
                    "fastexcel conversion complete in %.1fs: %d rows, %.1f MB %s (%s)",
                    elapsed, row_count, size_mb, target.suffix, str(target),
                )
                return str(target)
            except Exception as ex:
                logger.warning("fastexcel fast-path failed, falling back to openpyxl: %s", ex)
        else:
            logger.info(
                "Skipping fastexcel fast-path: file is %.1f MB "
                "(est. %.1f GB RAM needed, %.1f GB available). Using streaming.",
                source_mb, estimated_mem_gb, avail_mem_gb,
            )

    # ── Tier 2: openpyxl streaming → CSV (→ optional Parquet) ──────────────
    csv_target = source.with_suffix(".converted.csv")
    logger.info("Converting Excel to CSV (streaming): %s -> %s", str(source), str(csv_target))
    row_count, elapsed = _stream_excel_to_csv(str(source), str(csv_target), progress=progress)
    csv_size_mb = csv_target.stat().st_size / (1024 * 1024)
    logger.info(
        "openpyxl conversion complete in %.1fs: %d rows, %.1f MB CSV (%s)",
        elapsed, row_count, csv_size_mb, str(csv_target),
    )

    # Upgrade CSV → Parquet if pyarrow available
    if intermediate_ext == ".parquet":
        parquet_target = source.with_suffix(".converted.parquet")
        logger.info("Upgrading CSV → Parquet: %s", str(parquet_target))
        try:
            pq_mb = _csv_to_parquet(str(csv_target), str(parquet_target))
            logger.info("Parquet written: %.1f MB (%.1f× vs CSV)", pq_mb, csv_size_mb / max(pq_mb, 0.01))
            # Remove the intermediate CSV to save disk space
            try:
                csv_target.unlink()
                logger.debug("Removed intermediate CSV: %s", str(csv_target))
            except OSError:
                pass
            return str(parquet_target)
        except Exception as ex:
            logger.warning("Parquet upgrade failed, keeping CSV: %s", ex)

    return str(csv_target)


def _available_memory_gb() -> float:
    """Best-effort estimate of available system memory in GB."""
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullAvailPhys / (1024 ** 3)
        else:
            avail = shutil.disk_usage("/").free  # rough proxy on Linux/Mac
            # Try /proc/meminfo first
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 * 1024)  # kB → GB
            return avail / (1024 ** 3)
    except Exception:
        return 0.0  # unknown → skip fast-path guard


def _load_excel(file_path: str, progress: ProgressCallback | None = None) -> pl.LazyFrame:
    """Stream an Excel file to a temporary intermediate, then return a lazy scan.

    This keeps peak memory low even for multi-GB workbooks:
      1. Conversion to an intermediate file (Parquet or CSV) via the tiered
         strategy in ``convert_excel_to_csv``.
      2. The intermediate is handed to ``pl.scan_parquet`` or ``pl.scan_csv``
         for lazy / streaming reads.
      3. The temp file is deleted when the process exits (via ``atexit``).
    """
    logger.info("Streaming Excel → intermediate: %s", file_path)

    intermediate_ext = _choose_intermediate_ext()

    # Create a temp file next to the original so it's on the same volume
    parent = Path(file_path).parent
    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=intermediate_ext, prefix=".xlconv_", dir=str(parent),
        )
    except OSError:
        fd, tmp_path = tempfile.mkstemp(suffix=intermediate_ext, prefix=".xlconv_")
    os.close(fd)
    _register_temp(tmp_path)

    # Delegate to the tiered conversion function
    result_path = convert_excel_to_csv(
        file_path, output_path=tmp_path, overwrite=True, progress=progress,
    )
    _register_temp(result_path)  # ensure cleanup even if path changed

    result_size_mb = os.path.getsize(result_path) / (1024 * 1024)
    logger.info(
        "Excel → %s conversion done: %.1f MB intermediate file (%s)",
        Path(result_path).suffix, result_size_mb, result_path,
    )

    # Scan the intermediate file lazily
    if result_path.endswith(".parquet"):
        return pl.scan_parquet(result_path)
    return pl.scan_csv(result_path, infer_schema_length=5000, ignore_errors=True)


def existing_columns(lf: pl.LazyFrame) -> set[str]:
    """Return the set of column names in a LazyFrame."""
    return set(lf.collect_schema().names())


def safe_datetime_expr(col_name: str, lf: pl.LazyFrame) -> pl.Expr:
    """Return a Polars expression that casts *col_name* to Datetime.

    The full timestamp is preserved so that downstream pivots can use any
    time granularity (hour, day, month, etc.).

    * Already Datetime → returned as-is.
    * Date → cast to Datetime (midnight).
    * String → parsed with multiple common timestamp/date formats.
    """
    dtype = lf.collect_schema()[col_name]
    if dtype == pl.Date:
        return pl.col(col_name).cast(pl.Datetime("us"), strict=False)
    if dtype in (pl.Datetime, pl.Datetime("ms"), pl.Datetime("us"), pl.Datetime("ns")):
        return pl.col(col_name)
    if str(dtype).startswith("Datetime"):
        return pl.col(col_name)
    # String column — try broad inference first, then common explicit formats.
    raw = pl.col(col_name).cast(pl.Utf8, strict=False).str.strip_chars()
    return (
        raw.str.to_datetime(strict=False)
        .fill_null(raw.str.to_datetime("%Y-%m-%dT%H:%M:%S", strict=False))
        .fill_null(raw.str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False))
        .fill_null(raw.str.to_datetime("%Y-%m-%d", strict=False))
        .fill_null(raw.str.to_datetime("%m/%d/%Y %H:%M:%S", strict=False))
        .fill_null(raw.str.to_datetime("%m/%d/%Y %H:%M", strict=False))
        .fill_null(raw.str.to_datetime("%m/%d/%Y %I:%M:%S %p", strict=False))
        .fill_null(raw.str.to_datetime("%m/%d/%Y", strict=False))
        .fill_null(raw.str.to_datetime("%d/%m/%Y %H:%M:%S", strict=False))
        .fill_null(raw.str.to_datetime("%d/%m/%Y %H:%M", strict=False))
        .fill_null(raw.str.to_datetime("%d/%m/%Y", strict=False))
        .fill_null(raw.str.to_datetime("%m-%d-%Y", strict=False))
        .fill_null(raw.str.to_datetime("%d-%m-%Y", strict=False))
    )
