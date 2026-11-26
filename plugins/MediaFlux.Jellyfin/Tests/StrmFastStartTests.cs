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
        public bool ProbeFails { get; set; }
        public StrmProbeLedger Ledger { get; }
        public StrmProbeMediaSourceManager Manager { get; }

        public Harness(bool fastStart = true)
        {
            Ledger = NewLedger();
            Inner.Setup(m => m.GetMediaStreams(It.IsAny<Guid>())).Returns(() => Streams);
            // 与 Jellyfin 一致：探测成功才写回媒体流并推进刷新时间；失败或不探测时两者都保持原样。
            Inner.Setup(m => m.GetPlaybackMediaSources(It.IsAny<BaseItem>(), It.IsAny<User>(), It.IsAny<bool>(), It.IsAny<bool>(), It.IsAny<CancellationToken>()))
                .Callback((BaseItem item, User _, bool probe, bool _, CancellationToken _) =>
                {
                    Probes.Add(probe);
                    if (probe && !ProbeFails) Completed(item);
                })
                .ReturnsAsync(Array.Empty<MediaSourceInfo>());
            Manager = new StrmProbeMediaSourceManager(Inner.Object, Ledger, NullLogger<StrmProbeMediaSourceManager>.Instance);
            SetFastStart(fastStart);
        }

        public void Completed(BaseItem item)
        {
            Streams = PlayableStreams;
            item.DateLastRefreshed = (item.DateLastRefreshed > DateTime.UtcNow ? item.DateLastRefreshed : DateTime.UtcNow).AddSeconds(1);
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
        File.WriteAllLines(file, Enumerable.Range(0, 1500).Select(i => $"2\t{id:N}\tOLD{i}")
            .Append("2\tnot-a-guid\tX").Append("broken line").Append($"2\t{id:N}\tNEW")
            .Append($"{id:N}\tLEGACY").Append($"{id:N}\t"));

        var ledger = h.NewLedger();
        Assert.True(ledger.Matches(id, "NEW"));
        Assert.Equal([$"2\t{id:N}\tNEW"], File.ReadAllLines(file));
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
    public async Task UnreadableLedgerUsesNativeProbeOnFirstPlayback()
    {
        using var h = new Harness();
        var item = h.Strm();
        var file = Path.Combine(h.F.Root, "config", "MediaFlux.Jellyfin.strm-probe.tsv");
        var line = $"2\t{item.Id:N}\t{StrmProbeLedger.Fingerprint(item.Path)}";
        File.WriteAllLines(file, [line]);
        h.Streams = PlayableStreams;

        using (File.Open(file, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
        {
            await h.Play(item);
            Assert.Equal([true], h.Probes);
            await h.Play(item);
            Assert.Equal([true, false], h.Probes);
        }

        Assert.Equal([line], File.ReadAllLines(file));
    }

    [Fact]
    public async Task LegacyUnverifiedRecordsRequireASuccessfulProbeAndThenSurviveReload()
    {
        using var h = new Harness();
        var item = h.Strm();
        var untouched = h.Strm("untouched.strm");
        var file = Path.Combine(h.F.Root, "config", "MediaFlux.Jellyfin.strm-probe.tsv");
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        File.WriteAllLines(file, new[] { item, untouched }
            .Select(i => $"{i.Id:N}\t{StrmProbeLedger.Fingerprint(i.Path)}"));
        h.Streams = PlayableStreams;
        h.ProbeFails = true;
        await h.Play(item);
        Assert.Equal([true], h.Probes);
        Assert.False(h.Ledger.Contains(item.Id));
        Assert.False(h.Ledger.Contains(untouched.Id));

        h.ProbeFails = false;
        await h.Play(item);
        var reloaded = h.NewLedger();
        var manager = new StrmProbeMediaSourceManager(h.Inner.Object, reloaded, NullLogger<StrmProbeMediaSourceManager>.Instance);
        await manager.GetPlaybackMediaSources(item, null!, true, true, CancellationToken.None);
        Assert.Equal([true, true, false], h.Probes);
        Assert.False(reloaded.Contains(untouched.Id));
        reloaded.Forget(item.Id);
        Assert.False(h.NewLedger().Contains(item.Id));
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
    public async Task FailedProbeLeavingOldStreamsIsNeverRecorded()
    {
        using var h = new Harness();
        var item = h.Strm();
        await h.Play(item);

        // 洗版后探测失败：库内仍是旧片源的音视频流，不能登记新指纹，否则会长期沿用错误信息。
        h.WriteStrm("S01E01.strm", "http://mf.invalid/playgy/replacement");
        h.ProbeFails = true;
        await h.Play(item);
        await h.Play(item);
        Assert.False(h.Ledger.Matches(item.Id, StrmProbeLedger.Fingerprint(item.Path)!));

        // 装插件前已探测过的存量条目，首次探测失败同样不登记。
        var existing = h.Strm("existing.strm");
        h.Streams = PlayableStreams;
        await h.Play(existing);
        Assert.False(h.Ledger.Contains(existing.Id));

        h.ProbeFails = false;
        await h.Play(item);
        await h.Play(item);
        Assert.Equal([true, true, true, true, true, false], h.Probes);
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
                    H.Completed(item);
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
    public async Task ItemLookupFailureLeavesTheWorkerAvailableForLaterAdditions()
    {
        using var p = new PreProbeHarness();
        var missing = p.Add(p.H.Strm("lookup-failure.strm"));
        var next = p.Add(p.H.Strm("next.strm"));
        p.Library.Setup(l => l.GetItemById(missing.Id)).Throws(new IOException("library read failed"));

        p.Raise("added", missing);
        p.Raise("added", next);
        Assert.Equal(next.Id, await p.NextRefresh());
        Assert.False(p.H.Ledger.Contains(missing.Id));
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

    [Theory]
    [InlineData("partial")]
    [InlineData("exception")]
    [InlineData("cancelled")]
    public async Task FailedPreProbeIgnoresItsOwnUpdateButAllowsALaterRetry(string failure)
    {
        using var p = new PreProbeHarness();
        var item = p.Add(p.H.Strm("changed.strm"));
        p.H.Ledger.Record(item.Id, StrmProbeLedger.Fingerprint(item.Path)!);
        p.H.WriteStrm("changed.strm", "http://mf.invalid/playgy/replacement");
        p.H.Streams = PlayableStreams;
        var barriers = new[] { p.Add(p.H.Strm("after-failure.strm")), p.Add(p.H.Strm("after-retry.strm")) };
        var completed = barriers.Select(_ => new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously)).ToArray();
        var attempts = 0;
        var recover = false;
        p.Providers.Setup(m => m.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
            .Callback((BaseItem refreshed, MetadataRefreshOptions _, CancellationToken _) =>
            {
                if (refreshed.Id != item.Id)
                {
                    p.H.Completed(refreshed);
                    completed[Array.FindIndex(barriers, b => b.Id == refreshed.Id)].TrySetResult();
                    return;
                }
                var attempt = Interlocked.Increment(ref attempts);
                if (recover) p.H.Completed(refreshed);
                // 宿主即使部分提供方失败，仍会保存其它结果并发出 ItemUpdated。
                if (attempt == 1) p.Raise("updated", refreshed);
                p.Raise("added", barriers[Math.Min(attempt - 1, 1)]);
                if (!recover && failure == "exception") throw new IOException("probe failed");
                if (!recover && failure == "cancelled") throw new OperationCanceledException("provider timeout");
            })
            .ReturnsAsync(ItemUpdateType.MetadataImport);

        p.Raise("updated", item);
        // 哨兵在自身更新事件之后入队，无需依赖睡眠来判断是否重复探测。
        await completed[0].Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(1, attempts);
        Assert.False(p.H.Ledger.Matches(item.Id, StrmProbeLedger.Fingerprint(item.Path)!));

        recover = true;
        p.Raise("updated", item);
        await completed[1].Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(2, attempts);
        await p.H.Play(item);
        Assert.Equal([false], p.H.Probes);
    }

    [Fact]
    public async Task StoppingTheServiceCancelsTheProbeWithoutRecordingItOrDrainingMoreItems()
    {
        using var p = new PreProbeHarness();
        var item = p.Add(p.H.Strm());
        var later = p.Add(p.H.Strm("later.strm"));
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var calls = 0;
        CancellationToken probeToken = default;
        p.Providers.Setup(m => m.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
            .Returns(async (BaseItem _, MetadataRefreshOptions _, CancellationToken token) =>
            {
                Interlocked.Increment(ref calls);
                probeToken = token;
                entered.TrySetResult();
                await Task.Delay(Timeout.Infinite, token);
                return ItemUpdateType.None;
            });
        p.Raise("added", item);
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
        p.Raise("added", later);
        await p.Service.StopAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(probeToken.IsCancellationRequested);
        Assert.Equal(1, calls);
        Assert.False(p.H.Ledger.Contains(item.Id));
        Assert.False(p.H.Ledger.Contains(later.Id));
    }

    [Fact]
    public async Task PreProbeIsRecordedOnlyWhenTheRefreshCompletesWithStreams()
    {
        using var p = new PreProbeHarness();
        var stale = p.Add(p.H.Strm("stale.strm"));
        var empty = p.Add(p.H.Strm("empty.strm"));
        // 刷新报告失败（刷新时间未推进）时即使库内残留旧音视频流也不登记。
        p.Providers.Setup(m => m.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(ItemUpdateType.MetadataImport);
        p.H.Streams = PlayableStreams;
        Assert.True(await p.Service.ProbeAsync(stale.Id, CancellationToken.None));
        Assert.False(p.H.Ledger.Contains(stale.Id));

        // 刷新完成但未取得音视频流同样不登记。
        p.Providers.Setup(m => m.RefreshSingleItem(It.IsAny<BaseItem>(), It.IsAny<MetadataRefreshOptions>(), It.IsAny<CancellationToken>()))
            .Callback((BaseItem item, MetadataRefreshOptions _, CancellationToken _) => item.DateLastRefreshed = DateTime.UtcNow.AddMinutes(1))
            .ReturnsAsync(ItemUpdateType.MetadataImport);
        p.H.Streams = [];
        Assert.True(await p.Service.ProbeAsync(empty.Id, CancellationToken.None));
        Assert.False(p.H.Ledger.Contains(empty.Id));

        await p.H.Play(stale);
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
