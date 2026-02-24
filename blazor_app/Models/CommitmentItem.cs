namespace AzureMaccAnalyst.Models;

/// <summary>
/// A Reserved Instance or Savings Plan commitment.
/// </summary>
public sealed class CommitmentItem
{
    public string Source { get; set; } = "";
    public string ItemId { get; set; } = "";
    public string Name { get; set; } = "";
    public string Term { get; set; } = "";
    public string Scope { get; set; } = "";
    public string State { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int? DaysRemaining { get; set; }
    public Dictionary<string, object?> Details { get; set; } = new();
}
