using System.Text.Json.Serialization;
using MediaBrowser.Model.Plugins;

namespace MediaFlux.Jellyfin.Configuration;

public sealed class PluginConfiguration : BasePluginConfiguration
{
    public bool Enabled { get; set; }
    public string MfUrl { get; set; } = "";
    public string Username { get; set; } = "";

    // Jellyfin's generic Plugin Configuration GET uses System.Text.Json too.
    // Persisted only in the server-side XML, never in any JSON response.
    [JsonIgnore]
    public string Password { get; set; } = "";

    public int TaskTimeoutSeconds { get; set; } = 120;
    public PathMapping[] MfMappings { get; set; } = [];
    public LibraryScope[] MfLibraries { get; set; } = [];
    public string SyncedMfUrl { get; set; } = "";
    public string SyncedUsername { get; set; } = "";
    public string SyncedAtUtc { get; set; } = "";
    public long Revision { get; set; }
}

public sealed record PathMapping
{
    [JsonPropertyName("local")]
    public string Local { get; set; } = "";
    [JsonPropertyName("server")]
    public string Server { get; set; } = "";
}

public sealed record LibraryScope
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string[] Locations { get; set; } = [];
}
