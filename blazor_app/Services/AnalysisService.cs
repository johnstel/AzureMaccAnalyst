using AzureMaccAnalyst.Models;
using Microsoft.Extensions.Logging;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Produces summary statistics and pivot tables from loaded invoice data.
/// Port of the Python <c>analysis.py</c>.
/// </summary>
public sealed class AnalysisService
{
    private readonly ILogger<AnalysisService> _logger;

    public AnalysisService(ILogger<AnalysisService> logger) => _logger = logger;

    /// <summary>
    /// Summarise a list of invoice records into an <see cref="AnalysisSummary"/>
    /// and a set of <see cref="PivotTables"/>.
    /// </summary>
    public (AnalysisSummary Summary, PivotTables Pivots) Summarise(List<InvoiceRecord> records)
    {
        _logger.LogInformation("Summarising {Count} invoice records", records.Count);

        var dates = records.Where(r => r.Date.HasValue).Select(r => r.Date!.Value).ToList();
        var currency = records.FirstOrDefault(r => !string.IsNullOrEmpty(r.BillingCurrency))?.BillingCurrency ?? "USD";

        var summary = new AnalysisSummary
        {
            PeriodStart = dates.Count > 0 ? dates.Min() : null,
            PeriodEnd = dates.Count > 0 ? dates.Max() : null,
            Currency = currency,
            TotalCost = records.Sum(r => r.Cost),
            TotalQuantity = records.Sum(r => r.Quantity),
            RecordCount = records.Count,
        };

        var pivots = BuildPivotTables(records);

        _logger.LogInformation(
            "Summary: period={Start} to {End}, cost={Cost:F2} {Ccy}, records={Count}",
            summary.PeriodStart, summary.PeriodEnd, summary.TotalCost, summary.Currency, summary.RecordCount);

        return (summary, pivots);
    }

    /// <summary>
    /// Build pay-go vs RI/SP savings analysis from invoice data + Advisor recommendations.
    /// </summary>
    public SavingsAnalysis BuildSavingsAnalysis(
        List<InvoiceRecord> records,
        List<AzureRecommendation> recommendations,
        double? augmentedAnnualPayGo = null)
    {
        _logger.LogInformation("Building savings analysis: {RecCount} records, {RecCount2} recommendations",
            records.Count, recommendations.Count);

        // Pricing model breakdown
        var pmGroups = records
            .GroupBy(r => string.IsNullOrEmpty(r.PricingModel) ? "Unknown" : r.PricingModel)
            .Select(g => new PivotRow { Label = g.Key, TotalCost = g.Sum(r => r.Cost), Count = g.Count() })
            .OrderByDescending(r => r.TotalCost)
            .ToList();

        var totalCost = records.Sum(r => r.Cost);
        var payGoLabels = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "OnDemand", "On Demand", "PAYG", "Pay-As-You-Go", "" };
        var payGoTotal = pmGroups.Where(r => payGoLabels.Contains(r.Label)).Sum(r => r.TotalCost);
        if (payGoTotal == 0) payGoTotal = totalCost;

        // Period length
        var dates = records.Where(r => r.Date.HasValue).Select(r => r.Date!.Value).ToList();
        int periodDays;
        if (dates.Count > 1)
        {
            periodDays = Math.Max((int)(dates.Max() - dates.Min()).TotalDays, 1);
        }
        else
        {
            periodDays = 30;
        }

        var annualPayGoFromCsv = payGoTotal / periodDays * 365;
        var annualPayGo = (augmentedAnnualPayGo.HasValue && augmentedAnnualPayGo.Value > annualPayGoFromCsv)
            ? augmentedAnnualPayGo.Value
            : annualPayGoFromCsv;
        var threeYearPayGo = annualPayGo * 3;

        // Advisor recs → savings
        var totalAnnualSavings = recommendations.Sum(r => Math.Max(r.AnnualSavingsEstimate, 0));
        totalAnnualSavings = Math.Min(totalAnnualSavings, annualPayGo);
        var threeYearSavings = totalAnnualSavings * 3;
        var riThreeYearCost = threeYearPayGo - threeYearSavings;
        var savingsPct = threeYearPayGo > 0 ? threeYearSavings / threeYearPayGo * 100 : 0;

