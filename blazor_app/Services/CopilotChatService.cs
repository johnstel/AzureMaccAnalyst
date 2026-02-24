using System.Text;
using System.Text.Json;
using AzureMaccAnalyst.Models;
using GitHub.Copilot.SDK;
using Microsoft.Extensions.AI;
using System.ComponentModel;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Manages a GitHub Copilot SDK client and session for interactive AI chat.
/// Exposes custom tools that let the AI query loaded invoice/analysis data.
/// </summary>
public sealed class CopilotChatService : IAsyncDisposable
{
    private readonly DashboardState _state;
    private readonly ILogger<CopilotChatService> _logger;

    private CopilotClient? _client;
    private CopilotSession? _session;
    private bool _disposed;

    // Chat state
    public List<ChatMessage> Messages { get; } = new();
    public bool IsConnected => _session is not null;
    public bool IsBusy { get; private set; }
    public string? ErrorMessage { get; private set; }
    public string StreamingContent { get; private set; } = "";

    public event Action? OnStateChanged;

    public CopilotChatService(DashboardState state, ILogger<CopilotChatService> logger)
    {
        _state = state;
        _logger = logger;
    }

    /// <summary>
    /// Initialise the Copilot SDK client and create a streaming session with data analysis tools.
    /// </summary>
    public async Task ConnectAsync()
    {
        if (_session is not null) return;

        ErrorMessage = null;

        try
        {
            _logger.LogInformation("Starting Copilot SDK client...");

            _client = new CopilotClient(new CopilotClientOptions
            {
                AutoStart = true,
                UseStdio = true,
                LogLevel = "warn"
            });

            await _client.StartAsync();
            _logger.LogInformation("Copilot CLI server started");

            _session = await _client.CreateSessionAsync(new SessionConfig
            {
                Model = "gpt-4.1",
                Streaming = true,
                SystemMessage = new SystemMessageConfig
                {
                    Mode = SystemMessageMode.Append,
                    Content = BuildSystemPrompt()
                },
                Tools = BuildTools(),
                OnPermissionRequest = PermissionHandler.ApproveAll
            });

            Messages.Add(new ChatMessage
            {
                Role = "assistant",
                Content = "Connected to GitHub Copilot. Ask me anything about your Azure cost data — " +
                          "I can query your loaded records, summarise spending, find anomalies, and more."
            });

            _logger.LogInformation("Copilot session created");
            OnStateChanged?.Invoke();
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to connect to Copilot SDK");
            ErrorMessage = $"Failed to connect: {ex.Message}";
            OnStateChanged?.Invoke();
        }
    }

