using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Xml.Linq;
using MediaBrowser.Model.Plugins;
using MediaFlux.Jellyfin.Configuration;
using Xunit;

namespace MediaFlux.Jellyfin.Tests;

public sealed class SettingsAndMappingTests
{
    [Fact]
    public async Task NativePluginImageEndpointServesThePackagedLogo()
    {
        using var f = new TestFixture();
        var logo = File.ReadAllBytes(Path.Combine(AppContext.BaseDirectory, "logo.png"));
        Assert.Equal("logo.png", f.InstalledPlugin.Manifest.ImagePath);
        Assert.Equal(f.Plugin.Id, f.InstalledPlugin.Manifest.Id);
        Assert.True(f.InstalledPlugin.Manifest.AutoUpdate);
        File.WriteAllBytes(Path.Combine(f.Root, "logo.png"), logo);
        using var response = await f.Client.GetAsync($"/Plugins/{f.Plugin.Id}/{f.InstalledPlugin.Manifest.Version}/Image");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("image/png", response.Content.Headers.ContentType!.MediaType);
        Assert.Equal(logo, await response.Content.ReadAsByteArrayAsync());
    }

    [Fact]
    public void PluginAssemblyHasReleaseVersionAndUsesTheUpstreamApi()
    {
        Assert.Equal("0.1.3.0", typeof(Plugin).Assembly.GetName().Version!.ToString());
        Assert.Equal("Jellyfin.Api", typeof(global::Jellyfin.Api.Controllers.LibraryController).Assembly.GetName().Name);
    }

