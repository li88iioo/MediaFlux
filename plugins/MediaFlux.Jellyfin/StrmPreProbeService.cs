using System.Collections.Concurrent;
using System.Threading.Channels;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.IO;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace MediaFlux.Jellyfin;

/// <summary>
/// 新入库或目标地址已变化的 .strm 在后台串行预探测一次，使首播也不必等远程探测。
/// 只处理入库/变更事件，不补探存量；每次之间留间隔，避免集中请求网盘直链。
/// </summary>
public sealed class StrmPreProbeService(
    ILibraryManager library, IFileSystem fileSystem, IMediaSourceManager mediaSources,
    StrmProbeLedger ledger, ILogger<StrmPreProbeService> logger) : IHostedService, IDisposable
{
    private readonly Channel<Guid> _queue = Channel.CreateUnbounded<Guid>();
    private readonly ConcurrentDictionary<Guid, byte> _pending = new();
    private readonly CancellationTokenSource _stop = new();
    private Task _worker = Task.CompletedTask;

    /// <summary>入库扫描自身也会刷新新条目；先等它落盘，避免两次刷新互相覆盖媒体流。</summary>
    public TimeSpan SettleDelay { get; init; } = TimeSpan.FromSeconds(5);
    public TimeSpan Gap { get; init; } = TimeSpan.FromSeconds(1);

    /// <summary>重建媒体库等批量入库时不集中请求网盘直链；超出部分留到首次播放时由 Jellyfin 探测。</summary>
    public int MaxPending { get; init; } = 200;

    public Task StartAsync(CancellationToken cancellationToken)
    {
        library.ItemAdded += OnItemAdded;
        library.ItemUpdated += OnItemUpdated;
        library.ItemRemoved += OnItemRemoved;
        _worker = Task.Run(() => WorkAsync(_stop.Token), CancellationToken.None);
        return Task.CompletedTask;
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        library.ItemAdded -= OnItemAdded;
        library.ItemUpdated -= OnItemUpdated;
        library.ItemRemoved -= OnItemRemoved;
        await _stop.CancelAsync().ConfigureAwait(false);
        await Task.WhenAny(_worker, Task.Delay(Timeout.Infinite, cancellationToken)).ConfigureAwait(false);
    }

    public void Dispose() => _stop.Dispose();

    private static bool IsStrmVideo(BaseItem item) => item is Video && StrmProbeLedger.IsStrm(item.Path);

    private void OnItemAdded(object? sender, ItemChangeEventArgs e)
    {
        if (StrmProbeMediaSourceManager.Enabled && IsStrmVideo(e.Item)) Enqueue(e.Item.Id);
    }

    // 只处理已探测过且内容已变的条目（洗版原地改写）；从未探测的存量条目不在这里补探。
    private void OnItemUpdated(object? sender, ItemChangeEventArgs e)
    {
        if (!StrmProbeMediaSourceManager.Enabled || !IsStrmVideo(e.Item) || !ledger.Contains(e.Item.Id)) return;
        var fingerprint = StrmProbeLedger.Fingerprint(e.Item.Path);
        if (fingerprint is not null && !ledger.Matches(e.Item.Id, fingerprint)) Enqueue(e.Item.Id);
    }

    private void OnItemRemoved(object? sender, ItemChangeEventArgs e) => ledger.Forget(e.Item.Id);

    private void Enqueue(Guid id)
    {
        if (_pending.Count < MaxPending && _pending.TryAdd(id, 0)) _queue.Writer.TryWrite(id);
    }

    private async Task WorkAsync(CancellationToken token)
    {
        try
        {
            await foreach (var id in _queue.Reader.ReadAllAsync(token).ConfigureAwait(false))
            {
                await Task.Delay(SettleDelay, token).ConfigureAwait(false);
                while (library.IsScanRunning) await Task.Delay(SettleDelay, token).ConfigureAwait(false);
                _pending.TryRemove(id, out _);
                if (!StrmProbeMediaSourceManager.Enabled) continue;
                if (await ProbeAsync(id, token).ConfigureAwait(false)) await Task.Delay(Gap, token).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
        }
    }

    /// <summary>与 Jellyfin 播放前的探测参数一致；返回是否实际发起了远程探测。</summary>
    public async Task<bool> ProbeAsync(Guid id, CancellationToken token)
    {
        if (library.GetItemById(id) is not Video item || !StrmProbeLedger.IsStrm(item.Path)
            || StrmProbeLedger.Fingerprint(item.Path) is not { } fingerprint)
            return false;
        if (ledger.Matches(id, fingerprint) && StrmProbeMediaSourceManager.HasVideoAndAudio(mediaSources.GetMediaStreams(id)))
            return false;
        try
        {
            await item.RefreshMetadata(
                new MetadataRefreshOptions(new DirectoryService(fileSystem))
                {
                    EnableRemoteContentProbe = true,
                    MetadataRefreshMode = MetadataRefreshMode.FullRefresh,
                },
                token).ConfigureAwait(false);
            if (StrmProbeMediaSourceManager.HasVideoAndAudio(mediaSources.GetMediaStreams(id))) ledger.Record(id, fingerprint);
            else logger.LogWarning("STRM 起播加速：预探测未取得音视频流 {Item}", id);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            logger.LogWarning(e, "STRM 起播加速：预探测失败 {Item}", id);
        }
        return true;
    }
}
