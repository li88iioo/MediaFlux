using System.Security.Cryptography;
using System.Text.Json.Serialization;
using Emby.Naming.Common;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.IO;

namespace MediaFlux.Jellyfin;

public sealed record DeleteScope(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("root")] string? Root,
    [property: JsonPropertyName("paths")] string[] Paths);
public sealed record ItemIdentity(Guid Id, string Path, string ItemType);
public sealed record SelectedItem(ItemIdentity Item, FileSystemMetadata[] DeletePaths,
    ItemIdentity[] Children, FileSystemMetadata[] Entries, DeleteScope? Scope);
public sealed record DeletePlan(Dictionary<string, string> Fingerprints, SelectedItem[] Items, ItemIdentity[] StrmItems)
{
    public DeleteScope[] Scopes
    {
        get
        {
            var claimed = new HashSet<string>(Fingerprints.Comparer);
            // 完整目录先认领其全部成员，父目录先于子目录；不依赖用户勾选顺序。
            return Items.Where(i => i.Scope is not null).Select(i => i.Scope!)
                .OrderBy(s => s.Root is null ? 1 : 0)
                .ThenBy(s => s.Root?.Length ?? int.MaxValue)
                .ThenBy(s => s.Root, StringComparer.Ordinal)
                .ThenBy(s => s.Kind, StringComparer.Ordinal)
                .Select(s => s with { Paths = s.Paths.Where(claimed.Add).ToArray() })
                .Where(s => s.Paths.Length > 0).ToArray();
        }
    }
}

public static class DeletePlanner
{
    private static readonly StringComparer PathComparer = OperatingSystem.IsWindows()
        ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
    private static readonly NamingOptions Naming = new();
    private static readonly HashSet<string> MediaExtensions = new(
        Naming.VideoFileExtensions.Concat(Naming.AudioFileExtensions).Concat(Naming.StubFileExtensions), StringComparer.OrdinalIgnoreCase);

    public static Task<DeletePlan> PrepareAsync(IReadOnlyList<BaseItem> items, MappingSet mappings,
        ILibraryManager library, User? user, CancellationToken token) =>
        InspectAsync(items, mappings, library, user, null, false, token);

    public static async Task RecheckAsync(DeletePlan plan, IReadOnlyList<BaseItem> items, MappingSet mappings,
        ILibraryManager library, User user, bool mfCompleted, CancellationToken token)
    {
        _ = await InspectAsync(items, mappings, library, user, plan, mfCompleted, token).ConfigureAwait(false);
    }

