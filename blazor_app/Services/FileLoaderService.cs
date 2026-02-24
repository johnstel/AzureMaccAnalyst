using System.Globalization;
using System.Text.RegularExpressions;
using AzureMaccAnalyst.Models;
using ClosedXML.Excel;
using CsvHelper;
using CsvHelper.Configuration;
using Microsoft.Extensions.Logging;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Loads Azure invoice exports (CSV / Excel), normalises column names to
/// PascalCase, and provides a Cost fallback chain — ported from the Python
/// <c>file_loader.py</c>.
/// </summary>
public sealed class FileLoaderService
{
    private readonly ILogger<FileLoaderService> _logger;

    public FileLoaderService(ILogger<FileLoaderService> logger) => _logger = logger;

    // ── Column-name mapping: lowered key → PascalCase canonical name ──────────
    private static readonly Dictionary<string, string> CamelToPascal = new(StringComparer.OrdinalIgnoreCase)
    {
        ["invoiceid"]              = "InvoiceId",
        ["billingaccountid"]       = "BillingAccountId",
        ["billingaccountname"]     = "BillingAccountName",
        ["billingprofileid"]       = "BillingProfileId",
        ["billingprofilename"]     = "BillingProfileName",
        ["invoicesectionid"]       = "InvoiceSectionId",
        ["invoicesectionname"]     = "InvoiceSectionName",
        ["date"]                   = "Date",
        ["quantity"]               = "Quantity",
        ["billingcurrency"]        = "BillingCurrency",
        ["subscriptionid"]         = "SubscriptionId",
        ["subscriptionname"]       = "SubscriptionName",
        ["servicefamily"]          = "ServiceFamily",
        ["metercategory"]          = "MeterCategory",
        ["metersubcategory"]       = "MeterSubcategory",
        ["meterregion"]            = "MeterRegion",
        ["meterid"]                = "MeterId",
        ["metername"]              = "MeterName",
        ["consumedservice"]        = "ConsumedService",
        ["product"]                = "Product",
        ["productorderid"]         = "ProductOrderId",
        ["productordername"]       = "ProductOrderName",
        ["pricingmodel"]           = "PricingModel",
        ["chargetype"]             = "ChargeType",
        ["resourcegroup"]          = "ResourceGroup",
        ["resourcegroupname"]      = "ResourceGroup",
        ["resourceid"]             = "ResourceId",
        ["resourcelocation"]       = "ResourceLocation",
        ["location"]               = "Location",
        ["reservationid"]          = "ReservationId",
        ["reservationname"]        = "ReservationName",
        ["term"]                   = "Term",
        ["publishertype"]          = "PublisherType",
        ["publisherid"]            = "PublisherId",
        ["publishername"]          = "PublisherName",
        ["effectiveprice"]         = "EffectivePrice",
        ["unitofmeasure"]          = "UnitOfMeasure",
        ["frequency"]              = "Frequency",
        ["unitprice"]              = "UnitPrice",
        ["paygprice"]              = "PayGPrice",
        ["provider"]               = "Provider",
        ["benefitid"]              = "BenefitId",
        ["benefitname"]            = "BenefitName",
        ["tags"]                   = "Tags",
        ["additionalinfo"]         = "AdditionalInfo",
        ["serviceinfo1"]           = "ServiceInfo1",
        ["serviceinfo2"]           = "ServiceInfo2",
        ["costcenter"]             = "CostCenter",
        ["isazurecrediteligible"]  = "IsAzureCreditEligible",
        ["costallocationrulename"] = "CostAllocationRuleName",
        ["cost"]                   = "Cost",
        ["costinbillingcurrency"]  = "CostInBillingCurrency",
        ["costinpricingcurrency"]  = "CostInPricingCurrency",
        ["costinusd"]              = "CostInUsd",
        ["paygcostinbillingcurrency"] = "PayGCostInBillingCurrency",
        ["paygcostinusd"]          = "PayGCostInUsd",
        ["exchangeratepricingtobilling"] = "ExchangeRatePricingToBilling",
    };

    private static readonly string[] CostFallbacks = ["CostInBillingCurrency", "CostInUsd", "CostInPricingCurrency"];
    private static readonly HashSet<string> ExcelExtensions = new(StringComparer.OrdinalIgnoreCase) { ".xlsx", ".xls", ".xlsm", ".xlsb" };
    private static readonly HashSet<string> CsvExtensions = new(StringComparer.OrdinalIgnoreCase) { ".csv", ".tsv", ".txt" };

    private static readonly Regex CellRefSuffix = new(@"[A-Z]+\d+$", RegexOptions.Compiled);

    // ── Public entry point ────────────────────────────────────────────────────

