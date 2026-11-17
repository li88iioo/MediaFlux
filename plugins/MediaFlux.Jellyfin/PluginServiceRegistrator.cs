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
        if (StrmProbeMediaSourceManager.Decorate(services)) services.AddHostedService<StrmPreProbeService>();
    }
}
