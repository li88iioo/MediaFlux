# Jellyfin 插件：MediaFlux 云端删除联动

## 功能与范围

插件保留 Jellyfin 原来的“删除”入口，仅在明确的单项或批量 HTTP 删除请求中介入：

1. 使用 Jellyfin 当前用户的 `CanDelete` 权限，先检查整批选择。
2. 核对原生删除涉及的文件和目录，读取选中 STRM 的原始内容指纹。
3. 将 Jellyfin 路径、指纹及电影／单集／整季／整剧范围交给 MediaFlux。MF 使用已有路径映射、STRM 索引和统一光鸭 Writer，重新校验本地文件及云端身份。
4. 在第一笔写入前核对完整文件清单，随后逐项回收视频与属于该范围的伴随文件，最后复核并回收空目录，记录删除审计并精确退役本次删除对象的 STRM／元数据索引。不会未经核验递归回收整个云盘目录。
5. 插件等待 MF 任务 `completed` 且 `result.ok=true`，重新检查权限、节目路径及删除范围，再继续 Jellyfin 原生本地删除。

**不会因为扫库、条目消失、目录离线或移除媒体库而调用云端删除。** 不经过 Media Agent，不新增账户系统、任务队列或映射数据库。

### 支持范围与边界

- 已验证平台：**Jellyfin 12.2 / 12.3，.NET 10**。官方源码分别为 `v12.2` / `v12.3`；插件仍以 NuGet `Jellyfin.Controller`／`Jellyfin.Model` **12.2.0** 编译，TFM 为 `net10.0`。最低 ABI 不是只允许一个小版本；12.3 不再被误拦截。未来版本仍需独立验收，跨主版本不直接放行。
- 插件版本：**0.1.3**（清单版本 `0.1.3.0`），`targetAbi=12.2.0.0`；插件 GUID 保持不变。
- 支持 MF 纳管的光鸭 **电影／单集／整季／整剧**，以及明确选择多项的原生批量删除；最多 1000 个 STRM，目录文件清单另有规模限制。
- 删除独立电影（含 NSFW 作品）时，若原生删除拥有该作品目录且云端没有未选择的视频，会回收视频、NFO、字幕、图片等目录内容，最终回收空作品目录。
- 删除单集时，只回收该集视频和明确同名的伴随文件（包括语言字幕、NFO、缩略图），保留其他集、`tvshow.nfo`、共享海报等。最后一集删除不等于整剧授权，仍有共享素材时保留目录。
- 删除整季／整剧时，枚举范围内的纳管 STRM 并校验权限；云端目录还有未选中或未同步视频时拒绝整批请求，不猜测其归属。核验完整后回收该范围的所有文件及空目录。
- 普通本地视频、未纳管 STRM 保持原生行为。宽映射目录内只有普通本地视频的文件夹仍可原生删除。
- **不支持把普通文件夹或媒体库根当作整部作品删除。** 同目录存在未选中的纳管 STRM、多版本、叠放分段／附加媒体等原生删除范围不明确时，会拒绝请求。不要通过关闭保护强行重试；先在 MF 核对文件。
- 混放电影目录不会整目录回收：云端有其他未选择视频时，仅回收目标电影及明确同名的伴随文件；本地原生删除会影响未选择媒体时，插件会直接拒绝。
- 仅有 API key、没有真实 Jellyfin 用户身份的纳管删除请求不允许联动；仍须由有删除权限的用户发起。
- 这是用户明确发起的删除，目录收尾随该删除范围执行，不依赖“整理后清理空目录”开关，也不新增单独开关。
- 不清空回收站，不删除媒体库根、配置来源根或归档根。不扩大到所选媒体范围以外，也不把“同目录”直接等同于“同一集伴随文件”。目录出现新增内容或状态不明时保留，实际结果记录在 MF 任务及删除审计中。
- 本地文件由 Jellyfin 原生删除；MF 只退役本次已确认回收对象的 STRM／元数据索引并取消对应元数据下载任务，不再为此扫描整个 STRM 来源或反向刷新 Jellyfin。确认云任务成功后，插件仍复验原生范围，防止等待期间出现的新媒体被连带删除。
- 同一云端文件若还被本次选择之外的 MF STRM 索引引用，MF 会拒绝删除，避免破坏其他来源。

## 安装

同时更新并重启 MediaFlux 与插件。新版插件在写入前读取 `/api/jellyfin/delete-capabilities`，核对媒体范围协议；旧版 MF 即使能登录，也不会降级为仅删除视频后假报完成。旧插件没有媒体范围参数，MF 不会据此推断整目录删除权限。

