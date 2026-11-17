using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Audio;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.IO;
using MediaBrowser.Model.MediaInfo;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;
using Xunit;

namespace MediaFlux.Jellyfin.Tests;

public sealed class StrmFastStartTests
{
    private static readonly MediaStream[] PlayableStreams =
        [new MediaStream { Type = MediaStreamType.Video }, new MediaStream { Type = MediaStreamType.Audio }];

    private sealed class Harness : IDisposable
    {
        public TestFixture F { get; } = new(enabled: false);
        public Mock<IMediaSourceManager> Inner { get; } = new();
        public List<bool> Probes { get; } = [];
        public IReadOnlyList<MediaStream> Streams { get; set; } = [];
        public StrmProbeLedger Ledger { get; }
        public StrmProbeMediaSourceManager Manager { get; }

        public Harness(bool fastStart = true)
        {
            Ledger = NewLedger();
            Inner.Setup(m => m.GetMediaStreams(It.IsAny<Guid>())).Returns(() => Streams);
            // Jellyfin 的远程探测成功后会把音视频流写回库；不探测时库内流保持不变。
            Inner.Setup(m => m.GetPlaybackMediaSources(It.IsAny<BaseItem>(), It.IsAny<User>(), It.IsAny<bool>(), It.IsAny<bool>(), It.IsAny<CancellationToken>()))
                .Callback((BaseItem _, User _, bool probe, bool _, CancellationToken _) =>
                {
                    Probes.Add(probe);
                    if (probe) Streams = PlayableStreams;
                })
                .ReturnsAsync(Array.Empty<MediaSourceInfo>());
            Manager = new StrmProbeMediaSourceManager(Inner.Object, Ledger, NullLogger<StrmProbeMediaSourceManager>.Instance);
            SetFastStart(fastStart);
        }

        public StrmProbeLedger NewLedger()
        {
            var paths = new Mock<IApplicationPaths>();
            paths.SetupGet(p => p.PluginConfigurationsPath).Returns(Path.Combine(F.Root, "config"));
            return new StrmProbeLedger(paths.Object);
        }

        public void SetFastStart(bool on)
        {
            var c = F.Plugin.Snapshot();
            c.StrmFastStart = on;
            F.Plugin.UpdateConfiguration(c);
        }

        public string WriteStrm(string name, string target)
        {
            var path = Path.Combine(F.Root, name);
            File.WriteAllText(path, target + "\n");
            return path;
        }

        public Episode Strm(string name = "S01E01.strm", string target = "http://mf.invalid/playgy/a") =>
            new() { Id = Guid.NewGuid(), Path = WriteStrm(name, target) };

        public Task Play(BaseItem item, bool allowMediaProbe = true) =>
            Manager.GetPlaybackMediaSources(item, null!, allowMediaProbe, true, CancellationToken.None);

        public void Dispose() => F.Dispose();
    }

    [Fact]
    public void LedgerPersistsRecordsAndRemovalsAcrossReload()
    {
        using var h = new Harness();
        var kept = Guid.NewGuid();
        var removed = Guid.NewGuid();
        h.Ledger.Record(kept, "AA");
        h.Ledger.Record(removed, "BB");
        h.Ledger.Forget(removed);

        var reloaded = h.NewLedger();
        Assert.True(reloaded.Matches(kept, "AA"));
        Assert.False(reloaded.Matches(kept, "CC"));
        Assert.False(reloaded.Contains(removed));
    }

    [Fact]
    public void LedgerIgnoresCorruptLinesAndCompactsRedundantHistory()
    {
        using var h = new Harness();
        var id = Guid.NewGuid();
        var file = Path.Combine(h.F.Root, "config", "MediaFlux.Jellyfin.strm-probe.tsv");
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        File.WriteAllLines(file, Enumerable.Range(0, 1500).Select(i => $"{id:N}\tOLD{i}")
            .Append("not-a-guid\tX").Append("broken line").Append($"{id:N}\tNEW"));

        var ledger = h.NewLedger();
        Assert.True(ledger.Matches(id, "NEW"));
        Assert.Equal([$"{id:N}\tNEW"], File.ReadAllLines(file));
    }

