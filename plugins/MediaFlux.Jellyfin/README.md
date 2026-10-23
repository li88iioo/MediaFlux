# MediaFlux Jellyfin 插件

版本 **0.1.3**（程序集 / manifest：**0.1.3.0**）。验证 **Jellyfin 12.2 / 12.3，.NET 10**；编译 API 基线保持 `12.2.0`，manifest 最低 ABI `12.2.0.0`。不再把同一主版本的小版本升级当成删除失败；未来版本仍需独立验证。

插件默认关闭，仅拦截用户明确发起的 Jellyfin `DELETE /Items/{itemId}`、`DELETE /Items?ids=...`。扫描发现 STRM 消失、清库或定时任务不会触发 MF 云端删除；插件关闭或请求完全不涉及映射内 STRM 时，继续原生删除行为。

## 配置

安装与在线更新见 [用户指南](../../docs/jellyfin-plugin.md)。仓库地址：

```text
https://github.com/li88iioo/MediaFlux/releases/download/manifest/manifest.json
```

在管理员插件配置页：

1. 保持关闭，保存 MF URL、用户名、密码并测试连接。连接使用登录会话和 CSRF，HTTPS 使用系统证书验证。
2. 点击“同步 MF 范围”，统一读取 MF 的 Jellyfin 映射和媒体库，不再支持手动映射。
3. 核对范围后启用。MF URL / 用户名变化后必须重新同步。旧手动配置首次升级会停用并清空旧范围，保留连接凭据，重新同步后才能启用；旧 MF 同步配置保留。
4. 仍需由真实 Jellyfin 用户发起纳管删除。API key 不能绕过用户可见性和 `CanDelete`，整批请求在云端写入前统一校验。

## 删除协议与范围

每次删除在同一已登录 MF 会话中先请求 `GET api/jellyfin/delete-capabilities`，必须返回 `scope_version: 1`，且 `kinds` 包含全部 `movie / episode / season / series`。能力不缓存；接口不存在、鉴权失败、返回格式或版本 / kind 不匹配均不发送删除 POST。

通过握手并再次复核本地状态后，调用原有 `POST api/jellyfin/delete`，固定使用 `mapping_mode=mf`，发送路径、指纹及范围：

```json
{
  "paths": ["/jellyfin/strm/Film/Film.strm"],
  "fingerprints": {"/jellyfin/strm/Film/Film.strm": "sha256:<STRM原始字节哈希>"},
  "mapping_mode": "mf",
  "scopes": [{"kind":"movie","root":"/jellyfin/strm/Film","paths":["/jellyfin/strm/Film/Film.strm"]}]
}
```

插件不发送手动 `mappings` 字段。所有路径都是 Jellyfin 文件系统路径，顶层 `paths` 与完整 `fingerprints` 对应所有 scopes 的 STRM 并集。

| kind | root | 授权范围 |
| --- | --- | --- |
| `episode` | `null` | 仅自身 STRM；本地原生删除还可移除同目录、同名且明确归属的伴随文件，不扩大到父目录 |
| `movie` | 独立直接父目录，或 `null` | 只有原生 `GetDeletePaths()` 已删除独立直接父目录时才授权目录；原生文件级删除传 `null`，仅自身 STRM 与明确伴随文件 |
| `season` / `series` | 原生所选季 / 剧目录 | 枚举全部纳管 STRM，核对每个文件对应的电影 / 单集及全部原生递归删除对象的当前用户权限 |

明确独立目录授权包括其**全部已有非媒体伴生内容**，如 `1`、`1.X`、NFO、封面及其它非媒体扩展名；不会将未知非媒体扩展名一律拒绝。仍拒绝以下不确定或扩大范围的请求：

- 映射根或包含映射根的目录、虚拟 / 非文件对象、没有唯一原生目录的季 / 剧。
- 目录混放 Jellyfin 12.2 官方命名规则识别的本地视频 / 音频，或库内其它归属对象。
- 未入库、不可见、无删除权限或不属于所选作品的 STRM；电影 / 单集范围出现其它 STRM。
- 符号链接、悬空链接、重解析点，以及路径经过链接。
- 主版本、关联版本、OwnerId、分段媒体；使用 12.2 的关联索引检查，不依赖旧内存版本数组。

云端目录绑定由 MF 按祖先链和既有 STRM 生成规则核对，插件不猜测云端目录。压缩 / 折叠层级无法对应的季 / 剧会被 MF 拒绝；电影云端目录存在未选视频时，MF 可降为同名伴随精确删除，季 / 剧则整批拒绝。插件不会自行创建另一套云端删除执行器。

## 等待和复核

只有 MF 任务 `completed` 且 `result.ok=true`，并通过成功后复核，才放行 Jellyfin 原生操作：

- 提交前（登录 / 能力握手之后）及 MF 成功后，使用同一套逻辑检查原生范围、目录快照、STRM 集合 / 原始字节哈希、可见性 / `CanDelete`、主版本与关联版本。
- 重新加载原生子项缓存，拒绝新增 / 换路径 / 换类型的原生递归删除对象。任何新增文件（包括 `1.X`、封面）、目录或 STRM 都不能扩张最初授权。
- MF 成功后允许原有文件或整个原有目录已被联动删除；不会把缺失文件当成新的删除触发器。剩余 STRM 必须仍与原 fingerprint 一致。
- MF 失败、partial、超时、失联、权限或内容变化均阻断本地删除；不会重新登录后重放云端 POST。已提交任务时先核对 MF 日志，勿盲目重试。

单次最多 1000 个 STRM，单项原生文件范围 / 递归对象检查最多 10000 条。最后一次复核与 Jellyfin 原生操作并非跨进程文件系统事务；不要在明确删除期间并发向该目录写入媒体。MF 的已完成任务也无法因本地复核拒绝而回滚。

## 本地构建与官方 TestServer 测试

使用 .NET SDK 10.0.402：

```bash
dotnet build plugins/MediaFlux.Jellyfin/MediaFlux.Jellyfin.csproj -c Release
DOTNET_BIN=dotnet bash plugins/MediaFlux.Jellyfin/scripts/verify.sh 12.2
DOTNET_BIN=dotnet bash plugins/MediaFlux.Jellyfin/scripts/verify.sh 12.3
```

可用 `DOTNET_BIN` 指定 SDK，`JELLYFIN_VERIFY_CACHE` 指定官方源码缓存。脚本复用已下载源码，不改变生产插件 12.2.0 的编译基线。

测试在 ASP.NET TestServer 中分别加载**真实官方 12.2 / 12.3 `LibraryController` / `PluginsController`**，不是复制控制器；底层媒体库 / 用户存储和 MF HTTP 响应为可控测试替身。覆盖四种范围、完整指纹、能力握手、权限 / 版本保护、文件竞态、失败不重放、配置迁移和 Logo HTTP 接口。不启动生产 Jellyfin，不连接生产 MF / 云盘。

## 打包

整合及最终验证完成后才运行：

```bash
DOTNET_BIN=dotnet bash plugins/MediaFlux.Jellyfin/scripts/package.sh
```

脚本校验版本、框架 / ABI、插件 ID，生成 `artifacts/MediaFlux.Jellyfin-0.1.3.0.zip` 和 SHA256。包内 README 使用 `docs/jellyfin-plugin.md` 用户指南；本文件说明插件开发协议与验证方式。打包不会改写文档。