    // 准备、提交前、成功后使用同一套范围/对象/权限检查；成功后仅放宽“原有文件已消失”。
    private static async Task<DeletePlan> InspectAsync(IReadOnlyList<BaseItem> items, MappingSet mappings,
        ILibraryManager library, User? user, DeletePlan? previous, bool allowMissing, CancellationToken token)
    {
        var snapshots = new List<SelectedItem>();
        var strmItems = new Dictionary<Guid, ItemIdentity>();
        var paths = new HashSet<string>(PathComparer);
        var previousOwners = previous?.StrmItems.ToHashSet();
        var nativeChildren = items.ToDictionary(i => i.Id, i =>
        {
            if (i is not Folder folder) return Array.Empty<BaseItem>();
            folder.ReleaseCachedChildren();
            var children = folder.GetRecursiveChildren(false).ToArray();
            if (children.Length > 10000) throw new PluginFault("原生删除对象过多；请在 MF 中操作。");
            return children;
        });
        var members = MediaMembers(items, nativeChildren, mappings, library, user);
        var ownedMedia = members.Values.SelectMany(v => v).DistinctBy(v => v.Id).ToDictionary(v => v.Id);
        var ownersByPath = new Dictionary<string, Video>(PathComparer);
        foreach (var member in ownedMedia.Values)
            if (!ownersByPath.TryAdd(PathKey(member.Path), member))
                throw new PluginFault("同一 STRM 对应多个媒体对象，无法确认归属；整批删除已取消。");
        var explicitlySelected = members.Values.SelectMany(v => v).Select(v => PathKey(v.Path)).ToHashSet(PathComparer);
        foreach (var item in items)
        {
            token.ThrowIfCancellationRequested();
            var identity = Identity(item);
            var old = previous?.Items.Single(i => i.Item.Id == item.Id);
            if (old is not null && old.Item != identity)
                throw new PluginFault("等待期间媒体条目已改名、换路径或类型；本地删除已取消。请核对 MF 日志。");
            var roots = item.GetDeletePaths().Select(CopyPath).ToArray();
            if (old is not null)
            {
                foreach (var root in roots)
                {
                    var original = old.DeletePaths.FirstOrDefault(p => PathComparer.Equals(p.FullName, root.FullName));
                    if (original is null || (original.IsDirectory != root.IsDirectory
                        && !(allowMissing && !File.Exists(root.FullName) && !Directory.Exists(root.FullName))))
                        throw new PluginFault("等待期间原生删除范围出现新的文件或目录；本地删除已取消。请核对 MF 日志。");
                }
                // 原生文件系统在目录消失后可能报告 IsDirectory=false；仍复核最初的完整范围。
                roots = old.DeletePaths;
            }
            // 同时冻结原生子项和有索引的分段归属；等待期间不能借关系变化扩张范围。
            var children = nativeChildren[item.Id];
            var nativeItems = children.Append(item).Concat(members[item.Id]).DistinctBy(i => i.Id).ToArray();
            var childIdentities = nativeItems.Select(Identity).ToArray();
            if (old is not null && !childIdentities.ToHashSet().IsSubsetOf(old.Children.ToHashSet()))
                throw new PluginFault("等待期间原生递归删除出现新增或变化的媒体对象；本地删除已取消。请核对 MF 日志。");
            var relevant = roots.Any(r => mappings.Overlaps(r.FullName)) || children.Append(item).Any(i => IsManaged(i.Path, mappings));
            var scan = relevant ? Scan(roots, token) : new ScanResult();
            if (old is not null)
            {
                var originalEntries = old.Entries.ToDictionary(e => e.FullName, e => e.IsDirectory, PathComparer);
                if (scan.Entries.Any(e => !originalEntries.TryGetValue(e.FullName, out var directory) || directory != e.IsDirectory))
                    throw new PluginFault("等待期间原生目录出现新增文件、STRM 或目录；本地删除已取消。请核对 MF 日志。");
            }
            var managed = scan.Entries.Where(e => !e.IsDirectory && IsManaged(e.FullName, mappings))
                .Select(e => e.FullName).Concat(nativeItems.Where(i => IsManaged(i.Path, mappings)).Select(i => PathKey(i.Path)))
                .ToHashSet(PathComparer);
            if (old?.Scope is not null) managed.UnionWith(old.Scope.Paths);
            DeleteScope? scope = null;
            if (managed.Count > 0)
            {
                if (user is null) throw new PluginFault("已管理 STRM 必须由真实 Jellyfin 用户发起，并通过 CanDelete 权限检查；不允许 API key 绕过。", 403);
                foreach (var root in roots.Where(r => mappings.Overlaps(r.FullName))) CheckNoLinks(root.FullName);
                if (scan.Links.Count > 0) throw new PluginFault("原生删除目录包含符号链接或重解析点，不能安全确认 STRM 范围。");
                if (!allowMissing && scan.MissingDirectories.Count > 0)
                    throw new PluginFault("无法读取原生删除目录；不能确认完整 STRM 范围，本地删除已取消。");
                scope = Scope(item, roots, managed, mappings, explicitlySelected,
                    members[item.Id].Select(v => PathKey(v.Path)).ToHashSet(PathComparer));
                if (old is not null && (old.Scope is null || old.Scope.Kind != scope.Kind || !PathComparer.Equals(old.Scope.Root, scope.Root)))
                    throw new PluginFault("等待期间纳管删除范围发生变化；本地删除已取消。请核对 MF 日志。");
                var nativeIds = nativeItems.Select(i => i.Id).ToHashSet();
                foreach (var entry in scan.Entries)
                {
                    if (IsStrm(entry.FullName) && !entry.IsDirectory)
                    {
                        if (!scope.Paths.Contains(entry.FullName, PathComparer) && !explicitlySelected.Contains(entry.FullName))
                            throw new PluginFault("原生删除还会移除未明确选中或未纳管的 STRM；整批请求已取消。");
                        continue;
                    }
                    if (!entry.IsDirectory && MediaExtensions.Contains(Path.GetExtension(entry.FullName)))
                        throw new PluginFault("原生删除范围混放本地视频或其它媒体文件；不能扩大目录删除。");
                    if (scope.Root is null && !IsCompanion(entry.FullName, item.Path))
                        throw new PluginFault("原生文件删除含非自身路径或无法确认归属的伴随文件；本地删除已取消。");
                    var owner = library.FindByPath(entry.FullName, null);
                    if (owner is not null && (!entry.IsDirectory || !nativeIds.Contains(owner.Id)))
                        throw new PluginFault("原生删除目录包含库内其它归属对象；不能扩大目录删除。");
                }
                foreach (var native in nativeItems)
                {
                    var current = Authorize(native, library, user);
                    if (!current.IsFileProtocol || (current is not Folder
                        && (current is not Video || !members[item.Id].Any(v => v.Id == current.Id))))
                        throw new PluginFault("原生递归删除含未知媒体对象或非文件对象；不能扩大目录删除。");
                    if (scope.Root is not null && !Within(current.Path, scope.Root))
                        throw new PluginFault("原生递归删除对象位于所选目录之外；不能扩大目录删除。");
                    if (current is Video)
                    {
                        if (!IsManaged(current.Path, mappings) || !scope.Paths.Contains(PathKey(current.Path), PathComparer))
                            throw new PluginFault("原生递归删除混放本地视频或范围外 STRM；不能扩大目录删除。");
                        strmItems[current.Id] = Identity(current);
                    }
                }
                paths.UnionWith(scope.Paths);
            }
            snapshots.Add(new SelectedItem(identity, roots, childIdentities, scan.Entries.ToArray(), scope));
        }
        if (paths.Count > 1000) throw new PluginFault("一次最多删除 1000 个 STRM 文件。");
        if (previous is not null && !paths.SetEquals(previous.Fingerprints.Keys))
            throw new PluginFault("等待期间 STRM 删除集合发生变化；本地删除已取消。请核对 MF 日志。");
        var fingerprints = new Dictionary<string, string>(PathComparer);
        foreach (var path in paths.OrderBy(p => p, PathComparer))
        {
            token.ThrowIfCancellationRequested();
            CheckNoLinks(path); // 先查链接，不能将悬空链接视作 MF 已移除的文件。
            if (allowMissing && !File.Exists(path))
            {
                if (Directory.Exists(path)) throw new PluginFault("等待期间 STRM 被替换成目录；本地删除已取消。");
                fingerprints.Add(path, previous!.Fingerprints[path]);
                continue;
            }
            if (!ownersByPath.TryGetValue(path, out var owner))
                throw new PluginFault("原生删除目录存在未知、未入库 STRM；不能扩大目录删除。");
            var current = Authorize(owner, library, user!);
            if (!PathComparer.Equals(PathKey(current.Path), path) || !current.IsFileProtocol
                || current is not Video || !ownedMedia.ContainsKey(current.Id))
                throw new PluginFault("STRM 没有对应可授权的电影或单集对象；本地删除已取消。");
            var identity = Identity(current);
            if (!strmItems.ContainsKey(current.Id))
                throw new PluginFault("STRM 属于所选作品之外的库对象；不能扩大目录删除。");
            if (previous is not null && !previousOwners!.Contains(identity))
                throw new PluginFault("等待期间 STRM 对应的媒体对象发生变化；本地删除已取消。请核对 MF 日志。");
            strmItems[current.Id] = identity;
            var fingerprint = await FingerprintAsync(path, token).ConfigureAwait(false);
            if (previous is not null && previous.Fingerprints[path] != fingerprint)
                throw new PluginFault("等待云任务期间 STRM 内容已变化，本地删除已取消。云任务可能已成功，请核对 MF 日志，勿盲目重试。");
            fingerprints.Add(path, fingerprint);
        }
        return new DeletePlan(fingerprints, snapshots.ToArray(), strmItems.Values.ToArray());
    }

