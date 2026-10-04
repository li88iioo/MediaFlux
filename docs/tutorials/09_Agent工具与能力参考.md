# Media Agent：工具与能力参考

> 核对日期：2026-09-10；代码基线：`main` 的 `d2318f4`。本文列出当前对话 Agent 的实际工具声明及 Provider 操作白名单，不代表所有工具在未配置服务时都可用。

架构与操作流程见 [Agent 架构与使用实战](08_Agent架构与使用实战.md)。用户不必背工具名，直接描述目标；工具名主要供能力查询、排错和维护使用。

## 1. 如何阅读清单

- 当前对话工具目录共 **190 项**：**115 READ、55 WRITE、20 DANGER**。
- 领域声明中的 `LOW_WRITE` 进入 Kernel 后也映射为 **WRITE**，仍需确认。原声明计数为 READ 115、LOW_WRITE 35、WRITE 20、DANGER 20。
- 另有 **31 项 Provider 操作**，由受控网关或具体工具复用，**不能与 190 相加当作独立工具总数**。
- 这是注册目录，不是一次模型调用的工具窗口。初始通常召回 6–10 项；缺少相关能力时可通过 `agent.capabilities` 动态发现。
- `READ` 指不执行目标业务写入；仍可能发起外部请求、消耗搜索额度、写入会话引用或刷新缓存。它不等于完全离线、零成本。
- `WRITE` / `DANGER` 表示普通聊天调用时先准备 EffectPlan，用户确认后才执行。带 `preview` 的只读工具只能冻结计划，不能替代执行确认。
- 参数、选择范围和当前条件以运行时 Schema、能力查询和确认卡为准；不提供手工绕过 owner、会话引用或快照的调用方式。
- 内部主动识别复核只使用专门的小型工具集，不等于后台自动取得下方全部权限。

| 工具组 | 数量 |
|---|---:|
| 工作区与综合查询 | 5 |
| 系统、配置诊断与能力发现 | 23 |
| 活动、跟踪与撤销 | 7 |
| 媒体库、缺集与批量核对 | 14 |
| 媒体质量、观看状态与播放列表 | 11 |
| 探索、元数据、推荐与网页 | 17 |
| 资源索引与统一接入 | 5 |
| 下载诊断与提交续查 | 3 |
| RSS、媒体追更与个人偏好 | 32 |
| 媒体自动化规则与摘要 | 3 |
| 识别知识、本地来源与路径映射 | 11 |
| 光鸭目录、文件变更与整理 | 22 |
| 光鸭账号、回收站与分享 | 8 |
| 本地媒体整理 | 13 |
| STRM 同步与失败处理 | 8 |
| STRM 伴随元数据 | 3 |
| 播放就绪与媒体反代 | 5 |

## 2. 按领域列出的对话工具

### 2.1 工作区与综合查询（5 项）

看板、健康状况、待办、下一步建议和全局搜索。

