using System.Text.Json.Serialization;
using MediaBrowser.Common.Api;
using MediaFlux.Jellyfin.Configuration;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace MediaFlux.Jellyfin;

[ApiController]
[Route("MediaFlux")]
[Authorize(Policy = Policies.RequiresElevation)]
public sealed class SettingsController(Plugin plugin, MfClient mf) : ControllerBase
{
    [HttpGet("Settings")]
    public ActionResult<SettingsView> GetSettings() => View();

    [HttpPost("Settings")]
    public ActionResult<SettingsView> SaveSettings([FromBody] SettingsRequest request)
    {
        try
        {
            var c = plugin.Snapshot();
            c.Enabled = request.Enabled; c.MfUrl = request.MfUrl; c.Username = request.Username;
            if (!string.IsNullOrEmpty(request.Password)) c.Password = request.Password;
            c.TaskTimeoutSeconds = request.TaskTimeoutSeconds;
            plugin.UpdateConfiguration(c);
            return View();
        }
        catch (PluginFault e) { return Error(e); }
    }

    [HttpPost("Test")]
    public async Task<ActionResult> TestConnection(CancellationToken token)
    {
        try
        {
            await mf.TestAsync(plugin.Snapshot(), token).ConfigureAwait(false);
            return Ok(new MessageReply("MF session 登录及 overview 读取成功。此测试不提交删除。"));
        }
        catch (PluginFault e) { return Error(e); }
    }

    [HttpPost("Sync")]
    public async Task<ActionResult<SettingsView>> Sync(CancellationToken token)
    {
        try
        {
            var c = plugin.Snapshot();
            var overview = await mf.OverviewAsync(c, token).ConfigureAwait(false);
            plugin.CommitSync(c, overview);
            return View();
        }
        catch (PluginFault e) { return Error(e); }
    }

    private SettingsView View()
    {
        var c = plugin.Snapshot();
        return new SettingsView(c.Enabled, c.MfUrl, c.Username, c.Password.Length > 0,
            Plugin.ActiveMappings(c).Mappings, c.MfLibraries,
            c.TaskTimeoutSeconds, c.SyncedAtUtc, Plugin.CacheCurrent(c), plugin.LastDeleteError, Plugin.CompiledBaseline, Plugin.SourceCheckedVersions);
    }

    private static ObjectResult Error(PluginFault e) => new(new MessageReply(e.Message)) { StatusCode = e.StatusCode };
}

public sealed record MessageReply([property: JsonPropertyName("message")] string Message);

public sealed class SettingsRequest
{
    [JsonPropertyName("enabled")] public bool Enabled { get; set; }
    [JsonPropertyName("mf_url")] public string MfUrl { get; set; } = "";
    [JsonPropertyName("username")] public string Username { get; set; } = "";
    [JsonPropertyName("password")] public string? Password { get; set; }
    [JsonPropertyName("task_timeout_seconds")] public int TaskTimeoutSeconds { get; set; } = 120;
}

public sealed record SettingsView(
    [property: JsonPropertyName("enabled")] bool Enabled,
    [property: JsonPropertyName("mf_url")] string MfUrl,
    [property: JsonPropertyName("username")] string Username,
    [property: JsonPropertyName("password_set")] bool PasswordSet,
    [property: JsonPropertyName("active_mappings")] PathMapping[] ActiveMappings,
    [property: JsonPropertyName("libraries")] LibraryScope[] Libraries,
    [property: JsonPropertyName("task_timeout_seconds")] int TaskTimeoutSeconds,
    [property: JsonPropertyName("synced_at_utc")] string SyncedAtUtc,
    [property: JsonPropertyName("cache_current")] bool CacheCurrent,
    [property: JsonPropertyName("last_delete_error")] string LastDeleteError,
    [property: JsonPropertyName("compiled_baseline")] string CompiledBaseline,
    [property: JsonPropertyName("source_checked_versions")] string SourceCheckedVersions);
