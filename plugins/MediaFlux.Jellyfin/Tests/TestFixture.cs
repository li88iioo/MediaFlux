using System.Net;
using System.Reflection;
using System.Security.Claims;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Xml.Serialization;
using Jellyfin.Api.Controllers;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Extensions.Json;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Common.Updates;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Configuration;
using MediaBrowser.Controller.Collections;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Activity;
using MediaBrowser.Model.Globalization;
using MediaBrowser.Model.IO;
using MediaBrowser.Model.MediaInfo;
using MediaBrowser.Model.Serialization;
using MediaFlux.Jellyfin.Configuration;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.ApplicationParts;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Moq;
using Xunit;

[assembly: CollectionBehavior(DisableTestParallelization = true)]
namespace MediaFlux.Jellyfin.Tests;

internal sealed class TestFixture : IDisposable
{
    public string Root { get; } = Path.Combine(AppContext.BaseDirectory, "fixtures", Guid.NewGuid().ToString("N"));
    public Mock<ILibraryManager> Library { get; } = new();
    public Mock<IUserManager> Users { get; } = new();
    public Dictionary<Guid, BaseItem> Items { get; } = [];
    public HashSet<Guid> Hidden { get; } = [];
    public int MfSessions { get; private set; }
    public List<Guid> NativeChildDeletes { get; } = [];
    public User User { get; } = new("tester", "auth", "reset");
    public Plugin Plugin { get; }
    public LocalPlugin InstalledPlugin { get; }
    public Func<Plugin> ReloadPlugin { get; }
    public FakeMfHandler Handler { get; } = new();
    public int NativeDeletes { get; private set; }
    private readonly WebApplication _application;
    public HttpClient Client { get; }

