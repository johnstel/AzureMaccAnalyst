namespace AzureMaccAnalyst.Models;

/// <summary>
/// Results of the Pay-Go vs RI/SP savings analysis.
/// </summary>
public sealed class SavingsAnalysis
{
    public List<PivotRow> PricingModelSummary { get; set; } = new();
    public double PayGoTotal { get; set; }
    public int PeriodDays { get; set; }
    public double AnnualPayGoRunRate { get; set; }
    public double ThreeYearPayGo { get; set; }
    public List<SavingsCategoryRow> SavingsByCategory { get; set; } = new();
    public List<SavingsRegionRow> SavingsByRegion { get; set; } = new();
    public List<TopOpportunityRow> TopOpportunities { get; set; } = new();
    public SavingsKpi Kpi { get; set; } = new();
}

public sealed class SavingsCategoryRow
{
    public string ResourceType { get; set; } = "";
    public double Current3YrCost { get; set; }
    public double RiSp3YrCost { get; set; }
    public double NetSavings3Yr { get; set; }
    public double SavingsPercent { get; set; }
    public double AnnualSavings { get; set; }
    public double MonthlySavings { get; set; }
    public double PercentOfTotalSavings { get; set; }
}

public sealed class SavingsRegionRow
{
    public string Region { get; set; } = "";
    public double Current3YrCost { get; set; }
    public double RiSp3YrCost { get; set; }
    public double NetSavings3Yr { get; set; }
    public double SavingsPercent { get; set; }
    public double AnnualSavings { get; set; }
    public double PercentOfTotalSavings { get; set; }
}

public sealed class TopOpportunityRow
{
    public int Rank { get; set; }
    public string ResourceType { get; set; } = "";
    public string Sku { get; set; } = "";
    public string Region { get; set; } = "";
    public double Current3YrCost { get; set; }
    public double RiSp3YrCost { get; set; }
    public double NetSavings3Yr { get; set; }
    public double SavingsPercent { get; set; }
    public double AnnualSavings { get; set; }
    public string Description { get; set; } = "";
}

public sealed class SavingsKpi
{
    public double Total3YrSavings { get; set; }
    public double SavingsPercent { get; set; }
    public int TotalRecommendations { get; set; }
    public double Current3YrSpend { get; set; }
    public double RiSp3YrCost { get; set; }
    public int ResourceCategories { get; set; }
    public double AnnualSavings { get; set; }
    public double MonthlySavings { get; set; }
}
