using System.Runtime.CompilerServices;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;

namespace MediaFlux.Jellyfin;

public sealed class PluginServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection services, IServerApplicationHost applicationHost)
    {
        services.AddSingleton(_ => Plugin.Instance ?? throw new InvalidOperationException("MediaFlux 插件尚未初始化"));
        services.AddSingleton<MfClient>();
        services.AddScoped<ExplicitDeleteFilter>();
        services.Configure<MvcOptions>(options => options.Filters.AddService<ExplicitDeleteFilter>());
        services.AddSingleton<StrmProbeLedger>();
        if (TryWrap(() => WrapMediaSources(services))) services.AddHostedService<StrmPreProbeService>();
    }

    /// <summary>
    /// 包装层实现宿主的 IMediaSourceManager。新版 Jellyfin 若改动该接口，包装类型会加载失败；
    /// 此时只放弃起播加速，避免注册异常让 Jellyfin 把整个插件（含联动删除）标记为故障。
    /// </summary>
    public static bool TryWrap(Func<bool> wrap)
    {
        try { return wrap(); }
        catch (Exception e) when (e is TypeLoadException or MissingMemberException) { return false; }
    }

    // 包装类型只在这里被引用；不内联，确保加载失败发生在 TryWrap 的 try 之内。
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static bool WrapMediaSources(IServiceCollection services) => StrmProbeMediaSourceManager.Decorate(services);
}
