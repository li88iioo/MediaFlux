using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using MediaFlux.Jellyfin.Configuration;
using System.Xml.Linq;
using System.Xml;

namespace MediaFlux.Jellyfin;

public sealed class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public const string CompiledBaseline = "12.2.0";
    public const string SourceCheckedVersions = "12.2 / 12.3";
    public static Plugin? Instance { get; private set; }
    private readonly IServerApplicationHost _host;
    private readonly object _gate = new();
    private string _lastDeleteError = "";

    public Plugin(IApplicationPaths paths, IXmlSerializer serializer, IServerApplicationHost host)
        : base(paths, serializer)
    {
        _host = host;
        Instance = this;
        MigrateMappingConfiguration();
    }

    public override string Name => "MediaFlux";
    public override string Description => "联动 MediaFlux 删除云盘内容";
    public override Guid Id => Guid.Parse("9037f29f-2ebc-4b8a-952f-960e24402bb5");
    public bool IsTargetVersion => _host.ApplicationVersion is { Major: 12, Minor: >= 2 };

    public IEnumerable<PluginPageInfo> GetPages() =>
    [new PluginPageInfo { Name = Name, EmbeddedResourcePath = "MediaFlux.Jellyfin.Configuration.configPage.html" }];

    public PluginConfiguration Snapshot()
    {
        lock (_gate)
        {
            var c = Configuration;
            return new PluginConfiguration
            {
                Enabled = c.Enabled, MfUrl = c.MfUrl, Username = c.Username, Password = c.Password,
                TaskTimeoutSeconds = c.TaskTimeoutSeconds,
                MfMappings = c.MfMappings.Select(m => m with { }).ToArray(),
                MfLibraries = c.MfLibraries.Select(l => l with { Locations = l.Locations.ToArray() }).ToArray(),
                SyncedMfUrl = c.SyncedMfUrl, SyncedUsername = c.SyncedUsername,
                SyncedAtUtc = c.SyncedAtUtc, Revision = c.Revision
            };
        }
    }

    public static bool CacheCurrent(PluginConfiguration c) => c.SyncedAtUtc.Length > 0
        && c.MfUrl == c.SyncedMfUrl && c.Username == c.SyncedUsername;

    public static MappingSet ActiveMappings(PluginConfiguration c) => new(c.MfMappings);

    private void MigrateMappingConfiguration()
    {
        if (!File.Exists(ConfigurationFilePath)) return;
        string? mode;
        try
        {
            mode = XDocument.Load(ConfigurationFilePath).Root?.Elements()
                .FirstOrDefault(element => element.Name.LocalName == "MappingMode")?.Value;
        }
        catch (Exception e) when (e is XmlException or IOException or UnauthorizedAccessException)
        {
            // 保持宿主恢复默认关闭配置的行为，不因迁移失败阻止插件加载。
            _lastDeleteError = "插件配置无法读取，请检查配置文件后重新保存并同步。";
            return;
        }
        if (mode is null) return;
        var c = Configuration;
        if (mode != "mf")
        {
            c.Enabled = false;
            c.MfMappings = []; c.MfLibraries = [];
            c.SyncedMfUrl = ""; c.SyncedUsername = ""; c.SyncedAtUtc = "";
            _lastDeleteError = "旧手动映射已停用，请从 MediaFlux 同步范围后再启用联动删除。";
        }
        c.Revision++;
        // 单次迁移后不再保存或使用旧映射模式，避免沿用历史手动范围。
        base.UpdateConfiguration(c);
    }

    // This also validates writes from Jellyfin's built-in generic config endpoint.
    // Generic JSON cannot read or replace Password, nor inject a trusted MF cache.
    public override void UpdateConfiguration(BasePluginConfiguration configuration)
    {
        lock (_gate)
        {
            var c = (PluginConfiguration)configuration;
            var old = Snapshot();
            if (c.Password.Length == 0) c.Password = old.Password;
            c.MfMappings = old.MfMappings; c.MfLibraries = old.MfLibraries;
            c.SyncedMfUrl = old.SyncedMfUrl; c.SyncedUsername = old.SyncedUsername;
            c.SyncedAtUtc = old.SyncedAtUtc;
            c.MfUrl = c.MfUrl.Trim(); c.Username = c.Username.Trim();
            if (c.MfUrl.Length > 0) c.MfUrl = MfClient.BaseUri(c.MfUrl).AbsoluteUri.TrimEnd('/');
            if (c.Username.Length > 128 || c.Password.Length > 512) throw new PluginFault("MF 用户名或密码超出允许长度。");
            if (c.TaskTimeoutSeconds is < 5 or > 600) throw new PluginFault("任务等待时间必须为 5–600 秒。");
            if (c.Enabled)
            {
                if (!IsTargetVersion) throw new PluginFault("当前 Jellyfin 版本不满足插件 API 基线 12.2，请安装匹配版本的插件。");
                RequireCredentials(c);
                if (!CacheCurrent(c))
                    throw new PluginFault("请先关闭插件、保存凭据并成功同步 MF 范围，再启用。");
                if (ActiveMappings(c).Mappings.Length == 0)
                    throw new PluginFault("启用前必须至少配置一条有效映射。");
            }
            c.Revision = old.Revision + 1;
            base.UpdateConfiguration(c);
        }
    }

    public static void RequireCredentials(PluginConfiguration c)
    {
        if (c.MfUrl.Length == 0 || c.Username.Length == 0 || c.Password.Length == 0)
            throw new PluginFault("请配置 MF URL、用户名和密码；本地内容未删除。");
        _ = MfClient.BaseUri(c.MfUrl);
    }

    public void CommitSync(PluginConfiguration expected, MfOverview overview)
    {
        lock (_gate)
        {
            var current = Snapshot();
            if (current.Revision != expected.Revision)
                throw new PluginFault("同步期间配置已变化；请重新同步。");
            current.MfMappings = overview.Mappings;
            current.MfLibraries = overview.Libraries;
            current.SyncedMfUrl = current.MfUrl; current.SyncedUsername = current.Username;
            current.SyncedAtUtc = DateTimeOffset.UtcNow.ToString("O");
            current.Revision++;
            base.UpdateConfiguration(current);
        }
    }

    public void EnsureUnchanged(PluginConfiguration expected)
    {
        if (Snapshot().Revision != expected.Revision)
            throw new PluginFault("等待期间插件配置已变化；云任务可能已完成，请核对 MF 日志，勿盲目重试。本地删除已取消。");
    }

    public void RecordDeleteError(string error) { lock (_gate) _lastDeleteError = error; }
    public string LastDeleteError { get { lock (_gate) return _lastDeleteError; } }

    public override void SaveConfiguration(PluginConfiguration config)
    {
        lock (_gate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(ConfigurationFilePath)!);
            if (!OperatingSystem.IsWindows())
            {
                using var file = new FileStream(ConfigurationFilePath, new FileStreamOptions
                {
                    Mode = FileMode.OpenOrCreate, Access = FileAccess.Write,
                    UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite
                });
                File.SetUnixFileMode(ConfigurationFilePath, UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }
            base.SaveConfiguration(config);
        }
    }
}
