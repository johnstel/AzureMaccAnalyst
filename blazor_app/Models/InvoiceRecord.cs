namespace AzureMaccAnalyst.Models;

/// <summary>
/// Raw invoice record with all canonical PascalCase columns.
/// Used as an intermediate type during file loading / analysis.
/// </summary>
public sealed class InvoiceRecord
{
    public string InvoiceId { get; set; } = "";
    public DateTime? Date { get; set; }
    public double Cost { get; set; }
    public double Quantity { get; set; }
    public string BillingCurrency { get; set; } = "";
    public string SubscriptionId { get; set; } = "";
    public string SubscriptionName { get; set; } = "";
    public string ServiceFamily { get; set; } = "";
    public string MeterCategory { get; set; } = "";
    public string MeterSubcategory { get; set; } = "";
    public string MeterRegion { get; set; } = "";
    public string MeterId { get; set; } = "";
    public string MeterName { get; set; } = "";
    public string ConsumedService { get; set; } = "";
    public string Product { get; set; } = "";
    public string PricingModel { get; set; } = "";
    public string ChargeType { get; set; } = "";
    public string ResourceGroup { get; set; } = "";
    public string ResourceId { get; set; } = "";
    public string ReservationId { get; set; } = "";
    public string ReservationName { get; set; } = "";
    public string Term { get; set; } = "";
    public string ResourceLocation { get; set; } = "";
    public string Location { get; set; } = "";
}