    private static DeleteScope Scope(BaseItem item, FileSystemMetadata[] roots, HashSet<string> managed, MappingSet mappings,
        HashSet<string> explicitlySelected, HashSet<string> ownMedia)
    {
        var directories = roots.Where(r => r.IsDirectory).ToArray();
        var kind = item switch
        {
            Movie => "movie", Episode => "episode", Season => "season", Series => "series", Folder => "folder",
            _ => throw new PluginFault("已映射 STRM 仅支持明确选择的电影、单集、季或剧；此媒体类型不能安全联动删除。")
        };
        string? root = null;
        if (item is Folder)
        {
            if (directories.Length != 1 || !PathComparer.Equals(directories[0].FullName, PathKey(item.Path)))
                throw new PluginFault("季/剧没有唯一原生目录范围；不能扩大目录删除。");
            root = directories[0].FullName;
        }
        else
        {
            if (!IsManaged(item.Path, mappings) || managed.Any(p => !explicitlySelected.Contains(p)))
                throw new PluginFault("原生删除还会移除未明确选中的 STRM；电影/单集只能授权自身 STRM。");
            if (directories.Length > 0)
            {
                if (item is not Movie || directories.Length != 1
                    || !PathComparer.Equals(directories[0].FullName, PathKey(Path.GetDirectoryName(PathKey(item.Path)))))
                    throw new PluginFault("电影/单集原生删除范围不是独立电影父目录；不能扩大目录删除。");
                root = directories[0].FullName;
            }
        }
        if (root is not null)
        {
            root = MappingSet.Normalize(root);
            if (mappings.Match(root) is null || mappings.Mappings.Any(m => MappingSet.Within(m.Server, root)))
                throw new PluginFault("原生删除目录是映射根或包含映射根；不能扩大目录删除。");
            if (managed.Any(p => !Within(p, root)))
                throw new PluginFault("STRM 位于原生目录范围之外；不能扩大目录删除。");
        }
        if (managed.Any(p => !roots.Any(r => r.IsDirectory ? Within(p, r.FullName) : PathComparer.Equals(p, r.FullName))))
            throw new PluginFault("STRM 不在原生删除范围内；本地删除已取消。");
        // 同目录多项明确选择仍是一次合法批量删除；每个 scope 只认领自身，
        // MF 按共同目录合并授权，不能把未选择的邻居带进某个电影 scope。
        // 显式 Folder 使用独立协议类型，不冒充整剧；每个原生子项仍需逐项授权。
        var scopePaths = (item is Video ? ownMedia : managed).OrderBy(p => p, PathComparer).ToArray();
        return new DeleteScope(kind, root, scopePaths);
    }