    [Fact]
    public async Task RepeatPlaybackSkipsRemoteProbeOnlyAfterARecordedProbe()
    {
        using var h = new Harness();
        var item = h.Strm();

        await h.Play(item);
        await h.Play(item);
        await h.Play(item);

        Assert.Equal([true, false, false], h.Probes);
        Assert.True(h.NewLedger().Matches(item.Id, StrmProbeLedger.Fingerprint(item.Path)!));
    }

    [Fact]
    public async Task ChangedStrmTargetOrClearedStreamsProbeAgain()
    {
        using var h = new Harness();
        var item = h.Strm();
        await h.Play(item);

        // 洗版改写 .strm 目标：已存媒体流属于旧片源，必须重新探测并记录新指纹。
        h.WriteStrm("S01E01.strm", "http://mf.invalid/playgy/replacement");
        await h.Play(item);
        await h.Play(item);

        // 库内音视频流缺失时，即使指纹相同也交给 Jellyfin 探测。
        h.Streams = [new MediaStream { Type = MediaStreamType.Video }];
        await h.Play(item);

        Assert.Equal([true, true, false, true], h.Probes);
    }

    [Fact]
    public async Task DisabledSwitchAndUnrelatedItemsKeepNativeProbeDecision()
    {
        using var h = new Harness(fastStart: false);
        var strm = h.Strm();
        await h.Play(strm);
        await h.Play(strm);
        Assert.Equal([true, true], h.Probes);
        Assert.False(h.Ledger.Contains(strm.Id));

        h.SetFastStart(true);
        h.Probes.Clear();
        var file = new Episode { Id = Guid.NewGuid(), Path = Path.Combine(h.F.Root, "local.mkv") };
        var music = new Audio { Id = Guid.NewGuid(), Path = h.WriteStrm("song.strm", "http://mf.invalid/song") };
        await h.Play(file);
        await h.Play(file);
        await h.Play(music);
        await h.Play(music);
        await h.Play(strm, allowMediaProbe: false);

        Assert.Equal([true, true, true, true, false], h.Probes);
        Assert.False(h.Ledger.Contains(music.Id));
    }

    [Fact]
    public void OtherMembersDelegateToJellyfin()
    {
        using var h = new Harness();
        var item = h.Strm();
        h.Inner.Setup(m => m.GetPathProtocol("http://x")).Returns(MediaProtocol.Http);
        h.Inner.Setup(m => m.GetStaticMediaSources(item, true, null)).Returns([new MediaSourceInfo { Id = "static" }]);

        Assert.Equal(MediaProtocol.Http, h.Manager.GetPathProtocol("http://x"));
        Assert.Equal("static", Assert.Single(h.Manager.GetStaticMediaSources(item, true)).Id);
        h.Streams = PlayableStreams;
        Assert.Same(PlayableStreams, h.Manager.GetMediaStreams(item.Id));
    }

    [Fact]
    public void DecorateWrapsTypeRegisteredSingletonAndKeepsOneOfficialInstance()
    {
        using var h = new Harness();
        var services = new ServiceCollection();
        services.AddLogging();
        services.AddSingleton(Mock.Of<IApplicationPaths>(p => p.PluginConfigurationsPath == Path.Combine(h.F.Root, "config")));
        services.AddSingleton<IMediaSourceManager, StubMediaSourceManager>();
        services.AddSingleton<StrmProbeLedger>();

        Assert.True(StrmProbeMediaSourceManager.Decorate(services));
        using var provider = services.BuildServiceProvider();
        Assert.IsType<StrmProbeMediaSourceManager>(provider.GetRequiredService<IMediaSourceManager>());
        Assert.Same(provider.GetRequiredService<StubMediaSourceManager>(), provider.GetRequiredService<StubMediaSourceManager>());
        Assert.Equal(MediaProtocol.Http, provider.GetRequiredService<IMediaSourceManager>().GetPathProtocol("anything"));
    }

