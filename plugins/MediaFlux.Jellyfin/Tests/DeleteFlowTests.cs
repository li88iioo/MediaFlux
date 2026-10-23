using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model.IO;
using MediaFlux.Jellyfin.Configuration;
using Moq;
using Xunit;

namespace MediaFlux.Jellyfin.Tests;

public sealed class DeleteFlowTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ActualNativeMovieAndEpisodeWaitForMfAndSendRawByteFingerprint(bool movie)
    {
        using var f = new TestFixture();
        BaseItem item = movie ? f.Movie().Object : f.Episode().Object;
        var bytes = File.ReadAllBytes(item.Path);
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.NativeDeletes); Assert.Equal(1, f.Handler.DeletePosts);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(item.Path, payload.RootElement.GetProperty("paths")[0].GetString());
        Assert.Equal("sha256:" + Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),
            payload.RootElement.GetProperty("fingerprints").GetProperty(item.Path).GetString());
        Assert.Equal("mf", payload.RootElement.GetProperty("mapping_mode").GetString());
        Assert.False(payload.RootElement.TryGetProperty("mappings", out _));
    }

    [Fact]
    public async Task NativeBatchHasOneCloudWriteAndPreflightsAllPermissions()
    {
        using var f = new TestFixture();
        var a = f.Episode("a.strm").Object; var b = f.Episode("b.strm").Object;
        using var response = await f.Batch(a, b);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(2, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(2, payload.RootElement.GetProperty("fingerprints").EnumerateObject().Count());
    }

    [Fact]
    public async Task UnauthorizedLastSelectionDoesNotDeleteFirstItemOrWriteCloud()
    {
        using var f = new TestFixture();
        var a = f.Episode("allowed.strm"); var b = f.Episode("denied.strm", allowed: false);
        using var response = await f.Batch(a.Object, b.Object);
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
        Assert.Equal(0, f.Handler.LoginPosts); Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
        a.Verify(i => i.CanDelete(f.User), Times.Once); b.Verify(i => i.CanDelete(f.User), Times.Once);
    }

    [Fact]
    public async Task RealUpstreamCanDeleteRejectsAuthenticatedUserWithoutDeletionPermission()
    {
        using var f = new TestFixture();
        f.User.Permissions.Clear();
        var path = Path.Combine(f.Root, "denied.strm"); File.WriteAllText(path, "https://example.invalid\n");
        var item = new Episode { Id = Guid.NewGuid(), Path = path, IsInMixedFolder = false };
        f.Items[item.Id] = item;
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task ApiKeyCannotBypassManagedUserCanDelete()
    {
        using var f = new TestFixture(); f.Client.DefaultRequestHeaders.Add("X-Test-ApiKey", "true");
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task RealMovieDirectoryDeleteCannotHideUnselectedAdditionalStrm()
    {
        using var f = new TestFixture(); var movie = f.Movie().Object;
        var extra = Path.Combine(Path.GetDirectoryName(movie.Path)!, "extras", "bonus.strm");
        Directory.CreateDirectory(Path.GetDirectoryName(extra)!); File.WriteAllText(extra, "https://example.invalid/bonus");
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Contains("未明确选中", await response.Content.ReadAsStringAsync());
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Theory]
    [InlineData("local-index", false)]
    [InlineData("linked-index", false)]
    [InlineData("primary-id", false)]
    [InlineData("owner-id", false)]
    [InlineData("parts", false)]
    [InlineData("local-index", true)]
    [InlineData("linked-index", true)]
    [InlineData("primary-id", true)]
    [InlineData("owner-id", true)]
    [InlineData("parts", true)]
    public async Task Version12RelationsBlockGroupedDeletesIncludingChangesWhileWaiting(string relation, bool whileWaiting)
    {
        using var f = new TestFixture(); var item = f.Episode().Object;
        // New SDK relations must be detected even when the legacy arrays are empty.
        Assert.Empty(item.LocalAlternateVersions); Assert.Empty(item.LinkedAlternateVersions);
        void Group()
        {
            switch (relation)
            {
                case "local-index": f.Library.Setup(l => l.GetLocalAlternateVersionIds(item)).Returns([Guid.NewGuid()]); break;
                case "linked-index": f.Library.Setup(l => l.GetLinkedAlternateVersions(item)).Returns([new Episode { Id = Guid.NewGuid() }]); break;
                case "primary-id": item.PrimaryVersionId = Guid.NewGuid(); break;
                case "owner-id": item.OwnerId = Guid.NewGuid(); break;
                case "parts": item.AdditionalParts = [Path.Combine(f.Root, "part2.strm")]; break;
            }
        }
        if (whileWaiting) f.Handler.OnPoll = Group; else Group();
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(whileWaiting ? 1 : 0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
        if (whileWaiting) Assert.Contains("云任务已成功", await response.Content.ReadAsStringAsync());
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task BroadMappingFolderRejectsManagedStrmButAllowsPureLocalVideo(bool strm)
    {
        using var f = new TestFixture(); var folder = f.Folder();
        File.WriteAllText(Path.Combine(folder.Path, strm ? "ep.strm" : "ep.mp4"), "local bytes");
        using var response = await f.Delete(folder);
        Assert.Equal(strm ? HttpStatusCode.Conflict : HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(strm ? 0 : 1, f.NativeDeletes);
    }

    [Theory]
    [InlineData("partial", false)]
    [InlineData("failed", false)]
    [InlineData("completed", false)]
    [InlineData("cancelled", false)]
    [InlineData("unknown", true)]
    public async Task OnlyCompletedAndResultOkMayContinueAndRemoteErrorIsVisible(string state, bool ok)
    {
        using var f = new TestFixture(); f.Handler.State = state; f.Handler.Ok = ok;
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Contains("后端已拒绝文件身份校验", await response.Content.ReadAsStringAsync());
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task RemoteErrorsAreRedactedIncludingPassword()
    {
        using var f = new TestFixture(); f.Handler.State = "partial"; f.Handler.Error = "问题 private-password after-login";
        using var response = await f.Delete(f.Episode().Object);
        Assert.DoesNotContain("private-password", await response.Content.ReadAsStringAsync());
        Assert.DoesNotContain("after-login", f.Plugin.LastDeleteError);
        Assert.Contains("已隐藏", f.Plugin.LastDeleteError);
    }

    [Fact]
    public async Task Mf403NeverReplaysWriteOrContinuesNativeDelete()
    {
        using var f = new TestFixture(); f.Handler.DeleteCode = HttpStatusCode.Forbidden;
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Contains("403", await response.Content.ReadAsStringAsync());
        Assert.Equal(1, f.Handler.LoginPosts); Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task CachedScopeFailsClosedWhenMfOfflineButUnmanagedStrmStaysNative()
    {
        using var f = new TestFixture(); f.Handler.Offline = true;
        f.Plugin.CommitSync(f.Plugin.Snapshot(), new MfOverview([new PathMapping { Local = "/mf", Server = Path.Combine(f.Root, "managed") }], []));
        using var managed = await f.Delete(f.Episode("managed/ep.strm").Object);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, managed.StatusCode); Assert.Equal(0, f.NativeDeletes);
        using var unmanaged = await f.Delete(f.Episode("ordinary.strm").Object);
        Assert.Equal(HttpStatusCode.NoContent, unmanaged.StatusCode); Assert.Equal(1, f.NativeDeletes);
        Assert.Single(f.Plugin.Snapshot().MfMappings);
    }

    [Fact]
    public async Task UnavailableStrmAbortsTheWholeBatchBeforeLoginOrWrite()
    {
        using var f = new TestFixture(); var a = f.Episode("a.strm").Object; var b = f.Episode("missing.strm").Object;
        File.Delete(b.Path);
        using var response = await f.Batch(a, b);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(0, f.Handler.LoginPosts); Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task TimeoutStopsNativeAndDoesNotRepeatCloudWrite()
    {
        using var f = new TestFixture(); var config = f.Plugin.Snapshot(); config.TaskTimeoutSeconds = 5; f.Plugin.UpdateConfiguration(config);
        f.Handler.State = "running"; f.Handler.PollDelayMilliseconds = 1000;
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.GatewayTimeout, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task UnchangedCsrfAfterLoginNeverSubmitsDelete()
    {
        using var f = new TestFixture(); f.Handler.RefreshToken = false;
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task RefreshedItemPointingAtNewPathStopsNativeDeleteAfterCloudSuccess()
    {
        using var f = new TestFixture(); var item = f.Episode().Object;
        var newItem = f.Episode("new-file.strm").Object; newItem.Id = item.Id;
        f.Handler.OnPoll = () => f.Items[item.Id] = newItem;
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
        Assert.True(File.Exists(newItem.Path));
    }

    [Fact]
    public async Task RefreshedDeletePathsCannotAddNewTargetsEvenWithSameItemPath()
    {
        using var f = new TestFixture(); var mock = f.Episode();
        f.Handler.OnPoll = () => mock.Setup(i => i.GetDeletePaths()).Returns([
            new FileSystemMetadata { FullName = mock.Object.Path },
            new FileSystemMetadata { FullName = Path.Combine(f.Root, "new-directory"), IsDirectory = true }]);
        using var response = await f.Delete(mock.Object);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task NewlyAppearingSiblingStrmAfterCloudSuccessStopsNativeDirectoryDelete()
    {
        using var f = new TestFixture(); var item = f.Movie().Object;
        f.Handler.OnPoll = () => File.WriteAllText(Path.Combine(Path.GetDirectoryName(item.Path)!, "new.strm"), "new");
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); Assert.Equal(0, f.NativeDeletes);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ConfirmedMfLinkageMayRemoveOldFileOrDirectoryWithoutInventingNewDeleteIntent(bool movie)
    {
        using var f = new TestFixture(); BaseItem item = movie ? f.Movie().Object : f.Episode().Object;
        f.Handler.OnPoll = () => { if (movie) Directory.Delete(Path.GetDirectoryName(item.Path)!, true); else File.Delete(item.Path); };
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
    }

    [Theory]
    [InlineData("10.11.11")]
    [InlineData("12.1.0")]
    [InlineData("13.0.0")]
    public async Task OutOfTargetVersionDoesNotBlockPlainLocalVideo(string version)
    {
        using var f = new TestFixture(version: version);
        using var response = await f.Delete(f.Episode("local.mp4").Object);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode); Assert.Equal(1, f.NativeDeletes); Assert.Equal(0, f.Handler.DeletePosts);
        using var managed = await f.Delete(f.Episode("managed.strm").Object);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, managed.StatusCode); Assert.Equal(1, f.NativeDeletes);
    }

    [Fact]
    public async Task DisabledPluginLeavesNativeDeleteUntouched()
    {
        using var f = new TestFixture(enabled: false); f.Handler.Offline = true;
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode); Assert.Equal(1, f.NativeDeletes); Assert.Equal(0, f.Handler.DeletePosts);
    }

    [Theory]
    [InlineData("12.2.0")]
    [InlineData("12.3.0")]
    [InlineData("12.3.1")]
    public async Task SupportedHostsAlwaysSendMfMappingsWithoutManualOverrides(string version)
    {
        using var f = new TestFixture(version: version);
        using var response = await f.Delete(f.Episode().Object);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        using var body = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal("mf", body.RootElement.GetProperty("mapping_mode").GetString());
        Assert.False(body.RootElement.TryGetProperty("mappings", out _));
    }
}