        // By category
        var catSavings = new Dictionary<string, double>(StringComparer.Ordinal);
        foreach (var rec in recommendations)
        {
            var cat = GuessCategory(rec, records);
            catSavings[cat] = catSavings.GetValueOrDefault(cat) + Math.Max(rec.AnnualSavingsEstimate, 0);
        }

        var catCosts = records
            .Where(r => string.IsNullOrEmpty(r.PricingModel) || payGoLabels.Contains(r.PricingModel))
            .GroupBy(r => string.IsNullOrEmpty(r.MeterCategory) ? "Other" : r.MeterCategory)
            .ToDictionary(g => g.Key, g => g.Sum(r => r.Cost));

        var allCats = catCosts.Keys.Union(catSavings.Keys).OrderBy(c => c).ToList();
        var savingsByCategory = new List<SavingsCategoryRow>();
        foreach (var cat in allCats)
        {
            var csvPayGoAnnual = catCosts.GetValueOrDefault(cat) / periodDays * 365;
            var annSav = catSavings.GetValueOrDefault(cat);

            double payGoAnnual;
            if (annSav > 0 && csvPayGoAnnual < annSav)
                payGoAnnual = Math.Max(csvPayGoAnnual, annSav / 0.40);
            else
                payGoAnnual = csvPayGoAnnual;

            var payGo3Yr = payGoAnnual * 3;
            var sav3Yr = annSav * 3;
            var riCost = payGo3Yr - sav3Yr;
            var pct = payGo3Yr > 0 ? sav3Yr / payGo3Yr * 100 : 0;
            var pctOfTotal = threeYearSavings > 0 ? sav3Yr / threeYearSavings * 100 : 0;

            savingsByCategory.Add(new SavingsCategoryRow
            {
                ResourceType = cat,
                Current3YrCost = Math.Round(payGo3Yr, 2),
                RiSp3YrCost = Math.Round(riCost, 2),
                NetSavings3Yr = Math.Round(sav3Yr, 2),
                SavingsPercent = Math.Round(pct, 1),
                AnnualSavings = Math.Round(annSav, 2),
                MonthlySavings = Math.Round(annSav / 12, 2),
                PercentOfTotalSavings = Math.Round(pctOfTotal, 1),
            });
        }
        savingsByCategory = savingsByCategory.OrderByDescending(r => r.NetSavings3Yr).ToList();

        // By region
        var savingsByRegion = BuildRegionSavings(records, recommendations, periodDays, threeYearSavings, payGoLabels);

        // Top opportunities
        var topOpps = recommendations
            .OrderByDescending(r => r.AnnualSavingsEstimate)
            .Select((rec, idx) =>
            {
                var ann = Math.Max(rec.AnnualSavingsEstimate, 0);
                var sav3 = ann * 3;
                var savRate = 0.40;
                var match = System.Text.RegularExpressions.Regex.Match(rec.ShortDescription ?? "", @"~(\d+)%");
                if (match.Success && int.TryParse(match.Groups[1].Value, out var parsed))
                    savRate = parsed / 100.0;
                savRate = Math.Max(savRate, 0.01);
                var impliedPayGo3Yr = sav3 > 0 ? sav3 / savRate : 0;
                var impliedRi3Yr = impliedPayGo3Yr - sav3;

                return new TopOpportunityRow
                {
                    Rank = idx + 1,
                    ResourceType = GuessCategory(rec, records),
                    Sku = ExtractFromRaw(rec, "sku") ?? rec.ShortDescription?[..Math.Min(60, rec.ShortDescription.Length)] ?? "",
                    Region = ExtractFromRaw(rec, "region") ?? "—",
                    Current3YrCost = Math.Round(impliedPayGo3Yr, 2),
                    RiSp3YrCost = Math.Round(impliedRi3Yr, 2),
                    NetSavings3Yr = Math.Round(sav3, 2),
                    SavingsPercent = Math.Round(savRate * 100, 1),
                    AnnualSavings = Math.Round(ann, 2),
                    Description = rec.ShortDescription ?? "",
                };
            })
            .ToList();