    [Fact]
    public void RegistratorOnlyStartsPreProbeWhenTheOfficialServiceCanBeWrapped()
    {
        var host = Mock.Of<IServerApplicationHost>();
        var wrapped = new ServiceCollection();
        wrapped.AddSingleton<IMediaSourceManager, StubMediaSourceManager>();
        new PluginServiceRegistrator().RegisterServices(wrapped, host);
        Assert.Contains(wrapped, d => d.ServiceType == typeof(IHostedService) && d.ImplementationType == typeof(StrmPreProbeService));

        var factory = new ServiceCollection();
        factory.AddSingleton<IMediaSourceManager>(_ => new StubMediaSourceManager());
        new PluginServiceRegistrator().RegisterServices(factory, host);
        Assert.DoesNotContain(factory, d => d.ServiceType == typeof(IHostedService));
        Assert.NotNull(Assert.Single(factory, d => d.ServiceType == typeof(IMediaSourceManager)).ImplementationFactory);
    }

    [Fact]
    public void WrapperLoadFailureOnlyDisablesFastStart()
    {
        // 宿主接口变化时包装类型加载失败：只放弃起播加速，不让注册异常停用整个插件；其它异常照常暴露。
        Assert.False(PluginServiceRegistrator.TryWrap(() => throw new TypeLoadException("IMediaSourceManager changed")));
        Assert.False(PluginServiceRegistrator.TryWrap(() => throw new MissingMethodException()));
        Assert.Throws<InvalidOperationException>(() => PluginServiceRegistrator.TryWrap(() => throw new InvalidOperationException()));
        Assert.True(PluginServiceRegistrator.TryWrap(() => true));
    }

    private sealed class PreProbeHarness : IDisposable
    {
        public Harness H { get; } = new();
        public Mock<ILibraryManager> Library { get; } = new();
        public Mock<IProviderManager> Providers { get; } = new();
        public List<(Guid Id, MetadataRefreshOptions Options)> Refreshes { get; } = [];
        public bool Scanning { get; set; }
        public StrmPreProbeService Service { get; }
        private readonly Dictionary<Guid, BaseItem> _items = [];
        private readonly SemaphoreSlim _refreshed = new(0);

        public PreProbeHarness(int maxPending = 200)
        {
            BaseItem.Logger = NullLogger<BaseItem>.Instance;
            BaseItem.ProviderManager = Providers.Object;
            Providers.Setup(p => p.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
                .Callback((BaseItem item, MetadataRefreshOptions options, CancellationToken _) =>
                {
                    lock (Refreshes) Refreshes.Add((item.Id, options));
                    H.Streams = PlayableStreams;
                    _refreshed.Release();
                })
                .ReturnsAsync(ItemUpdateType.MetadataImport);
            Library.Setup(l => l.GetItemById(It.IsAny<Guid>())).Returns((Guid id) => _items.GetValueOrDefault(id));
            Library.SetupGet(l => l.IsScanRunning).Returns(() => Scanning);
            Service = new StrmPreProbeService(Library.Object, Mock.Of<IFileSystem>(), H.Inner.Object, H.Ledger,
                NullLogger<StrmPreProbeService>.Instance)
                { SettleDelay = TimeSpan.FromMilliseconds(10), Gap = TimeSpan.Zero, MaxPending = maxPending };
            Service.StartAsync(CancellationToken.None).GetAwaiter().GetResult();
        }

        public T Add<T>(T item) where T : BaseItem { _items[item.Id] = item; return item; }

        public void Raise(string kind, BaseItem item)
        {
            // ItemChangeEventArgs 不继承 EventArgs，Moq 需要显式传入 sender 与参数。
            var args = new ItemChangeEventArgs { Item = item };
            if (kind == "added") Library.Raise(l => l.ItemAdded += null, Library.Object, args);
            else if (kind == "updated") Library.Raise(l => l.ItemUpdated += null, Library.Object, args);
            else Library.Raise(l => l.ItemRemoved += null, Library.Object, args);
        }

        public async Task<Guid> NextRefresh()
        {
            Assert.True(await _refreshed.WaitAsync(TimeSpan.FromSeconds(10)), "预探测未在 10 秒内执行");
            lock (Refreshes) return Refreshes[^1].Id;
        }

        public void Dispose()
        {
            Service.StopAsync(CancellationToken.None).GetAwaiter().GetResult();
            Service.Dispose();
            H.Dispose();
        }
    }

