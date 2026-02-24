using AzureMaccAnalyst.Models;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Holds the current analysis state across Blazor components (in-memory, per-circuit).
/// </summary>
public sealed class DashboardState
{
    public const string AppVersion = "2.0.0";

    // File / records
    public string? FileName { get; set; }
    public List<InvoiceRecord> Records { get; set; } = new();

    // Analysis
    public AnalysisSummary? Summary { get; set; }
    public PivotTables? Pivots { get; set; }

    // Enrichment
    public List<AzureRecommendation> Recommendations { get; set; } = new();
    public List<CommitmentItem> Commitments { get; set; } = new();
    public Dictionary<string, object>? RecommendationSummary { get; set; }
    public SavingsAnalysis? SavingsAnalysis { get; set; }
    public List<string> Warnings { get; set; } = new();
    public bool IsDemoMode { get; set; }

    // UI state
    public bool IsLoading { get; set; }
    public string? ErrorMessage { get; set; }

    public bool HasSummary => Summary is not null;
    public bool HasEnrichment => RecommendationSummary is not null;

    public void Reset()
    {
        FileName = null;
        Records.Clear();
        Summary = null;
        Pivots = null;
        Recommendations.Clear();
        Commitments.Clear();
        RecommendationSummary = null;
        SavingsAnalysis = null;
        Warnings.Clear();
        ErrorMessage = null;
    }
}
