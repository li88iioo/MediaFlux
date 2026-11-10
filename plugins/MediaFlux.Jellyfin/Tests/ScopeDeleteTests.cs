using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Model.IO;
using MediaFlux.Jellyfin.Configuration;
using Moq;
using Xunit;

namespace MediaFlux.Jellyfin.Tests;

public sealed class ScopeDeleteTests
{
    [Fact]
    public async Task NativeFullNamesAndSelectedPathsUseTheSameNormalizedIdentity()
    {
        using var f = new TestFixture(); var movie = f.Movie().Object;
        var original = movie.Path;
        // FileSystemInfo.FullName 规范化重复分隔符；库内路径仍可保留另一种拼写。
        // Windows UNC 的正反斜杠差异落在同一个比较边界，不能扩大删除集合。
        movie.Path = original.Replace("movie" + Path.DirectorySeparatorChar,
            "movie" + Path.DirectorySeparatorChar + Path.DirectorySeparatorChar, StringComparison.Ordinal);
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(MappingSet.Normalize(original), Assert.Single(payload.RootElement.GetProperty("paths").EnumerateArray()).GetString());
    }

    [Fact]
    public async Task ExplicitIndexedFolderAuthorizesItsCompleteNativeMediaRange()
    {
        using var f = new TestFixture(); var folder = f.Folder("光鸭云盘/Source/selected");
        var a = f.Episode("光鸭云盘/Source/selected/a.strm", parent: folder).Object;
        var b = f.Episode("光鸭云盘/Source/selected/b.strm", parent: folder).Object;
        using var response = await f.Delete(folder);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(new[] { a.Path, b.Path }.Order(), payload.RootElement.GetProperty("paths").EnumerateArray().Select(p => p.GetString()).Order());
        Assert.Equal(folder.Path, Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray()).GetProperty("root").GetString());
        Assert.Equal("folder", Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray()).GetProperty("kind").GetString());
        ExportMfContract(f, "folder");
    }

    [Fact]
    public async Task SelectedMovieIncludesIndexedNativeAdditionalPartsButNeverAnUnrelatedSibling()
    {
        using var f = new TestFixture(); var movie = f.Movie("光鸭云盘/Source/parts/film-CD1.strm").Object;
        var part = f.Part(movie, "光鸭云盘/Source/parts/film-CD2.strm").Object;
        movie.AdditionalParts = [part.Path];
        Assert.Null(f.Library.Object.FindByPath(part.Path, false)); // 官方普通路径查询不返回 owned 部件。
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(new[] { movie.Path, part.Path }.Order(), payload.RootElement.GetProperty("paths").EnumerateArray().Select(p => p.GetString()).Order());
        Assert.Equal(2, payload.RootElement.GetProperty("fingerprints").EnumerateObject().Count());
        ExportMfContract(f, "movie-parts");
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task OverlappingExplicitFolderAndChildAlwaysKeepTheCompleteFolderScope(bool childFirst)
    {
        using var f = new TestFixture(); var folder = f.Folder("光鸭云盘/Source/selected");
        var first = f.Episode("光鸭云盘/Source/selected/a.strm", parent: folder).Object;
        var second = f.Episode("光鸭云盘/Source/selected/b.strm", parent: folder).Object;
        using var response = childFirst ? await f.Batch(first, folder) : await f.Batch(folder, first);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(2, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        var scope = Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray());
        Assert.Equal("folder", scope.GetProperty("kind").GetString());
        Assert.Equal(new[] { first.Path, second.Path }.Order(), scope.GetProperty("paths").EnumerateArray().Select(p => p.GetString()).Order());
        ExportMfContract(f, childFirst ? "child-first" : "folder-first");
    }

    [Theory]
    [InlineData("unknown")]
    [InlineData("sibling")]
    [InlineData("local")]
    [InlineData("denied")]
    [InlineData("hidden")]
    [InlineData("outside")]
    public async Task ProvenAdditionalPartsNeverBypassIndexPermissionOrNativeRange(string change)
    {
        using var f = new TestFixture(); var movie = f.Movie("parts/CD1.strm").Object;
        var part = f.Part(movie, change == "outside" ? "outside/CD2.strm" : "parts/CD2.strm");
        movie.AdditionalParts = [part.Object.Path];
        switch (change)
        {
            case "unknown": f.Items.Remove(part.Object.Id); break;
            case "sibling": f.Movie("parts/unrelated.strm"); break;
            case "local": File.WriteAllText(Path.Combine(Path.GetDirectoryName(movie.Path)!, "local.mp4"), "local"); break;
            case "denied": part.Setup(i => i.CanDelete(It.IsAny<User>())).Returns(false); break;
            case "hidden": f.Hidden.Add(part.Object.Id); break;
        }
        using var response = await f.Delete(movie);
        Assert.False(response.IsSuccessStatusCode); AssertNoWrites(f);
    }

    [Fact]
    public async Task FolderCapabilityMustBeAdvertisedBeforeAnyDeletePost()
    {
        using var f = new TestFixture(); var folder = f.Folder();
        f.Episode("series/a.strm", parent: folder);
        f.Handler.Capabilities = "{\"scope_version\":1,\"kinds\":[\"movie\",\"episode\",\"season\",\"series\"]}";
        using var response = await f.Delete(folder);
        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    private static void ExportMfContract(TestFixture f, string name)
    {
        // 本地验收可把真实发送字节交给 Python 的真实 MF 校验/执行函数。
        var output = Environment.GetEnvironmentVariable("MEDIAFLUX_DELETE_CONTRACT_DIR");
        if (string.IsNullOrEmpty(output)) return;
        Directory.CreateDirectory(output);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        var files = Directory.EnumerateFiles(f.Root, "*", SearchOption.AllDirectories)
            .Where(DeletePlanner.IsStrm).ToDictionary(MappingSet.Normalize, p => Convert.ToBase64String(File.ReadAllBytes(p)));
        File.WriteAllText(Path.Combine(output, name + ".json"), JsonSerializer.Serialize(new { root = f.Root, payload = payload.RootElement, files }));
    }

    [Theory]
    [InlineData("episode", false)]
    [InlineData("movie", false)]
    [InlineData("movie", true)]
    [InlineData("season", true)]
    [InlineData("series", true)]
    public async Task ScopesUseOnlyTheActualNativeRangeAndCompleteRawFingerprints(string kind, bool directory)
    {
        using var f = new TestFixture();
        BaseItem item;
        BaseItem[] videos;
        if (kind == "episode") { item = f.Episode().Object; videos = [item]; }
        else if (kind == "movie") { item = f.Movie(directoryDelete: directory).Object; videos = [item]; }
        else
        {
            var series = f.Series();
            var season = f.Season(parent: series);
            videos = [f.Episode("series/Season 1/a.strm", parent: season).Object, f.Episode("series/Season 1/b.STRM", parent: season).Object];
            item = kind == "series" ? series : season;
        }
        var before = videos.ToDictionary(v => v.Path, v => "sha256:" + Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(v.Path))).ToLowerInvariant());
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        var scope = Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray());
        Assert.Equal(kind, scope.GetProperty("kind").GetString());
        Assert.Equal(directory ? kind == "movie" ? Path.GetDirectoryName(item.Path) : item.Path : null, scope.GetProperty("root").GetString());
        Assert.Equal(before.Keys.Order(), scope.GetProperty("paths").EnumerateArray().Select(p => p.GetString()).Order());
        Assert.Equal(before.Keys.Order(), payload.RootElement.GetProperty("paths").EnumerateArray().Select(p => p.GetString()).Order());
        Assert.Equal(before.Count, payload.RootElement.GetProperty("fingerprints").EnumerateObject().Count());
        foreach (var (path, hash) in before) Assert.Equal(hash, payload.RootElement.GetProperty("fingerprints").GetProperty(path).GetString());
        if (kind is "season" or "series") Assert.All(videos, v => Assert.Contains(v.Id, f.NativeChildDeletes));
    }

    [Fact]
    public async Task SingleEpisodeDoesNotAuthorizeItsSeasonOrSibling()
    {
        using var f = new TestFixture(); var season = f.Season();
        var a = f.Episode("series/Season 1/a.strm", parent: season).Object;
        var b = f.Episode("series/Season 1/b.strm", allowed: false, parent: season).Object;
        using var response = await f.Delete(a);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        var scope = Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray());
        Assert.Equal(JsonValueKind.Null, scope.GetProperty("root").ValueKind);
        Assert.Equal(a.Path, Assert.Single(scope.GetProperty("paths").EnumerateArray()).GetString());
        Assert.True(File.Exists(b.Path)); Assert.Empty(f.NativeChildDeletes);
    }

    [Fact]
    public async Task MixedBatchSendsAllFourScopesInOneWrite()
    {
        using var f = new TestFixture();
        var movie = f.Movie().Object; var episode = f.Episode().Object;
        var season = f.Season("standalone-season"); f.Episode("standalone-season/a.strm", parent: season);
        var series = f.Series(); var childSeason = f.Season(parent: series); f.Episode("series/Season 1/a.strm", parent: childSeason);
        using var response = await f.Batch(movie, episode, season, series);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(4, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(new[] { "movie", "episode", "season", "series" }.Order(), payload.RootElement.GetProperty("scopes").EnumerateArray().Select(s => s.GetProperty("kind").GetString()).Order());
        Assert.Equal(4, payload.RootElement.GetProperty("fingerprints").EnumerateObject().Count());
    }

    [Theory]
    [InlineData("movie")]
    [InlineData("season")]
    [InlineData("series")]
    public async Task OwnedDirectoryAuthorizesExistingNonMediaFilesIncluding1X(string kind)
    {
        using var f = new TestFixture(); var item = ManagedDirectory(f, kind);
        var root = kind == "movie" ? Path.GetDirectoryName(item.Path)! : item.Path;
        foreach (var name in new[] { "1.X", "1", "poster.jpg", "notes.weird", "info.nfo" }) File.WriteAllText(Path.Combine(root, name), "existing companion");
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        Assert.Equal(root, Assert.Single(payload.RootElement.GetProperty("scopes").EnumerateArray()).GetProperty("root").GetString());
        Assert.Single(payload.RootElement.GetProperty("paths").EnumerateArray());
    }

    [Theory]
    [InlineData("movie", "mp4")]
    [InlineData("season", "MKV")]
    [InlineData("series", "flac")]
    [InlineData("series", "mp4")]
    public async Task ManagedDirectoryMixedWithLocalMediaRejectsTheWholeRequest(string kind, string extension)
    {
        using var f = new TestFixture(); var item = ManagedDirectory(f, kind);
        var root = kind == "movie" ? Path.GetDirectoryName(item.Path)! : item.Path;
        File.WriteAllText(Path.Combine(root, "local." + extension), "local media");
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData("season", false)]
    [InlineData("series", true)]
    public async Task UnknownOrOtherOwnedStrmCannotBecomeDirectoryAuthorization(string kind, bool indexed)
    {
        using var f = new TestFixture(); var item = ManagedDirectory(f, kind);
        var name = Path.GetRelativePath(f.Root, Path.Combine(item.Path, "other.strm"));
        if (indexed) f.Episode(name); else File.WriteAllText(Path.Combine(f.Root, name), "unknown");
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Fact]
    public async Task IndependentMovieCannotDeleteAnotherLibraryObjectsDirectory()
    {
        using var f = new TestFixture(); var series = f.Series("movie"); var movie = f.Movie().Object;
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
        Assert.True(File.Exists(movie.Path)); Assert.True(Directory.Exists(series.Path));
    }

    [Theory]
    [InlineData("season", false)]
    [InlineData("series", true)]
    public async Task EveryChildMustBeVisibleAndDeletableBeforeCloudWrite(string kind, bool hidden)
    {
        using var f = new TestFixture(); var item = ManagedDirectory(f, kind);
        var child = f.Items.Values.OfType<Episode>().Single();
        if (hidden) f.Hidden.Add(child.Id); else Mock.Get(child).Setup(i => i.CanDelete(It.IsAny<User>())).Returns(false);
        using var response = await f.Delete(item);
        Assert.Equal(hidden ? HttpStatusCode.Forbidden : HttpStatusCode.Unauthorized, response.StatusCode); AssertNoWrites(f);
    }

    [Fact]
    public async Task NativeObjectsWithoutTheirOwnStrmAlsoNeedUserAuthorization()
    {
        using var f = new TestFixture(); var series = (Series)ManagedDirectory(f, "series");
        var emptySeason = f.Season("series/empty", series); f.Hidden.Add(emptySeason.Id);
        using var response = await f.Delete(series);
        Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode); AssertNoWrites(f);
    }

    [Fact]
    public async Task NativeObjectOutsideDirectoryOrUnknownMediaObjectBlocksCloudWrite()
    {
        using var f = new TestFixture(); var series = (Series)ManagedDirectory(f, "series");
        f.Episode("outside.strm", parent: series);
        using var response = await f.Delete(series);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData("movie")]
    [InlineData("season")]
    [InlineData("series")]
    public async Task MappingRootIsNeverAnOwnedDirectoryScope(string kind)
    {
        using var f = new TestFixture();
        BaseItem item;
        if (kind == "movie") item = f.Movie("movie.strm").Object;
        else
        {
            var folder = kind == "season" ? (Folder)f.Season("") : f.Series("");
            f.Episode("a.strm", parent: folder); item = folder;
        }
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData("strm")]
    [InlineData("directory")]
    [InlineData("dangling")]
    public async Task DirectoryLinksAndDanglingLinksAreRejected(string link)
    {
        using var f = new TestFixture(); var season = (Season)ManagedDirectory(f, "season");
        var path = Path.Combine(season.Path, link == "strm" ? "linked.strm" : "linked.X");
        if (link == "directory") Directory.CreateSymbolicLink(path, season.Path);
        else File.CreateSymbolicLink(path, Path.Combine(f.Root, link == "strm" ? "target.strm" : "missing"));
        using var response = await f.Delete(season);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData("season")]
    [InlineData("series")]
    public async Task UnmanagedDirectoryStillUsesNativeBehaviorEvenWithLocalOrUnknownFiles(string kind)
    {
        using var f = new TestFixture(); Folder folder = kind == "season" ? f.Season() : f.Series();
        f.Episode(Path.GetRelativePath(f.Root, Path.Combine(folder.Path, "local.mp4")), parent: folder);
        File.WriteAllText(Path.Combine(folder.Path, "1.X"), "native content");
        using var response = await f.Delete(folder);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.NativeDeletes); Assert.Equal(0, f.Handler.CapabilityGets); Assert.Equal(0, f.Handler.DeletePosts);
    }

    [Theory]
    [InlineData("new-strm")]
    [InlineData("new-nonmedia")]
    [InlineData("new-directory")]
    [InlineData("new-native-object")]
    [InlineData("changed-fingerprint")]
    [InlineData("changed-permission")]
    [InlineData("changed-visibility")]
    [InlineData("changed-version")]
    [InlineData("dangling-link")]
    public async Task CloudSuccessCannotAuthorizeAnyNewRangeOrChangedIdentity(string change)
    {
        using var f = new TestFixture(); var series = (Series)ManagedDirectory(f, "series");
        var episode = f.Items.Values.OfType<Episode>().Single();
        f.Handler.OnPoll = () =>
        {
            switch (change)
            {
                case "new-strm": f.Episode("series/new.strm", parent: series); break;
                case "new-nonmedia": File.WriteAllText(Path.Combine(series.Path, "1.X"), "new file"); break;
                case "new-directory": Directory.CreateDirectory(Path.Combine(series.Path, "new")); break;
                case "new-native-object":
                    var newSeason = new Season { Id = Guid.NewGuid(), Path = Path.Combine(series.Path, "missing"), ParentId = series.Id };
                    f.Items[newSeason.Id] = newSeason; break;
                case "changed-fingerprint": File.AppendAllText(episode.Path, "new bytes"); break;
                case "changed-permission": Mock.Get(episode).Setup(i => i.CanDelete(It.IsAny<User>())).Returns(false); break;
                case "changed-visibility": f.Hidden.Add(episode.Id); break;
                case "changed-version": episode.PrimaryVersionId = Guid.NewGuid(); break;
                case "dangling-link": File.Delete(episode.Path); File.CreateSymbolicLink(episode.Path, Path.Combine(f.Root, "missing")); break;
            }
        };
        using var response = await f.Delete(series);
        Assert.False(response.IsSuccessStatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Theory]
    [InlineData("movie", false)]
    [InlineData("season", false)]
    [InlineData("season", true)]
    [InlineData("series", true)]
    public async Task CloudSuccessMayHaveRemovedOriginalFilesOrWholeDirectories(string kind, bool wholeDirectory)
    {
        using var f = new TestFixture(); var item = ManagedDirectory(f, kind);
        var episode = kind == "movie" ? item : f.Items.Values.OfType<Episode>().Single();
        f.Handler.OnPoll = () =>
        {
            if (wholeDirectory) Directory.Delete(item.Path, true); else File.Delete(episode.Path);
        };
        using var response = await f.Delete(item);
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(1, f.NativeDeletes);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RootNullOnlyDeletesItsNativeFileAndProvenCompanions(bool companion)
    {
        using var f = new TestFixture(); var episode = f.Episode();
        var extra = Path.Combine(f.Root, companion ? "episode.zh.srt" : "other.X"); File.WriteAllText(extra, "content");
        episode.Setup(i => i.GetDeletePaths()).Returns([new FileSystemMetadata { FullName = episode.Object.Path }, new FileSystemMetadata { FullName = extra }]);
        using var response = await f.Delete(episode.Object);
        Assert.Equal(companion ? HttpStatusCode.NoContent : HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(companion ? 1 : 0, f.Handler.DeletePosts); Assert.Equal(companion ? 1 : 0, f.NativeDeletes);
    }

    [Fact]
    public async Task OtherLibraryObjectsAreNotCompanionsEvenWithNonMediaExtension()
    {
        using var f = new TestFixture(); var movie = f.Movie().Object;
        var path = Path.Combine(Path.GetDirectoryName(movie.Path)!, "1.X"); File.WriteAllText(path, "indexed media");
        var other = new Photo { Id = Guid.NewGuid(), Path = path }; f.Items[other.Id] = other;
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Fact]
    public async Task ActualNativeFolderCanDeleteMustAuthorizeEvenEmptyDescendants()
    {
        using var f = new TestFixture(); var series = (Series)ManagedDirectory(f, "series");
        var deniedSeason = f.Season("series/empty", series);
        var allowedLibrary = new Folder { Id = Guid.NewGuid() };
        f.User.Permissions.Clear();
        f.User.Preferences.Add(new Preference(PreferenceKind.EnableContentDeletionFromFolders, allowedLibrary.Id.ToString()));
        f.Library.Setup(l => l.GetCollectionFolders(It.IsAny<BaseItem>(), It.IsAny<IEnumerable<Folder>>()))
            .Returns((BaseItem item, IEnumerable<Folder> _) => item.Id == deniedSeason.Id ? new List<Folder>() : new List<Folder> { allowedLibrary });
        Assert.True(series.CanDelete(f.User)); Assert.False(deniedSeason.CanDelete(f.User));
        using var response = await f.Delete(series);
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public async Task Version12IndexesProtectDescendantsBeforeAndAfterMf(bool linked, bool whileWaiting)
    {
        using var f = new TestFixture(); var season = ManagedDirectory(f, "season");
        var child = f.Items.Values.OfType<Episode>().Single();
        void Group()
        {
            if (linked) f.Library.Setup(l => l.GetLinkedAlternateVersions(child)).Returns([new Episode { Id = Guid.NewGuid() }]);
            else f.Library.Setup(l => l.GetLocalAlternateVersionIds(child)).Returns([Guid.NewGuid()]);
        }
        if (whileWaiting) f.Handler.OnPoll = Group; else Group();
        using var response = await f.Delete(season);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(whileWaiting ? 1 : 0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task NewFileReplacingAnOldDirectoryIsNotAnAllowedMissingDirectory()
    {
        using var f = new TestFixture(); var movie = f.Movie().Object; var root = Path.GetDirectoryName(movie.Path)!;
        f.Handler.OnPoll = () => { Directory.Delete(root, true); File.WriteAllText(root, "new file"); };
        using var response = await f.Delete(movie);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }

    [Fact]
    public async Task ParentDirectoryContainingANestedMappingRootIsRejected()
    {
        using var f = new TestFixture(); var series = ManagedDirectory(f, "series");
        f.Plugin.CommitSync(f.Plugin.Snapshot(), new MfOverview([
            new PathMapping { Local = "/mf/strm", Server = f.Root },
            new PathMapping { Local = "/mf/nested", Server = Path.Combine(series.Path, "nested") }], []));
        using var response = await f.Delete(series);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode); AssertNoWrites(f);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ExplicitlySelectedMoviesMayShareNativeDirectoryWithoutBorrowingPermissions(bool permitted)
    {
        using var f = new TestFixture();
        var first = f.Movie("shared/first.strm");
        var second = f.Movie("shared/second.strm");
        second.Setup(i => i.CanDelete(It.IsAny<User>())).Returns(permitted);
        using var response = await f.Batch(first.Object, second.Object);
        if (!permitted)
        {
            Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
            AssertNoWrites(f);
            return;
        }
        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(1, f.Handler.DeletePosts);
        Assert.Equal(2, f.NativeDeletes);
        using var payload = JsonDocument.Parse(f.Handler.DeleteBody!);
        var scopes = payload.RootElement.GetProperty("scopes").EnumerateArray().ToArray();
        Assert.Equal(2, scopes.Length);
        Assert.Equal(first.Object.Path, Assert.Single(scopes[0].GetProperty("paths").EnumerateArray()).GetString());
        Assert.Equal(second.Object.Path, Assert.Single(scopes[1].GetProperty("paths").EnumerateArray()).GetString());
        Assert.Equal(scopes[0].GetProperty("root").GetString(), scopes[1].GetProperty("root").GetString());
    }

    private static BaseItem ManagedDirectory(TestFixture f, string kind)
    {
        if (kind == "movie") return f.Movie().Object;
        var series = f.Series(); var season = f.Season(parent: series);
        f.Episode("series/Season 1/a.strm", parent: season);
        return kind == "series" ? series : season;
    }

    private static void AssertNoWrites(TestFixture f)
    {
        Assert.Equal(0, f.Handler.LoginPosts); Assert.Equal(0, f.Handler.DeletePosts); Assert.Equal(0, f.NativeDeletes);
    }
}