    [Fact]
    public async Task NewStrmIsPreProbedWithPlaybackProbeOptionsThenPlaysWithoutProbe()
    {
        using var p = new PreProbeHarness();
        var item = p.Add(p.H.Strm());

        p.Raise("added", item);
        Assert.Equal(item.Id, await p.NextRefresh());

        var options = Assert.Single(p.Refreshes).Options;
        Assert.True(options.EnableRemoteContentProbe);
        Assert.Equal(MetadataRefreshMode.FullRefresh, options.MetadataRefreshMode);
        await p.H.Play(item);
        Assert.Equal([false], p.H.Probes);
    }

    [Fact]
    public async Task PreProbeOnlyHandlesNewOrRetargetedStrmAndForgetsRemovedItems()
    {
        using var p = new PreProbeHarness();
        var known = p.Add(p.H.Strm("known.strm"));
        p.H.Ledger.Record(known.Id, StrmProbeLedger.Fingerprint(known.Path)!);
        var unknown = p.Add(p.H.Strm("unknown.strm"));
        var local = p.Add(new Episode { Id = Guid.NewGuid(), Path = Path.Combine(p.H.F.Root, "local.mkv") });

        // 存量未探测条目的普通更新、未变化条目和本地文件都不入队；入队的条目按 FIFO 串行处理。
        p.Raise("updated", unknown);
        p.Raise("updated", known);
        p.Raise("added", local);
        p.H.WriteStrm("known.strm", "http://mf.invalid/playgy/replacement");
        p.Raise("updated", known);
        Assert.Equal(known.Id, await p.NextRefresh());
        Assert.Single(p.Refreshes);

        p.Raise("removed", known);
        Assert.False(p.H.Ledger.Contains(known.Id));
        Assert.False(p.H.NewLedger().Contains(known.Id));
    }

    [Fact]
    public async Task PreProbeWaitsForLibraryScanAndRespectsTheSwitch()
    {
        using var p = new PreProbeHarness();
        var first = p.Add(p.H.Strm("first.strm"));
        p.Scanning = true;
        p.Raise("added", first);
        await Task.Delay(200);
        Assert.Empty(p.Refreshes);
        p.Scanning = false;
        Assert.Equal(first.Id, await p.NextRefresh());

        p.H.SetFastStart(false);
        p.Raise("added", p.Add(p.H.Strm("off.strm")));
        p.H.SetFastStart(true);
        var last = p.Add(p.H.Strm("last.strm"));
        p.Raise("added", last);
        Assert.Equal(last.Id, await p.NextRefresh());
        Assert.Equal([first.Id, last.Id], p.Refreshes.Select(r => r.Id));
    }

    [Fact]
    public async Task BulkAdditionsBeyondThePendingLimitAreLeftToFirstPlayback()
    {
        using var p = new PreProbeHarness(maxPending: 2);
        var items = Enumerable.Range(1, 3).Select(i => p.Add(p.H.Strm($"bulk{i}.strm"))).ToArray();
        p.Scanning = true;
        foreach (var item in items) p.Raise("added", item);
        p.Scanning = false;
        Assert.Equal(items[0].Id, await p.NextRefresh());
        Assert.Equal(items[1].Id, await p.NextRefresh());

        // 队列腾空后新入库条目恢复预探测；被跳过的条目首次播放时照常由 Jellyfin 探测。
        var later = p.Add(p.H.Strm("later.strm"));
        p.Raise("added", later);
        Assert.Equal(later.Id, await p.NextRefresh());
        Assert.Equal([items[0].Id, items[1].Id, later.Id], p.Refreshes.Select(r => r.Id));
        await p.H.Play(items[2]);
        Assert.Equal([true], p.H.Probes);
    }

