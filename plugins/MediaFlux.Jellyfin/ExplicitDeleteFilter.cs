using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;

namespace MediaFlux.Jellyfin;

// Only the pinned native explicit HTTP actions, never ItemRemoved or scan disappearance.
public sealed class ExplicitDeleteFilter(Plugin plugin, ILibraryManager library, IUserManager users, MfClient mf)
    : IAsyncActionFilter, IOrderedFilter
{
    public int Order => -1000;

    public static bool IsDeleteAction(ActionExecutingContext context) => context.HttpContext.Request.Method == "DELETE"
        && context.ActionDescriptor is ControllerActionDescriptor action
        && action.ControllerTypeInfo.FullName == "Jellyfin.Api.Controllers.LibraryController"
        && action.ActionName is "DeleteItem" or "DeleteItems";

    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (!IsDeleteAction(context))
        {
            await next().ConfigureAwait(false);
            return;
        }
        var config = plugin.Snapshot();
        if (!config.Enabled)
        {
            await next().ConfigureAwait(false);
            return;
        }
        var token = context.HttpContext.RequestAborted;
        try
        {
            var ids = context.ActionArguments.TryGetValue("itemId", out var id) && id is Guid single
                ? new[] { single }
                : context.ActionArguments.TryGetValue("ids", out var many) && many is Guid[] batch ? batch : [];
            if (ids.Length == 0 || ids.Any(i => i == Guid.Empty)) throw new PluginFault("请选择有效的 Jellyfin 媒体条目。", 400);
            var principal = context.HttpContext.User;
            // Constants verified in Jellyfin.Api/Constants/InternalClaimTypes.cs.
            var apiKey = bool.TryParse(principal.FindFirst("Jellyfin-IsApiKey")?.Value, out var isKey) && isKey;
            var userId = Guid.TryParse(principal.FindFirst("Jellyfin-UserId")?.Value, out var parsed) ? parsed : Guid.Empty;
            User? user = apiKey || userId == Guid.Empty ? null : users.GetUserById(userId);
            var items = ids.Distinct().Select(i => library.GetItemById<BaseItem>(i, user)
                ?? throw new PluginFault("选中的媒体不存在或对当前用户不可见；整批删除已取消。", 404)).ToArray();
            // Preflight EVERY selected item before ANY cloud write. Native batch action checks one-by-one.
            if (user is not null && items.Any(i => !i.CanDelete(user)))
                throw new PluginFault("当前用户没有删除全部所选媒体的权限；整批删除已取消。", 401);
            var mappings = Plugin.ActiveMappings(config);
            var plan = await DeletePlanner.PrepareAsync(items, mappings, library, user, token).ConfigureAwait(false);
            if (plan.Fingerprints.Count > 0)
            {
                if (!plugin.IsTargetVersion) throw new PluginFault("当前 Jellyfin 版本不满足插件 API 基线 12.2；本地删除已取消，请安装匹配版本的插件。", 503);
                Plugin.RequireCredentials(config);
                if (!Plugin.CacheCurrent(config))
                    throw new PluginFault("MF 范围缓存不属于当前连接配置；请先关闭插件并重新同步。本地删除已取消。");
                async Task Recheck(bool completed, CancellationToken checkToken)
                {
                    plugin.EnsureUnchanged(config);
                    var currentUser = users.GetUserById(userId)
                        ?? throw new PluginFault("等待期间用户已不存在；本地删除已取消。如已提交云任务，请核对 MF 日志。", 401);
                    var currentItems = plan.Items.Select(p => library.GetItemById<BaseItem>(p.Item.Id, currentUser)
                        ?? throw new PluginFault("媒体条目已不存在或不可见；本地删除已取消。如已提交云任务，请核对 MF 日志。", 404)).ToArray();
                    if (currentItems.Any(i => !i.CanDelete(currentUser)))
                        throw new PluginFault("等待期间删除权限已变化；本地删除已取消。如已提交云任务，请核对 MF 日志。", 401);
                    await DeletePlanner.RecheckAsync(plan, currentItems, mappings, library, currentUser, completed, checkToken).ConfigureAwait(false);
                }
                await mf.DeleteAsync(config, plan, t => Recheck(false, t), token).ConfigureAwait(false);
                await Recheck(true, token).ConfigureAwait(false);
            }
        }
        catch (PluginFault fault)
        {
            Reject(context, fault.Message, fault.StatusCode);
            return;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or OperationCanceledException)
        {
            Reject(context, "无法安全读取 STRM、检查原生删除范围，或请求已中断；本地删除已取消。如已提交云任务，请先核对 MF 日志。", 409);
            return;
        }
        await next().ConfigureAwait(false);
    }

    private void Reject(ActionExecutingContext context, string message, int status)
    {
        plugin.RecordDeleteError(message);
        // Message is also present for Jellyfin clients using the legacy error-body convention.
        context.Result = new ObjectResult(new { Message = message, Title = "MediaFlux 已取消本地删除", Detail = message })
        {
            StatusCode = status
        };
    }
}
