namespace AzureMaccAnalyst.Models;

/// <summary>
/// An Azure Advisor cost-saving recommendation.
/// </summary>
public sealed class AzureRecommendation
{
    public string ResourceId { get; set; } = "";
    public string Category { get; set; } = "";
    public string Impact { get; set; } = "";
    public string ShortDescription { get; set; } = "";
    public double AnnualSavingsEstimate { get; set; }
    public Dictionary<string, object?> Raw { get; set; } = new();
}