    [Fact]
    public async Task PreProbeWithoutPlayableStreamsIsNotRecorded()
    {
        using var p = new PreProbeHarness();
        var item = p.Add(p.H.Strm());
        p.Providers.Setup(m => m.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(ItemUpdateType.MetadataImport);

        Assert.True(await p.Service.ProbeAsync(item.Id, CancellationToken.None));
        Assert.False(p.H.Ledger.Contains(item.Id));
        await p.H.Play(item);
        Assert.Equal([true], p.H.Probes);
    }

    [Fact]
    public async Task SettingsSaveFastStartWithoutMfConnectionAndReportAvailability()
    {
        using var h = new Harness(fastStart: false);
        h.F.Client.DefaultRequestHeaders.Add("X-Test-Admin", "true");
        using var before = JsonDocument.Parse(await h.F.Client.GetStringAsync("/MediaFlux/Settings"));
        Assert.False(before.RootElement.GetProperty("strm_fast_start").GetBoolean());
        Assert.False(before.RootElement.GetProperty("strm_fast_start_available").GetBoolean());

        using var saved = await h.F.Client.PostAsJsonAsync("/MediaFlux/Settings", new
        {
            enabled = false, mf_url = "http://mf.invalid", username = "mf-user", password = (string?)null,
            task_timeout_seconds = 120, strm_fast_start = true,
        });
        Assert.Equal(HttpStatusCode.OK, saved.StatusCode);
        Assert.True(h.F.Plugin.StrmFastStart);
        Assert.True(h.F.ReloadPlugin().StrmFastStart);

        BaseItem.MediaSourceManager = h.Manager;
        using var after = JsonDocument.Parse(await h.F.Client.GetStringAsync("/MediaFlux/Settings"));
        Assert.True(after.RootElement.GetProperty("strm_fast_start").GetBoolean());
        Assert.True(after.RootElement.GetProperty("strm_fast_start_available").GetBoolean());
    }
}

#nullable disable
public sealed class StubMediaSourceManager : IMediaSourceManager
{
    public void AddParts(IEnumerable<IMediaSourceProvider> providers) { }
    public IReadOnlyList<MediaStream> GetMediaStreams(Guid itemId) => [];
    public IReadOnlyList<MediaStream> GetMediaStreams(MediaStreamQuery query) => [];
    public IReadOnlyList<MediaAttachment> GetMediaAttachments(Guid itemId) => [];
    public IReadOnlyList<MediaAttachment> GetMediaAttachments(MediaAttachmentQuery query) => [];
    public Task<IReadOnlyList<MediaSourceInfo>> GetPlaybackMediaSources(BaseItem item, User user, bool allowMediaProbe, bool enablePathSubstitution, CancellationToken cancellationToken) => throw new NotSupportedException();
    public IReadOnlyList<MediaSourceInfo> GetStaticMediaSources(BaseItem item, bool enablePathSubstitution, User user = null) => [];
    public Task<MediaSourceInfo> GetMediaSource(BaseItem item, string mediaSourceId, string liveStreamId, bool enablePathSubstitution, CancellationToken cancellationToken) => throw new NotSupportedException();
    public Task<LiveStreamResponse> OpenLiveStream(LiveStreamRequest request, CancellationToken cancellationToken) => throw new NotSupportedException();
    public Task<Tuple<LiveStreamResponse, IDirectStreamProvider>> OpenLiveStreamInternal(LiveStreamRequest request, CancellationToken cancellationToken) => throw new NotSupportedException();
    public Task<MediaSourceInfo> GetLiveStream(string id, CancellationToken cancellationToken) => throw new NotSupportedException();
    public Task<Tuple<MediaSourceInfo, IDirectStreamProvider>> GetLiveStreamWithDirectStreamProvider(string id, CancellationToken cancellationToken) => throw new NotSupportedException();
    public ILiveStream GetLiveStreamInfo(string id) => null;
    public ILiveStream GetLiveStreamInfoByUniqueId(string uniqueId) => null;
    public Task<IReadOnlyList<MediaSourceInfo>> GetRecordingStreamMediaSources(ActiveRecordingInfo info, CancellationToken cancellationToken) => throw new NotSupportedException();
    public Task CloseLiveStream(string id) => Task.CompletedTask;
    public Task<MediaSourceInfo> GetLiveStreamMediaInfo(string id, CancellationToken cancellationToken) => throw new NotSupportedException();
    public bool SupportsDirectStream(string path, MediaProtocol protocol) => false;
    public MediaProtocol GetPathProtocol(string path) => MediaProtocol.Http;
    public void SetDefaultAudioAndSubtitleStreamIndices(BaseItem item, MediaSourceInfo source, User user) { }
    public Task AddMediaInfoWithProbe(MediaSourceInfo mediaSource, bool isAudio, string cacheKey, bool addProbeDelay, bool isLiveStream, CancellationToken cancellationToken) => Task.CompletedTask;
}
#nullable restore