    /// <summary>
    /// Send a user prompt and stream the response.
    /// </summary>
    public async Task SendMessageAsync(string prompt)
    {
        if (_session is null || IsBusy || string.IsNullOrWhiteSpace(prompt)) return;

        IsBusy = true;
        StreamingContent = "";
        ErrorMessage = null;

        Messages.Add(new ChatMessage { Role = "user", Content = prompt });
        OnStateChanged?.Invoke();

        var done = new TaskCompletionSource();
        var sb = new StringBuilder();

        using var subscription = _session.On(evt =>
        {
            switch (evt)
            {
                case AssistantMessageDeltaEvent delta:
                    sb.Append(delta.Data.DeltaContent);
                    StreamingContent = sb.ToString();
                    OnStateChanged?.Invoke();
                    break;

                case SessionErrorEvent err:
                    ErrorMessage = err.Data.Message;
                    if (!done.Task.IsCompleted) done.TrySetResult();
                    break;

                case SessionIdleEvent:
                    if (!done.Task.IsCompleted) done.TrySetResult();
                    break;
            }
        });

        try
        {
            await _session.SendAsync(new MessageOptions { Prompt = prompt });
            await done.Task;

            var finalContent = sb.ToString();
            if (!string.IsNullOrWhiteSpace(finalContent))
            {
                Messages.Add(new ChatMessage { Role = "assistant", Content = finalContent });
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error during chat");
            ErrorMessage = $"Chat error: {ex.Message}";
        }
        finally
        {
            IsBusy = false;
            StreamingContent = "";
            OnStateChanged?.Invoke();
        }
    }

    /// <summary>
    /// Disconnect and clean up the Copilot client.
    /// </summary>
    public async Task DisconnectAsync()
    {
        if (_session is not null)
        {
            await _session.DisposeAsync();
            _session = null;
        }
        if (_client is not null)
        {
            try { await _client.StopAsync(); }
            catch { /* best effort */ }
            await _client.DisposeAsync();
            _client = null;
        }

        Messages.Clear();
        ErrorMessage = null;
        OnStateChanged?.Invoke();
    }

    // ─── Custom Tools ───────────────────────────────────────────────

    private List<AIFunction> BuildTools()
    {
        return new List<AIFunction>
        {
            AIFunctionFactory.Create(GetDataSummary, "get_data_summary",
                "Get a summary of the currently loaded Azure invoice data including total cost, record count, date range, and currency."),

            AIFunctionFactory.Create(GetTopServicesBySpend, "get_top_services_by_spend",
                "Get the top N services ranked by total cost. Returns service family names and their total spend."),

            AIFunctionFactory.Create(GetCostBySubscription, "get_cost_by_subscription",
                "Get costs broken down by Azure subscription."),

            AIFunctionFactory.Create(GetCostByRegion, "get_cost_by_region",
                "Get costs broken down by Azure region/location."),

            AIFunctionFactory.Create(GetDailySpendTrend, "get_daily_spend_trend",
                "Get daily spend trend data showing cost per day over the loaded period."),

            AIFunctionFactory.Create(SearchRecords, "search_records",
                "Search invoice records by keyword across service, subscription, meter, product, or resource group fields. Returns matching records with details."),

            AIFunctionFactory.Create(GetCostByChargeType, "get_cost_by_charge_type",
                "Get costs broken down by charge type (e.g., Usage, Purchase, Refund)."),

            AIFunctionFactory.Create(GetRecommendations, "get_recommendations",
                "Get Azure Advisor-style recommendations and savings opportunities (available in demo mode)."),
        };
    }

    private string GetDataSummary()
    {
        if (_state.Records.Count == 0)
            return "No data loaded. Please upload an Azure invoice file first.";

        var records = _state.Records;
        var totalCost = records.Sum(r => r.Cost);
        var currency = records.FirstOrDefault()?.BillingCurrency ?? "USD";
        var minDate = records.Where(r => r.Date.HasValue).Min(r => r.Date);
        var maxDate = records.Where(r => r.Date.HasValue).Max(r => r.Date);
        var serviceCount = records.Select(r => r.ServiceFamily).Where(s => !string.IsNullOrEmpty(s)).Distinct().Count();
        var subCount = records.Select(r => r.SubscriptionName).Where(s => !string.IsNullOrEmpty(s)).Distinct().Count();
        var regionCount = records.Select(r => r.MeterRegion).Where(s => !string.IsNullOrEmpty(s)).Distinct().Count();

        return JsonSerializer.Serialize(new
        {
            recordCount = records.Count,
            totalCost = Math.Round(totalCost, 2),
            currency,
            periodStart = minDate?.ToString("yyyy-MM-dd"),
            periodEnd = maxDate?.ToString("yyyy-MM-dd"),
            uniqueServices = serviceCount,
            uniqueSubscriptions = subCount,
            uniqueRegions = regionCount,
            fileName = _state.FileName
        });
    }

    private string GetTopServicesBySpend([Description("Number of top services to return")] int count = 10)
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var top = _state.Records
            .GroupBy(r => string.IsNullOrEmpty(r.ServiceFamily) ? "(Unknown)" : r.ServiceFamily)
            .Select(g => new { service = g.Key, totalCost = Math.Round(g.Sum(r => r.Cost), 2) })
            .OrderByDescending(x => x.totalCost)
            .Take(count);

        return JsonSerializer.Serialize(top);
    }

    private string GetCostBySubscription()
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var data = _state.Records
            .GroupBy(r => string.IsNullOrEmpty(r.SubscriptionName) ? "(Unknown)" : r.SubscriptionName)
            .Select(g => new { subscription = g.Key, totalCost = Math.Round(g.Sum(r => r.Cost), 2) })
            .OrderByDescending(x => x.totalCost);

