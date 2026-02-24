namespace AzureMaccAnalyst.Models;

/// <summary>
/// A single row from a pivot / group-by table (e.g. cost by service, by region, etc.).
/// </summary>
public sealed class PivotRow
{
    public string Label { get; set; } = "";
    public double TotalCost { get; set; }
    public int Count { get; set; }
}

/// <summary>
/// Container for all pivot tables produced by analysis.
/// </summary>
public sealed class PivotTables
{
    public List<PivotRow> CostByServiceFamily { get; set; } = new();
    public List<PivotRow> CostBySubscription { get; set; } = new();
    public List<PivotRow> CostByChargeType { get; set; } = new();
    public List<PivotRow> CostByPricingModel { get; set; } = new();
    public List<PivotRow> CostByRegion { get; set; } = new();
    public List<DailyPivotRow> CostByDay { get; set; } = new();
}

/// <summary>
/// A daily cost row for the trend chart.
/// </summary>
public sealed class DailyPivotRow
{
    public DateTime Date { get; set; }
    public double TotalCost { get; set; }
}