### 在线安装与更新（推荐）

1. Jellyfin 管理后台 → 插件 → 仓库 → 添加，名称填写 **MediaFlux**，地址填写：

   ```text
   https://github.com/li88iioo/MediaFlux/releases/download/manifest/manifest.json
   ```

2. 保存并刷新插件目录，找到带 MediaFlux Logo 的 **MediaFlux**，安装兼容版本，重启 Jellyfin。
3. 后续更新使用同一个仓库地址，通过 Jellyfin 插件更新功能安装，更新后重启。联动删除首次安装默认关闭；插件自动更新与联动删除启用是两个独立设置。
4. 已手动安装 0.1.2 的用户可添加此仓库更新（GUID 不变）。旧包关闭了自动更新，首次升级请在插件目录手动选择新版；勿另建第二份插件目录。升级前备份插件配置。
5. **旧手动映射已退役**：升级后暂停联动并移除旧范围缓存，保留连接凭据。进入配置页同步 MF 范围，核对后重新启用。旧 MF 同步配置保留启用状态、凭据及缓存。

### 离线安装

1. 停止 Jellyfin，备份插件配置。
2. 解压 `MediaFlux.Jellyfin-0.1.3.0.zip` 到 Jellyfin **数据目录**下的 `plugins/MediaFlux/`。目录内直接包含 `MediaFlux.Jellyfin.dll`、`meta.json`、`logo.png`、README 和 LICENSE。升级时替换原插件目录内容，不要并排保留两份 DLL；保留独立的插件 XML 配置。
3. 不要复制构建目录中的 Jellyfin、ASP.NET 或测试 DLL；宿主提供这些程序集。
4. 重启 Jellyfin，进入管理后台 → 插件 → MediaFlux，按下述步骤配置。先用可恢复的测试条目验证。

实际数据目录取决于 Jellyfin 的安装方式和 Docker 卷挂载，以该实例的数据目录为准。

### 从源码构建

需要 .NET 10 SDK、Python 3，首次还原依赖需要访问 NuGet。在仓库根目录执行：

```bash
bash plugins/MediaFlux.Jellyfin/scripts/package.sh
```

可用 `DOTNET_BIN=/path/to/dotnet` 指定 SDK 可执行文件。产物：

```text
plugins/MediaFlux.Jellyfin/artifacts/MediaFlux.Jellyfin-0.1.3.0.zip
plugins/MediaFlux.Jellyfin/artifacts/MediaFlux.Jellyfin-0.1.3.0.zip.sha256
```

产物、`bin/`、`obj/` 均不纳入 Git。打包脚本从 csproj 读取插件版本及 TFM，核对清单版本、插件 GUID、`net10.0` 与两个 Jellyfin NuGet 包的 `12.2.0` 基线；`targetAbi` 按 NuGet 版本加 `.0` 核对，并生成 SHA256 校验文件，不创建 Git 标签或上传包。

## 连接与认证

填写 **MF 完整地址（协议、IP／域名、端口和必要的反代前缀）＋Web 用户名＋密码**。地址必须能从 Jellyfin 服务端访问，不是只从浏览器可访问。

插件复用 MF Web 登录、session Cookie 和 CSRF，不使用光鸭 access token 或播放链接签名作为删除授权。密码保存在 Jellyfin 服务端插件 XML 中，**不是加密保险库**：

- 自定义设置 API 和 Jellyfin 自带的插件配置 API 都不回显密码；留空保存会保留已有密码。
- Linux 配置文件创建／保存时使用仅当前用户读写权限；系统管理员仍能读取。备份也必须按敏感文件保护。
- 使用该账号即持有 MF Web 账号权限，并非仅删除权限。插件启用后，具有相应 Jellyfin 删除权限的用户可以通过此链路请求 MF 删除。
- 跨不可信网络必须使用 HTTPS。HTTPS 保持系统标准证书验证；自签名证书需安装到 Jellyfin 宿主的受信任证书库，插件不提供跳过验证开关。

## 路径映射：统一从 MediaFlux 同步

1. 保持插件关闭，填写连接信息，保存。
2. 点击“测试已保存连接”，确认 MF 登录及媒体库接口可用；测试不会提交删除。
3. 点击“同步 MF 范围”，从已有 `/api/media-libraries/overview` 读取 Jellyfin 的媒体库及 `local/server` 映射。
4. 核对展示的范围，勾选启用并保存。

