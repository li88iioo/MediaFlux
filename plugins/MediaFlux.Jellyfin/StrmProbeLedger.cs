using System.Security.Cryptography;
using MediaBrowser.Common.Configuration;

namespace MediaFlux.Jellyfin;

/// <summary>
/// 记录每个 .strm 条目最近一次远程探测时的文件内容指纹。
/// 追加写 TSV，启动后首次使用时加载；内容变化（如洗版改写目标地址）即视为未探测。
/// </summary>
public sealed class StrmProbeLedger(IApplicationPaths paths)
{
    // 旧版两列记录可能来自失败探测；只信任通过刷新完成校验的新版记录。
    private const string Version = "2";
    private readonly string _path = Path.Combine(paths.PluginConfigurationsPath, "MediaFlux.Jellyfin.strm-probe.tsv");
    private readonly object _gate = new();
    private Dictionary<Guid, string>? _entries;
    private int _lines;

    public static bool IsStrm(string? path) => path?.EndsWith(".strm", StringComparison.OrdinalIgnoreCase) == true;

    /// <summary>.strm 原始字节的 SHA256；文件不可读时返回 null，调用方按未探测处理。</summary>
    public static string? Fingerprint(string path) =>
        WithIoFallback<string?>(() => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))), null);

    public bool Matches(Guid id, string fingerprint)
    {
        lock (_gate) return Entries().TryGetValue(id, out var known) && known == fingerprint;
    }

    public bool Contains(Guid id)
    {
        lock (_gate) return Entries().ContainsKey(id);
    }

    public void Record(Guid id, string fingerprint)
    {
        lock (_gate)
        {
            var entries = Entries();
            if (entries.TryGetValue(id, out var known) && known == fingerprint) return;
            entries[id] = fingerprint;
            Append($"{Version}\t{id:N}\t{fingerprint}");
        }
    }

    public void Forget(Guid id)
    {
        lock (_gate)
        {
            if (Entries().Remove(id)) Append($"{Version}\t{id:N}\t");
        }
    }

    // 完整读完才缓存；读到一半可能遗漏后续墓碑。
    private Dictionary<Guid, string> Entries() =>
        _entries ??= WithIoFallback<Dictionary<Guid, string>?>(Load, null) ?? [];

    private Dictionary<Guid, string> Load()
    {
        var entries = new Dictionary<Guid, string>();
        var lines = 0;
        foreach (var line in File.ReadLines(_path))
        {
            lines++;
            var parts = line.Split('\t');
            if (parts.Length != 3 || parts[0] != Version || !Guid.TryParseExact(parts[1], "N", out var id)) continue;
            if (parts[2].Length == 0) entries.Remove(id);
            else entries[id] = parts[2];
        }
        // 墓碑和覆盖行过多时整体重写，避免文件随播放与删除无限增长。
        _lines = lines > entries.Count * 2 + 1024 ? Rewrite(entries, lines) : lines;
        return entries;
    }

    private void Append(string line) => _lines = WithIoFallback(() =>
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        File.AppendAllText(_path, line + "\n");
        return _lines + 1;
    }, _lines);

    private int Rewrite(Dictionary<Guid, string> entries, int lines) => WithIoFallback(() =>
    {
        var temp = _path + ".tmp";
        File.WriteAllLines(temp, entries.Select(e => $"{Version}\t{e.Key:N}\t{e.Value}"));
        File.Move(temp, _path, overwrite: true);
        return entries.Count;
    }, lines);

    private static T WithIoFallback<T>(Func<T> operation, T fallback)
    {
        try { return operation(); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return fallback; }
    }
}