    private static BaseItem Authorize(BaseItem item, ILibraryManager library, User user)
    {
        var current = library.GetItemById<BaseItem>(item.Id, user)
            ?? throw new PluginFault("原生删除对象对当前用户不可见或已不存在；整批删除已取消。", 403);
        if (Identity(current) != Identity(item)) throw new PluginFault("原生删除对象路径或类型已变化；本地删除已取消。");
        if (!current.CanDelete(user)) throw new PluginFault("当前用户没有删除全部子项/原生对象的权限；整批删除已取消。", 401);
        return current;
    }

    private static Dictionary<Guid, Video[]> MediaMembers(IReadOnlyList<BaseItem> items,
        Dictionary<Guid, BaseItem[]> children, MappingSet mappings, ILibraryManager library, User? user)
    {
        var result = new Dictionary<Guid, Video[]>();
        foreach (var item in items)
        {
            var pending = new Queue<Video>(children[item.Id].Append(item).OfType<Video>()
                .Where(v => (v is Movie or Episode) && IsManaged(v.Path, mappings)));
            var owned = new Dictionary<Guid, Video>();
            while (pending.TryDequeue(out var video))
            {
                if (user is null) throw new PluginFault("已管理 STRM 必须由真实 Jellyfin 用户发起，并通过 CanDelete 权限检查；不允许 API key 绕过。", 403);
                var current = Authorize(video, library, user);
                if (current is not Video member || !current.IsFileProtocol
                    || !IsManaged(current.Path, mappings))
                    throw new PluginFault("分段媒体没有可授权的纳管电影或单集身份；整批删除已取消。");
                if (!owned.TryAdd(current.Id, member)) continue;
                if (owned.Count > 1000) throw new PluginFault("一次最多删除 1000 个 STRM 文件。");
                if (member is not (Movie or Episode) && member.AdditionalParts.Length > 0)
                    throw new PluginFault("额外部件不能自行扩大分段范围；整批删除已取消。");
                foreach (var partPath in member.AdditionalParts)
                {
                    // FindByPath 默认排除 owned 部件；按官方分片 ID 规则读取真实索引。
                    var part = library.GetItemById<BaseItem>(library.GetNewItemId(partPath, typeof(Video)), user);
                    if (part is not Video partVideo || part.Id == member.Id
                        || part.Id == Guid.Empty || part.OwnerId != member.Id
                        || !PathComparer.Equals(PathKey(part.Path), PathKey(partPath)))
                        throw new PluginFault("分段媒体存在未入库或归属不明的 STRM；整批删除已取消。若云任务已成功，请核对 MF 日志。");
                    pending.Enqueue(partVideo);
                }
            }
            result[item.Id] = owned.Values.ToArray();
        }
        var selected = result.Values.SelectMany(v => v).DistinctBy(v => v.Id).ToDictionary(v => v.Id);
        foreach (var video in selected.Values)
        {
            var ownsPart = ((video is Movie or Episode) && video.OwnerId == Guid.Empty)
                || (selected.TryGetValue(video.OwnerId, out var owner) && owner is (Movie or Episode)
                && owner.AdditionalParts.Any(p => PathComparer.Equals(PathKey(p), PathKey(video.Path))));
            // 只接纳所选条目明确声明且有索引的分段；版本合并仍不推断授权。
            if (!ownsPart || video.PrimaryVersionId.HasValue || library.GetLocalAlternateVersionIds(video).Any()
                || library.GetLinkedAlternateVersions(video).Any())
                throw new PluginFault("该电影/单集含未明确授权的版本或关联媒体；本地删除已取消。若云任务已成功，请核对 MF 日志。");
        }
        return result;
    }

