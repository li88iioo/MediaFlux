# Changelog

所有关于 MediaFlux 的重大变更都将记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.10] - 2026-08-30

### Added
- 新增按来源目录启用的成人内容整理链路：仅处理显式指定的 NSFW 来源，支持番号清洗、MetaTube 多来源候选归并、同番号分段文件、Telegram/Web 人工确认，以及无元数据时“清洗标题后入库”的安全兜底；归档位置继续复用媒体库页面配置的真实分类目录（[`d7f12ab`](https://github.com/li88iioo/MediaFlux/commit/d7f12ab)）。
- 新增统一 Telegram 通知中心与线程快照，整理、STRM、下载、RSS、Agent 等长任务可共享可恢复的消息身份、修订号和投递结果，并等待卡片更新完成后再结束输入状态（[`bb3f99c`](https://github.com/li88iioo/MediaFlux/commit/bb3f99c)、[`a7ca872`](https://github.com/li88iioo/MediaFlux/commit/a7ca872)）。

### Changed
- Telegram 媒体通知统一为紧凑、分段且信息完整的富文本布局，恢复入库媒体、文件、STRM 与媒体库刷新明细，同时减少重复终态与链路噪声（[`8763dd2`](https://github.com/li88iioo/MediaFlux/commit/8763dd2)、[`bb3f99c`](https://github.com/li88iioo/MediaFlux/commit/bb3f99c)）。
- 运行时生命周期统一采用关闭门控、在途任务排空和有界缓存；Web 刷新保留旧数据并呈现部分失败，整理轮询改为可见性感知重试，订阅媒体库映射改为批量读取，Docker 非 root 数据目录仅在首次或显式请求时递归迁移权限；后续可靠性收敛进一步补齐数据库隔离、异步资源回收、任务恢复、媒体反代与下载边界以及刷新态稳定性（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)、[`0b9bd05`](https://github.com/li88iioo/MediaFlux/commit/0b9bd05)、[`b20da18`](https://github.com/li88iioo/MediaFlux/commit/b20da18)、[`c604b4a`](https://github.com/li88iioo/MediaFlux/commit/c604b4a)、[`c3c8a8d`](https://github.com/li88iioo/MediaFlux/commit/c3c8a8d)、[`5f2a987`](https://github.com/li88iioo/MediaFlux/commit/5f2a987)）。
- 资源检索补齐 BTBTLA 新旧页面解析与原生翻页、Nyaa 镜像翻页识别和 1LOU 超时降级；下线 AnimeTosho，并在读取旧配置时安全忽略残留站点 ID（[`b667719`](https://github.com/li88iioo/MediaFlux/commit/b667719)）。
- Docker 工作流在普通 `main` 提交仅执行完整测试与 amd64 冒烟验证，多架构构建、镜像发布和 GitHub Release 仅由正式版本标签触发，减少无标签构建的重复资源消耗（[`74a71ae`](https://github.com/li88iioo/MediaFlux/commit/74a71ae)）。

### Fixed
- 修复整理联动 STRM 在变化目标合并时把布尔值当作可迭代对象，导致 `'bool' object is not iterable`；同时加强发布名清洗与队列合并边界（[`50aaf85`](https://github.com/li88iioo/MediaFlux/commit/50aaf85)）。
- 修复 Telegram 终态编辑失败时重复补发、投递结果未知时盲目重放、旧 revision 覆盖新状态，以及输入状态早于候选卡更新消失的问题（[`9a41d6f`](https://github.com/li88iioo/MediaFlux/commit/9a41d6f)、[`0636477`](https://github.com/li88iioo/MediaFlux/commit/0636477)、[`0f2de1d`](https://github.com/li88iioo/MediaFlux/commit/0f2de1d)、[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 修复本地媒体移动提交后收尾异常可能错误恢复 qB、配置文件与 STRM 退役状态可能分裂、发现/索引/反代客户端关机泄漏、持久缓存无界增长，以及 Chromium 首次打开目录菜单时被自身 resize/scroll 立即关闭的问题（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 发布流水线现拒绝用不同提交覆盖既有精确版本镜像或 GitHub Release，保持正式版本标签与制品不可变（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 修复 Python 3.13/部分文件系统快速替换 SQLite 数据库时 inode 被复用，导致连接误用旧 WAL 协商缓存并使发布测试失败的问题；文件代际现同时校验纳秒级 ctime（[`74a71ae`](https://github.com/li88iioo/MediaFlux/commit/74a71ae)）。

## [0.1.9] - 2026-08-29

### Added
- 新增统一媒体库映射工作台，可在同一页面为 STRM 子目录与本地归档分类绑定 Jellyfin/Emby 媒体库、容器路径和服务器可见路径，并通过受限目录浏览器选择挂载目录（[`d0ee96f`](https://github.com/li88iioo/MediaFlux/commit/d0ee96f)）。
- 本地手动整理补齐目录级识别、外部候选线索、季集编号模式持久化、媒体参数探测及多版本替换计划；预览与执行复用同一编号规则，并在来源或既有目标变化时安全拒绝或回滚（[`d0ee96f`](https://github.com/li88iioo/MediaFlux/commit/d0ee96f)）。

### Changed
- Agent Web 与 Telegram 统一采用面向用户的自然语言结果投影，推荐查询支持年份、地区与类型约束，并减少内部检查状态、原始工具结构和无效后续提示对会话的干扰（[`0a39e1e`](https://github.com/li88iioo/MediaFlux/commit/0a39e1e)、[`483437a`](https://github.com/li88iioo/MediaFlux/commit/483437a)、[`8d1eee1`](https://github.com/li88iioo/MediaFlux/commit/8d1eee1)）。
- Agent Web 工作区移除重复的常用任务与欢迎卡，改为稳定的空会话输入态，并收敛标题、阴影、按钮和继续会话交互，使首次会话与历史会话保持一致（[`6c69b3e`](https://github.com/li88iioo/MediaFlux/commit/6c69b3e)、[`7f7e46e`](https://github.com/li88iioo/MediaFlux/commit/7f7e46e)）。

### Fixed
- 修复包含站点包装、集号和发布组信息的剧集文件名清洗不完整，导致标题或季集位置识别偏差的问题（[`021e1f1`](https://github.com/li88iioo/MediaFlux/commit/021e1f1)）。
- 修复统一媒体库映射并发保存时数据库绑定与 `user.env` 可能互相覆盖的问题；保存操作现跨线程/进程串行，配置冲突会回滚绑定并返回明确状态，同时显式释放媒体服务器探测连接（[`b14b867`](https://github.com/li88iioo/MediaFlux/commit/b14b867)）。
- 修复非全屏桌面窗口、iPad 与窄屏下固定保存栏、侧边栏退出入口及设置双栏可能被窗口边界裁切的问题，并统一共享工作台的平板响应断点（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。
- 修复本地媒体删除快照中的纳秒时间及文件系统标识经浏览器 JSON 往返后发生整数舍入，导致 Docker/NAS 部署误报“条目在读取后发生变化”的问题（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。
- 修复媒体反代客户端切源、取消播放或离开页面时 `ClientDisconnect` 被错误记录为 Uvicorn ASGI 异常堆栈的问题（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。

## [0.1.8] - 2026-08-28

### Added
- Media Agent 升级为面向项目全链路的领域编排运行时：新增能力检索、媒体事实状态、统一响应契约与目标识别，可通过自然语言组合媒体检索、缺集检查、本地来源扫描、STRM 同步、媒体库刷新、反代诊断及下载分发，并对写操作统一生成可审计确认计划（[`ccc9f14`](https://github.com/li88iioo/MediaFlux/commit/ccc9f14)、[`c84825f`](https://github.com/li88iioo/MediaFlux/commit/c84825f)、[`c858c38`](https://github.com/li88iioo/MediaFlux/commit/c858c38)）。
- 新增受控的光鸭媒体工作区，支持目录检查、残留垃圾识别、媒体名称清理、批量改名和变更计划预览；所有移动、回收与改名操作均经过冻结计划、用户确认、后端复核和结果审计（[`b0e75db`](https://github.com/li88iioo/MediaFlux/commit/b0e75db)）。
- 索引检索新增规范化发布信息、分层查询计划、并发控制与质量排序，改善 Nyaa、OneLou、Pirate Bay、BTBTLA 等来源的搜索召回、镜像降级和候选资源排序（[`ade8236`](https://github.com/li88iioo/MediaFlux/commit/ade8236)）。

### Changed
- Jellyfin/Emby 精准刷新改为持久化合并队列：自动合并相邻变化路径、去重并发刷新，优先刷新 Series、Movie 或媒体库物理根，自动链路不再因定位失败隐式触发全库扫描（[`daf7659`](https://github.com/li88iioo/MediaFlux/commit/daf7659)）。
- Agent 的 Telegram/Web 进度、确认卡、操作编号与部分成功结果采用统一投影，长任务可恢复已完成检查，并允许按来源或计划项精确执行而非固定处理全部对象（[`ccc9f14`](https://github.com/li88iioo/MediaFlux/commit/ccc9f14)、[`c84825f`](https://github.com/li88iioo/MediaFlux/commit/c84825f)）。

### Fixed
- 修复本地目录浏览无法正确进入部分挂载路径、整理媒体规格统计重复，以及增量 STRM 同步跳过数量不准确的问题（[`515e3f8`](https://github.com/li88iioo/MediaFlux/commit/515e3f8)）。
- 修复 Agent 对 Telegram 状态查询、Jellyfin/Emby 反代别名、Agent 开关表达和“检查剧集更新后推送”等复合请求的误路由，并补齐确认门、限流、操作历史及结果投影的一致性（[`c858c38`](https://github.com/li88iioo/MediaFlux/commit/c858c38)）。

## [0.1.7] - 2026-08-27

### Added
- Media Agent 新增执行阶段进度事件与 Telegram 实时状态更新，并优化长回复的段落、列表及反馈识别，减少等待过程中的无响应感（[`b0209ae`](https://github.com/li88iioo/MediaFlux/commit/b0209ae)、[`0196d2b`](https://github.com/li88iioo/MediaFlux/commit/0196d2b)）。
- 下载管理新增原始种子缓存保留策略；光鸭重新提交可从 qBittorrent 5.x 导出任务种子恢复文件树，降低历史资源因本地种子缺失而无法重试的概率（[`f56e59c`](https://github.com/li88iioo/MediaFlux/commit/f56e59c)）。

### Changed
- Docker 生产部署改为开箱即用的精简 Compose：默认 host 网络、首次启动 Web 初始化、自动创建持久化目录并兼容 NAS 权限；开发配置独立维护，仍可按需启用固定 UID/GID 与 bridge 端口映射（[`04cf206`](https://github.com/li88iioo/MediaFlux/commit/04cf206)）。

### Fixed
- 修复 Telegram“全部整理”与本地手动整理流程中确认卡缺失、确认结果未闭环、任务恢复及 Agent 路由异常的问题（[`b9e9822`](https://github.com/li88iioo/MediaFlux/commit/b9e9822)、[`ab8e23d`](https://github.com/li88iioo/MediaFlux/commit/ab8e23d)、[`2d9e0b9`](https://github.com/li88iioo/MediaFlux/commit/2d9e0b9)）。
- 修复媒体标题使用 Unicode 罗马数字表达续作季数时无法正确识别的问题，并补齐目录身份缓存与识别链路回归覆盖（[`b16b7e2`](https://github.com/li88iioo/MediaFlux/commit/b16b7e2)）。
- 修复下载请求、本地整理任务、调度器和终态回写之间的并发一致性问题；qB 完成任务现以事务方式创建或复用并绑定，避免重复重置、错误重跑与状态漂移（[`f105546`](https://github.com/li88iioo/MediaFlux/commit/f105546)）。

## [0.1.6] - 2026-08-26

### Added
- 媒体反代实例新增可信代理来源配置，可按实例校验直接连接方的 IP/CIDR，并在明确授权后安全还原 `X-Forwarded-For` 中的真实客户端地址（[`0430e3d`](https://github.com/li88iioo/MediaFlux/commit/0430e3d)）。
- 新增统一的媒体库路径映射管理，可为本地媒体分类和 STRM 子目录绑定 Jellyfin/Emby 媒体库及服务器可见路径，并据此执行精准刷新（[`fedd358`](https://github.com/li88iioo/MediaFlux/commit/fedd358)）。
- Telegram 新增 Media Agent 控制面板，可直接查看并切换全局与 Telegram Agent 状态，传统整理、同步、搜索、RSS 和运行状态命令保持独立可用（[`1bff479`](https://github.com/li88iioo/MediaFlux/commit/1bff479)）。

### Changed
- 拆分媒体反代的播放信息、签名直链与重定向耗时统计，使首播延迟、缓存命中和具体慢点可独立诊断（[`8129a72`](https://github.com/li88iioo/MediaFlux/commit/8129a72)）。
- 优化索引器站点适配、镜像故障切换、请求超时与重试边界，提高 Mikan、BtBtLa 等来源失效时的检索可用性（[`b96ccc5`](https://github.com/li88iioo/MediaFlux/commit/b96ccc5)）。
- 统一整理规则、元数据设置、整理详情和日志界面的视觉细节，稳定移动端弹窗布局并移除不必要的卡片动效与侧栏滚动条干扰（[`e7aded8`](https://github.com/li88iioo/MediaFlux/commit/e7aded8)、[`6593c49`](https://github.com/li88iioo/MediaFlux/commit/6593c49)、[`e9d671d`](https://github.com/li88iioo/MediaFlux/commit/e9d671d)、[`051e06b`](https://github.com/li88iioo/MediaFlux/commit/051e06b)、[`74ae25e`](https://github.com/li88iioo/MediaFlux/commit/74ae25e)、[`ae55d10`](https://github.com/li88iioo/MediaFlux/commit/ae55d10)）。

### Fixed
- 修复媒体反代高级配置折叠区因浏览器命中旧主样式缓存而显示为散落图标和文本的问题（[`fd5ecb0`](https://github.com/li88iioo/MediaFlux/commit/fd5ecb0)）。
- 修复仪表盘可播放媒体统计偏差，以及本地媒体整理识别结果未完整持久化、详情中标题/TMDB/类型/季集信息缺失的问题（[`03be7dd`](https://github.com/li88iioo/MediaFlux/commit/03be7dd)、[`5bf3078`](https://github.com/li88iioo/MediaFlux/commit/5bf3078)）。
- 加强下载、RSS、订阅、整理、媒体代理、通知和任务队列的端到端生命周期、并发边界、失败恢复与状态一致性（[`f250ff9`](https://github.com/li88iioo/MediaFlux/commit/f250ff9)、[`2a4e179`](https://github.com/li88iioo/MediaFlux/commit/2a4e179)、[`0a86175`](https://github.com/li88iioo/MediaFlux/commit/0a86175)、[`07f8a6a`](https://github.com/li88iioo/MediaFlux/commit/07f8a6a)、[`fa244da`](https://github.com/li88iioo/MediaFlux/commit/fa244da)）。
- 修复光鸭离线任务仅视频选择、完成状态与整理触发边界，并支持清理历史记录后显式重新提交同一资源（[`c2745c1`](https://github.com/li88iioo/MediaFlux/commit/c2745c1)、[`5594d78`](https://github.com/li88iioo/MediaFlux/commit/5594d78)）。
- Agent 开关改为运行时切换，不再为了启停 Telegram Agent 重启 Bot；关闭时会阻止旧任务继续产生受控副作用（[`71281d0`](https://github.com/li88iioo/MediaFlux/commit/71281d0)）。
- 修复进程重启后 STRM 元数据持久刷新任务可能在启动初期被错误节流的问题，确保待刷新路径会立即恢复执行（[`ea56761`](https://github.com/li88iioo/MediaFlux/commit/ea56761)）。
- 修复媒体库路径映射的 STRM 目录选择器将真实子目录误判为空的问题，现在可从 `/data/strm/光鸭云盘` 逐级选择整理目录与媒体分类（[`c0eede0`](https://github.com/li88iioo/MediaFlux/commit/c0eede0)）。

## [0.1.5] - 2026-08-24

### Added
- 媒体 Agent 扩展为可持续的影视工作台：支持从已核验候选创建、暂停、恢复和删除媒体追更订阅，并覆盖 RSS、资源候选、本地媒体恢复、媒体库巡检、STRM 与媒体反代诊断等连续工作流；高风险写操作仍需明确确认（[`8213751`](https://github.com/li88iioo/MediaFlux/commit/8213751)、[`a4d2a69`](https://github.com/li88iioo/MediaFlux/commit/a4d2a69)、[`3037a9e`](https://github.com/li88iioo/MediaFlux/commit/3037a9e)、[`c676b04`](https://github.com/li88iioo/MediaFlux/commit/c676b04)、[`bb32494`](https://github.com/li88iioo/MediaFlux/commit/bb32494)）。
- Agent 新增结构化核验、持久会话上下文、跨重启候选恢复、流式步骤跟踪和离线评测门禁；自然语言“确认/取消”可直接驱动当前待确认动作（[`4527f68`](https://github.com/li88iioo/MediaFlux/commit/4527f68)、[`9154c02`](https://github.com/li88iioo/MediaFlux/commit/9154c02)、[`8477595`](https://github.com/li88iioo/MediaFlux/commit/8477595)、[`c8cc360`](https://github.com/li88iioo/MediaFlux/commit/c8cc360)、[`def6f6c`](https://github.com/li88iioo/MediaFlux/commit/def6f6c)）。

### Changed
- Agent 路由改为优先结合 LLM 规划、已验证上下文和安全证据执行，并统一 Web/Telegram 的结果呈现、后续提问与恢复语义；兼容型 LLM Provider 遇到可恢复协议错误或瞬时限流时会在预算内降级或重试（[`e5eea7a`](https://github.com/li88iioo/MediaFlux/commit/e5eea7a)、[`f5a8472`](https://github.com/li88iioo/MediaFlux/commit/f5a8472)、[`9ea3903`](https://github.com/li88iioo/MediaFlux/commit/9ea3903)、[`a5b2438`](https://github.com/li88iioo/MediaFlux/commit/a5b2438)、[`a3a5a17`](https://github.com/li88iioo/MediaFlux/commit/a3a5a17)）。
- 本地媒体整理后的 Jellyfin/Emby 刷新改为按变化路径和已绑定媒体库精确触发；无法安全定位目标库时默认跳过全库扫描，避免无关媒体库被重复刷新（[`d486f4f`](https://github.com/li88iioo/MediaFlux/commit/d486f4f)）。
- Docker 发布链路新增源码版本、标签祖先、带日期非空 CHANGELOG、非 root 运行、健康检查、Doctor、数据库升级、多架构元数据、provenance 与 SBOM 门禁，并生成可校验的发布资产；发布脚本纳入 shell 语法回归检查，amd64/arm64 候选镜像按各自 manifest digest 独立执行 smoke，避免本地镜像缓存混淆平台（[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)、[`1f3a9a3`](https://github.com/li88iioo/MediaFlux/commit/1f3a9a3)、[`1acda80`](https://github.com/li88iioo/MediaFlux/commit/1acda80)）。

### Fixed
- 修复 Agent 在自然跟进、多轮短指令、话题切换、较慢旧操作覆盖新结果、订阅单项超时和中断恢复等场景中的上下文误继承或状态丢失；存在 RSS 与媒体追更歧义时会先要求明确类别（[`4602ebf`](https://github.com/li88iioo/MediaFlux/commit/4602ebf)、[`ae3c1a5`](https://github.com/li88iioo/MediaFlux/commit/ae3c1a5)、[`3eff0c9`](https://github.com/li88iioo/MediaFlux/commit/3eff0c9)、[`7655df2`](https://github.com/li88iioo/MediaFlux/commit/7655df2)、[`826ef0a`](https://github.com/li88iioo/MediaFlux/commit/826ef0a)）。
- 修复 Jellyfin、Findroid、Yamby 与 Android ExoPlayer 等原生客户端的 302 直连、认证恢复和 `HEAD` 播放前探测兼容性，并完善 Jellyfin HLS Token 的大小写兼容（[`7f161b3`](https://github.com/li88iioo/MediaFlux/commit/7f161b3)、[`97b8ad7`](https://github.com/li88iioo/MediaFlux/commit/97b8ad7)、[`3439110`](https://github.com/li88iioo/MediaFlux/commit/3439110)、[`9e60d06`](https://github.com/li88iioo/MediaFlux/commit/9e60d06)、[`a5059f5`](https://github.com/li88iioo/MediaFlux/commit/a5059f5)）。
- 修复 Jellyfin Web 切换清晰度后黑屏或标题丢失的问题；直放来源会保持安全播放能力并避免重新落入会被浏览器跨域策略阻断的 HLS 路径（[`160587f`](https://github.com/li88iioo/MediaFlux/commit/160587f)、[`9fe7e12`](https://github.com/li88iioo/MediaFlux/commit/9fe7e12)）。
- 加强整理识别、人工确认、纠错回退与清理保护：歧义标题和高季数剧集必须获得充分证据，否则进入人工确认；任务取消后不会继续调用云盘写入或删除接口（[`2c9295e`](https://github.com/li88iioo/MediaFlux/commit/2c9295e)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。
- 修复 STRM 整理联动在连续任务、静默窗口、进程重启或初始化失败时可能遗漏变化或遗留运行锁的问题；变更会先持久化、合并，再按顺序恢复执行（[`2c9295e`](https://github.com/li88iioo/MediaFlux/commit/2c9295e)、[`dd1b534`](https://github.com/li88iioo/MediaFlux/commit/dd1b534)）。
- 数据库 schema 升级改为升级前备份和保存点内原子迁移，失败会完整回滚；恢复备份会核验真实数据库 schema，拒绝未来版本或缺少数据库载荷的完整恢复（[`924e2f8`](https://github.com/li88iioo/MediaFlux/commit/924e2f8)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。
- 确认动作与持久化整理队列增加崩溃恢复语义：中断中的动作会标记为“结果待核对”，避免重启后误报成功或重复执行；队列严格按创建顺序领取（[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。

### Security
- 加固媒体反代边界：规范化转发头、阻止 WebSocket 上游重定向、过滤内部直放地址与上游 Cookie，不向媒体服务器泄露 MediaFlux 登录会话，并拒绝跨服务器、目录穿越、重复编码和不安全相对跳转（[`183e249`](https://github.com/li88iioo/MediaFlux/commit/183e249)、[`b47e4b3`](https://github.com/li88iioo/MediaFlux/commit/b47e4b3)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。

## [0.1.4] - 2026-08-22

### Changed
- Jellyfin/Emby Web 的光鸭播放改为由 MediaFlux 提供同源流式中继，支持 `Range`、`If-Range`、`HEAD` 与 CDN 内部重定向；Infuse、VidHub、Fileball、Jellyfin 原生客户端等非浏览器客户端仍保持 302 直连 CDN。浏览器中继会占用 MediaFlux 所在设备的网络带宽，这是绕过上游 CDN 缺少 CORS 响应头所必需的兼容路径（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。

### Fixed
- 修复 Jellyfin Web 的 HTML5 视频请求跟随光鸭 CDN 302 后，因目标 CDN 未返回 `Access-Control-Allow-Origin` 而被浏览器拦截、最终无法播放的问题；Web 播放会话现在会稳定保持同源中继策略，不依赖 `Sec-Fetch-*` 请求头（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。
- 加固浏览器媒体中继的 signed URL 校验与资源释放：逐跳固定公网 DNS 地址，拒绝私网、CGNAT、链路本地、site-local、云元数据及带凭据目标，并过滤 Cookie、Authorization、媒体服务器 Token 与上游 `Set-Cookie`/`Location`（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。

## [0.1.3] - 2026-08-22

### Fixed
- 修复 Jellyfin Web 经光鸭 302 直链播放时，因 PlaybackInfo 残留 HLS/转码字段而错误使用 HLS.js 跨域请求 CDN，最终被浏览器 CORS 策略拦截的问题；光鸭媒体源现在会完整清理转码元数据并继续通过带短时能力凭据的 DirectStream URL 安全跳转至 CDN（[`f89a41c`](https://github.com/li88iioo/MediaFlux/commit/f89a41c)）。

## [0.1.2] - 2026-08-22

### Added
- 本地媒体扫描支持把多层作品/季度目录展开为独立视频单元，并过滤非媒体文件、精确绑定同级字幕；单集识别异常不再阻塞同目录其他内容（[`c6b437c`](https://github.com/li88iioo/MediaFlux/commit/c6b437c)、[`bf31930`](https://github.com/li88iioo/MediaFlux/commit/bf31930)）。
- 本地媒体待确认任务接入 Telegram 原子确认流程，增强 qB 完成探测的重试、失败反馈和任务终态保护（[`7b339f1`](https://github.com/li88iioo/MediaFlux/commit/7b339f1)）。
- 新增整理后媒体规格异步补全队列；实时 `ffprobe` 失败时后台低并发重试，成功后安全重命名并触发 STRM 增量同步（[`9e46a82`](https://github.com/li88iioo/MediaFlux/commit/9e46a82)）。
- 整理与追更通知支持发送媒体封面，图片投递失败时自动降级为文本消息（[`8d826f6`](https://github.com/li88iioo/MediaFlux/commit/8d826f6)）。

### Changed
- 移除已停止维护的 Windows SMB 运行时、UNC 凭据输入和对应测试；本地媒体来源统一使用 Docker 容器绝对路径，启动时清空旧数据库中的 SMB 用户名与密码。qB 的 Windows/UNC 路径前缀映射仍保留（[`37c3ef2`](https://github.com/li88iioo/MediaFlux/commit/37c3ef2)）。
- 动画电影统一按电影类型归档，并收敛 STRM 文件命名，降低 Jellyfin/Emby 元数据识别歧义（[`dd49f9b`](https://github.com/li88iioo/MediaFlux/commit/dd49f9b)）。
- 高频网络、Telegram、302 与媒体接口日志增加限流和敏感信息脱敏；测试进程默认不再写入正式 `app.log`（[`2c015e9`](https://github.com/li88iioo/MediaFlux/commit/2c015e9)、[`37c3ef2`](https://github.com/li88iioo/MediaFlux/commit/37c3ef2)）。

### Fixed
- 修复浏览器经媒体 302 反代播放时 HTML5 视频请求缺少媒体服务器 Token、播放会话无法恢复、重复参数误判，以及普通标题含冒号或斜杠时名称丢失的问题（[`90cc553`](https://github.com/li88iioo/MediaFlux/commit/90cc553)）。
- 修复持续播放超过 15 分钟后短时授权、媒体源映射与播放许可过期的问题；活跃请求会滑动续期，并保留 12 小时绝对安全上限，视频数据继续通过 302 由终端直连云盘 CDN（[`e809255`](https://github.com/li88iioo/MediaFlux/commit/e809255)）。
- 修复人工确认整理日志仍显示跳过、错误提供批量回退操作，以及 Telegram 整理消息封面不稳定的问题（[`8d826f6`](https://github.com/li88iioo/MediaFlux/commit/8d826f6)）。
- 修复移动端弹窗、工具栏和虚拟键盘场景超出 visual viewport 或发生布局跳动的问题（[`8536281`](https://github.com/li88iioo/MediaFlux/commit/8536281)、[`49b27f7`](https://github.com/li88iioo/MediaFlux/commit/49b27f7)）。
- 探索、RSS 和全局搜索中的媒体档案改为页面内弹窗打开，避免查看详情时整页跳转和状态丢失（[`7fccf61`](https://github.com/li88iioo/MediaFlux/commit/7fccf61)）。

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

[Unreleased]: https://github.com/li88iioo/MediaFlux/compare/v0.1.10...HEAD
[0.1.10]: https://github.com/li88iioo/MediaFlux/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/li88iioo/MediaFlux/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/li88iioo/MediaFlux/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/li88iioo/MediaFlux/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/li88iioo/MediaFlux/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/li88iioo/MediaFlux/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/li88iioo/MediaFlux/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/li88iioo/MediaFlux/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/li88iioo/MediaFlux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/li88iioo/MediaFlux/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/li88iioo/MediaFlux/releases/tag/v0.1.0