    [Fact]
    public async Task NativeGenericConfigurationGetAndCustomSettingsNeverEchoPassword()
    {
        using var f = new TestFixture();
        f.Client.DefaultRequestHeaders.Add("X-Test-Admin", "true");
        using var native = await f.Client.GetAsync("/Plugins/" + f.Plugin.Id + "/Configuration");
        using var settings = await f.Client.GetAsync("/MediaFlux/Settings");
        Assert.Equal(HttpStatusCode.OK, native.StatusCode); Assert.Equal(HttpStatusCode.OK, settings.StatusCode);
        foreach (var response in new[] { native, settings })
        {
            var json = await response.Content.ReadAsStringAsync();
            Assert.DoesNotContain("private-password", json);
            using var obj = JsonDocument.Parse(json);
            Assert.False(obj.RootElement.TryGetProperty("Password", out _));
            Assert.False(obj.RootElement.TryGetProperty("password", out _));
        }
        using var metadata = JsonDocument.Parse(await settings.Content.ReadAsStringAsync());
        Assert.Equal("12.2.0", metadata.RootElement.GetProperty("compiled_baseline").GetString());
        Assert.Equal("12.2 / 12.3", metadata.RootElement.GetProperty("source_checked_versions").GetString());
        Assert.False(metadata.RootElement.TryGetProperty("mapping_mode", out _));
        Assert.False(metadata.RootElement.TryGetProperty("manual_mappings", out _));
        var direct = JsonSerializer.Serialize((BasePluginConfiguration)f.Plugin.Configuration, f.Plugin.ConfigurationType);
        Assert.DoesNotContain("private-password", direct);
        Assert.Contains("private-password", File.ReadAllText(f.Plugin.ConfigurationFilePath));
        if (!OperatingSystem.IsWindows())
            Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, File.GetUnixFileMode(f.Plugin.ConfigurationFilePath));
    }

    [Fact]
    public async Task AllSettingsActionsAndNativeGenericConfigurationAreAdminOnly()
    {
        using var f = new TestFixture();
        foreach (var endpoint in new[] { "Settings", "Test", "Sync" })
        {
            using var response = await f.Client.PostAsJsonAsync("/MediaFlux/" + endpoint, new { });
            Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
        }
        using var get = await f.Client.GetAsync("/MediaFlux/Settings");
        using var generic = await f.Client.GetAsync("/Plugins/" + f.Plugin.Id + "/Configuration");
        Assert.Equal(HttpStatusCode.Forbidden, get.StatusCode); Assert.Equal(HttpStatusCode.Forbidden, generic.StatusCode);
        Assert.Equal(0, f.Handler.LoginPosts);
    }

    [Fact]
    public async Task NativeGenericConfigurationCannotReplaceSecretOrInjectMfScope()
    {
        using var f = new TestFixture(); f.Client.DefaultRequestHeaders.Add("X-Test-Admin", "true");
        var before = f.Plugin.Snapshot();
        using var response = await f.Client.PostAsJsonAsync("/Plugins/" + f.Plugin.Id + "/Configuration", new
        {
            Enabled = true, MfUrl = before.MfUrl, Username = before.Username, Password = "injected-secret",
            TaskTimeoutSeconds = 120, MfMappings = new[] { new { local = "/evil", server = "/wrong" } }
        });
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal("private-password", f.Plugin.Snapshot().Password);
        Assert.Equal(f.Root, Assert.Single(f.Plugin.Snapshot().MfMappings).Server);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task OverviewUsesExactPublicServerTypeFieldAndNeverClearsCacheOnFailure(bool correctField)
    {
        using var f = new TestFixture(); f.Client.DefaultRequestHeaders.Add("X-Test-Admin", "true");
        var server = new Dictionary<string, object>
        {
            [correctField ? "server_type" : "type"] = "jellyfin", ["enabled"] = true, ["configured"] = true,
            ["mappings"] = new[] { new { local = "/mf/new", server = f.Root } },
            ["libraries"] = new[] { new { id = "lib", name = "Movies", locations = new[] { f.Root } } }
        };
        f.Handler.Overview = JsonSerializer.Serialize(new { servers = new[] { server } });
        using var response = await f.Client.PostAsJsonAsync("/MediaFlux/Sync", new { });
        Assert.Equal(correctField ? HttpStatusCode.OK : HttpStatusCode.Conflict, response.StatusCode);
        var cached = Assert.Single(f.Plugin.Snapshot().MfMappings);
        Assert.Equal(correctField ? "/mf/new" : "/mf/strm", cached.Local);
        if (correctField) Assert.Equal("Movies", Assert.Single(f.Plugin.Snapshot().MfLibraries).Name);
    }

    [Fact]
    public async Task OfflineSyncPreservesCachedMfScope()
    {
        using var f = new TestFixture(); f.Client.DefaultRequestHeaders.Add("X-Test-Admin", "true"); f.Handler.Offline = true;
        using var failed = await f.Client.PostAsJsonAsync("/MediaFlux/Sync", new { });
        Assert.Equal(HttpStatusCode.ServiceUnavailable, failed.StatusCode);
        Assert.Equal("/mf/strm", Assert.Single(f.Plugin.Snapshot().MfMappings).Local);
    }

    [Theory]
    [InlineData("manual")]
    [InlineData("invalid")]
    public void LegacyManualConfigurationIsDisabledAndRequiresFreshMfSync(string mode)
    {
        using var f = new TestFixture();
        var xml = XDocument.Load(f.Plugin.ConfigurationFilePath);
        xml.Root!.Add(new XElement("MappingMode", mode), new XElement("ManualMappings",
            new XElement("PathMapping", new XElement("Local", "/legacy"), new XElement("Server", f.Root))));
        xml.Save(f.Plugin.ConfigurationFilePath);
        var plugin = f.ReloadPlugin();
        var c = plugin.Snapshot();
        Assert.False(c.Enabled); Assert.Empty(c.MfMappings); Assert.Empty(c.MfLibraries);
        Assert.False(Plugin.CacheCurrent(c)); Assert.Equal("private-password", c.Password);
        Assert.Contains("手动映射已停用", plugin.LastDeleteError);
        Assert.DoesNotContain("MappingMode", File.ReadAllText(plugin.ConfigurationFilePath));
        Assert.DoesNotContain("ManualMappings", File.ReadAllText(plugin.ConfigurationFilePath));
        c.Enabled = true;
        Assert.Throws<PluginFault>(() => plugin.UpdateConfiguration(c));
        plugin.CommitSync(plugin.Snapshot(), new MfOverview([new PathMapping { Local = "/mf/strm", Server = f.Root }], []));
        c = plugin.Snapshot(); c.Enabled = true; plugin.UpdateConfiguration(c);
        var reloaded = f.ReloadPlugin().Snapshot();
        Assert.True(reloaded.Enabled); Assert.True(Plugin.CacheCurrent(reloaded));
    }

    [Fact]
    public void LegacyMfConfigurationKeepsCredentialsAndScopeAcrossMigration()
    {
        using var f = new TestFixture();
        var xml = XDocument.Load(f.Plugin.ConfigurationFilePath);
        xml.Root!.Add(new XElement("MappingMode", "mf"), new XElement("ManualMappings"));
        xml.Save(f.Plugin.ConfigurationFilePath);
        var c = f.ReloadPlugin().Snapshot();
        Assert.True(c.Enabled); Assert.True(Plugin.CacheCurrent(c));
        Assert.Equal("private-password", c.Password);
        Assert.Equal("/mf/strm", Assert.Single(c.MfMappings).Local);
        Assert.DoesNotContain("MappingMode", File.ReadAllText(f.Plugin.ConfigurationFilePath));
    }

    [Fact]
    public void BrokenLegacyXmlKeepsHostDefaultDisabledRecovery()
    {
        using var f = new TestFixture();
        const string broken = "<PluginConfiguration><MappingMode>manual";
        File.WriteAllText(f.Plugin.ConfigurationFilePath, broken);
        var plugin = f.ReloadPlugin();
        Assert.False(plugin.Snapshot().Enabled);
        Assert.Contains("配置无法读取", plugin.LastDeleteError);
        Assert.False(Plugin.CacheCurrent(plugin.Snapshot()));
        Assert.Empty(plugin.Snapshot().MfMappings);
    }

    [Fact]
    public void ChangedMfIdentityKeepsCachedScopeButRequiresResyncBeforeEnabling()
    {
        using var f = new TestFixture(); var c = f.Plugin.Snapshot();
        c.Enabled = false; c.MfUrl = "http://new-mf.invalid"; f.Plugin.UpdateConfiguration(c);
        Assert.False(Plugin.CacheCurrent(f.Plugin.Snapshot()));
        Assert.Equal(f.Root, Assert.Single(f.Plugin.Snapshot().MfMappings).Server);
        c = f.Plugin.Snapshot(); c.Enabled = true;
        Assert.Throws<PluginFault>(() => f.Plugin.UpdateConfiguration(c));
    }

    [Theory]
    [InlineData("/", "/media")]
    [InlineData("/mf", "/")]
    public void FilesystemRootMappingsAreRejectedLikeMf(string local, string server)
    {
        Assert.Throws<PluginFault>(() => new MappingSet([new PathMapping { Local = local, Server = server }]));
    }

    [Fact]
    public void LongestComponentPrefixAndAmbiguousTargetsAreHandled()
    {
        var set = new MappingSet([
            new PathMapping { Local = "/mf", Server = "/media" },
            new PathMapping { Local = "/mf/tv", Server = "/media/tv" }
        ]);
        Assert.Equal("/mf/tv", set.Match("/media/tv/episode.strm")!.Local);
        Assert.Equal("/mf", set.Match("/media/tv-old/movie.strm")!.Local);
        Assert.Null(set.Match("/media-old/movie.strm"));
        Assert.True(set.Overlaps("/"));
        Assert.Throws<PluginFault>(() => new MappingSet([
            new PathMapping { Local = "/source1", Server = "/media/" },
            new PathMapping { Local = "/source2", Server = "/media" }
        ]));
        Assert.Throws<PluginFault>(() => MappingSet.Normalize("/media/../secret"));
    }

    [Fact]
    public void WindowsMappingComparisonIsComponentAwareAndCaseInsensitive()
    {
        var set = new MappingSet([new PathMapping { Local = "/mf", Server = @"C:\Media" }]);
        Assert.NotNull(set.Match(@"c:\media\Movie.strm"));
        Assert.Null(set.Match(@"C:\Media-Other\Movie.strm"));
        Assert.Throws<PluginFault>(() => new MappingSet([
            new PathMapping { Local = "/a", Server = @"C:\Media" },
            new PathMapping { Local = "/b", Server = @"c:\media\" }
        ]));
    }
}
