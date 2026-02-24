using AzureMaccAnalyst.Components;
using AzureMaccAnalyst.Services;

var builder = WebApplication.CreateBuilder(args);

// Add services to the container.
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

builder.Services.AddScoped<FileLoaderService>();
builder.Services.AddScoped<AnalysisService>();
builder.Services.AddScoped<DemoDataService>();
builder.Services.AddScoped<DashboardState>();
builder.Services.AddScoped<AzureAuthService>();

var app = builder.Build();

// Configure the HTTP request pipeline.
if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/Error", createScopeForErrors: true);
}
app.UseStatusCodePagesWithReExecute("/not-found", createScopeForStatusCodePages: true);
app.UseAntiforgery();

app.MapStaticAssets();
app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