        return new SavingsAnalysis
        {
            PricingModelSummary = pmGroups,
            PayGoTotal = payGoTotal,
            PeriodDays = periodDays,
            AnnualPayGoRunRate = Math.Round(annualPayGo, 2),
            ThreeYearPayGo = Math.Round(threeYearPayGo, 2),
            SavingsByCategory = savingsByCategory,
            SavingsByRegion = savingsByRegion,
            TopOpportunities = topOpps,
            Kpi = new SavingsKpi
            {
                Total3YrSavings = Math.Round(threeYearSavings, 2),
                SavingsPercent = Math.Round(savingsPct, 1),
                TotalRecommendations = recommendations.Count,
                Current3YrSpend = Math.Round(threeYearPayGo, 2),
                RiSp3YrCost = Math.Round(riThreeYearCost, 2),
                ResourceCategories = savingsByCategory.Count,
                AnnualSavings = Math.Round(totalAnnualSavings, 2),
                MonthlySavings = Math.Round(totalAnnualSavings / 12, 2),
            }
        };
    }

    // ── Pivots ────────────────────────────────────────────────────────────────

    private static PivotTables BuildPivotTables(List<InvoiceRecord> records)
    {
        var tables = new PivotTables();

        tables.CostByServiceFamily = GroupAndSort(records, r => string.IsNullOrEmpty(r.ServiceFamily) ? "Other" : r.ServiceFamily);
        tables.CostBySubscription = GroupAndSort(records, r => string.IsNullOrEmpty(r.SubscriptionName) ? "Other" : r.SubscriptionName);
        tables.CostByChargeType = GroupAndSort(records, r => string.IsNullOrEmpty(r.ChargeType) ? "Other" : r.ChargeType);
        tables.CostByPricingModel = GroupAndSort(records, r => string.IsNullOrEmpty(r.PricingModel) ? "Other" : r.PricingModel);
        tables.CostByRegion = GroupAndSort(records, r => string.IsNullOrEmpty(r.MeterRegion) ? "Other" : r.MeterRegion);

        tables.CostByDay = records
            .Where(r => r.Date.HasValue)
            .GroupBy(r => r.Date!.Value.Date)
            .Select(g => new DailyPivotRow { Date = g.Key, TotalCost = g.Sum(r => r.Cost) })
            .OrderBy(r => r.Date)
            .ToList();

        return tables;
    }

    private static List<PivotRow> GroupAndSort(List<InvoiceRecord> records, Func<InvoiceRecord, string> keySelector)
    {
        return records
            .GroupBy(keySelector)
            .Select(g => new PivotRow { Label = g.Key, TotalCost = g.Sum(r => r.Cost), Count = g.Count() })
            .OrderByDescending(r => r.TotalCost)
            .ToList();
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static readonly Dictionary<string, string> ProviderMap = new(StringComparer.OrdinalIgnoreCase)
    {
        ["microsoft.compute"] = "Virtual Machines",
        ["microsoft.sql"] = "SQL Database",
        ["microsoft.dbformysql"] = "Azure Database for MySQL",
        ["microsoft.dbforpostgresql"] = "Azure Database for PostgreSQL",
        ["microsoft.storage"] = "Storage",
        ["microsoft.web"] = "Azure App Service",
        ["microsoft.network"] = "Virtual Network",
        ["microsoft.apimanagement"] = "API Management",
        ["microsoft.containerservice"] = "Azure Kubernetes Service",
        ["microsoft.cache"] = "Azure Cache for Redis",
        ["microsoft.cosmosdb"] = "Azure Cosmos DB",
        ["microsoft.documentdb"] = "Azure Cosmos DB",
        ["microsoft.dataprotection"] = "Backup",
        ["microsoft.recoveryservices"] = "Backup",
    };

    private static string GuessCategory(AzureRecommendation rec, List<InvoiceRecord> records)
    {
        var rid = (rec.ResourceId ?? "").ToLowerInvariant();
        foreach (var (provider, category) in ProviderMap)
        {
            if (rid.Contains(provider))
                return category;
        }

        var desc = (rec.ShortDescription ?? "").ToLowerInvariant();
        var cats = records
            .Where(r => !string.IsNullOrEmpty(r.MeterCategory))
            .Select(r => r.MeterCategory)
            .Distinct()
            .ToList();

        foreach (var cat in cats)
        {
            if (desc.Contains(cat.ToLowerInvariant()))
                return cat;
        }

        return string.IsNullOrEmpty(rec.Category) ? "Other" : rec.Category;
    }

    private static string? ExtractFromRaw(AzureRecommendation rec, string key)
    {
        if (rec.Raw.TryGetValue("extendedProperties", out var ext) && ext is Dictionary<string, object?> extDict)
        {
            if (extDict.TryGetValue(key, out var val) && val is string s && !string.IsNullOrEmpty(s))
                return s;
        }
        return null;
    }

    private static List<SavingsRegionRow> BuildRegionSavings(
        List<InvoiceRecord> records,
        List<AzureRecommendation> recommendations,
        int periodDays,
        double total3YrSavings,
        HashSet<string> payGoLabels)
    {
        var regionCosts = records
            .Where(r => string.IsNullOrEmpty(r.PricingModel) || payGoLabels.Contains(r.PricingModel))
            .GroupBy(r => string.IsNullOrEmpty(r.MeterRegion) ? "Unspecified" : r.MeterRegion)
            .ToDictionary(g => g.Key, g => g.Sum(r => r.Cost));

        var regionSavings = new Dictionary<string, double>(StringComparer.Ordinal);
        foreach (var rec in recommendations)
        {
            var region = ExtractFromRaw(rec, "region") ?? ExtractFromRaw(rec, "location") ?? "Unspecified";
            regionSavings[region] = regionSavings.GetValueOrDefault(region) + Math.Max(rec.AnnualSavingsEstimate, 0);
        }

        var allRegions = regionCosts.Keys.Union(regionSavings.Keys).OrderBy(r => r).ToList();
        var rows = new List<SavingsRegionRow>();
        foreach (var rgn in allRegions)
        {
            var csvPayGoAnn = regionCosts.GetValueOrDefault(rgn) / periodDays * 365;
            var annSav = regionSavings.GetValueOrDefault(rgn);

            var payGoAnn = (annSav > 0 && csvPayGoAnn < annSav) ? annSav / 0.40 : csvPayGoAnn;
            var payGo3Yr = payGoAnn * 3;
            var sav3Yr = annSav * 3;
            var riCost = payGo3Yr - sav3Yr;
            var pct = payGo3Yr > 0 ? sav3Yr / payGo3Yr * 100 : 0;
            var pctOfTotal = total3YrSavings > 0 ? sav3Yr / total3YrSavings * 100 : 0;

            rows.Add(new SavingsRegionRow
            {
                Region = rgn,
                Current3YrCost = Math.Round(payGo3Yr, 2),
                RiSp3YrCost = Math.Round(riCost, 2),
                NetSavings3Yr = Math.Round(sav3Yr, 2),
                SavingsPercent = Math.Round(pct, 1),
                AnnualSavings = Math.Round(annSav, 2),
                PercentOfTotalSavings = Math.Round(pctOfTotal, 1),
            });
        }

        return rows.OrderByDescending(r => r.NetSavings3Yr).ToList();
    }

    /// <summary>
    /// Build recommendation summary KPIs (commitment counts, savings).
    /// </summary>
    public static Dictionary<string, object> BuildRecommendationSummary(
        List<AzureRecommendation> recommendations,
        List<CommitmentItem> commitments)
    {
        var annualSavings = recommendations.Sum(r => Math.Max(r.AnnualSavingsEstimate, 0));
        var openCommitments = commitments.Count(c => !c.DaysRemaining.HasValue || c.DaysRemaining > 0);
        var expiring90D = commitments.Count(c => c.DaysRemaining.HasValue && c.DaysRemaining <= 90);

        return new Dictionary<string, object>
        {
            ["AdvisorCostRecommendationCount"] = recommendations.Count,
            ["AdvisorEstimatedAnnualSavings"] = annualSavings,
            ["ActiveCommitments"] = openCommitments,
            ["CommitmentsExpiringWithin90Days"] = expiring90D,
        };
    }
}