    public TestFixture(bool enabled = true, string? version = null)
    {
        Directory.CreateDirectory(Root);
        User.Permissions.Add(new Permission(PermissionKind.EnableContentDeletion, true));
        BaseItem.LibraryManager = Library.Object;
        var repository = new Mock<IItemRepository>();
        repository.Setup(r => r.GetItemList(It.IsAny<InternalItemsQuery>()))
            .Returns((InternalItemsQuery q) => Items.Values.Where(i => i.ParentId == q.ParentId).ToArray());
        BaseItem.ItemRepository = repository.Object;
        var fileSystem = new Mock<IFileSystem>();
        fileSystem.Setup(fs => fs.GetFileSystemInfo(It.IsAny<string>())).Returns((string p) =>
            new FileSystemMetadata { FullName = p, IsDirectory = Directory.Exists(p), Exists = File.Exists(p) || Directory.Exists(p) });
        BaseItem.FileSystem = fileSystem.Object;
        Library.Setup(l => l.GetLocalAlternateVersionIds(It.IsAny<Video>())).Returns([]);
        Library.Setup(l => l.GetLinkedAlternateVersions(It.IsAny<Video>())).Returns([]);
        var mediaSources = new Mock<IMediaSourceManager>();
        mediaSources.Setup(m => m.GetPathProtocol(It.IsAny<string>())).Returns(MediaProtocol.File);
        BaseItem.MediaSourceManager = mediaSources.Object;
        Video.RecordingsManager = Mock.Of<IRecordingsManager>();
        Library.Setup(l => l.GetUserRootFolder()).Returns(new UserRootFolder { Children = [] });
        Library.Setup(l => l.GetCollectionFolders(It.IsAny<BaseItem>(), It.IsAny<IEnumerable<Folder>>())).Returns([]);
        var paths = new Mock<IApplicationPaths>();
        paths.SetupGet(p => p.PluginsPath).Returns(Path.Combine(Root, "plugins"));
        paths.SetupGet(p => p.PluginConfigurationsPath).Returns(Path.Combine(Root, "config"));
        var serializer = new Mock<IXmlSerializer>();
        serializer.Setup(s => s.DeserializeFromFile(It.IsAny<Type>(), It.IsAny<string>())).Returns((Type t, string file) =>
        {
            using var stream = File.OpenRead(file);
            return new XmlSerializer(t).Deserialize(stream)!;
        });
        serializer.Setup(s => s.SerializeToFile(It.IsAny<object>(), It.IsAny<string>())).Callback((object o, string file) =>
        {
            using var stream = File.Create(file);
            new XmlSerializer(o.GetType()).Serialize(stream, o);
        });
        var host = new Mock<IServerApplicationHost>();
        host.SetupGet(h => h.ApplicationVersion).Returns(
            version is null ? typeof(IServerApplicationHost).Assembly.GetName().Version! : Version.Parse(version));
        ReloadPlugin = () => new Plugin(paths.Object, serializer.Object, host.Object);
        Plugin = ReloadPlugin();
        Plugin.UpdateConfiguration(new PluginConfiguration { MfUrl = "http://mf.invalid", Username = "mf-user", Password = "private-password" });
        Plugin.CommitSync(Plugin.Snapshot(), new MfOverview([new PathMapping { Local = "/mf/strm", Server = Root }], []));
        var config = Plugin.Snapshot();
        // Tests can construct enabled out-of-family configurations to check unmanaged passthrough.
        config.Enabled = enabled;
        if (Plugin.IsTargetVersion || !enabled) Plugin.UpdateConfiguration(config);
        else Plugin.Configuration.Enabled = enabled;
        Users.Setup(u => u.GetUserById(User.Id)).Returns(User);
        Library.Setup(l => l.GetItemById<BaseItem>(It.IsAny<Guid>(), It.IsAny<User?>()))
            .Returns((Guid id, User? user) => user is not null && Hidden.Contains(id) ? null : Items.GetValueOrDefault(id));
        Library.Setup(l => l.FindByPath(It.IsAny<string>(), It.IsAny<bool?>()))
            .Returns((string path, bool? folder) => Items.Values.FirstOrDefault(i => i.Path == path && (!folder.HasValue || i.IsFolder == folder)));
        Library.Setup(l => l.DeleteItem(It.IsAny<BaseItem>(), It.IsAny<DeleteOptions>(), true))
            .Callback((BaseItem item, DeleteOptions options, bool _) =>
            {
                Assert.True(options.DeleteFileLocation); NativeDeletes++;
                if (item is Folder folder) NativeChildDeletes.AddRange(folder.GetRecursiveChildren(false).Select(i => i.Id));
            });
        Library.Setup(l => l.GetVirtualFolders()).Returns([]);
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions { EnvironmentName = "Testing", ContentRootPath = Root });
        builder.WebHost.UseTestServer();
        builder.Logging.ClearProviders();
        var services = builder.Services;
        services.AddLogging();
        services.AddAuthentication("Test").AddScheme<AuthenticationSchemeOptions, TestAuthHandler>("Test", _ => { });
        services.AddAuthorization(o => o.AddPolicy("RequiresElevation", p => p.RequireRole("Administrator")));
        services.AddSingleton(Library.Object); services.AddSingleton(Users.Object);
        services.AddSingleton(Mock.Of<ISimilarItemsManager>()); services.AddSingleton(Mock.Of<ICollectionManager>());
        services.AddSingleton(Mock.Of<IProviderManager>()); services.AddSingleton(Mock.Of<IDtoService>());
        services.AddSingleton(Mock.Of<IActivityManager>()); services.AddSingleton(Mock.Of<ILocalizationManager>());
        services.AddSingleton(Mock.Of<ILibraryMonitor>()); services.AddSingleton(Mock.Of<IServerConfigurationManager>());
        services.AddSingleton(Mock.Of<IInstallationManager>());
        var plugins = new Mock<IPluginManager>();
        var manifest = JsonSerializer.Deserialize<PluginManifest>(
            File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "plugin-meta.json")), JsonDefaults.Options)!;
        InstalledPlugin = new LocalPlugin(Root, true, manifest) { Instance = Plugin };
        plugins.Setup(p => p.GetPlugin(Plugin.Id, It.IsAny<Version?>())).Returns(InstalledPlugin);
        services.AddSingleton(plugins.Object);
        new PluginServiceRegistrator().RegisterServices(services, host.Object);
        services.AddSingleton(Plugin);
        services.AddSingleton(new MfClient(() => { MfSessions++; return Handler; }, TimeSpan.FromMilliseconds(1)));
        services.AddControllers().ConfigureApplicationPartManager(p =>
        {
            p.FeatureProviders.Clear(); p.FeatureProviders.Add(new SelectedControllers());
        });
        _application = builder.Build();
        _application.UseRouting(); _application.UseAuthentication(); _application.UseAuthorization();
        _application.MapControllers();
        _application.StartAsync().GetAwaiter().GetResult();
        Client = _application.GetTestClient();
        Client.DefaultRequestHeaders.Add("X-Test-User", User.Id.ToString());
    }

    public Mock<Episode> Episode(string name = "episode.strm", bool allowed = true, Folder? parent = null)
    {
        var path = Path.Combine(Root, name); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllBytes(path, System.Text.Encoding.UTF8.GetBytes("https://example.invalid/video?x=1\r\n"));
        var item = new Mock<Episode> { CallBase = true };
        item.Object.Id = Guid.NewGuid(); item.Object.Path = path; item.Object.ParentId = parent?.Id ?? Guid.Empty;
        item.Setup(i => i.CanDelete(It.IsAny<User>())).Returns(allowed);
        item.Setup(i => i.GetDeletePaths()).Returns(() => [new FileSystemMetadata { FullName = item.Object.Path }]);
        Items[item.Object.Id] = item.Object;
        return item;
    }

    public Mock<Movie> Movie(string name = "movie/movie.strm", bool directoryDelete = true)
    {
        var path = Path.Combine(Root, name); Directory.CreateDirectory(Path.GetDirectoryName(path)!); File.WriteAllText(path, "https://example.invalid/movie\n");
        var item = new Mock<Movie> { CallBase = true };
        item.Object.Id = Guid.NewGuid(); item.Object.Path = path;
        item.Setup(i => i.CanDelete(It.IsAny<User>())).Returns(true);
        // Call the actual upstream Video.GetDeletePaths for directory-delete movies.
        item.Object.IsInMixedFolder = !directoryDelete;
        if (!directoryDelete) item.Setup(i => i.GetDeletePaths()).Returns(() => [new FileSystemMetadata { FullName = item.Object.Path }]);
        Items[item.Object.Id] = item.Object;
        return item;
    }

    public Folder Folder(string directory = "series") => DirectoryItem<Folder>(directory, null);
    public Series Series(string directory = "series") => DirectoryItem<Series>(directory, null);
    public Season Season(string directory = "series/Season 1", Folder? parent = null) => DirectoryItem<Season>(directory, parent);

    private T DirectoryItem<T>(string directory, Folder? parent) where T : Folder, new()
    {
        var path = Path.Combine(Root, directory); Directory.CreateDirectory(path);
        var item = new T { Id = Guid.NewGuid(), Path = path, ParentId = parent?.Id ?? Guid.Empty };
        // 使用官方 CanDelete、BaseItem.GetDeletePaths、GetCachedChildren 和 GetRecursiveChildren(false)。
        Items[item.Id] = item;
        return item;
    }

    public Task<HttpResponseMessage> Delete(BaseItem item) => Client.DeleteAsync("/Items/" + item.Id);
    public Task<HttpResponseMessage> Batch(params BaseItem[] items) => Client.DeleteAsync("/Items?ids=" + string.Join(',', items.Select(i => i.Id)));
    public void Dispose() { Client.Dispose(); _application.DisposeAsync().GetAwaiter().GetResult(); Directory.Delete(Root, true); }

    private sealed class SelectedControllers : IApplicationFeatureProvider<ControllerFeature>
    {
        public void PopulateFeature(IEnumerable<ApplicationPart> parts, ControllerFeature feature)
        {
            feature.Controllers.Add(typeof(LibraryController).GetTypeInfo());
            feature.Controllers.Add(typeof(PluginsController).GetTypeInfo());
            feature.Controllers.Add(typeof(SettingsController).GetTypeInfo());
        }
    }
}

