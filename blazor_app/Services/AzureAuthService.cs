using Azure.Core;
using Azure.Identity;

namespace AzureMaccAnalyst.Services;

/// <summary>
/// Handles Azure authentication via Microsoft Entra ID.
/// Mirrors the Python azure_auth module — auto-detect (CLI / cached) and interactive browser flows.
/// Scoped per-circuit in Blazor Server.
/// </summary>
public sealed class AzureAuthService
{
    private readonly ILogger<AzureAuthService> _logger;

    public AzureAuthService(ILogger<AzureAuthService> logger)
    {
        _logger = logger;
    }

    // ── State ──
    public TokenCredential? Credential { get; private set; }
    public bool IsSignedIn => Credential is not null;
    public string? UserName { get; private set; }
    public string? UserEmail { get; private set; }
    public string? ErrorMessage { get; private set; }
    public bool IsBusy { get; private set; }

    private string TenantId =>
        Environment.GetEnvironmentVariable("AZURE_TENANT_ID") ?? "common";

    private string? ClientId =>
        Environment.GetEnvironmentVariable("AZURE_CLIENT_ID");

    // ── Auto-detect sign-in (CLI / cached / shared token cache) ──
    public async Task<bool> SignInAutoDetectAsync()
    {
        if (IsSignedIn) return true;
        IsBusy = true;
        ErrorMessage = null;

        try
        {
            _logger.LogInformation("Auto-detect sign-in starting (tenant={Tenant})", TenantId);
            var cred = BuildChainedCredential();
            var token = await cred.GetTokenAsync(
                new TokenRequestContext(["https://management.azure.com/.default"]),
                CancellationToken.None);

            Credential = cred;
            ExtractIdentity(token.Token);
            _logger.LogInformation("Auto-detect sign-in succeeded: {Email}", UserEmail ?? "unknown");
            return true;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Auto-detect sign-in failed");
            ErrorMessage = $"Auto sign-in failed: {ex.Message}";
            return false;
        }
        finally
        {
            IsBusy = false;
        }
    }

    // ── Interactive browser sign-in ──
    public async Task<bool> SignInInteractiveAsync()
    {
        IsBusy = true;
        ErrorMessage = null;

        try
        {
            _logger.LogInformation("Interactive browser sign-in starting (tenant={Tenant})", TenantId);
            var cred = BuildInteractiveCredential();
            var token = await cred.GetTokenAsync(
                new TokenRequestContext(["https://management.azure.com/.default"]),
                CancellationToken.None);

            Credential = cred;
            ExtractIdentity(token.Token);
            _logger.LogInformation("Interactive sign-in succeeded: {Email}", UserEmail ?? "unknown");
            return true;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Interactive sign-in failed");
            ErrorMessage = $"Sign-in failed: {ex.Message}";
            return false;
        }
        finally
        {
            IsBusy = false;
        }
    }

    // ── Sign out ──
    public void SignOut()
    {
        _logger.LogInformation("User signed out ({Email})", UserEmail);
        Credential = null;
        UserName = null;
        UserEmail = null;
        ErrorMessage = null;
    }

    // ── Build credential chains (mirrors Python) ──
    private ChainedTokenCredential BuildChainedCredential()
    {
        var creds = new List<TokenCredential>
        {
            new AzureCliCredential(new AzureCliCredentialOptions { TenantId = TenantId }),
            new AzureDeveloperCliCredential(new AzureDeveloperCliCredentialOptions { TenantId = TenantId }),
            new AzurePowerShellCredential(new AzurePowerShellCredentialOptions { TenantId = TenantId }),
            new SharedTokenCacheCredential(new SharedTokenCacheCredentialOptions { TenantId = TenantId }),
            BuildInteractiveCredential(),
        };
        return new ChainedTokenCredential(creds.ToArray());
    }

    private InteractiveBrowserCredential BuildInteractiveCredential()
    {
        var opts = new InteractiveBrowserCredentialOptions
        {
            TenantId = TenantId,
            TokenCachePersistenceOptions = new TokenCachePersistenceOptions { Name = "azure-macc-analyst-token-cache" },
        };
        if (!string.IsNullOrEmpty(ClientId))
            opts.ClientId = ClientId;
        return new InteractiveBrowserCredential(opts);
    }

    // ── Extract identity from JWT ──
    private void ExtractIdentity(string jwt)
    {
        try
        {
            var parts = jwt.Split('.');
            if (parts.Length < 2) return;

            var payload = parts[1];
            // Pad base64
            switch (payload.Length % 4)
            {
                case 2: payload += "=="; break;
                case 3: payload += "="; break;
            }
            var json = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(payload));
            var doc = System.Text.Json.JsonDocument.Parse(json);
            var root = doc.RootElement;

            UserName = root.TryGetProperty("name", out var n) ? n.GetString() : null;
            UserEmail = root.TryGetProperty("upn", out var u) ? u.GetString()
                      : root.TryGetProperty("unique_name", out var un) ? un.GetString()
                      : root.TryGetProperty("email", out var e) ? e.GetString()
                      : null;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Could not extract identity from token");
        }
    }
}