        return JsonSerializer.Serialize(data);
    }

    private string GetCostByRegion()
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var data = _state.Records
            .GroupBy(r => string.IsNullOrEmpty(r.MeterRegion) ? "(Unknown)" : r.MeterRegion)
            .Select(g => new { region = g.Key, totalCost = Math.Round(g.Sum(r => r.Cost), 2) })
            .OrderByDescending(x => x.totalCost);

        return JsonSerializer.Serialize(data);
    }

    private string GetDailySpendTrend()
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var data = _state.Records
            .Where(r => r.Date.HasValue)
            .GroupBy(r => r.Date!.Value.Date)
            .Select(g => new { date = g.Key.ToString("yyyy-MM-dd"), totalCost = Math.Round(g.Sum(r => r.Cost), 2) })
            .OrderBy(x => x.date);

        return JsonSerializer.Serialize(data);
    }

    private string SearchRecords(
        [Description("Search keyword to match against service, subscription, meter, product, or resource group")] string keyword,
        [Description("Max results to return")] int limit = 20)
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var kw = keyword.ToLowerInvariant();
        var matches = _state.Records
            .Where(r =>
                r.ServiceFamily.Contains(kw, StringComparison.OrdinalIgnoreCase) ||
                r.SubscriptionName.Contains(kw, StringComparison.OrdinalIgnoreCase) ||
                r.MeterCategory.Contains(kw, StringComparison.OrdinalIgnoreCase) ||
                r.Product.Contains(kw, StringComparison.OrdinalIgnoreCase) ||
                r.ResourceGroup.Contains(kw, StringComparison.OrdinalIgnoreCase) ||
                r.ConsumedService.Contains(kw, StringComparison.OrdinalIgnoreCase))
            .Take(limit)
            .Select(r => new
            {
                date = r.Date?.ToString("yyyy-MM-dd"),
                cost = Math.Round(r.Cost, 2),
                service = r.ServiceFamily,
                meter = r.MeterCategory,
                product = r.Product,
                subscription = r.SubscriptionName,
                region = r.MeterRegion,
                resourceGroup = r.ResourceGroup
            });

        return JsonSerializer.Serialize(new { matchCount = matches.Count(), results = matches });
    }

    private string GetCostByChargeType()
    {
        if (_state.Records.Count == 0) return "No data loaded.";

        var data = _state.Records
            .GroupBy(r => string.IsNullOrEmpty(r.ChargeType) ? "(Unknown)" : r.ChargeType)
            .Select(g => new { chargeType = g.Key, totalCost = Math.Round(g.Sum(r => r.Cost), 2) })
            .OrderByDescending(x => x.totalCost);

        return JsonSerializer.Serialize(data);
    }

    private string GetRecommendations()
    {
        if (!_state.IsDemoMode || _state.Recommendations.Count == 0)
            return "No recommendations available. Enable Demo Mode and generate demo data first.";

        var data = _state.Recommendations
            .Select(r => new
            {
                category = r.Category,
                impact = r.Impact,
                description = r.ShortDescription,
                potentialSavings = r.AnnualSavingsEstimate
            });

        return JsonSerializer.Serialize(data);
    }

    // ─── System Prompt ──────────────────────────────────────────────

    private string BuildSystemPrompt()
    {
        return """
            You are the Azure MACC Analyst AI assistant embedded in a FinOps dashboard.
            Your role is to help users understand their Azure cloud spending data.

            You have access to tools that query the currently loaded invoice data:
            - get_data_summary: Overview of loaded data (cost, records, date range)
            - get_top_services_by_spend: Top services by cost
            - get_cost_by_subscription: Spending by subscription
            - get_cost_by_region: Spending by region
            - get_daily_spend_trend: Daily cost trend
            - search_records: Search records by keyword
            - get_cost_by_charge_type: Breakdown by charge type
            - get_recommendations: Advisor-style recommendations (demo mode)

            Guidelines:
            - Always use the tools to answer data questions — do not guess.
            - Format currency values with the correct currency code.
            - When showing numbers, use thousands separators.
            - Be concise and actionable in your responses.
            - If no data is loaded, tell the user to upload a file first.
            - You can help with general Azure FinOps questions using your knowledge.
            """;
    }

    // ─── IAsyncDisposable ───────────────────────────────────────────

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _disposed = true;
        await DisconnectAsync();
    }
}

/// <summary>Simple chat message DTO.</summary>
public sealed class ChatMessage
{
    public string Role { get; set; } = "";
    public string Content { get; set; } = "";
    public DateTime Timestamp { get; set; } = DateTime.Now;
}