internal sealed class TestAuthHandler(IOptionsMonitor<AuthenticationSchemeOptions> options, ILoggerFactory logger, UrlEncoder encoder)
    : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
{
    protected override Task<AuthenticateResult> HandleAuthenticateAsync()
    {
        var id = Request.Headers["X-Test-User"].ToString();
        if (id.Length == 0) return Task.FromResult(AuthenticateResult.NoResult());
        var claims = new List<Claim> { new("Jellyfin-UserId", id), new("Jellyfin-IsApiKey", Request.Headers["X-Test-ApiKey"].ToString()) };
        if (Request.Headers["X-Test-Admin"] == "true") claims.Add(new Claim(ClaimTypes.Role, "Administrator"));
        return Task.FromResult(AuthenticateResult.Success(new AuthenticationTicket(new ClaimsPrincipal(new ClaimsIdentity(claims, Scheme.Name)), Scheme.Name)));
    }
}

internal sealed class FakeMfHandler : HttpMessageHandler
{
    public int DeletePosts { get; private set; }
    public int LoginPosts { get; private set; }
    public int CapabilityGets { get; private set; }
    public string Capabilities { get; set; } = "{\"scope_version\":1,\"kinds\":[\"movie\",\"episode\",\"season\",\"series\"]}";
    public HttpStatusCode CapabilityCode { get; set; } = HttpStatusCode.OK;
    public List<string> Requests { get; } = [];
    public Action? OnCapabilities { get; set; }
    public string? DeleteBody { get; private set; }
    public HttpStatusCode DeleteCode { get; set; } = HttpStatusCode.Accepted;
    public string State { get; set; } = "completed";
    public bool Ok { get; set; } = true;
    public string Error { get; set; } = "后端已拒绝文件身份校验";
    public bool Offline { get; set; }
    public Action? OnPoll { get; set; }
    public string? Overview { get; set; }
    public bool RefreshToken { get; set; } = true;
    public int PollDelayMilliseconds { get; set; }
    public HttpStatusCode LoginCode { get; set; } = HttpStatusCode.Found;

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
    {
        if (Offline) throw new HttpRequestException("offline");
        var path = request.RequestUri!.AbsolutePath;
        Requests.Add(request.Method + " " + path);
        if (path == "/login" && request.Method == HttpMethod.Get) return Reply("<input value='before-login' name='csrf_token'>");
        if (path == "/login")
        {
            LoginPosts++;
            var form = await request.Content!.ReadAsStringAsync(token);
            Assert.Contains("csrf_token=before-login", form);
            var response = Reply("", LoginCode); response.Headers.Location = new Uri("/", UriKind.Relative); return response;
        }
        if (path == "/") return Reply("<meta content='" + (RefreshToken ? "after-login" : "before-login") + "' name='csrf-token'>");
        if (path == "/api/media-libraries/overview") return Reply(Overview ?? "{\"servers\":[]}");
        if (path == "/api/jellyfin/delete-capabilities")
        {
            Assert.Equal(HttpMethod.Get, request.Method);
            Assert.True(LoginPosts > 0);
            CapabilityGets++;
            OnCapabilities?.Invoke();
            return Reply(Capabilities, CapabilityCode);
        }
        if (path == "/api/jellyfin/delete")
        {
            DeletePosts++;
            Assert.Equal("after-login", request.Headers.GetValues("X-CSRF-Token").Single());
            DeleteBody = await request.Content!.ReadAsStringAsync(token);
            return Reply("{\"task_id\":\"writer-test-1\"}", DeleteCode);
        }
        if (path == "/api/guangya/organize/status")
        {
            Assert.Contains("task_id=writer-test-1", request.RequestUri.Query);
            if (PollDelayMilliseconds > 0) await Task.Delay(PollDelayMilliseconds, token);
            OnPoll?.Invoke();
            return Reply(JsonSerializer.Serialize(new { status = State, result = new { ok = Ok, error = Error } }));
        }
        throw new InvalidOperationException("Unexpected test HTTP path: " + path);
    }
    private static HttpResponseMessage Reply(string text, HttpStatusCode code = HttpStatusCode.OK) => new(code) { Content = new StringContent(text) };
    protected override void Dispose(bool disposing) { }
}
