# Changelog

所有关于 MediaFlux 的重大变更都将记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed
- 修复浏览器经媒体 302 反代播放光鸭视频时，HTML5 视频请求未携带媒体服务器 Token，导致已生成的短时播放会话无法恢复并错误回退上游的问题。
- 修复媒体播放会话只显示 Item/MediaSource ID、无法识别具体视频，并将上游 `PlaySessionId` 与内部 `_mfps` 能力会话合并为同一条播放链路。

## [0.1.1] - 2026-08-21

### Added
- 新增本地媒体来源的多级子目录浏览、面包屑导航和整理任务详情，可查看文件映射与原子执行步骤（[`3471aac`](https://github.com/li88iioo/MediaFlux/commit/3471aac)）。
- 本地媒体手动刮削支持覆盖剧集季号与集数，并复用统一的媒体刮削与位置识别弹窗（[`ae51076`](https://github.com/li88iioo/MediaFlux/commit/ae51076)）。
- Media Agent 新增媒体追更订阅实时核对能力，可联动 TMDB、媒体库和索引器检查更新候选（[`689ed56`](https://github.com/li88iioo/MediaFlux/commit/689ed56)）。
- Docker 镜像内置 `ffprobe`，无需额外安装即可进行音视频规格探测（[`baa4a94`](https://github.com/li88iioo/MediaFlux/commit/baa4a94)）。

### Changed
- 剧集季目录改为不补零的标准形式，例如 `Season 1`；特别篇仍使用 `Specials`（[`efbb911`](https://github.com/li88iioo/MediaFlux/commit/efbb911)）。
- 重构光鸭整理、离线转存、STRM、媒体反代、分享转存、GCID 与本地媒体工作台的布局和响应式交互（[`de6d3ae`](https://github.com/li88iioo/MediaFlux/commit/de6d3ae)）。
- STRM 页面将播放地址操作改为候选发现与完整刷新流程，移除用户侧快速同步入口，并保留整理链路内部精准增量能力（[`a7290df`](https://github.com/li88iioo/MediaFlux/commit/a7290df)）。
- STRM 扫描增加并发校验与批量指纹补写，提升大规模目录同步和校准速度（[`6ed5cab`](https://github.com/li88iioo/MediaFlux/commit/6ed5cab)）。
- 优化 README 的项目标识、徽章链接和标题间距（[`f2b1a55`](https://github.com/li88iioo/MediaFlux/commit/f2b1a55)、[`70e3211`](https://github.com/li88iioo/MediaFlux/commit/70e3211)）。

### Fixed
- 修复 Telegram 富文本进度与终态消息换行被压缩的问题（[`76bb941`](https://github.com/li88iioo/MediaFlux/commit/76bb941)）。
- 修复 Telegram Bot Token 保存后直接测试时被误判为空或无效的问题（[`2a665d5`](https://github.com/li88iioo/MediaFlux/commit/2a665d5)）。
- 修复 Telegram 轮询冲突重复输出堆栈，并优化 NAS/CIFS 文件权限告警与错误提示（[`08dab05`](https://github.com/li88iioo/MediaFlux/commit/08dab05)）。
- 修复侧边栏图标加载时的布局抖动（[`1f0bc1f`](https://github.com/li88iioo/MediaFlux/commit/1f0bc1f)）。

## [0.1.0] - 2026-08-18

### 🚀 初始版本发布 (Initial Release)

MediaFlux 首个正式开源版本发布！致力于为家庭媒体中心提供一站式、全流程、安全可控的影视整理与流转编排方案。

#### 📥 下载编排与任务调度
- **多渠道任务接入**：支持 Mikan 等 RSS 自动追番订阅、Telegram Bot 快捷提交磁力/种子/分享链接，以及 Web 控制台手动推送。
- **双引擎分发**：无缝分发任务至本地 **qBittorrent** 下载或 **光鸭云盘** 离线转存。
- **全链路自动闭环**：下载完成后自动触发刮削、整理归档、STRM 生成以及 Jellyfin/Emby 媒体库刷新。

#### 🎯 TMDB 智能刮削与识别
- **高精度识别算法**：结合标题分词清洗、年份约束与拼音模糊匹配，电影使用独立 TMDB 目录，剧集标准化为 `Season NN`，特别篇进入 `Specials` 且文件使用 `S00E##` 统一命名。
- **人工复核保护**：低置信度结果自动进入人工待确认列表，拒绝误入库。
- **自定义规则与映射锁**：支持自定义正则重命名规则与 TMDB 永久映射锁，特殊命名源一次锁定、永久精准匹配。

#### 📂 本地媒体安全整理
- **事务性安全移动**：同文件系统执行毫秒级原子重命名；跨文件系统采用“先写入目标临时文件 → 校验完整性 → 确认入库 → 安全清理源文件”事务机制。
- **垃圾精准清理**：媒体入库且 qB 任务移除后，仅清理已识别的广告文档、sample 样片等垃圾文件；未知文件与外挂字幕/特效字体原地安全保留。

#### ⚡ 光鸭云盘管理与 STRM 302 直链
- **免 Key 登录**：Web 端支持手机验证码安全登录，Token 本地私密持久化并自动定时刷新。
- **302 直链零转码播放**：Jellyfin/Emby 读取本地 `.strm` 文件，MediaFlux 提供短时签名并 302 重定向至云盘 CDN 直链，视频播放不消耗本地服务器 CPU 与出口下行带宽。
- **增量防误删引擎**：基于本地 SQLite 索引增量维护 STRM，网络抖动或远端异常时自动熔断，坚决防止误删本地媒体库。

#### 🐳 容器化部署与运行时支持
- **Docker Compose**：支持容器化一键部署与多架构（`linux/amd64` + `linux/arm64`）镜像，内置高性能网络栈与非 root 安全隔离。
- **Python 源码运行**：支持标准 Python 3.11+ 生产环境直接运行。
- **Docker-Only 网络配置**：容器内部固定监听 `0.0.0.0:1258`，宿主机发布地址与端口统一由 Compose `.env` 管理，Web 设置页不再修改网络绑定或触发进程自重启。

#### 🛠️ CLI 运维与安全基线
- **内置 `mediaflux` 命令行运维工具**：支持服务状态查询、环境权限诊断 (`doctor`)、一致性数据备份/校验/恢复以及脱敏支持包导出。
- **本地运行与零遥测**：100% 独立运行在用户设备，无任何远程遥测或数据上报，所有凭据与数据库均保存在本地。
- **严格安全防护**：全局 CSRF 防护、Session 防篡改、首启绑定本地回环与生产密钥强制校验。

[Unreleased]: https://github.com/li88iioo/MediaFlux/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/li88iioo/MediaFlux/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/li88iioo/MediaFlux/releases/tag/v0.1.0
