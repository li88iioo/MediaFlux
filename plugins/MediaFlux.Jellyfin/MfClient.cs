using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using MediaFlux.Jellyfin.Configuration;

namespace MediaFlux.Jellyfin;

public sealed record MfOverview(PathMapping[] Mappings, LibraryScope[] Libraries);

public sealed class MfClient
{
    private readonly Func<HttpMessageHandler> _handlerFactory;
    private readonly TimeSpan _pollInterval;

    public MfClient() : this(() => new HttpClientHandler
    {
        AllowAutoRedirect = false, CookieContainer = new CookieContainer(), UseCookies = true
        // Standard OS certificate validation; no insecure certificate callback.
    }, TimeSpan.FromSeconds(1)) { }

    public MfClient(Func<HttpMessageHandler> handlerFactory, TimeSpan pollInterval)
    {
        _handlerFactory = handlerFactory;
        _pollInterval = pollInterval;
    }

    public static Uri BaseUri(string url)
    {
        if (!Uri.TryCreate(url.Trim().TrimEnd('/') + "/", UriKind.Absolute, out var uri)
            || uri.Scheme is not ("http" or "https") || uri.UserInfo.Length > 0
            || uri.Query.Length > 0 || uri.Fragment.Length > 0)
            throw new PluginFault("MF URL 必须为 HTTP(S) 地址，不能包含凭据、查询或片段。");
        return uri;
    }

    public async Task TestAsync(PluginConfiguration c, CancellationToken token)
    {
        await ExecuteAsync(c, async session =>
        {
            using var data = await session.GetJsonAsync("api/media-libraries/overview", token).ConfigureAwait(false);
            if (!data.RootElement.TryGetProperty("servers", out var servers) || servers.ValueKind != JsonValueKind.Array)
                throw new PluginFault("MF overview 响应缺少 servers 列表。", 502);
            return true;
        }, token).ConfigureAwait(false);
    }

    public Task<MfOverview> OverviewAsync(PluginConfiguration c, CancellationToken token) =>
        ExecuteAsync(c, async session =>
        {
            using var data = await session.GetJsonAsync("api/media-libraries/overview", token).ConfigureAwait(false);
            if (!data.RootElement.TryGetProperty("servers", out var servers) || servers.ValueKind != JsonValueKind.Array)
                throw new PluginFault("MF overview 响应缺少 servers 列表。", 502);
            var candidates = servers.EnumerateArray().Where(s => Text(s, "server_type") == "jellyfin").ToArray();
            if (candidates.Length != 1) throw new PluginFault("MF overview 必须恰好包含一个 server_type=jellyfin 配置。");
            var server = candidates[0];
            if (!True(server, "enabled") || !True(server, "configured"))
                throw new PluginFault("MF 中的 Jellyfin 尚未启用或未配置完整。");
            foreach (var name in new[] { "mapping_error", "library_error" })
                if (Text(server, name).Length > 0)
                    throw new PluginFault("MF 范围同步失败：" + session.SafeError(Text(server, name)));
            if (!server.TryGetProperty("mappings", out var mappings) || mappings.ValueKind != JsonValueKind.Array
                || !server.TryGetProperty("libraries", out var libraries) || libraries.ValueKind != JsonValueKind.Array)
                throw new PluginFault("MF Jellyfin 配置缺少 mappings 或 libraries 列表。", 502);
            var validMappings = new MappingSet(mappings.EnumerateArray().Select(m => new PathMapping
            {
                Local = Text(m, "local"), Server = Text(m, "server")
            })).Mappings;
            if (validMappings.Length == 0) throw new PluginFault("MF Jellyfin 没有有效映射；保留上一次范围缓存。");
            var validLibraries = libraries.EnumerateArray().Select(l => new LibraryScope
            {
                Id = Text(l, "id"), Name = Text(l, "name"),
                Locations = l.GetProperty("locations").EnumerateArray()
                    .Select(p => MappingSet.Normalize(p.GetString() ?? "")).ToArray()
            }).ToArray();
            return new MfOverview(validMappings, validLibraries);
        }, token);