    /// <summary>
    /// Load an Azure invoice file (CSV or Excel) and return a list of
    /// <see cref="InvoiceRecord"/>s with normalised column names.
    /// </summary>
    public List<InvoiceRecord> LoadInvoiceFile(string filePath)
    {
        if (!File.Exists(filePath))
            throw new FileNotFoundException($"File not found: {filePath}");

        var ext = Path.GetExtension(filePath);
        var sizeMb = new FileInfo(filePath).Length / (1024.0 * 1024.0);
        _logger.LogInformation("Loading invoice file: {Path} (format: {Ext}, size: {Size:F2} MB)", filePath, ext, sizeMb);

        var (headers, rows) = ExcelExtensions.Contains(ext)
            ? ReadExcel(filePath)
            : ReadCsv(filePath);

        // Build normalised header map
        var headerMap = NormaliseHeaders(headers);

        // Map rows → InvoiceRecord list
        var records = new List<InvoiceRecord>(rows.Count);
        foreach (var row in rows)
        {
            var rec = MapRowToRecord(headerMap, row);
            records.Add(rec);
        }

        _logger.LogInformation("Loaded {Count} records with {Cols} columns", records.Count, headerMap.Count);
        return records;
    }

    /// <summary>
    /// Load an Azure invoice file from a <see cref="Stream"/> (e.g. from browser upload).
    /// </summary>
    public List<InvoiceRecord> LoadInvoiceFile(Stream stream, string fileName)
    {
        var ext = Path.GetExtension(fileName);
        _logger.LogInformation("Loading invoice stream: {Name} (format: {Ext})", fileName, ext);

        var (headers, rows) = ExcelExtensions.Contains(ext)
            ? ReadExcelFromStream(stream)
            : ReadCsvFromStream(stream);

        var headerMap = NormaliseHeaders(headers);
        var records = new List<InvoiceRecord>(rows.Count);
        foreach (var row in rows)
            records.Add(MapRowToRecord(headerMap, row));

        _logger.LogInformation("Loaded {Count} records with {Cols} columns", records.Count, headerMap.Count);
        return records;
    }

    // ── Column normalisation ──────────────────────────────────────────────────

    private static string NormaliseColumnName(string raw)
    {
        var cleaned = raw.Trim();
        if (cleaned.Contains(':'))
            cleaned = cleaned.Split(':')[0];

        // Strip trailing cell-ref junk (e.g. "invoiceIdA1" → "invoiceId")
        cleaned = CellRefSuffix.Replace(cleaned, "");
        if (string.IsNullOrEmpty(cleaned))
            cleaned = raw.Trim();

        return CamelToPascal.TryGetValue(cleaned.ToLowerInvariant(), out var mapped)
            ? mapped
            : cleaned;
    }

    /// <summary>
    /// Build a mapping: column index → normalised name. Duplicate-safe.
    /// </summary>
    private static Dictionary<int, string> NormaliseHeaders(List<string> headers)
    {
        var map = new Dictionary<int, string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (int i = 0; i < headers.Count; i++)
        {
            var norm = NormaliseColumnName(headers[i]);
            if (seen.Contains(norm))
                continue; // skip duplicates
            map[i] = norm;
            seen.Add(norm);
        }

        // Cost fallback: if no "Cost" column, alias the best available alternative
        if (!seen.Contains("Cost"))
        {
            foreach (var fb in CostFallbacks)
            {
                var idx = map.FirstOrDefault(kv => kv.Value == fb).Key;
                if (map.ContainsValue(fb))
                {
                    // Add a virtual "Cost" mapping to the same index
                    // We'll handle this in MapRowToRecord
                    break;
                }
            }
        }

        return map;
    }

    // ── Row → InvoiceRecord mapping ───────────────────────────────────────────

    private InvoiceRecord MapRowToRecord(Dictionary<int, string> headerMap, List<string> row)
    {
        var dict = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (idx, name) in headerMap)
        {
            if (idx < row.Count)
                dict[name] = row[idx];
        }

        // Cost fallback
        if (!dict.ContainsKey("Cost") || string.IsNullOrWhiteSpace(dict["Cost"]))
        {
            foreach (var fb in CostFallbacks)
            {
                if (dict.TryGetValue(fb, out var val) && !string.IsNullOrWhiteSpace(val))
                {
                    dict["Cost"] = val;
                    break;
                }
            }
        }

