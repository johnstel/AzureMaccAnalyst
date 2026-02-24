namespace AzureMaccAnalyst.Models;

/// <summary>
/// High-level summary of an Azure invoice export analysis.
/// </summary>
public sealed class AnalysisSummary
{
    public DateTime? PeriodStart { get; set; }
    public DateTime? PeriodEnd { get; set; }
    public string Currency { get; set; } = "USD";
    public double TotalCost { get; set; }
    public double TotalQuantity { get; set; }
    public int RecordCount { get; set; }
}