媒体库列表用于核对，**实际纳管范围以路径映射为准**。配置以最后一次成功同步的缓存为准；不是每次删除都探测整个媒体库。MF 中修改路径映射、媒体库挂载或连接账号后，应重新同步。同步失败不会清空旧范围，也不会因为 MF 离线而放行已纳管 STRM 删除。

`local` 是 **MF 容器内**路径，`server` 是 **当前 Jellyfin 实例可见**路径，不是 NAS 主机路径或 SMB URL。需要调整时，在 MediaFlux 配置 Jellyfin 路径映射，再回插件同步，不在插件中维护第二份映射。

映射按完整路径片段、最长前缀匹配；同一 Jellyfin 前缀对应多个 MF 目录会被拒绝。映射后的文件仍需通过 MF 索引及两端内容指纹校验，不能只凭同名文件删除。缺少索引时先执行 MF STRM 同步，不要手工伪造索引。

## 失败、恢复与卸载

HTTP 202 仅表示 MF 接收任务，不表示云端已删除。等待上限可配置为 5–600 秒，默认 120 秒；超时后云端任务可能仍在运行。

- MF 离线、鉴权失败、映射／指纹不符、下载保护命中等情况会中止本地删除。
- 云端调用失败、结果未知、部分成功或 STRM 索引收尾失败，不会伪报整批成功，也不会自动重放删除请求。部分成功只退役已明确回收对象的索引，未确认对象仍保留，不自动扫描整个来源。
- **云盘与 Jellyfin 不构成一个事务。** 云端成功而本地失败、超时或请求中断时，不能假定云端已回滚。MF 保留旧路径与内容指纹作为恢复凭据；后续正常或手动 STRM 同步可按凭据收尾残留，已被替换的本地内容不会强删。先核对 MF 任务结果、云盘目录及回收站，再决定恢复或同步。
- 部分 Jellyfin 客户端只展示通用删除失败弹窗。插件配置页的“最近删除错误”提供当前进程内的详细错误；MF 删除审计持久保存。不要根据通用弹窗盲目重试。
- 需要恢复时，依据 MF 删除审计在光鸭回收站核对并恢复对应文件及目录，再执行 MF STRM 同步及 Jellyfin 媒体库刷新。文件和空目录是分别回收的，不承诺跨多项的原子回滚。清空回收站后不能依靠此插件恢复；插件不保存媒体副本。
- 已收到明确成功后，若节目路径、原生删除范围、内容或用户权限又变化，插件会取消继续本地删除。
- 关闭插件仅恢复后续请求的原生行为，不会撤销已提交的 MF 任务。卸载前等待任务终止，停服删除插件目录；需要移除凭据时再删除对应插件 XML，并妥善处理备份。

## 开发验证

后端：

```bash
.venv/bin/python -m pytest -q tests/test_jellyfin_delete.py tests/test_jellyfin_scoped_delete.py \
  tests/test_media_server_path_mapping.py tests/test_media_libraries_api.py \
  tests/test_guangya_active_download_delete.py tests/test_guangya_direct_delete.py
```

插件验证脚本分别下载并构建**官方 Git tag `v12.2` / `v12.3` 的真实 LibraryController／PluginsController**，在 ASP.NET TestServer 中调用 HTTP 删除及配置路由。插件始终按 12.2.0 编译，测试宿主依赖随所测版本切换，用于检查同一插件程序集的兼容性：

```bash
bash plugins/MediaFlux.Jellyfin/scripts/verify.sh 12.2
bash plugins/MediaFlux.Jellyfin/scripts/verify.sh 12.3
```

需要 .NET SDK **10.0.402**、curl、tar，以及 GitHub codeload／NuGet 网络；可用 `DOTNET_BIN=/path/to/dotnet` 指定 SDK，`JELLYFIN_VERIFY_CACHE` 指定缓存目录。默认缓存位于 `.superpowers/tmp/jellyfin-plugin/`。发布工作流独立于主程序 Docker/FPK 发布，只由 `jellyfin-plugin-v*` 标签触发；先完成双版本测试和 ZIP 发布，再更新稳定仓库清单，保留历史版本与校验和。

MF HTTP 响应、媒体库管理器及云端行为为隔离模拟，不连接生产。自动化验证不等于运行完整生产 Jellyfin 或实际删除云盘文件。安装到实际实例后仍应先用单个可恢复测试条目确认挂载、权限及云端回执，再扩大使用范围。
