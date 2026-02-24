using System.Security.Cryptography;
using System.Text;
using AzureMaccAnalyst.Models;
using Microsoft.Extensions.Logging;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Generates simulated Azure data (Advisor recommendations, RI/SP commitments)
/// grounded in the actual invoice file, for demonstration purposes.
/// Port of the Python <c>demo_data.py</c>.
/// </summary>
public sealed class DemoDataService
{
    private readonly FileLoaderService _loader;
    private readonly AnalysisService _analysis;
    private readonly ILogger<DemoDataService> _logger;

    public DemoDataService(FileLoaderService loader, AnalysisService analysis, ILogger<DemoDataService> logger)
    {
        _loader = loader;
        _analysis = analysis;
        _logger = logger;
    }

    // Realistic RI savings rates per service
    private static readonly Dictionary<string, (double Min, double Max)> SavingsRates = new()
    {
        ["Virtual Machines"] = (0.40, 0.62),
        ["SQL Database"] = (0.35, 0.55),
        ["Azure Cosmos DB"] = (0.30, 0.50),
        ["Azure Cache for Redis"] = (0.35, 0.55),
        ["Azure Database for MySQL"] = (0.30, 0.48),
        ["Azure Database for PostgreSQL"] = (0.30, 0.48),
        ["API Management"] = (0.25, 0.42),
        ["Azure App Service"] = (0.30, 0.50),
        ["Storage"] = (0.20, 0.38),
        ["Azure Kubernetes Service"] = (0.35, 0.55),
        ["Backup"] = (0.15, 0.30),
        ["Virtual Network"] = (0.10, 0.25),
        ["Azure DNS"] = (0.10, 0.20),
    };

    private static readonly Dictionary<string, string[]> SkuTemplates = new()
    {
        ["Virtual Machines"] = ["Standard_D4s_v5", "Standard_E8s_v5", "Standard_B2ms", "Standard_D2s_v5", "Standard_F4s_v2"],
        ["SQL Database"] = ["GP_Gen5_4", "GP_Gen5_8", "BC_Gen5_2", "GP_S_Gen5_2"],
        ["Azure Cosmos DB"] = ["Standard_D4s", "Standard_D8s"],
        ["API Management"] = ["Standard", "Premium"],
        ["Storage"] = ["Standard_LRS", "Standard_GRS", "Premium_LRS"],
        ["Azure App Service"] = ["P1v3", "P2v3", "S1", "B2"],
        ["Azure Cache for Redis"] = ["C1 Standard", "C2 Standard", "P1 Premium"],
        ["Virtual Network"] = ["VPN Gateway S2S", "Private Link"],
        ["Backup"] = ["Standard", "Enhanced"],
        ["Azure DNS"] = ["Standard"],
    };

    private static readonly Dictionary<string, string> CategoryToProvider = new()
    {
        ["API Management"] = "Microsoft.ApiManagement",
        ["Virtual Network"] = "Microsoft.Network",
        ["Storage"] = "Microsoft.Storage",
        ["Azure DNS"] = "Microsoft.Network",
        ["Backup"] = "Microsoft.DataProtection",
        ["Azure App Service"] = "Microsoft.Web",
        ["Virtual Machines"] = "Microsoft.Compute",
        ["SQL Database"] = "Microsoft.Sql",
        ["Azure Cosmos DB"] = "Microsoft.DocumentDb",
        ["Azure Cache for Redis"] = "Microsoft.Cache",
        ["Azure Kubernetes Service"] = "Microsoft.ContainerService",
        ["Azure Database for PostgreSQL"] = "Microsoft.DBforPostgreSQL",
        ["Azure Database for MySQL"] = "Microsoft.DBforMySQL",
    };

    private static readonly List<(string Cat, string Region, double Monthly, string Provider)> ExtraServices =
    [
        ("Virtual Machines", "US East", 8500.00, "Microsoft.Compute"),
        ("Virtual Machines", "US East 2", 4200.00, "Microsoft.Compute"),
        ("SQL Database", "US East", 3100.00, "Microsoft.Sql"),
        ("Azure Cosmos DB", "US East", 1800.00, "Microsoft.DocumentDb"),
        ("Azure Cache for Redis", "US East 2", 950.00, "Microsoft.Cache"),
        ("Azure App Service", "US East", 620.00, "Microsoft.Web"),
        ("Azure Kubernetes Service", "US East", 2400.00, "Microsoft.ContainerService"),
        ("Azure Database for PostgreSQL", "US East 2", 780.00, "Microsoft.DBforPostgreSQL"),
    ];