    private sealed class ScanResult
    {
        public List<FileSystemMetadata> Entries { get; } = [];
        public List<string> Links { get; } = [];
        public List<string> MissingDirectories { get; } = [];
    }

    private static ScanResult Scan(IEnumerable<FileSystemMetadata> roots, CancellationToken token)
    {
        var result = new ScanResult();
        var examined = new HashSet<string>(PathComparer);
        foreach (var root in roots)
        {
            var pending = new Stack<FileSystemMetadata>();
            pending.Push(root);
            while (pending.TryPop(out var entry))
            {
                token.ThrowIfCancellationRequested();
                if (!examined.Add(entry.FullName)) continue;
                if (examined.Count > 10000) throw new PluginFault("原生删除目录过大；请在 MF 中操作。");
                FileSystemInfo info = entry.IsDirectory ? new DirectoryInfo(entry.FullName) : new FileInfo(entry.FullName);
                if (info.LinkTarget is not null || (info.Exists && (info.Attributes & FileAttributes.ReparsePoint) != 0))
                {
                    result.Links.Add(entry.FullName);
                    result.Entries.Add(entry);
                    continue;
                }
                // 快照记录真实存在的节点和类型，不能授权原本不存在的伴随文件或节点替换。
                var directory = Directory.Exists(entry.FullName);
                if (File.Exists(entry.FullName) || directory)
                    result.Entries.Add(new FileSystemMetadata { FullName = PathKey(entry.FullName), IsDirectory = directory });
                if (!entry.IsDirectory) continue;
                if (!directory) { result.MissingDirectories.Add(entry.FullName); continue; }
                foreach (var child in new DirectoryInfo(entry.FullName).EnumerateFileSystemInfos())
                    pending.Push(new FileSystemMetadata { FullName = PathKey(child.FullName), IsDirectory = child is DirectoryInfo });
            }
        }
        return result;
    }

    private static string PathKey(string? path) => string.IsNullOrEmpty(path) ? "" : MappingSet.IsAbsolute(path) ? MappingSet.Normalize(path) : path;
    private static ItemIdentity Identity(BaseItem item) => new(item.Id, PathKey(item.Path), item.GetType().FullName!);
    private static FileSystemMetadata CopyPath(FileSystemMetadata path) => new() { FullName = PathKey(path.FullName), IsDirectory = path.IsDirectory };
    private static bool IsCompanion(string path, string itemPath)
    {
        path = PathKey(path); itemPath = PathKey(itemPath);
        if (!PathComparer.Equals(Path.GetDirectoryName(path), Path.GetDirectoryName(itemPath))) return false;
        var stem = Path.GetFileNameWithoutExtension(itemPath);
        var name = Path.GetFileNameWithoutExtension(path);
        return PathComparer.Equals(stem, name) || new[] { ".", "-", "_" }.Any(separator =>
            name.StartsWith(stem + separator, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
    }
    private static bool Within(string path, string root) => MappingSet.Within(MappingSet.Normalize(path), MappingSet.Normalize(root));
    private static bool IsManaged(string? path, MappingSet mappings) => IsStrm(path) && mappings.Match(path!) is not null;
    public static bool IsStrm(string? path) => !string.IsNullOrEmpty(path) && path.EndsWith(".strm", StringComparison.OrdinalIgnoreCase);

    private static void CheckNoLinks(string path)
    {
        FileSystemInfo info = Directory.Exists(path) ? new DirectoryInfo(path) : new FileInfo(path);
        if (info.LinkTarget is not null || (info.Exists && (info.Attributes & FileAttributes.ReparsePoint) != 0))
            throw new PluginFault("STRM 或删除目录不能是符号链接或重解析点。");
        for (var dir = new DirectoryInfo(Path.GetDirectoryName(path)!); dir is not null; dir = dir.Parent)
            if (dir.LinkTarget is not null || (dir.Exists && (dir.Attributes & FileAttributes.ReparsePoint) != 0))
                throw new PluginFault("STRM 或删除目录经过符号链接，无法确认文件身份。");
    }

    private static async Task<string> FingerprintAsync(string path, CancellationToken token)
    {
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            bufferSize: 4096, FileOptions.Asynchronous | FileOptions.SequentialScan);
        return "sha256:" + Convert.ToHexString(await SHA256.HashDataAsync(stream, token).ConfigureAwait(false)).ToLowerInvariant();
    }
}