定义：[`workspace.py`](../../app/agent/domain_catalog/workspace.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `workspace.briefing` | READ | 生成本地系统简报，汇总工作区待办、下载后核验、媒体库巡检、索引器就绪与媒体服务器配置完整性；不访问网络，也不扫描媒体或云盘内容目录。 |
| `workspace.health` | READ | 执行媒体系统健康总检，聚合本地工作区、关键配置与媒体服务器连通性；不扫描内容目录、不搜索资源且不执行写操作。 |
| `workspace.todo` | READ | 只读汇总下载、RSS、整理、STRM、本地媒体、下载后核验与媒体库巡检的工作区待办计数；不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。 |
| `workspace.next_actions` | READ | 从本地安全待办快照生成按固定优先级排列的只读下一步行动卡；不执行诊断、预检或写操作，不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。 |
| `workspace.search` | READ | 按标题搜索媒体库、RSS、下载、整理与本地媒体工作流；不返回路径、URL、凭据、哈希、业务标识或错误正文。 |

### 2.2 系统、配置诊断与能力发现（23 项）

当前运行条件、工具发现、Provider 网关、功能与索引站开关、任务状态。

定义：[`system.py`](../../app/agent/domain_catalog/system.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `agent.runtime_status` | READ | 只读返回 Media Agent 总开关、Telegram 接入和模型路由的当前启用状态，不返回令牌、密钥或供应商配置值。 |
| `provider.capabilities` | READ | 列出媒体服务器与 qBittorrent 当前已开放的原生语义操作、参数和非敏感 profile；不会连接上游，也不会返回地址或凭据。 |
| `provider.query` | READ | 调用静态目录中已登记的 Jellyfin、Emby 或 qBittorrent 只读操作。 |
| `provider.change.preview` | READ | 为静态目录中已开放的媒体服务器或 qBittorrent 写操作执行实时预检并冻结短期计划。 |
| `provider.change.execute` | WRITE | 在用户确认后执行一个 owner/session 绑定的冻结 Provider 写计划。 |
| `provider.job.status` | READ | 读取当前会话中指定 Provider 写计划的持久状态、公开目标摘要与写后核验结果。 |
| `config.diagnose` | READ | 检查媒体服务器、TMDB、下载器、STRM 与 AI 回退配置是否完整，不返回配置值。 |
| `config.explain_component` | READ | 解释一个白名单配置组件的状态、必要字段标签、受影响能力与安全下一步，不返回配置键或配置值。 |
| `config.feature_summary` | READ | 只读汇总媒体探索、资源检索与联网搜索的启用状态和依赖可用性，不返回配置值或供应商凭据。 |
| `automation.diagnose_pipeline` | READ | 只读汇总下载、RSS、光鸭整理与 STRM 的本地自动化状态，不访问外部服务且不返回路径、凭据或业务标识。 |
| `config.diagnose_media_servers` | READ | 使用服务端当前生效配置汇总诊断 Jellyfin 12 与 Emby / Jellyfin 10.x 节点的连通性、产品版本和兼容槽位，不返回地址、服务器名称或凭据。 |
| `config.test_media_server` | READ | 使用服务端当前生效配置测试 Jellyfin 或 Emby / Jellyfin 10.x 的连通性与鉴权，不返回地址或凭据。 |
| `recognition.set_rule_enabled` | WRITE | 预检并在用户确认后，按明确规则类型和编号启用或停用一条识别规则；不会修改规则内容、映射、别名或优先级。 |
| `config.indexer_sites_summary` | READ | 读取当前固定白名单资源站点的选择，仅返回站点 ID、展示名和数量。 |
| `config.set_indexer_sites` | WRITE | 预检并在用户确认后更新固定白名单资源站点，不接受配置键、URL、凭据、Cookie 或路径。 |
| `telegram.send_test_notification` | WRITE | 预检并在用户确认后向当前已配置会话发送一条固定 Telegram 连接测试消息；不接受消息、凭据或会话参数。 |
| `config.safe_policy_summary` | READ | 读取 Agent 可安全管理的固定白名单策略，只返回公开值和环境托管状态。 |
| `config.set_safe_policy` | WRITE | 预检并在用户确认后修改一项固定白名单非敏感策略，不接受任意配置键、凭据、URL 或路径。 |
| `config.set_feature_state` | WRITE | 预检并在用户确认后开启或关闭一个非敏感白名单功能，不接受配置键或任意配置值。 |
| `agent.job_status` | READ | 查询当前登录会话发起的后台全库检查进度与安全结果；不会启动或修改任务。 |
| `agent.cancel_job` | WRITE | 预检并在用户确认后安全取消当前会话的后台全库检查。 |
| `agent.action_history` | READ | 查看最近经确认执行的 Agent 动作审计，仅返回脱敏状态、聚合计数与耗时。 |
| `agent.capabilities` | READ | 回答 MediaFlux Media Agent 是谁、能做什么，并列出当前可以读取或经确认执行的项目能力；传query可从全项目发现需要的原子工具，或传tool_names核实可用状态，并在下一次模型调用加载Schema。 |

### 2.3 活动、跟踪与撤销（7 项）

按媒体或任务查活动时间线；回退必须先检查支持范围，不能承诺任意操作可撤销。

定义：[`activity.py`](../../app/agent/domain_catalog/activity.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `activity.search` | READ | 按标题查找下载、光鸭整理和本地整理活动，并返回可续查的会话引用。 |
| `activity.timeline` | READ | 通过安全活动引用读取下载→整理→STRM→媒体库复核的持久化阶段及故障原因，只按真实请求/运行标识关联。 |
| `action.undo.inspect` | READ | 核对指定光鸭整理日志是否有可逆移动/改名的完整操作快照。 |
| `action.undo.execute` | DANGER | 使用服务端回退凭证生成冻结恢复计划；用户确认后才调用原领域执行器。 |
| `activity.follow` | WRITE | 持续跟踪选定任务，异常、已有阶段结束或到期时发送一次 Telegram 通知；只观察、不替任务重试。 |
| `activity.follows` | READ | 查看我持久保存的任务跟踪规则及启用状态和到期时间。 |
| `activity.unfollow` | WRITE | 停用我的活动跟踪通知，不停止或删除下载/整理任务。 |

### 2.4 媒体库、缺集与批量核对（14 项）

库存搜索、批量在库匹配、季集计数、缺集资源、巡检与复核工作流。

定义：[`library.py`](../../app/agent/domain_catalog/library.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `library.search` | READ | 在已配置的 Jellyfin / Emby 媒体库中搜索一个具体标题；适合单片核对，并在可用时返回已校验的 open_url。 |
| `library.batch_presence` | READ | 一次按 TMDB ID 批量核对最多 50 部电影或剧集是否存在于已配置的 Jellyfin / Emby。 |
| `library.search_missing_episode_resources` | READ | 先确认指定季集属于已播缺集，再按当前身份保存的资源偏好排序；本次 preference_overrides 优先于长期偏好（空数组/0/any 可取消对应约束），不会自动下载。 |
| `library.search_missing_season_resources` | READ | 单部核对指定季度；多部用 items 一次核对最多 12 部/季，每部最多搜索 3 个已播缺集。统一输出最多 12 项全局候选及同一快照，保留未覆盖项和季集映射不确定性；一次预检整批、人工确认后才下载。 |
| `library.missing_media_workflows` | READ | 查看当前用户最近缺集补库流程的安全状态；只返回剧名、季集、阶段、目标类型与是否已建立下载任务，不返回资源句柄、磁力、URL、路径或凭据。 |
| `library.check_updates` | READ | 单部或批量核对最多 20 部媒体；默认刷新 Jellyfin / Emby 库存，对照 TMDB 已播季集。逐部保留缺集、无已播缺集、歧义与不可用状态；不代表全网资源发布进度。 |
| `library.audit_library_episodes` | READ | 有界枚举已配置媒体服务器中的剧集，并按可靠 TMDB 映射巡检截至指定日期的已播缺集。 |
| `library.start_episode_audit` | WRITE | 在用户确认后创建可恢复、可查询进度、可取消的后台全库剧集完整性检查。 |
| `library.patrol_status` | READ | 查询最近一次后台全库缺集巡检的安全摘要；不会触发巡检、资源搜索或下载。 |
| `library.patrol_policy` | READ | 读取后台全库缺集巡检的启用、通知、间隔和单轮检查上限，不返回其他配置。 |
| `library.set_patrol_policy` | WRITE | 预检并在用户确认后修改全库缺集巡检的四项白名单策略，不接受配置键、凭据、URL 或路径。 |
| `library.trigger_patrol_now` | WRITE | 预检并在用户确认后，按当前全库缺集巡检策略把单例后台任务排到现在；不修改策略、不搜索资源、不下载。 |
| `library.count_series_episodes` | READ | 直接读取已配置 Jellyfin / Emby 中指定剧集的本地普通集数量与季度分布；不访问 TMDB，也不判断缺集。 |
| `library.audit_episodes` | READ | 核对媒体库剧集与 TMDB 截止日期前已播普通集，报告缺集或可更新集。 |

### 2.5 媒体质量、观看状态与播放列表（11 项）

读取服务器报告的质量和用户状态，受控修改已看、收藏和播放列表；不进行真实播放探测。

定义：[`library_quality.py`](../../app/agent/domain_catalog/library_quality.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `media.library.quality` | READ | 分页只读检查媒体库视频版本、分辨率、中文字幕和服务器报告缺失状态；未知字段保持 unknown，仅本页查重，不将分页样本当全库结果，不播放或探测 STRM。 |
| `media.user.inspect` | READ | 读取搜索结果中单个媒体条目的已看、收藏和观看进度状态，创建绑定用户的短期状态引用；修改前必须读取。 |
| `media.playlists.list` | READ | 分页列出当前媒体服务器用户可访问的播放列表，并返回短期列表引用。 |
| `media.playlist.inspect` | READ | 读取播放列表及完整成员快照，最多250项；条目分页展示；返回修改用的列表快照引用和移除用的成员引用。 |
| `media.user.mark_played` | WRITE | 人工确认后标记已看单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.mark_unplayed` | WRITE | 人工确认后标记未看单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.favorite` | WRITE | 人工确认后收藏单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.unfavorite` | WRITE | 人工确认后取消收藏单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.playlist.create` | WRITE | 人工确认后为配置用户创建私有 Jellyfin 视频播放列表；可放入最多20个明确视频，不展开剧集。 |
| `media.playlist.add_items` | WRITE | 人工确认后把明确视频加入已有播放列表，跳过已经存在的条目；playlist_ref 来自 media.playlist.inspect。 |
| `media.playlist.remove_items` | WRITE | 人工确认后按成员引用移出播放列表，只改成员关系，绝不删除媒体文件；两个引用均来自 media.playlist.inspect。 |

### 2.6 探索、元数据、推荐与网页（17 项）

元数据详情与演职员、电影人作品列表、本地与外部推荐、网页搜索/正文及两类动漫日历。

定义：[`discovery.py`](../../app/agent/domain_catalog/discovery.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `web.search` | READ | 通过受控 Tavily Provider 搜索公开网页；用于核对官方平台当前更新进度、最新播出信息和其他时效性事实。 |
| `web.read` | READ | 读取一个普通公开 HTTPS 网页的正文，用于总结用户给出的文章、公告、文档，或在网页搜索后打开权威来源核实事实。 |
| `discovery.search` | READ | 搜索 TMDB、豆瓣与 Bangumi 的影视身份和概况，不含演职员表。 |
| `discovery.person_filmography` | READ | 按人物名称读取 TMDB 电影演职员表，可按导演、演员、编剧或制片身份筛选，并一次返回按上映日期升序排列的有界作品列表。 |
| `discovery.lookup_rating` | READ | 按明确影视名称、类型和年份查询豆瓣评分；优先使用豆瓣结构化数据，必要时受控检索并读取已验证的豆瓣条目页。 |
| `discovery.detail` | READ | 读取精确条目的标题、年份、播出时间、简介和映射状态，不包含演职员表。 |
| `discovery.credits` | READ | 只读查询已确认 TMDB 作品的演员、角色/配音及导演等主创。 |
| `discovery.mapping_candidates` | READ | 只读查询一个非 TMDB 来源条目的 TMDB 映射候选，并将内部候选身份短期绑定到当前会话；不会自动保存高置信映射。 |
| `discovery.confirm_mapping` | WRITE | 预检并在用户确认后保存当前会话最近映射候选中的一个；候选会重新通过 TMDB 详情核验。 |
| `discovery.watchlist_summaries` | READ | 只读列出有界探索收藏摘要，仅含收藏编号、来源、媒体类型、标题和年份。 |
| `discovery.get_watchlist_summary` | READ | 按精确收藏编号读取一条探索收藏的安全摘要。 |
| `discovery.add_watchlist` | WRITE | 预检并在用户确认后把一个精确影视条目加入本地探索收藏；不会下载资源。 |
| `discovery.remove_watchlist` | WRITE | 预检并在用户确认后按精确收藏编号移除本地探索收藏；不会删除媒体文件或下载任务。 |
| `media.recommend_from_library` | READ | 从已配置的 Jellyfin / Emby 本地媒体库中推荐可以立即观看的作品。 |
| `discovery.recommend` | READ | 读取已启用的 TMDB 或豆瓣推荐列表；可按用户明确给出的年份、地区和题材做受控筛选，不返回海报地址、收藏状态或配置值。 |
| `discovery.anime_calendar` | READ | 读取腾讯视频、爱奇艺和优酷公开动漫排期的追漫日历；默认今天，可按日期、平台和片名筛选。 |
| `bangumi.calendar` | READ | 读取用户明确指定的 Bangumi 本周或指定星期放送表，不表示国内视频平台排期；不返回图片地址、收藏状态或 Provider 配置。 |

### 2.7 资源索引与统一接入（5 项）

搜索得到候选引用，再检查、确认和提交资源；直接链接与分享也复用受控接入链路。

定义：[`resource.py`](../../app/agent/domain_catalog/resource.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `indexer.diagnose_readiness` | READ | 只读检查多站资源索引器的本地开关、启用站点与能力声明；不访问资源站、网络或文件系统，也不返回 URL、Cookie 或凭据。 |
| `indexer.search_resources` | READ | 在已启用的多站索引中搜索短期资源结果，只返回 opaque result_id 与公开元数据。 |
| `ingest.inspect` | READ | 统一只读检查资源接入来源：可识别光鸭官方分享、Magnet、ED2K、明确 HTTP(S) 下载直链，或读取当前会话最近资源搜索候选。 |
| `ingest.submit` | DANGER | 在用户确认后统一提交最近检查的直链或光鸭分享，或按资源搜索候选序号提交。 |
| `ingest.status` | READ | 按公开请求编号读取统一资源接入状态，覆盖 qB、光鸭、整理与 STRM 阶段；不返回链接、路径、哈希或后端任务标识。 |

### 2.8 下载诊断与提交续查（3 项）

查看下载队列及最近提交，重试前检查结果与幂等边界；实时 qB 操作另见 Provider 目录。

定义：[`download.py`](../../app/agent/domain_catalog/download.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `downloads.diagnose_queue` | READ | 只读诊断 qBittorrent 当前队列、传输状态与疑似停滞任务，不返回 hash、路径或凭据。 |
| `downloads.request_summaries` | READ | 只读列出 MediaFlux 统一下载请求在 qB、光鸭、整理与 STRM 各阶段的安全状态摘要，不返回链接、路径、哈希或云端任务标识。 |
| `downloads.retry_submission` | DANGER | 预检并在用户确认后，将一条明确编号的下载待处理记录重新提交到 qBittorrent、光鸭或两者；不返回资源链接、种子、路径、任务标识或凭据。 |

### 2.9 RSS、媒体追更与个人偏好（32 项）

RSS 规则订阅和影视追更订阅是不同对象；本组也维护观看/下载偏好、最近播放与内容汇总。

定义：[`subscription.py`](../../app/agent/domain_catalog/subscription.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `rss.diagnose` | READ | 只读诊断 RSS 订阅、待处理、失败与长期提交中条目，不访问订阅源且不返回 URL、GUID、payload 或路径。 |
| `rss.subscription_summaries` | READ | 只读列出 RSS 规则订阅（不是媒体追更订阅）的安全摘要，仅含编号、名称、启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。 |
| `rss.get_subscription_summary` | READ | 按精确订阅编号读取 RSS 安全摘要，仅含名称、启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。 |
| `rss.create_subscription` | WRITE | 预检并在用户确认后创建一个 RSS 订阅；支持订阅地址、过滤、刷新、下载目标和媒体去重配置，不接受任意下载路径或云端目录标识。 |
| `rss.update_subscription` | WRITE | 预检并在用户确认后更新一个指定 RSS 订阅的名称、地址、过滤、刷新、下载目标或媒体去重配置；不接受任意路径。 |
| `rss.delete_subscription` | DANGER | 预检并在用户确认后永久删除一个指定 RSS 订阅及其本地条目记录；不删除下载任务或已下载文件。 |
| `media.subscription_summaries` | READ | 只读列出影视/动画媒体追更订阅（不是 RSS 规则订阅）摘要，仅含编号、标题、媒体类型、启用状态和缺失数量。 |
| `media.subscription_updates` | READ | 实时检查全部媒体追更订阅：逐条比较 TMDB 已播清单与 Jellyfin/Emby 本地库存，并对确认缺失项执行有界多站资源搜索；只返回下载建议，不提交 qBittorrent 或光鸭。 |
| `media.get_subscription_summary` | READ | 按精确订阅编号读取一条媒体追更订阅的安全摘要。 |
| `media.get_subscription_policy` | READ | 按精确订阅编号读取追更范围、动作模式、下载目标和检查周期；不返回站点明细或凭据。 |
| `media.set_subscription_policy` | DANGER | 预检并在用户确认后修改一个媒体追更订阅的追更范围、动作模式、下载目标或检查周期；不会立即检查或下载。 |
| `media.create_subscription` | WRITE | 预检并在用户确认后，为一个精确影视条目创建媒体追更订阅；不会立即搜索或下载资源。 |
| `media.delete_subscription` | DANGER | 预检并在用户明确确认后软删除一个精确编号的媒体追更订阅；不会删除已提交下载任务或媒体文件。 |
| `media.set_subscription_enabled` | WRITE | 预检并在用户确认后暂停或恢复一个指定媒体追更订阅；不会操作已提交下载任务或媒体文件。 |
| `media.recently_played` | READ | 读取媒体服务器用户的真实最近播放历史（播放事件/DatePlayed）；优先使用明确配置的用户，未配置时沿用服务器默认用户选择。 |
| `media.recently_added` | READ | 读取 Jellyfin 或 Emby 最近入库的内容；连续单集会按作品去重，并在可用时返回已校验的 open_url。 |
| `media.continue_watching` | READ | 读取媒体服务器用户的继续观看列表；优先使用明确配置用户，未配置时沿用服务器默认用户选择；不返回用户 ID、路径或凭据，但会在可用时返回已校验的 open_url。 |
| `media.preferences` | READ | 读取当前身份显式保存的媒体服务器、下载目标、清晰度/HDR/编码/字幕/音轨/发布组/排除词/单集大小/题材与观看偏好；单次请求条件优先，不从聊天摘要推断。 |
| `media.set_preferences` | WRITE | 预检并在用户确认后按字段更新结构化媒体偏好；本地推荐和资源排序使用这些默认偏好，单次显式参数（包括空数组、0、False）优先。 |
| `media.clear_preferences` | WRITE | 预检并在用户确认后清除当前会话保存的显式媒体偏好，恢复产品默认值。 |
| `media.today_summary` | READ | 按本机今天的日期汇总全局管理员范围内的追更检查、整理入库、RSS 与下载内容事件；不返回路径、磁力、凭据或错误正文。 |
| `media.subscription_notification_rule` | READ | 读取指定全局媒体追更订阅的通知规则；只返回公开订阅编号、标题和布尔开关。 |
| `media.set_subscription_notification_rule` | WRITE | 预检并在用户确认后修改指定全局媒体追更订阅的缺集、满足或错误通知开关；不会改变订阅巡检策略。 |
| `media.reset_subscription_notification_rule` | WRITE | 预检并在用户确认后删除指定全局媒体追更订阅的显式通知规则，恢复默认关闭状态。 |
| `rss.recent_activity` | READ | 统计最近 24 小时 RSS 成功下载次数，并按订阅名称汇总；不返回 URL、条目正文或路径。 |
| `rss.entry_summaries` | READ | 安全列出 RSS 条目的公开编号、标题、状态、季集线索和固定失败分类；不返回 GUID、payload、下载 URL、路径或凭据。 |
| `rss.mark_entries` | WRITE | 预检并在用户确认后把精确 RSS 条目编号标记为已处理或未处理；不会覆盖正在提交或已下载的条目。 |
| `rss.submit_entries_to_qb` | DANGER | 预检并在用户确认后把精确的 pending RSS 条目集合提交到 qBittorrent；集合与配置会在确认时重新核对。 |
| `rss.refresh_subscription` | WRITE | 预检并在用户确认后刷新一个指定 RSS 订阅；不自动下载且不返回 URL、过滤词、条目正文或凭据。 |
| `rss.refresh_subscriptions` | WRITE | 预检并在用户确认后依次刷新一组 RSS 订阅，单次最多 32 个；不自动下载且不返回 URL、过滤词、条目正文或凭据。 |
| `rss.submit_pending_to_qb` | DANGER | 预检并在用户确认后，将最新的待处理 RSS 条目有界提交到 qBittorrent；不返回条目、URL、路径或凭据。 |
| `rss.retry_failed` | DANGER | 预检并在用户确认后，按各订阅目标有界重试已明确分类为可安全重试的 qBittorrent / 光鸭 RSS 失败条目；不返回条目、URL、路径、失败原文或凭据。 |

### 2.10 媒体自动化规则与摘要（3 项）

创建媒体规则、查询或配置定时摘要；配置变更仍需要确认。

定义：[`automation_rules.py`](../../app/agent/domain_catalog/automation_rules.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `automation.create_media_rule` | WRITE | 创建持续运行的媒体追更规则：既有调度器按每3天或7天检查缺集、搜索指定站点、按提醒/确认/自动策略提交光鸭或qB。 |
| `automation.digest_rules` | READ | 读取当前身份保存的每日媒体动态/异常摘要规则，默认没有开启。 |
| `automation.set_digest` | WRITE | 确认后新增或修改每日摘要规则；按本机时区每天指定时刻汇总媒体动态，或只汇总失败和需关注项。 |

### 2.11 识别知识、本地来源与路径映射（11 项）

在白名单范围内管理识别知识、来源和媒体路径映射，复用现有配置服务。

定义：[`configuration_management.py`](../../app/agent/domain_catalog/configuration_management.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `config.recognition_knowledge` | READ | 读取Web识别知识库的发布组/尾部制作组词条与别名，区分内置和用户词条；不是媒体TMDB锁定规则。 |
| `config.create_recognition_knowledge` | WRITE | 添加用户识别知识（发布组或尾部制作组与别名），与Web知识库共用实现。 |
| `config.update_recognition_knowledge` | WRITE | 修改识别知识名称、别名或停用状态；不允许篡改来源、证据或内置知识身份。 |
| `config.delete_recognition_knowledge` | DANGER | 删除用户识别知识；内置知识不能删除，只能停用。 |
| `config.create_local_source` | WRITE | 新增Web本地媒体来源。 |
| `config.update_local_source` | WRITE | 修改本地媒体来源名称、容器目录、qB路径前缀、识别类型或整理模式，保留既有归档映射。 |
| `config.delete_local_source` | DANGER | 删除本地媒体来源配置（不删除媒体文件）；有未完成任务的来源不能删除。 |
| `config.media_path_mappings` | READ | 读取Jellyfin/Emby的STRM与本地路径前缀映射摘要；不暴露配置凭据，不等同于本地分类归档绑定。 |
| `config.create_media_path_mapping` | WRITE | 添加Jellyfin/Emby路径前缀映射，使用用户给出的本地STRM目录和媒体服务器可见目录。 |
| `config.update_media_path_mapping` | WRITE | 修改一条媒体服务器路径前缀映射，保留同服务器其它映射。 |
| `config.delete_media_path_mapping` | WRITE | 删除一条媒体库路径前缀映射，不删除媒体文件；后续该路径将不再进行前缀转换。 |

### 2.12 光鸭目录、文件变更与整理（22 项）

浏览与文件变更不同于刮削整理；预览可以是 READ，但实际执行入口仍受确认约束。

定义：[`cloud.py`](../../app/agent/domain_catalog/cloud.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `guangya.capabilities` | READ | 读取 Agent 当前开放的光鸭业务能力与安全边界。 |
| `guangya.connection_status` | READ | 验证光鸭账号是否已配置且可通过普通最小只读请求连接；允许 SDK 续签登录态，但不返回凭据。 |
| `guangya.organize.schedule_policy` | READ | 读取光鸭定时整理的启用状态、五段 cron 和通知开关，不返回目录或凭据。 |
| `guangya.organize.set_schedule_policy` | WRITE | 预检并在用户确认后修改光鸭定时整理三项白名单策略，不立即运行整理。 |
| `guangya.organize.status` | READ | 查看光鸭整理任务、排队操作和定时调度状态；可按公开操作编号查询终态，不返回目录或错误正文。 |
| `organize.audit_logs` | READ | 按来源和规范状态只读查看整理记录摘要，不返回路径、任务标识、文件名、外部 ID 或错误正文。 |
| `guangya.organize.cleanup.preview` | READ | 只读检查指定精确光鸭目录，或未指定时检查所有正式整理来源中的真空目录和严格垃圾残留目录。 |
| `guangya.organize.cleanup.classify` | READ | 逐项复核最近冻结的光鸭残留候选。 |
| `guangya.organize.cleanup.execute` | DANGER | 在用户确认后执行最近一次冻结的光鸭整理残留计划：真空目录经复核后进入回收站，仅将逐项确认隔离的残留目录整体移入 MediaFlux 隔离区。 |
| `guangya.fs.query` | READ | 通用只读光鸭文件查询。 |
| `guangya.fs.change.preview` | READ | 把当前会话近期光鸭观察中的对象引用编译为确定性冻结计划；observation_ref 只指定主快照，同一 owner 与凭据世代的近期安全引用会自动合并，无需为了跨快照对象重复扫描。 |
| `guangya.fs.change.execute` | DANGER | 在用户确认后执行最近一次通用光鸭文件变更冻结计划。 |
| `guangya.media_hygiene.preview` | READ | 只读扫描一个精确光鸭目录中的媒体名称污染。 |
| `guangya.rename.preview` | READ | 按 1 到 4 个精确光鸭绝对路径只读预览批量名称转换；支持递归删除旧式 Mbps 码率字段或字面文本替换。 |
| `guangya.rename.execute` | DANGER | 在用户确认后执行当前会话最近冻结的光鸭重命名计划，包括批量名称转换和媒体名称清理；不接受文件 ID、路径或名称参数，执行前复核凭据、快照和目标冲突，写后按file_id 验证真实名称。 |
| `guangya.directory_scrape.inspect` | READ | 按当前整理规则只读检查一个准备直接识别并归档入媒体库的精确光鸭目录或视频；后续可搜索 TMDB、预览最终归档目录与编号方案。 |
| `guangya.directory_scrape.search` | READ | 基于当前会话最近一次光鸭刮削检查搜索 TMDB/MetaTube 匹配候选；不写入映射或云盘。 |
| `guangya.directory_scrape.preview` | READ | 按当前会话最近的匹配候选生成安全刮削预览，展示将创建的归档目录、TMDB 绝对集数或季度编号映射以及批量重命名结果；只做 dry-run，不移动、重命名或删除云盘文件。 |
| `guangya.directory_scrape.run` | DANGER | 预检并在用户确认后把当前会话最近的光鸭刮削预览提交到现有整理互斥队列；执行前会重新核对内容与计划。 |
| `guangya.organize.preview` | READ | 按当前服务端配置只读预览光鸭整理计划，不移动、改名或删除内容。 |
| `guangya.organize.run_once` | DANGER | 预览并在用户确认后按当前配置启动一次光鸭网盘整理，不接受执行参数。 |
| `guangya.organize.stop` | DANGER | 预检并在用户确认后协作式停止当前光鸭整理任务；已完成的云盘操作不会回滚。 |

### 2.13 光鸭账号、回收站与分享（8 项）

账号容量、回收站恢复/全局清空、异步操作状态及分享管理；不开放本地上传。

定义：[`cloud_sdk.py`](../../app/agent/domain_catalog/cloud_sdk.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `guangya.account.status` | READ | 读取当前光鸭账号连接状态与服务端可用的容量摘要。 |
| `guangya.recycle.list` | READ | 分页读取光鸭回收站，返回名称、类型、体积和会话绑定的 guangya_recycle_items_ref；不返回 Provider 文件 ID。 |
| `guangya.recycle.restore` | WRITE | 对 guangya.recycle.list 返回的回收站引用按 index 冻结恢复计划。 |
| `guangya.recycle.clear` | DANGER | 完整读取并冻结当前光鸭回收站后生成不可逆清空计划。 |
| `guangya.operation.status` | READ | 使用回收站恢复、清空或其他 SDK 异步操作返回的 guangya_task_ref 查询 Provider 状态。 |
| `guangya.share.list` | READ | 分页读取当前账号自己创建的光鸭分享，返回标题、状态、时间摘要和会话绑定引用；不返回分享 ID、访问码或底层响应。 |
| `guangya.share.create` | WRITE | 为 guangya.fs.query 最近观察中的 1–100 个 object_ref 创建分享。 |
| `guangya.share.revoke` | WRITE | 按 guangya.share.list 返回的分享引用和 index 冻结撤销计划；确认后仅撤销分享链接，不会删除或移动原始云盘文件。 |

### 2.14 本地媒体整理（13 项）

只处理已配置来源和可核验任务，不开放任意本地路径操作；任务已索引不代表真实播放测试。

定义：[`local_media.py`](../../app/agent/domain_catalog/local_media.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `local_media.diagnose` | READ | 只读汇总本地媒体来源、整理任务与调度器状态，不扫描文件系统、不访问外部服务且不返回路径或业务标识。 |
| `local_media.source_summaries` | READ | 只读列出本地媒体来源的公开序号、触发状态和安全配置摘要，不返回名称、路径、媒体库标识或凭据。 |
| `local_media.get_source_summary` | READ | 只读查看一个公开序号对应的本地媒体来源触发状态与安全摘要，不返回名称、路径、媒体库标识或凭据。 |
| `local_media.set_source_trigger_enabled` | WRITE | 确认后精确启停一个本地媒体来源的 qB 下载完成自动接管；不修改目录、规则、目标或凭据。 |
| `local_media.scan_sources` | WRITE | 预检并确认后扫描全部或指定公开序号的已配置本地媒体来源，把发现的媒体加入整理队列；不接受任意路径。 |
| `local_media.review_queue_summary` | READ | 只读汇总本地媒体待人工确认队列的数量、触发来源和等待时长，不返回标题、路径、任务标识或错误正文。 |
| `local_media.task_summaries` | READ | 只读列出本地媒体任务的 owner 绑定短期公开序号、媒体标题、阶段和可用动作，不返回路径、哈希、数据库 ID 或错误正文。 |
| `local_media.inspect_task` | READ | 只读检查一个短期公开序号对应的待人工确认任务，生成 owner 绑定检查序号；不返回路径、错误正文或内部句柄。 |
| `local_media.preview_task` | READ | 基于 owner 绑定短期检查序号生成本地整理匹配预览；只读且不返回路径、TMDB ID、规则快照或内部检查 ID。 |
| `local_media.retry_task` | WRITE | 预检并确认后仅重试 failed 或 requires_manual 的本地媒体任务；使用版本条件原子重新排队，不直接移动文件。 |
| `local_media.refresh_task_library` | WRITE | 预检并确认后，仅对已完成任务重新解析出的唯一绑定媒体服务器与媒体库执行精准路径刷新；不接受 URL、路径或内部 ID。 |
| `local_media.verify_task_library_visibility` | READ | 只读核验已完成任务的媒体是否已在唯一绑定媒体库中索引，并明确标记未执行真实播放探测。 |
| `local_media.history_summary` | READ | 只读汇总本地媒体已完成与失败历史的数量、触发来源和时间分布，不返回标题、路径、任务标识或错误正文。 |

### 2.15 STRM 同步与失败处理（8 项）

状态、历史、失败诊断、受控重试及指定来源同步；不以排队成功冒充生成完成。

定义：[`strm.py`](../../app/agent/domain_catalog/strm.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `strm.diagnose` | READ | 检查 STRM 索引、缺失文件、失败记录和最近同步状态，不执行修复。 |
| `strm.run_history` | READ | 读取最近 STRM 运行的安全历史、固定统计、失败聚合和队列计数；不返回运行 ID、来源、路径、文件名、对象标识或错误正文。 |
| `strm.triage_failures` | READ | 只读汇总 STRM 失败账本的状态与动作类别，不返回路径、文件名、来源、对象标识或错误正文。 |
| `strm.retry_failures` | DANGER | 预检并在用户确认后重试当前 STRM 失败项，仅返回聚合计数，不暴露失败明细。 |
| `strm.run_once` | DANGER | 预检并在用户确认后同步全部或指定的已配置 STRM 来源；source_names 只能使用设置中名称唯一的来源，不接受目录或来源 ID。 |
| `strm.status` | READ | 查看 STRM 当前运行进度、调度状态、最近结果和可选择的来源显示名称；不返回目录、来源 ID 或错误正文。 |
| `strm.schedule_policy` | READ | 读取 STRM 定时同步的启用状态、五段 cron 和任务通知开关，不返回目录、地址或凭据。 |
| `strm.set_schedule_policy` | WRITE | 预检并在用户确认后修改 STRM 定时同步的三项白名单策略，不立即运行同步。 |

### 2.16 STRM 伴随元数据（3 项）

管理 NFO 等伴随元数据同步及待处理队列，与视频 STRM 生成状态分开解释。

定义：[`strm_metadata.py`](../../app/agent/domain_catalog/strm_metadata.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `strm.metadata.status` | READ | 读取伴随元数据（云盘 NFO、字幕、海报同步）队列数量、开关、消费者线程和熔断状态；running=0只代表瞬时任务数，不代表不会自动工作。 |
| `strm.metadata.set_enabled` | WRITE | 经人工确认调整已有伴随同步开关。 |
| `strm.metadata.cancel_pending` | WRITE | 预览并经人工确认取消当前 queued/retry_wait 伴随元数据积压。 |

### 2.17 播放就绪与媒体反代（5 项）

查看媒体链接相关状态、反代和播放准备情况；重启/修改反代策略属于写入。

定义：[`playback.py`](../../app/agent/domain_catalog/playback.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `media_proxy.status_summary` | READ | 安全汇总媒体反代实例的数量、类型、启用状态与运行状态，不返回地址、端口、路径、实例 ID 或凭据。 |
| `media_proxy.playback_failure_summary` | READ | 按固定时间窗聚合已记录的媒体反代播放请求、失败阶段、路由类别、缓存命中与平均时延；不返回媒体名、用户、会话、URL、路径或错误正文。 |
| `media_proxy.test_instance` | READ | 按公开序号测试一个已保存媒体反代实例的上游连通性，不返回地址、端口、路径、实例 ID、凭据或原始错误。 |
| `media_proxy.set_instance_enabled` | WRITE | 预检并在用户确认后按公开序号启用或停用一个媒体反代实例；不会修改地址、监听、路径或凭据。 |
| `media_proxy.restart_instance` | WRITE | 预检并确认后按公开序号强制重建一个已启用媒体反代实例的运行时；不修改实例配置。 |

## 3. Provider 操作白名单

Provider 网关包含 **24 项媒体操作与 7 项 qB 操作**，是复用现有客户端的受控入口，不是让模型持有服务器密钥直接调用任意 API。可用操作由 [`provider_operations.py`](../../app/agent/provider_operations.py) 声明；会话只得到必要的安全引用。

```text
provider.capabilities                 查看当前支持的操作与条件
provider.query                       执行白名单读取
provider.change.preview              校验并冻结白名单变更
provider.change.execute              请求确认；确认后执行冻结内容
provider.job.status                  查询当前会话冻结写计划的持久状态与已记录执行结果
```

计划 `succeeded` 表示该写计划已完成其声明的执行或受理验证，不一定表示上游异步工作完成；媒体库刷新、下载进度等仍需查询相应业务事实。

部分操作同时由更具体的领域工具暴露，例如最近播放、媒体质量和播放列表。它们复用底层能力和同一确认边界，不是两套 Agent 决策循环。

`provider.capabilities` 中的 profile `online` 表示已启用且必要配置完整，并不执行真实网络连通测试。实际读取成功与否仍以 `provider.query` 或对应业务检查结果为准。

### 3.1 Jellyfin / Emby

使用已配置的媒体服务器及可确定的用户，不接受模型自行注入任意服务地址或管理凭据。用户配置/观看历史不足时，不能保证“排除已看”或最近播放结果完整。

播放列表创建目前仅向 Jellyfin 开放；当前适配器不能可靠绑定 Emby 新播放列表的所有者，因此拒绝创建，但在满足引用和权限校验时可修改已有播放列表。

| 操作 | Kernel 风险 | 用途 |
|---|---|---|
| `media.system.info` | READ | 读取已配置 Jellyfin 或 Emby 的产品、版本和服务器状态。 |
| `media.items.counts` | READ | 读取媒体服务器中的可播放媒体总数，以及电影、剧集和单集数量。 |
| `media.items.recent_added` | READ | 读取媒体服务器最近入库的电影、剧集或单集，按作品去除重复单集展示，并返回可用的已校验 open_url。 |
| `media.items.recent_played` | READ | 读取媒体服务器用户的真实最近播放历史；优先使用明确配置用户，未配置时沿用服务器默认用户选择。 |
| `media.items.recommend_from_library` | READ | 从 Jellyfin 或 Emby 本地媒体库读取 Genres、Tags、评分和用户播放状态，结合最近播放题材信号排序，并可排除已播放或已开始的作品；候选会返回可用的已校验 open_url。 |
| `media.items.continue_watching` | READ | 读取媒体服务器用户尚未看完的继续观看 Resume 列表，并返回可用的已校验 open_url。 |
| `media.libraries.list` | READ | 列出指定媒体服务器中的媒体库，不返回真实目录路径。 |
| `media.library.counts` | READ | 读取先前列出的指定媒体库中的可播放媒体总数，以及电影、剧集和单集数量；library_ref 必须来自 media.libraries.list。 |
| `media.items.search` | READ | 使用媒体服务器原生搜索查询电影、剧集或单集，并返回可用的已校验 open_url。 |
| `media.series.search` | READ | 在媒体服务器中搜索剧集候选，并返回 TMDB 映射、对象引用和可用的已校验 open_url。 |
| `media.series.episodes` | READ | 读取先前选中剧集的本地季集位置，用于集数和缺集核验。 |
| `media.library.refresh` | WRITE | 精准提交先前选中媒体库的后台刷新，不允许退化为全库刷新。 |
| `media.item.refresh` | WRITE | 精准提交先前搜索到的单个媒体条目刷新，不允许全库刷新。 |
| `media.library.quality` | READ | 分页只读检查媒体库视频版本、分辨率、中文字幕和服务器报告缺失状态；未知字段保持 unknown，仅本页查重，不将分页样本当全库结果，不播放或探测 STRM。 |
| `media.user.inspect` | READ | 读取搜索结果中单个媒体条目的已看、收藏和观看进度状态，创建绑定用户的短期状态引用；修改前必须读取。 |
| `media.playlists.list` | READ | 分页列出当前媒体服务器用户可访问的播放列表，并返回短期列表引用。 |
| `media.playlist.inspect` | READ | 读取播放列表及完整成员快照，最多250项；条目分页展示；返回修改用的列表快照引用和移除用的成员引用。 |
| `media.user.mark_played` | WRITE | 人工确认后标记已看单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.mark_unplayed` | WRITE | 人工确认后标记未看单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.favorite` | WRITE | 人工确认后收藏单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.user.unfavorite` | WRITE | 人工确认后取消收藏单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。 |
| `media.playlist.create` | WRITE | 人工确认后为配置用户创建私有 Jellyfin 视频播放列表；可放入最多20个明确视频，不展开剧集。 |
| `media.playlist.add_items` | WRITE | 人工确认后把明确视频加入已有播放列表，跳过已经存在的条目；playlist_ref 来自 media.playlist.inspect。 |
| `media.playlist.remove_items` | WRITE | 人工确认后按成员引用移出播放列表，只改成员关系，绝不删除媒体文件；两个引用均来自 media.playlist.inspect。 |

### 3.2 qBittorrent

实时 qB 任务与 MediaFlux 的历史下载请求不是同一份数据。询问“qB 当前任务”应读取 Provider，不应只用本地活动队列替代。

| 操作 | Kernel 风险 | 用途 |
|---|---|---|
| `qb.app.version` | READ | 读取 qBittorrent 应用和 WebUI API 版本。 |
| `qb.transfer.info` | READ | 读取 qBittorrent 全局传输速度、累计流量和连接状态。 |
| `qb.torrents.info` | READ | 列出 qBittorrent 下载任务的状态、进度、速度和分类。 |
| `qb.torrents.pause` | WRITE | 暂停或停止先前查询选中的一个或多个 qBittorrent 任务。 |
| `qb.torrents.resume` | WRITE | 恢复或开始先前查询选中的一个或多个 qBittorrent 任务。 |
| `qb.torrents.delete_task` | WRITE | 只从 qBittorrent 移除先前查询选中的任务，始终保留已下载文件。 |
| `qb.torrents.files` | READ | 读取先前选中 qB 任务的文件清单，不返回保存目录或任务 hash。 |

下载新资源走 `ingest.inspect` → `ingest.submit` 的资源接入链路，不需要给模型开放任意种子提交 HTTP 接口。`qb.torrents.delete_task` 只删除任务并保留下载文件，没有开放删除下载数据的操作。

### 3.3 光鸭为什么不在上面的 Provider 操作表中？

光鸭通过第 2 节的 `guangya.*` 和统一 `ingest.*` 领域工具复用 SDK/项目客户端，不要求再加一层相同的 `provider.query` 操作。目录浏览、文件计划、账号容量、回收站和分享已各有专门约束。

通用文件变更预览支持的操作枚举：

| 操作 | 含义 |
|---|---|
| `rename` | 对明确对象改名 |
| `move` | 移动到核验后的目标目录 |
| `copy` | 复制明确对象；异步/大目录结果须继续核验 |
| `relocate` | 在同一冻结计划内移动并改名 |
| `batch_relocate` | 按对象引用与季集等参数展开批量规范命名计划 |
| `create_directory` | 新建目录，可与指向该目录的后续操作一起规划 |
| `trash` | 将活动目录中的明确对象移入回收站 |

这些是 `guangya.fs.change.preview` 的参数枚举，不是额外七个免确认工具。`guangya.fs.change.execute` 只能执行已冻结计划，不能临时追加对象或改写路径。

## 4. 容易混淆的能力边界

| 需求 | 正确入口或边界 |
|---|---|
| RSS 订阅源列表 | `rss.subscription_summaries`；不是媒体追更列表 |
| 媒体追更列表 | `media.subscription_summaries`；不是 RSS 规则 |
| 全库总数与单库总数 | `media.items.counts` 与 `media.library.counts`；先确定库，再统计 |
| 查询一批影视是否在库 | `library.batch_presence`，不要循环几十次单片搜索 |
| 最近播放与继续观看 | `media.recently_played` 与 `media.continue_watching`；含义不同 |
| 本地推荐与公开推荐 | `media.recommend_from_library` 与 `discovery.recommend`；外部推荐不代表已经在库 |
| 演员与作品详情 | `discovery.credits` 与 `discovery.detail`；缺少演职员数据时再核实其他来源 |
| 三平台更新与 Bangumi 放送 | `discovery.anime_calendar` 与 `bangumi.calendar`；不能混用日期和来源完整性 |
| 网页搜索与打开正文 | `web.search` 使用 Tavily Search，`web.read` 使用 Tavily Extract；二者均需 `WEB_SEARCH_ENABLED` 与 `TAVILY_API_KEY`，不是任意 HTTP/浏览器工具 |
| 列云盘目录与刮削/清理 | `guangya.fs.query`；普通浏览不需要启动刮削或垃圾清理计划 |
| 停止回合与停止任务 | 停止模型不撤销已经发生的业务写入，需查询并使用对应任务控制 |
| 关闭元数据同步与取消积压 | `strm.metadata.set_enabled` 与 `strm.metadata.cancel_pending` 是不同操作 |
| 清空回收站 | `guangya.recycle.clear` 为全账号永久清空，不是当前页/选中项删除 |
| 本地上传到光鸭 | 不向 Agent 开放；不能从 SDK 存在上传方法推断 Agent 有权限 |
| 任意配置、Shell、内网 Fetch | 不开放；只能使用声明的配置字段、来源与操作 |
| 媒体详情链接 | 条件满足时返回已校验的打开链接，不保证唤起 App 或提供永久直链 |
| 识别主动复核 | 单独授权、有限案例工具及结构化审计，不放开聊天 Agent 的全部写权限 |

## 5. 维护这份清单

1. 以 [`domain_catalog/catalog.py`](../../app/agent/domain_catalog/catalog.py) 的 `build_tool_specs()` 及各 `register_specs()` 实际注册结果为准。循环注册的工具也应计入，不能只搜索字面量 `name=`。
2. 按 [`kernel/ports/existing_actions.py`](../../app/agent/kernel/ports/existing_actions.py) 核对领域风险到 Kernel 风险的映射。不要因 `LOW_WRITE` 字样取消确认。
3. 单独核对 Provider 操作目录；不得把内部操作、参数枚举与顶层工具重复相加。
4. 功能新增/退役时同步更新名称、用途、配置条件和本文核对日期；运行时能力窗口和 Schema 始终优先。
5. 注册表/文档校验只证明声明一致，不能替代真实 Provider 冒烟、确认安全测试、跨轮引用和两端事件验收。

返回：[Agent 架构与使用实战](08_Agent架构与使用实战.md) · [自动化流转全景](00_自动化流转全景与工作流程.md) · [开发文档](../开发文档.md)。