    /// <summary>
    /// Generate demo data from a loaded set of invoice records.
    /// </summary>
    public DemoDataResult Generate(List<InvoiceRecord> records)
    {
        _logger.LogInformation("Generating demo data from {Count} records", records.Count);
        var rng = new Random(DeterministicSeed(records.Count));

        // Period info
        var dates = records.Where(r => r.Date.HasValue).Select(r => r.Date!.Value).ToList();
        var periodDays = dates.Count > 1 ? Math.Max((int)(dates.Max() - dates.Min()).TotalDays, 1) : 30;

        // Group real services
        var realServices = records
            .Where(r => !string.IsNullOrEmpty(r.MeterCategory))
            .GroupBy(r => (Cat: r.MeterCategory, Region: string.IsNullOrEmpty(r.MeterRegion) ? "US East" : r.MeterRegion))
            .Select(g =>
            {
                var consumed = g.FirstOrDefault(r => !string.IsNullOrEmpty(r.ConsumedService))?.ConsumedService
                               ?? CategoryToProvider.GetValueOrDefault(g.Key.Cat, "Microsoft.Compute");
                return (Cat: g.Key.Cat, Region: g.Key.Region, Monthly: g.Sum(r => r.Cost) / periodDays * 30, Provider: consumed);
            })
            .ToList();

        var allServices = realServices
            .Concat(ExtraServices)
            .ToList();

        // Generate recommendations
        var recommendations = new List<AzureRecommendation>();
        foreach (var svc in allServices)
        {
            if (svc.Monthly < 0.01) continue;

            var (lo, hi) = SavingsRates.GetValueOrDefault(svc.Cat, (0.25, 0.45));
            var savingsPct = rng.NextDouble() * (hi - lo) + lo;
            var annualSavings = svc.Monthly * 12 * savingsPct;
            var skus = SkuTemplates.GetValueOrDefault(svc.Cat, ["Standard"]);
            var sku = skus[rng.Next(skus.Length)];

            var recId = $"/subscriptions/demo-sub-01/providers/{svc.Provider}/recommendations/{Md5Short($"{svc.Cat}-{sku}-{svc.Region}")}";

            recommendations.Add(new AzureRecommendation
            {
                ResourceId = recId,
                Category = "Cost",
                Impact = rng.Next(5) switch { 0 => "Low", 1 or 2 => "Medium", _ => "High" },
                ShortDescription = $"Purchase Reserved Instances for {svc.Cat} ({sku}) in {svc.Region} — save ~{savingsPct * 100:F0}%",
                AnnualSavingsEstimate = Math.Round(annualSavings, 2),
                Raw = new Dictionary<string, object?>
                {
                    ["extendedProperties"] = new Dictionary<string, object?>
                    {
                        ["sku"] = sku,
                        ["region"] = svc.Region,
                        ["vmSize"] = svc.Cat == "Virtual Machines" ? sku : "",
                        ["term"] = "P3Y",
                        ["annualSavingsAmount"] = Math.Round(annualSavings, 2).ToString(),
                        ["savingsCurrency"] = "USD",
                    }
                }
            });
        }

        // Generate commitments
        var today = DateTime.Today;
        var commitments = new List<CommitmentItem>();
        var riSamples = new (string Name, string Term, int StartOffset)[]
        {
            ("Virtual Machines – Standard_D4s_v5", "P3Y", -400),
            ("SQL Database – GP_Gen5_4", "P1Y", -180),
            ("Azure Cosmos DB – Standard_D4s", "P3Y", -700),
            ("Virtual Machines – Standard_B2ms", "P1Y", -60),
            ("Azure App Service – P1v3", "P1Y", -340),
        };

        foreach (var (name, term, startOffset) in riSamples)
        {
            var start = today.AddDays(startOffset);
            var years = term.Contains("P3Y") ? 3 : 1;
            var end = start.AddYears(years);
            var daysRem = (int)(end - today).TotalDays;
            if (daysRem < 0) continue;

            commitments.Add(new CommitmentItem
            {
                Source = "Reservation",
                ItemId = $"demo-ri-{Md5Short(name)}",
                Name = name,
                Term = term,
                Scope = "Shared",
                State = "Succeeded",
                StartDate = start.ToString("yyyy-MM-dd"),
                EndDate = end.ToString("yyyy-MM-dd"),
                DaysRemaining = daysRem,
            });
        }

        var spSamples = new (string Name, string Term, int StartOffset)[]
        {
            ("Compute Savings Plan – $500/hr", "P3Y", -200),
            ("Compute Savings Plan – $150/hr", "P1Y", -50),
        };

        foreach (var (name, term, startOffset) in spSamples)
        {
            var start = today.AddDays(startOffset);
            var years = term.Contains("P3Y") ? 3 : 1;
            var end = start.AddYears(years);
            var daysRem = (int)(end - today).TotalDays;

            commitments.Add(new CommitmentItem
            {
                Source = "SavingsPlan",
                ItemId = $"demo-sp-{Md5Short(name)}",
                Name = name,
                Term = term,
                Scope = "Shared",
                State = "Succeeded",
                StartDate = start.ToString("yyyy-MM-dd"),
                EndDate = end.ToString("yyyy-MM-dd"),
                DaysRemaining = daysRem,
            });
        }

        var fullAnnualPayGo = allServices.Where(s => s.Monthly > 0).Sum(s => s.Monthly * 12);

        _logger.LogInformation("Demo data: {Recs} recommendations, {Comms} commitments, annual paygo={PayGo:F2}",
            recommendations.Count, commitments.Count, fullAnnualPayGo);

        return new DemoDataResult
        {
            Recommendations = recommendations,
            Commitments = commitments,
            FullAnnualPayGo = Math.Round(fullAnnualPayGo, 2),
        };
    }

    private static int DeterministicSeed(int recordCount) => recordCount * 31337;

    private static string Md5Short(string input)
    {
        var hash = MD5.HashData(Encoding.UTF8.GetBytes(input));
        return Convert.ToHexString(hash)[..12].ToLowerInvariant();
    }
}

/// <summary>
/// Result container from <see cref="DemoDataService.Generate"/>.
/// </summary>
public sealed class DemoDataResult
{
    public List<AzureRecommendation> Recommendations { get; set; } = new();
    public List<CommitmentItem> Commitments { get; set; } = new();
    public double FullAnnualPayGo { get; set; }
}
