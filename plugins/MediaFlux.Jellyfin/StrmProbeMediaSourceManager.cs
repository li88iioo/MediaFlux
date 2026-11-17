#nullable disable
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace MediaFlux.Jellyfin;

/// <summary>
/// Jellyfin 对 .strm 每次 PlaybackInfo 都强制远程探测。已探测且内容未变的 .strm
/// 改为不探测，其余逻辑与全部其它成员原样委托给 Jellyfin 官方实现。
/// </summary>
public sealed class StrmProbeMediaSourceManager(
    IMediaSourceManager inner, StrmProbeLedger ledger, ILogger<StrmProbeMediaSourceManager> logger) : IMediaSourceManager
{
    /// <summary>
    /// 插件注册晚于核心服务。仅接管按类型注册的单例官方实现；宿主改用工厂或实例注册时
    /// 保持原样，设置页显示起播加速不可用。
    /// </summary>
    public static bool Decorate(IServiceCollection services)
    {
        var original = services.LastOrDefault(d => d.ServiceType == typeof(IMediaSourceManager));
        if (original is not { Lifetime: ServiceLifetime.Singleton, ImplementationType: { } implementation }) return false;
        services.Remove(original);
        services.AddSingleton(implementation);
        services.AddSingleton<IMediaSourceManager>(sp => new StrmProbeMediaSourceManager(
            (IMediaSourceManager)sp.GetRequiredService(implementation),
            sp.GetRequiredService<StrmProbeLedger>(),
            sp.GetRequiredService<ILogger<StrmProbeMediaSourceManager>>()));
        return true;
    }

    // IMediaSourceManager 在插件实例创建前就会被解析，开关只能在调用时读取。
    public static bool Enabled => Plugin.Instance?.StrmFastStart == true;

    public static bool HasVideoAndAudio(IReadOnlyList<MediaStream> streams) =>
        streams.Any(s => s.Type == MediaStreamType.Video) && streams.Any(s => s.Type == MediaStreamType.Audio);

    public async Task<IReadOnlyList<MediaSourceInfo>> GetPlaybackMediaSources(
        BaseItem item, User user, bool allowMediaProbe, bool enablePathSubstitution, CancellationToken cancellationToken)
    {
        if (!allowMediaProbe || !Enabled || item is not Video || !StrmProbeLedger.IsStrm(item.Path))
            return await inner.GetPlaybackMediaSources(item, user, allowMediaProbe, enablePathSubstitution, cancellationToken).ConfigureAwait(false);

        var fingerprint = StrmProbeLedger.Fingerprint(item.Path);
        if (fingerprint is not null && ledger.Matches(item.Id, fingerprint) && HasVideoAndAudio(inner.GetMediaStreams(item.Id)))
            return await inner.GetPlaybackMediaSources(item, user, false, enablePathSubstitution, cancellationToken).ConfigureAwait(false);

        var sources = await inner.GetPlaybackMediaSources(item, user, true, enablePathSubstitution, cancellationToken).ConfigureAwait(false);
        if (fingerprint is not null && HasVideoAndAudio(inner.GetMediaStreams(item.Id)))
        {
            ledger.Record(item.Id, fingerprint);
            logger.LogDebug("STRM 起播加速：已记录探测结果 {Item}", item.Id);
        }
        return sources;
    }

    public void AddParts(IEnumerable<IMediaSourceProvider> providers) => inner.AddParts(providers);
    public IReadOnlyList<MediaStream> GetMediaStreams(Guid itemId) => inner.GetMediaStreams(itemId);
    public IReadOnlyList<MediaStream> GetMediaStreams(MediaStreamQuery query) => inner.GetMediaStreams(query);
    public IReadOnlyList<MediaAttachment> GetMediaAttachments(Guid itemId) => inner.GetMediaAttachments(itemId);
    public IReadOnlyList<MediaAttachment> GetMediaAttachments(MediaAttachmentQuery query) => inner.GetMediaAttachments(query);
    public IReadOnlyList<MediaSourceInfo> GetStaticMediaSources(BaseItem item, bool enablePathSubstitution, User user = null) =>
        inner.GetStaticMediaSources(item, enablePathSubstitution, user);
    public Task<MediaSourceInfo> GetMediaSource(BaseItem item, string mediaSourceId, string liveStreamId, bool enablePathSubstitution, CancellationToken cancellationToken) =>
        inner.GetMediaSource(item, mediaSourceId, liveStreamId, enablePathSubstitution, cancellationToken);
    public Task<LiveStreamResponse> OpenLiveStream(LiveStreamRequest request, CancellationToken cancellationToken) => inner.OpenLiveStream(request, cancellationToken);
    public Task<Tuple<LiveStreamResponse, IDirectStreamProvider>> OpenLiveStreamInternal(LiveStreamRequest request, CancellationToken cancellationToken) =>
        inner.OpenLiveStreamInternal(request, cancellationToken);
    public Task<MediaSourceInfo> GetLiveStream(string id, CancellationToken cancellationToken) => inner.GetLiveStream(id, cancellationToken);
    public Task<Tuple<MediaSourceInfo, IDirectStreamProvider>> GetLiveStreamWithDirectStreamProvider(string id, CancellationToken cancellationToken) =>
        inner.GetLiveStreamWithDirectStreamProvider(id, cancellationToken);
    public ILiveStream GetLiveStreamInfo(string id) => inner.GetLiveStreamInfo(id);
    public ILiveStream GetLiveStreamInfoByUniqueId(string uniqueId) => inner.GetLiveStreamInfoByUniqueId(uniqueId);
    public Task<IReadOnlyList<MediaSourceInfo>> GetRecordingStreamMediaSources(ActiveRecordingInfo info, CancellationToken cancellationToken) =>
        inner.GetRecordingStreamMediaSources(info, cancellationToken);
    public Task CloseLiveStream(string id) => inner.CloseLiveStream(id);
    public Task<MediaSourceInfo> GetLiveStreamMediaInfo(string id, CancellationToken cancellationToken) => inner.GetLiveStreamMediaInfo(id, cancellationToken);
    public bool SupportsDirectStream(string path, MediaProtocol protocol) => inner.SupportsDirectStream(path, protocol);
    public MediaProtocol GetPathProtocol(string path) => inner.GetPathProtocol(path);
    public void SetDefaultAudioAndSubtitleStreamIndices(BaseItem item, MediaSourceInfo source, User user) =>
        inner.SetDefaultAudioAndSubtitleStreamIndices(item, source, user);
    public Task AddMediaInfoWithProbe(MediaSourceInfo mediaSource, bool isAudio, string cacheKey, bool addProbeDelay, bool isLiveStream, CancellationToken cancellationToken) =>
        inner.AddMediaInfoWithProbe(mediaSource, isAudio, cacheKey, addProbeDelay, isLiveStream, cancellationToken);
}
