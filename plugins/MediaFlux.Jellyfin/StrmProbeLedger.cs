using System.Security.Cryptography;
using MediaBrowser.Common.Configuration;

namespace MediaFlux.Jellyfin;

/// <summary>
/// 记录每个 .strm 条目最近一次远程探测时的文件内容指纹。
/// 追加写 TSV，启动后首次使用时加载；内容变化（如洗版改写目标地址）即视为未探测。
/// </summary>
public sealed class StrmProbeLedger(IApplicationPaths paths)
{
    private readonly string _path = Path.Combine(paths.PluginConfigurationsPath, "MediaFlux.Jellyfin.strm-probe.tsv");
    private readonly object _gate = new();
    private Dictionary<Guid, string>? _entries;
    private int _lines;

    public static bool IsStrm(string? path) => path?.EndsWith(".strm", StringComparison.OrdinalIgnoreCase) == true;

    /// <summary>.strm 原始字节的 SHA256；文件不可读时返回 null，调用方按未探测处理。</summary>
    public static string? Fingerprint(string path)
    {
        try { return Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return null; }
    }

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
            if (Entries().TryGetValue(id, out var known) && known == fingerprint) return;
            _entries![id] = fingerprint;
            Append($"{id:N}\t{fingerprint}");
        }
    }

    public void Forget(Guid id)
    {
        lock (_gate)
        {
            if (Entries().Remove(id)) Append($"{id:N}\t");
        }
    }

    private Dictionary<Guid, string> Entries()
    {
        if (_entries is not null) return _entries;
        _entries = [];
        if (!File.Exists(_path)) return _entries;
        foreach (var line in File.ReadLines(_path))
        {
            _lines++;
            var parts = line.Split('\t');
            if (parts.Length != 2 || !Guid.TryParseExact(parts[0], "N", out var id)) continue;
            if (parts[1].Length == 0) _entries.Remove(id);
            else _entries[id] = parts[1];
        }
        // 墓碑和覆盖行过多时整体重写，避免文件随播放与删除无限增长。
        if (_lines > _entries.Count * 2 + 1024) Rewrite();
        return _entries;
    }

    private void Append(string line)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.AppendAllText(_path, line + "\n");
            _lines++;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // 写盘失败只影响重启后的命中率；本次进程内仍按内存记录跳过重复探测。
        }
    }

    private void Rewrite()
    {
        var temp = _path + ".tmp";
        try
        {
            File.WriteAllLines(temp, _entries!.Select(e => $"{e.Key:N}\t{e.Value}"));
            File.Move(temp, _path, overwrite: true);
            _lines = _entries!.Count;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // 压缩失败保留原文件，下次加载结果相同。
        }
    }
}