    public Task<bool> DeleteAsync(PluginConfiguration c, DeletePlan plan, Func<CancellationToken, Task> beforeSubmit, CancellationToken token) =>
        ExecuteAsync(c, async session =>
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
            deadline.CancelAfter(TimeSpan.FromSeconds(c.TaskTimeoutSeconds));
            var waitToken = deadline.Token;
            // 每次在同一登录会话握手，绝不让旧 MF 忽略 scopes 后假成功。
            using var capabilities = await session.GetJsonAsync("api/jellyfin/delete-capabilities", waitToken).ConfigureAwait(false);
            var capability = capabilities.RootElement;
            if (capability.ValueKind != JsonValueKind.Object
                || !capability.TryGetProperty("scope_version", out var version) || !version.TryGetInt32(out var number) || number != 1
                || !capability.TryGetProperty("kinds", out var kinds) || kinds.ValueKind != JsonValueKind.Array
                || kinds.EnumerateArray().Any(k => k.ValueKind != JsonValueKind.String)
                || !new[] { "movie", "episode", "season", "series" }.All(k => kinds.EnumerateArray().Any(v => v.GetString() == k)))
                throw new PluginFault("MF 不支持完整 scopes v1 删除协议；请升级 MF。本地删除已取消，未提交云任务。", 502);
            await beforeSubmit(waitToken).ConfigureAwait(false);
            var payload = new DeleteRequest(plan.Fingerprints.Keys.ToArray(), "mf", plan.Fingerprints, plan.Scopes);
            using var request = new HttpRequestMessage(HttpMethod.Post, "api/jellyfin/delete")
            {
                Content = new StringContent(JsonSerializer.Serialize(payload), System.Text.Encoding.UTF8, "application/json")
            };
            request.Headers.Add("X-CSRF-Token", session.Csrf);
            // Send exactly once. Never re-login/replay this write after a 403, timeout, or disconnect.
            using var response = await session.Http.SendAsync(request, waitToken).ConfigureAwait(false);
            Session.CheckStatus(response, HttpStatusCode.Accepted);
            using var accepted = JsonDocument.Parse(await response.Content.ReadAsStringAsync(waitToken).ConfigureAwait(false));
            var taskId = Text(accepted.RootElement, "task_id");
            if (!Regex.IsMatch(taskId, @"^[A-Za-z0-9_.-]{1,128}$"))
                throw new PluginFault("MF 已接收请求，但没有返回有效 task_id；请核对 MF 日志，勿盲目重试。", 502);
            while (true)
            {
                using var status = await session.GetJsonAsync("api/guangya/organize/status?task_id=" + Uri.EscapeDataString(taskId), waitToken).ConfigureAwait(false);
                var root = status.RootElement;
                var state = Text(root, "status");
                if (state == "completed" && root.TryGetProperty("result", out var result) && True(result, "ok"))
                    return true;
                if (state is not ("pending" or "queued" or "running"))
                {
                    var error = root.TryGetProperty("result", out var failedResult) ? Text(failedResult, "error") : "";
                    if (error.Length == 0) error = Text(root, "error");
                    if (error.Length == 0) error = "任务未确认全部成功（状态 " + state + "）。";
                    throw new PluginFault("MF 任务 " + taskId + "：" + session.SafeError(error)
                        + " 本地删除已取消；请核对 MF 日志，勿盲目重试。");
                }
                await Task.Delay(_pollInterval, waitToken).ConfigureAwait(false);
            }
        }, token);

    private async Task<T> ExecuteAsync<T>(PluginConfiguration c, Func<Session, Task<T>> action, CancellationToken token)
    {
        Plugin.RequireCredentials(c);
        try
        {
            using var http = new HttpClient(_handlerFactory()) { BaseAddress = BaseUri(c.MfUrl), Timeout = TimeSpan.FromSeconds(30) };
            var session = new Session(http, c);
            await session.LoginAsync(token).ConfigureAwait(false);
            return await action(session).ConfigureAwait(false);
        }
        catch (HttpRequestException)
        {
            throw new PluginFault("无法连接 MF 或 TLS 证书校验失败；本地删除已取消。如已提交云任务，请核对 MF 日志，勿盲目重试。", 503);
        }
        catch (OperationCanceledException)
        {
            throw new PluginFault("等待 MF 超时或请求已中断；云任务可能仍在运行，本地删除已取消。请核对 MF 日志，勿盲目重试。", 504);
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException or KeyNotFoundException)
        {
            throw new PluginFault("MF 响应格式不符合约定；本地删除已取消。如已提交任务，请核对 MF 日志，勿盲目重试。", 502);
        }
    }

    private static string Text(JsonElement obj, string name) => obj.ValueKind == JsonValueKind.Object
        && obj.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : "";
    private static bool True(JsonElement obj, string name) => obj.ValueKind == JsonValueKind.Object
        && obj.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.True;

    private sealed record DeleteRequest(
        [property: JsonPropertyName("paths")] string[] Paths,
        [property: JsonPropertyName("mapping_mode")] string MappingMode,
        [property: JsonPropertyName("fingerprints")] IReadOnlyDictionary<string, string> Fingerprints,
        [property: JsonPropertyName("scopes")] DeleteScope[] Scopes);

    private sealed class Session(HttpClient http, PluginConfiguration c)
    {
        public HttpClient Http { get; } = http;
        public string Csrf { get; private set; } = "";

        public async Task LoginAsync(CancellationToken token)
        {
            using var initial = await Http.GetAsync("login", token).ConfigureAwait(false);
            CheckStatus(initial, HttpStatusCode.OK);
            var loginCsrf = HtmlToken(await initial.Content.ReadAsStringAsync(token).ConfigureAwait(false), "input", "csrf_token", "value");
            using var form = new FormUrlEncodedContent(new Dictionary<string, string>
            {
                ["username"] = c.Username, ["password"] = c.Password, ["csrf_token"] = loginCsrf
            });
            using var submitted = await Http.PostAsync("login", form, token).ConfigureAwait(false);
            if (submitted.StatusCode is not (HttpStatusCode.Found or HttpStatusCode.SeeOther))
                throw new PluginFault("MF 登录失败：请检查用户名、密码、CSRF 或登录限流。本地删除未执行。", 502);
            var location = submitted.Headers.Location;
            var redirect = location is null ? null : new Uri(Http.BaseAddress!, location);
            if (redirect is null || !SameService(redirect)
                || redirect.AbsolutePath.TrimEnd('/').EndsWith("/login", StringComparison.Ordinal)
                || redirect.AbsolutePath.TrimEnd('/').EndsWith("/setup", StringComparison.Ordinal))
                throw new PluginFault("MF 登录未进入已认证页面；本地删除未执行。", 502);
            using var home = await Http.GetAsync(redirect, token).ConfigureAwait(false);
            CheckStatus(home, HttpStatusCode.OK);
            Csrf = HtmlToken(await home.Content.ReadAsStringAsync(token).ConfigureAwait(false), "meta", "csrf-token", "content");
            if (Csrf == loginCsrf) throw new PluginFault("MF 登录没有刷新 CSRF；已取消本地删除。", 502);
        }

        private bool SameService(Uri uri) => uri.Scheme == Http.BaseAddress!.Scheme && uri.Authority == Http.BaseAddress.Authority
            && uri.AbsolutePath.StartsWith(Http.BaseAddress.AbsolutePath, StringComparison.Ordinal);

        public async Task<JsonDocument> GetJsonAsync(string path, CancellationToken token)
        {
            using var response = await Http.GetAsync(path, token).ConfigureAwait(false);
            CheckStatus(response, HttpStatusCode.OK);
            return JsonDocument.Parse(await response.Content.ReadAsStringAsync(token).ConfigureAwait(false));
        }

        public static void CheckStatus(HttpResponseMessage response, HttpStatusCode expected)
        {
            if (response.StatusCode != expected)
                throw new PluginFault($"MF 返回 HTTP {(int)response.StatusCode}，未获得授权或未确认操作；本地删除已取消。请核对 MF 日志，勿盲目重试。", 502);
        }

        public string SafeError(string text)
        {
            foreach (var secret in new[] { c.Password, Uri.EscapeDataString(c.Password), WebUtility.UrlEncode(c.Password), Csrf })
                if (!string.IsNullOrEmpty(secret)) text = text.Replace(secret, "[已隐藏]", StringComparison.Ordinal);
            text = new string(text.Select(ch => char.IsControl(ch) ? ' ' : ch).ToArray());
            return text.Length > 512 ? text[..512] + "…" : text;
        }

        private static string HtmlToken(string html, string tag, string name, string valueAttribute)
        {
            foreach (Match element in Regex.Matches(html, "<" + tag + @"\b[^>]*>", RegexOptions.IgnoreCase))
            {
                var attributes = Regex.Matches(element.Value, @"([a-zA-Z-]+)\s*=\s*(?:""([^""]*)""|'([^']*)')")
                    .ToDictionary(m => m.Groups[1].Value.ToLowerInvariant(), m => WebUtility.HtmlDecode(m.Groups[2].Success ? m.Groups[2].Value : m.Groups[3].Value));
                if (attributes.GetValueOrDefault("name") == name
                    && attributes.TryGetValue(valueAttribute, out var value) && value.Length is > 0 and <= 1024)
                    return value;
            }
            throw new PluginFault("MF 页面缺少 CSRF token；已取消本地删除。", 502);
        }
    }
}