        return new InvoiceRecord
        {
            InvoiceId        = dict.GetValueOrDefault("InvoiceId", ""),
            Date             = ParseDate(dict.GetValueOrDefault("Date", "")),
            Cost             = ParseDouble(dict.GetValueOrDefault("Cost", "")),
            Quantity         = ParseDouble(dict.GetValueOrDefault("Quantity", "")),
            BillingCurrency  = dict.GetValueOrDefault("BillingCurrency", ""),
            SubscriptionId   = dict.GetValueOrDefault("SubscriptionId", ""),
            SubscriptionName = CoalesceEmpty(dict.GetValueOrDefault("SubscriptionName", ""),
                                             dict.GetValueOrDefault("SubscriptionId", "")),
            ServiceFamily    = CoalesceEmpty(dict.GetValueOrDefault("ServiceFamily", ""),
                                             dict.GetValueOrDefault("MeterCategory", "")),
            MeterCategory    = dict.GetValueOrDefault("MeterCategory", ""),
            MeterSubcategory = dict.GetValueOrDefault("MeterSubcategory", ""),
            MeterRegion      = dict.GetValueOrDefault("MeterRegion", ""),
            MeterId          = dict.GetValueOrDefault("MeterId", ""),
            MeterName        = dict.GetValueOrDefault("MeterName", ""),
            ConsumedService  = dict.GetValueOrDefault("ConsumedService", ""),
            Product          = dict.GetValueOrDefault("Product", ""),
            PricingModel     = dict.GetValueOrDefault("PricingModel", ""),
            ChargeType       = dict.GetValueOrDefault("ChargeType", ""),
            ResourceGroup    = dict.GetValueOrDefault("ResourceGroup", ""),
            ResourceId       = dict.GetValueOrDefault("ResourceId", ""),
            ReservationId    = dict.GetValueOrDefault("ReservationId", ""),
            ReservationName  = dict.GetValueOrDefault("ReservationName", ""),
            Term             = dict.GetValueOrDefault("Term", ""),
            ResourceLocation = dict.GetValueOrDefault("ResourceLocation", ""),
            Location         = dict.GetValueOrDefault("Location", ""),
        };
    }

    // ── CSV reading ───────────────────────────────────────────────────────────

    private (List<string> Headers, List<List<string>> Rows) ReadCsv(string filePath)
    {
        using var reader = new StreamReader(filePath);
        return ReadCsvCore(reader);
    }

    private (List<string> Headers, List<List<string>> Rows) ReadCsvFromStream(Stream stream)
    {
        using var reader = new StreamReader(stream);
        return ReadCsvCore(reader);
    }

    private static (List<string> Headers, List<List<string>> Rows) ReadCsvCore(StreamReader reader)
    {
        var config = new CsvConfiguration(CultureInfo.InvariantCulture)
        {
            HasHeaderRecord = true,
            BadDataFound = null,
            MissingFieldFound = null,
        };

        using var csv = new CsvReader(reader, config);
        csv.Read();
        csv.ReadHeader();
        var headers = csv.HeaderRecord?.ToList() ?? new List<string>();

        var rows = new List<List<string>>();
        while (csv.Read())
        {
            var row = new List<string>(headers.Count);
            for (int i = 0; i < headers.Count; i++)
                row.Add(csv.GetField(i) ?? "");
            rows.Add(row);
        }

        return (headers, rows);
    }

    // ── Excel reading ─────────────────────────────────────────────────────────

    private (List<string> Headers, List<List<string>> Rows) ReadExcel(string filePath)
    {
        using var stream = File.OpenRead(filePath);
        return ReadExcelFromStream(stream);
    }

    private (List<string> Headers, List<List<string>> Rows) ReadExcelFromStream(Stream stream)
    {
        using var wb = new XLWorkbook(stream);
        var ws = wb.Worksheets.First();

        var usedRange = ws.RangeUsed();
        if (usedRange is null)
            throw new InvalidOperationException("Excel file has no data.");

        var firstRow = usedRange.FirstRow().RowNumber();
        var lastRow = usedRange.LastRow().RowNumber();
        var lastCol = usedRange.LastColumn().ColumnNumber();

        // Headers from first row
        var headers = new List<string>(lastCol);
        for (int c = 1; c <= lastCol; c++)
        {
            var val = ws.Cell(firstRow, c).GetString().Trim();
            headers.Add(string.IsNullOrEmpty(val) ? $"_col{c}" : val);
        }

        // Data rows
        var rows = new List<List<string>>(lastRow - firstRow);
        for (int r = firstRow + 1; r <= lastRow; r++)
        {
            var row = new List<string>(lastCol);
            for (int c = 1; c <= lastCol; c++)
            {
                var cell = ws.Cell(r, c);
                row.Add(CellToString(cell));
            }
            rows.Add(row);
        }

        return (headers, rows);
    }

    /// <summary>
    /// Convert an Excel cell to a string, preserving full datetime fidelity.
    /// </summary>
    private static string CellToString(IXLCell cell)
    {
        if (cell.IsEmpty()) return "";

        var dataType = cell.DataType;
        if (dataType == XLDataType.DateTime)
        {
            var dt = cell.GetDateTime();
            return dt.TimeOfDay == TimeSpan.Zero
                ? dt.ToString("yyyy-MM-dd")
                : dt.ToString("yyyy-MM-ddTHH:mm:ss");
        }

        return cell.GetString();
    }

    // ── Parsing helpers ───────────────────────────────────────────────────────

    private static readonly string[] DateFormats = [
        "yyyy-MM-ddTHH:mm:ss",
        "yyyy-MM-dd HH:mm:ss",
        "yyyy-MM-dd",
        "M/d/yyyy",
        "M/d/yyyy H:mm:ss",
        "MM/dd/yyyy",
        "dd/MM/yyyy",
    ];

    private static DateTime? ParseDate(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        if (DateTime.TryParseExact(value, DateFormats, CultureInfo.InvariantCulture, DateTimeStyles.None, out var dt))
            return dt;
        if (DateTime.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.None, out dt))
            return dt;
        return null;
    }

    private static double ParseDouble(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return 0.0;
        return double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out var d) ? d : 0.0;
    }

    private static string CoalesceEmpty(string primary, string fallback)
        => string.IsNullOrWhiteSpace(primary) ? fallback : primary;
}
