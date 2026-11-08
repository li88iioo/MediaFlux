# Media Agent：工具与能力参考

> 核对日期：2026-11-06。本文说明工作区 SDK 与会话控制方法；配置、权限和部署条件会影响实际可用能力。`mf --help` 查看分组，`mf <模块> --help` 查看方法，完整签名以 `mf <模块.方法> --help` 为准。

架构与操作流程见 [Agent 架构与使用实战](08_Agent架构与使用实战.md)。用户不必背工具名，直接描述目标；工具名主要供能力查询、排错和维护使用。

## 1. 如何阅读清单

- 普通生产模型只有 `agent.run_code`；业务与会话控制通过 Python/Shell 工作区中的 `mf` SDK 使用，Skills 按需读取。
- 下表业务方法是工作区接口；完整签名以 `mf --help` 和 SDK methods 为准，不把内部执行目录作为模型工具集。
- `LOW_WRITE` 进入确认执行器后仍按 WRITE 处理；已授予的范围授权按原范围、期限和次数生效。
- `READ` 指不执行目标业务写入；仍可能发起外部请求、消耗搜索额度、写入会话引用或刷新缓存。它不等于完全离线、零成本。
- `WRITE` / `DANGER` 表示普通聊天调用时先准备 EffectPlan，用户确认后才执行。带 `preview` 的只读工具只能冻结计划，不能替代执行确认。
- 各领域（包括光鸭）的可调用方法通过 `mf --help`、`mf <模块> --help` 和 SDK 方法文档发现；读取方法文档不会取得写入授权。
- 参数、选择范围和当前条件以实际 SDK 签名、方法文档和确认卡为准。使用业务响应中的真实 ID 与版本；结果归档、卡片选择和补偿凭据各有专用语义，不是通用业务句柄，也不能绕过 owner 或快照校验。
- 内部主动识别复核只使用专门的小型工具集，不等于后台自动取得下方全部权限。

## 2. 按领域列出的工作区 SDK

### 2.1 工作区与综合查询

在原生 Python/Shell 工作区用 `from mf import diagnostics` 或 `mf diagnostics --help` 查询。公共业务实现：[`workspace.py`](../../app/modules/workspace.py) 与 [`workspace_search.py`](../../app/modules/workspace_search.py)。这些 SDK 读方法不注册为模型窄工具。

`session.result(handle, path="")`（原生 SDK READ）读取当前 owner/会话已保存结果的完整快照；`data` 保留原节点类型与全文，`path` 可选 JSON Pointer。沿用原引用有效期，失效或路径错误可捕获，不重新查询业务服务；SDK 响应保存在既有工作区输出文件中。

会话方法定义于 [`sdk.Session`](../../app/sdk.py)，状态控制沿 [`kernel/session_service.py`](../../app/agent/kernel/session_service.py)。普通查询和计划笔记无需创建持久任务；只有跨回合恢复或严格验收场景才使用以下任务方法。

| SDK 方法 | 边界 |
|---|---|
| `session.task()` | 只读当前验收任务及真实证据；没有任务返回 `empty`。 |
| `session.start_task(root_goal, constraints, obligations, *, source)` | `source` 必须是已接纳用户完整原文；义务包含 `id/description/required=true/acceptance`，不预猜工具名、不授业务写权。 |
| `session.revise_task(root_goal, constraints, obligations, *, source)` | 按当前用户原文修订；保留任务 ID、推进 epoch、使旧证据失效，累计预算不重置。 |
| `session.checkpoint_task(evidence_ids, blockers, *, read_evidence=None)` | 只关联本任务、本 epoch 已登记的真实成功 READ 证据；`read_evidence` 为义务 ID 到证据 ID 的映射，不能用笔记或 `result_handle` 代替。 |
| `session.cancel_task(source)` | 取消持久验收跟踪；不取消确认卡、不停止后台业务、不撤销已执行操作。 |
| `session.pending_plan(operation="inspect")` | `inspect/cancel` 查看或取消当前未执行确认卡；不接受模型指定 owner、plan_id 或确认指令。 |
| `session.batch(steps)` | 将已声明可组合的写方法 `method/arguments` 冻结为一张确认卡，沿原逐项额度、快照和确认执行器；失败或 unknown 停止后项，不回滚或重放。 |

`session.batch` 是业务确认写入口；任务方法仅更新会话验收状态。普通生产模型不再获得 `agent.task`。默认模型轮次与工具总次数均无额外次数上限，仍受 `SessionLimits` 的累计估算 Token、活跃时间、写调用限制及业务限流约束；具体预算见 [架构与使用实战](08_Agent架构与使用实战.md#32-一轮请求如何运行)。

| SDK 方法 | 用途 |
|---|---|
| `diagnostics.overview(probe=False)` | 汇总本地待办、固定行动卡、索引器声明和完整配置事实；`probe=True` 复用媒体节点连接服务。保留 partial、来源失败和未知计数；空待办不证明外部健康。Web 下一步也读取同一公共服务。 |
| `diagnostics.search(query, sections=None, limit=20, offset=0)` | 按标题检索媒体库、RSS、下载、整理与本地媒体任务。返回真实 ID 与来源分页；仅 `library` 访问媒体节点，其他来源仅读 SQLite。按各来源的 `has_more/next_offset` 继续，不把同名记录视为同一任务链。 |

### 2.2 系统、配置诊断与能力发现

当前运行条件、模型路由、功能与索引站开关、配置诊断。

定义：[`sdk.py`](../../app/sdk.py)、[`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `diagnostics.runtime()`（SDK） | READ | 只读返回 Media Agent 总开关、Telegram 接入和模型路由的当前启用状态，不返回令牌、密钥或供应商配置值。 |
| `diagnostics.configuration()`（SDK） | READ | 完整读取媒体服务器、TMDB、下载器、STRM 与 AI 回退的配置事实、字段来源和全部问题；不返回配置键、值或凭据，不探测连通性。 |
| `diagnostics.component(component)`（SDK） | READ | 读取一个白名单组件的完整状态、字段来源、缺项、校验错误、能力影响和安全下一步；功能开关共用公共功能配置服务。 |
| `settings.features()`（SDK） | READ | 只读汇总媒体探索、资源检索与联网搜索的启用状态和依赖可用性，不返回配置值或供应商凭据。 |
| `diagnostics.pipeline(limit=20, offset=0)`（SDK） | READ | 读取完整本地自动化汇总和 RSS 公共诊断，保留来源、错误及真实订阅 ID；RSS 关注项按 `limit`（1–100）与 `offset` 穷尽分页，部分来源失败仍保留其余事实。不访问外网或启动任务，不返回路径、配置值或凭据。 |
| `recognition.rules(rule_type)`（SDK） | READ | 完整只读列出 `preprocess_rule/tmdb_regex_rule/release_format` 的真实 ID、启停状态和内容；格式包含 revision，类型间 ID 可重号，不初始化种子或使用临时编号。 |
| `recognition.set_rule_enabled(rule_type, rule_id, enabled)`（SDK） | LOW_WRITE | 确认后单条启停三类规则，事务内复核业务行；格式仍走原 change 的 revision/注册表复验及启用回归检查。知识不在此接口，统一使用 `recognition.update_knowledge(entry_id, disabled=not enabled)`。不会改变内容、TMDB 或优先级，未知结果先回读，不重放旧卡或原脚本。 |
| `resources.sources()`（SDK） | READ | 读取当前固定白名单资源站点的选择，仅返回站点 ID、展示名和数量。 |
| `resources.configure_sites(site_ids, enable_search=None)`（SDK） | WRITE | 预检并在用户确认后更新固定白名单资源站点，不接受配置键、URL、凭据、Cookie 或路径。 |
| `settings.test_notification` | WRITE | 预检并在用户确认后向当前已配置会话发送一条固定 Telegram 连接测试消息；不接受消息、凭据或会话参数。 |
| `settings.policies` | READ | 读取 Agent 可安全管理的固定白名单策略，只返回公开值和环境托管状态。 |
| `settings.set_policy` | WRITE | 预检并在用户确认后修改一项固定白名单非敏感策略，不接受任意配置键、凭据、URL 或路径。 |
| `settings.set_feature(feature, enabled)`（SDK） | WRITE | 预检并在用户确认后开启或关闭一个非敏感白名单功能，不接受配置键或任意配置值。 |
| `library.audit_job(job_id=None, limit=5)`（SDK） | READ | 查询当前用户真实 job_id 或最近任务窗口，保留聚合计数、未知总数与覆盖完整度；持久缺集样本最多20项、每项20集，不能冒充全量结果。 |
| `library.cancel_audit(job_id=None)`（SDK） | WRITE | 确认后取消指定后台全库检查；省略ID在预检时冻结最近活动任务，运行任务在批次边界停止并保留已保存结果。 |
| `activity.actions(limit=50, outcome="all", before_id=None)`（SDK） | READ | 查看最近经确认执行的 Agent 动作审计，仅返回脱敏状态、聚合计数与耗时。 |
| `diagnostics.query_db`（SDK） | READ | 仅 Web 登录身份：在运维表上执行一条只读 SELECT；SQLite 授权回调限定白名单表与非凭据列，不可读 Agent 私有会话表，不带 sql 时返回可读表与列，结果中的凭据片段打码。 |
| `diagnostics.search_logs`（SDK） | READ | 仅 Web 登录身份：按关键词、最低级别和日期检索最近 14 天运行日志，返回最近的完整匹配记录及扫描范围，最多读取文件末尾32MiB；凭据片段打码。 |

### 2.3 活动、跟踪与撤销

按媒体或任务查活动时间线；回退必须先检查支持范围，不能承诺任意操作可撤销。

稳定接口：[`sdk.py`](../../app/sdk.py)；补偿业务复用 [`activity_undo.py`](../../app/modules/activity_undo.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `activity.search` | READ | 按标题分页查找下载、光鸭整理和本地整理活动，返回真实 kind/id、总数和 next_offset。 |
| `activity.timeline` | READ | 使用真实 kind/id 读取下载→整理→STRM→媒体库复核的持久化阶段及故障原因，只按真实请求/运行标识关联。 |
| `activity.inspect_undo(log_id)` | READ | 核对真实整理日志的最近可逆移动/改名快照，宿主签发当前 owner 的补偿凭据。 |
| `activity.undo(receipt_id)` | DANGER | 使用 data.receipt_id 原样生成冻结恢复计划；确认后调用公共补偿业务，拒绝覆盖后续修改，凭据只消费一次。明确未受理可重新预检；写后结果未知不可重投。 |
| `activity.follow(kind, identifier, hours=24)` | SDK确认写 | 按activity.search/timeline的持久kind/id持续跟踪任务，异常、已有阶段结束或到期时发送一次 Telegram 通知；只观察、不替任务重试。 |
| `activity.follows()` | SDK只读 | 查看我持久保存的任务跟踪规则及启用状态和到期时间。 |
| `activity.unfollow(rule_id)` | SDK确认写 | 停用我的活动跟踪通知，不停止或删除下载/整理任务。 |

### 2.4 媒体库、缺集与批量核对

库存搜索、批量在库匹配、季集计数、缺集资源、巡检与复核工作流。

定义：[`sdk.py`](../../app/sdk.py)、公共 [`media_library.py`](../../app/modules/media_library.py) 与 [`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `library.search(...)`（工作区 SDK） | READ | 共用服务按标题或演员元数据搜索；保留真实对象 ID、准确总数、分页与来源范围。`count_only=True` 只统计；旧工具入口已退役。 |
| `resources.search_missing`（SDK） | READ | items统一单集、单季和最多12部/季的已播缺集资源检索；保留真实result_ids、完整verification_targets和来源分页/partial/未完成目标，不自动下载。 |
| `resources.missing_workflows`（SDK） | READ | 当前用户的持久补库流程、全部成员、真实workflow_id/item_id/revision/request_id与状态；total/has_more/next_offset用于分页。 |
| `library.connection`（SDK） | READ | 当前配置单槽位或全部媒体服务器的连接与身份诊断；server=all/jellyfin/emby，保留产品、版本、兼容性、disabled/unknown与安全错误；固定目标拒绝凭据重定向，不返回地址或凭据，失败不当空结果。 |
| `library.libraries(server="auto")`（SDK） | READ | 返回真实库 ID、名称和类型；auto 遵循已保存的首选服务器，多源未选定时需明确服务器。 |
| `library.recent`（SDK） | READ | 按真实库 ID 或精确库名读取最近入库；支持电影、剧集或单集，最多200项，最多扫描2000条；不代表全库库存。 |
| `library.quality`（SDK） | READ | 按真实库 ID 分页检查版本、分辨率、字幕及服务器缺失状态；未知保持未知，重复检查仅限本页，不播放或探测 STRM。 |
| `library.refresh(server, library_id, mode="scan", replace_images=False, regenerate_trickplay=False)`（SDK） | LOW_WRITE | 明确指令直接执行，不弹确认卡；scan增量扫描、missing补齐缺失、replace覆盖元数据，后两种模式可独立替换图片／重建Jellyfin预览图。复用一次执行与持久回执；accepted仅表示排队，结果未知不重发。自动入库仍使用增量。 |
| `media.refresh(item_id, server="auto", ...)`（SDK） | LOW_WRITE | 与媒体库刷新同参数，目标为真实电影、整部剧、季或单集ID；不扩展为全库。默认不覆盖元数据、图片或预览图。 |
| `library.presence(items)`（SDK） | READ | 一次按 TMDB ID 批量核对最多 50 部电影或剧集是否存在于已配置的 Jellyfin / Emby。 |
| `library.updates`（SDK） | READ | 单部或批量核对媒体；默认刷新 Jellyfin / Emby 库存，对照 TMDB 已播季集。逐部保留缺集、无已播缺集、歧义与不可用状态；不代表全网资源发布进度。 |
| `library.audit_all(as_of=None, max_series=50, after_tmdb_id="")`（SDK） | READ | 分批检查全库剧集与已播缺集；按返回游标续查，保留来源失败与未知映射。 |
| `library.start_audit(as_of=None, max_series=50)`（SDK） | WRITE | 确认后创建可恢复的后台全库剧集检查并返回真实 job_id；max_series为每批上限，活动同范围任务复用，accepted仅表示已入队。 |
| `library.patrol_status(run_id=None)`（SDK） | READ | 查询最近或指定 run_id 的后台巡检状态及安全缺集摘要，不触发巡检或下载。 |
| `library.patrol_policy`（工作区 SDK） | READ | 读取后台全库缺集巡检的启用、通知、间隔和单轮检查上限，不返回其他配置。 |
| `library.set_patrol_policy`（工作区 SDK） | WRITE | 预检并在用户确认后修改全库缺集巡检的四项白名单策略，不接受配置键、凭据、URL 或路径。 |
| `library.run_patrol()`（SDK） | WRITE | 确认后按当前策略立即排队；已运行时复用 run_id。accepted 表示已受理，可按 run_id 继续查询；不修改策略、不搜索或下载。 |
| `library.episodes(query, tmdb_id="", library_name="")`（SDK） | READ | 直接读取已配置 Jellyfin / Emby 中指定剧集的本地普通集数量与季度分布；不访问 TMDB，也不判断缺集。 |
| `library.audit`（SDK） | READ | 核对媒体库剧集与 TMDB 截止日期前已播普通集，报告缺集或可更新集。 |

### 2.5 媒体质量、观看状态与播放列表

读取服务器报告的质量和用户状态，受控修改已看、收藏和播放列表；不进行真实播放探测。

定义：[`modules/media_user.py`](../../app/modules/media_user.py)，原生SDK见[`app/sdk.py`](../../app/sdk.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `library.user_state`（SDK） | READ | 按真实item_id读取配置/默认用户的已看、收藏、计数和进度；缺失字段保持None，返回用户scope。 |
| `library.playlists`（SDK） | READ | 当前用户可访问列表分页；真实id及total/has_more/next_offset，公开和归属未知保持None。 |
| `library.playlist`（SDK） | READ | 真实playlist_id与完整成员item_id/entry_id，默认最多250项；next_offset续页，完整快照才允许修改。 |
| `library.set_played`（SDK） | WRITE | 按真实item_id与布尔played设置当前用户已看状态；冻结状态、用户和配置，确认后回读，不展开剧集。 |
| `library.set_favorite`（SDK） | WRITE | 按真实item_id与布尔favorite设置收藏；普通播放变化不使收藏确认失效。 |
| `library.create_playlist`（SDK） | WRITE | 为明确配置用户创建Jellyfin私有Video列表；1–20个真实视频ID，同名拒绝，Emby无法验证归属时不支持。 |
| `library.add_to_playlist`（SDK） | WRITE | 1–20个真实视频ID加入列表，跳过已有媒体；完整成员CAS校验，最多250成员，写后返回完整成员。 |
| `library.remove_from_playlist`（SDK） | WRITE | 按真实entry_ids移出列表成员；不接受媒体ID代替，不删除媒体文件；完整成员CAS及写后回读。 |

### 2.6 探索、元数据、推荐与网页

元数据详情与演职员、电影人作品列表、本地与外部推荐、网页搜索/正文及两类动漫日历。

元数据、评分、周历及映射接口统一见 [`sdk.py`](../../app/sdk.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `web.search` | READ | Tavily 公开网页搜索；支持包含/排除域名及相对或绝对日期范围，返回实际筛选条件和条数上限。日期可能是发布或更新时间，具体事实需读原文。 |
| `web.read` | READ | 读取公开 HTTPS 正文，标记正文截断，并保留本次响应正文中的完整可读链接供继续查证。不执行 JavaScript，不支持登录验证或 PDF。 |
| `media.search`（SDK） | READ | 搜索 TMDB、豆瓣与 Bangumi 的完整当前页；类型/年份精确过滤，地区/题材仅作关键词。保留来源错误与实际分页，不含演职员表。 |
| `media.people`（SDK） | READ | 按姓名读取完整 TMDB 人物候选页及真实 ID/已知作品，保留 total_pages/next_page；不按热度自动挑选，同名歧义须核对。 |
| `media.filmography`（SDK） | READ | 按明确 TMDB 人物 ID 读取完整电影作品、年份与角色；可筛选职务/已上映作品，`library_check_items` 可直接用于 `library.presence`。 |
| `media.rating(query, media_type, year)`（SDK） | READ | 按明确影视名称、类型和年份查询豆瓣评分；优先使用豆瓣结构化数据，必要时受控检索并读取已验证的豆瓣条目页。 |
| `media.detail`（SDK） | READ | 读取真实作品 ID 的完整概况、映射确认状态与默认季集；未知计数保留 null，未查询演员不表示源站没有演员。 |
| `media.credits`（SDK） | READ | 按已确认 TMDB 作品 ID 读取电影、全剧或指定季（含第 0 季）完整演职员及 roles/jobs；默认不截表，显式 limit 可配合 offset 续读。 |
| `media.resolve(provider, external_id, media_type, title, year="")`（SDK） | READ | 只读返回已确认映射或完整 TMDB 候选，包含真实 tmdb_id/media_type；不自动保存。 |
| `media.confirm_mapping(provider, external_id, media_type, tmdb_id)`（SDK） | WRITE | 按真实来源与目标 ID 预览，用户确认后保存映射。来源、目标或旧映射变化时需重新预检；不收藏、订阅或下载。 |
| `watchlist.list/get/statistics`（SDK） | READ | 读取本地收藏列表、真实 id 对应作品及完整统计。列表按 `next` 续页，`total` 为全库总数；外部探索关闭时仍可读取已保存收藏。 |
| `watchlist.add` | WRITE | 工作区通过 `mf.watchlist.add(provider, media_type, external_id)` 预检并确认添加精确影视条目；支持原有范围授权和批量确认，不会下载资源。 |
| `watchlist.remove` | WRITE | 工作区通过 `mf.watchlist.remove(watchlist_number)` 预检并确认移除精确收藏；不会删除媒体文件或下载任务。 |
| `library.recommend`（SDK） | READ | 从已配置的 Jellyfin / Emby 本地媒体库分页读取最多5000个候选，结合50条真实历史确定性推荐；仅最终作品列表按limit截取。省略参数采用当前身份已存偏好，显式空数组/0/False优先。库存未读全或总数未知保留partial/None，计数仅代表已扫描范围；返回真实ID及完整元数据。 |
| `media.catalog`（SDK） | READ | 默认读取一页榜单。显式 `limit` 收集 TMDB/discover 或豆瓣/recommend，支持年份范围、产地和题材；`exclude_library=true` 只保留核验不在库者。未知不是缺失；按 `scope.next_page/next_offset` 续取，达到请求数后不因 `has_more` 继续。 |
| `media.calendar`（SDK） | READ | 返回上海当前周完整七天的腾讯视频、爱奇艺和优酷排期，保留会员/免费事件、未知时间和来源状态；脚本可按日期、平台、片名筛选，不推断本周外排期。 |
| `media.catalog("bangumi", "calendar", "tv")`（SDK） | READ | 读取 Bangumi 周历的一页，`filters={"weekday":"1"}` 可选星期；不表示国内三平台排期，按 `has_more` 续页。 |

### 2.7 资源索引与统一接入

资源搜索返回真实 `result_ids`；工作区用 `resources.present` 将当前搜索批的候选投递到现有 Web/TG 卡片，下载另行预检和确认。直接链接与分享复用受控接入链路。

定义：[`sdk.py`](../../app/sdk.py)、[`kernel/ports/resource_presentation.py`](../../app/agent/kernel/ports/resource_presentation.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `resources.sources()`（SDK） | READ | 只读检查多站资源索引器的本地开关、启用站点与能力声明；不访问资源站、网络或文件系统，也不返回 URL、Cookie 或凭据。 |
| `resources.search(...)`（SDK） | READ | 使用共用索引服务检索资源；完整结果可在工作区过滤、保存，返回实际参与站点、部分失败与截取范围。 |
| `resources.present(result_ids, ...)`（SDK） | READ | 只展示当前会话最近一次实际搜索批中的 0–50 个真实 ID，沿用查询规格和缺集复核目标；空列表明确撤卡。复用 Web/TG 批选与回复绑定，不下载、不新建确认。 |
| `cloud.inspect_share(url)`（SDK） | READ | 完整读取分享顶层文件列表，返回真实文件 ID、数量与覆盖范围；分页无法完成时明确失败，不截取前 200 项冒充完整。 |
| `cloud.transfer_share(url, file_ids)`（SDK） | DANGER | 明确选择真实文件 ID，冻结所选文件及配置目标；冷确认重新读取并核对后进入既有持久转存任务。结果未知时不得重新转存。 |
| `resources.download(result_ids, target="preferred", verification_targets=None)`（SDK） | WRITE | 按真实资源 ID 批量预检和确认，沿用 owner 偏好或显式目标；缺集复核目标经服务端审计后随原请求持久化。 |
| `downloads.submit(url, target="preferred")`（SDK） | DANGER | 只读预检 Magnet、ED2K 或明确 HTTP(S) 下载直链，冻结实际目标后由唯一 EffectExecutor 确认提交。活跃请求不隐式补投；request_id 可交给 activity.timeline("download", request_id) 跟踪。 |
| `activity.timeline("download", request_id)`（SDK） | READ | 按原请求编号续查下载、转存、整理与 STRM 状态；受理或转存完成不等于全部后处理成功。 |

原生资源提交使用实际 `result_ids`；候选卡仍保留签名批次与展示序号，提交前转换为同批真实 ID，集号或质量排名不能替代 ID。待核对资源不会自动推荐，但用户明确选择后可以生成预检卡；候选仍需确认才能提交。单次候选卡和确认的业务容量为 50 项，完整搜索观察不因展示容量而截断；推荐摘要和模型上下文预算不裁剪快照、确认范围或批量结果。默认搜索返回 20 项时，可在一次预检和人工确认中提交全部 20 项，无需为尾项再次发指令。

### 2.8 下载诊断与提交续查

查看下载队列及最近提交，重试前检查结果与幂等边界；实时 qB 任务和传输信息通过 SDK downloads.tasks/transfer 读取。

定义：[`sdk.py`](../../app/sdk.py)，诊断复用 [`modules/download_diagnosis.py`](../../app/modules/download_diagnosis.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `downloads.diagnose()`（SDK） | READ | 只读诊断 qBittorrent 当前队列、传输状态与疑似停滞任务，保留原诊断字段并返回关注任务的真实 hash；不公开凭据、路径或下载链接。queue_state 和 unavailable_sources 区分已读空队列与读取失败；partial 保留成功读取的事实，快照不证明持续停滞或入库完成。 |
| `downloads.requests(scope="active", limit=12, offset=0)` | READ | 分页读取 MF 持久请求；`request_number` 是可传给 retry 的真实请求 ID，按 next_offset 续读；不代表 qB 或光鸭全账户实时队列。 |
| `downloads.retry(request_id, target)` | DANGER | 真实请求 ID 来自 `downloads.requests`；target 为 qb/guangya/both。只补可重试目标，未知受理不重投；确认后沿原请求和配置快照提交，回执实际 request_id 可供 activity.timeline 核验。 |

### 2.9 RSS、媒体追更与个人偏好

RSS 规则订阅和影视追更订阅是不同对象；本组也维护观看/下载偏好、最近播放与内容汇总。

定义：媒体追更与 RSS 均使用 [`sdk.py`](../../app/sdk.py) 中的原生方法；公共业务分别位于 [`media_subscriptions.py`](../../app/modules/media_subscriptions.py) 与 [`rss.py`](../../app/modules/rss.py)，写入共用 [`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `rss.diagnose` | READ | 原生 SDK 本地诊断调度、积压、失败和不一致状态；`attention.next` 可续页，不访问订阅源。 |
| `rss.subscriptions` | READ | 原生 SDK 读取完整 RSS 配置及条目计数；`limit/after_id` 与 `next` 真实分页，URL 凭据脱敏。 |
| `rss.subscription` | READ | 按真实 `subscription_id` 读取完整配置、调度及条目聚合；保留长字段，URL 凭据脱敏。 |
| `rss.create` | WRITE | 原生 SDK 创建 RSS 配置；沿 Web 规则校验地址、过滤、调度、下载目标/路径及媒体绑定，工作区先确认，不立即刷新或下载。 |
| `rss.update` | WRITE | 仅更新明确提供的配置字段；保留 False、0、空字符串语义，确认时原子复核配置快照，无关计数不使确认失效。 |
| `rss.delete` | DANGER | 确认后删除指定订阅及其本地条目；运行中刷新/提交拒绝删除，不删除下载任务或媒体文件。 |
| `subscriptions.list(status="", enabled=None, limit=200, before_updated_at=None, before_id=None)` | READ | 完整持久订阅、真实 id、revision、last_error、缺失与工作流；按 next 续读，不再裁成16条摘要。持久状态不等于实时库存。 |
| `subscriptions.check_updates(subscription_id=None, limit=8, before_updated_at=None, before_id=None)` | READ | 分页实时比较 TMDB 已播与 Jellyfin/Emby 库存，保留全部缺失差异与不完整原因；每项资源搜索仍限首个缺集及有界候选，按 next 读完。不提交下载，不创建后台检查。 |
| `subscriptions.get(subscription_id)` | READ | 精确真实 ID 读取完整配置/策略、修订号、已保存错误与缺失；不存在直接报错。 |
| `subscriptions.update(subscription_id, **显式变更)` | LOW_WRITE / DANGER | 启停与策略合并；未传字段保留，单独恢复启用保留在途工作；暂停/策略变化使未提交工作失效。完整配置/修订/准入作用域CAS，已进入提交阶段无法撤回，不立即检查下载。 |
| `subscriptions.create(provider, external_id, media_type, season=None, check_interval_minutes=4320, action="confirm", download_target="guangya", sites=None, enabled=True)` | LOW_WRITE / WRITE | 唯一媒体追更创建/恢复入口；精确来源与已确认映射，支持单季、站点、策略、目标及启停。默认3天；原自动规则的每周语义显式传10080。auto确认后持续自动下载，仅沿已有整理/STRM联动，不支持固定星期或独立4K/字幕硬过滤。 |
| `subscriptions.delete(subscription_id)` | DANGER | 确认后软删除并停止后续检查，失效未提交工作；保留下载、媒体文件和历史。 |
| `library.history`（SDK） | READ | 读取媒体服务器用户的真实最近播放历史（播放事件/DatePlayed），limit为1–50；优先配置用户，否则沿用默认用户。auto使用当前身份已存首选服务器；保留真实ID、完整条目及用户范围，count仅为返回数，未知total/has_more/complete保持None；状态按本次窗口为success（有项）或empty（真实空）。 |
| `library.resume`（SDK） | READ | 读取当前配置或默认用户的继续观看列表，limit为1–20，auto遵循首选服务器。完整条目保留真实ID及用户范围，可用时返回open_url；状态按本次窗口为success/empty，未知total/has_more/complete保持None，不能用返回数推算总量。 |
| `preferences.get` | READ | 工作区读取当前身份显式保存的媒体服务器、下载目标、清晰度/HDR/编码/字幕/音轨/发布组/排除词/单集大小/题材与观看偏好及长期备忘；单次请求条件优先，不从聊天摘要推断。 |
| `preferences.update` | WRITE | 工作区按命名字段预检并确认更新（如 `preferences.update(preferred_resolution="2160p")`），确认卡支持编辑；更新结构化媒体偏好与长期备忘 `memory_notes`（最多 20 条、每条 200 字，按完整列表替换）；保存后每轮对话都会作为用户数据带入上下文；本地推荐和资源排序使用这些默认偏好，单次显式参数（包括空数组、0、False）优先。 |
| `preferences.clear` | WRITE | 工作区预检并确认清除当前用户保存的显式媒体偏好，恢复产品默认值。 |
| `subscriptions.today()` | READ | 管理员全局今日追更、整理、RSS、下载事件，全量计数与明确本地日期/as_of；最多8个安全标题样本，不把失败当空记录。 |
| `subscriptions.notification(subscription_id)` | READ | 原全局媒体订阅通知规则；完整返回规则/订阅版本、explicit/default和开关，不按当前聊天另建规则。 |
| `subscriptions.set_notification(subscription_id, **显式通知开关)` | LOW_WRITE | 未传开关保留，以完整原规则与订阅版本做事务CAS；模型不能指定owner/chat/route，沿原通知中心授权。 |
| `subscriptions.reset_notification(subscription_id)` | LOW_WRITE | 确认后删除显式规则，恢复默认并返回explicit=false/revision=0；已默认时不生成新写计划，已入队消息仍沿原授权处理。 |
| `rss.activity` | READ | 最近24小时成功下载总数与逐订阅计数；全局总数不受分页影响，可用 `next` 续页。 |
| `rss.entries` | READ | 真实条目 ID、完整标题、状态与持久 delivery；offset/next 真实分页。 |
| `rss.mark` | WRITE | 确认后原子标记选定 entry_ids；不覆盖正在提交或已下载条目。 |
| `rss.refresh` | WRITE | 刷新 subscription_ids 或 scope=all_configured，不自动下载；run_id 可用 rss.activity(run_id=...) 查询。 |
| `rss.download` | DANGER | 指定 pending entry_ids，或省略 ID 选择最近 limit 项（默认10，可按 subscription_id 过滤）；确认冻结完整集合及 qB/光鸭目标，has_more 明示剩余。 |
| `rss.retry` | DANGER | 范围参数同 download，仅重试原冷却、次数和失败分类允许的条目；未知投递结果不重放。 |

### 2.10 媒体自动化规则与摘要

媒体追更创建统一使用 `subscriptions.create`；`automation` 仅查询或配置定时摘要，配置变更仍需要确认。

定义：[`sdk.Automation`](../../app/sdk.py)，写入经原生 EffectExecutor 确认；业务由公共 `media_subscriptions` / `media_automation_rules` 服务编排。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `automation.digests()`（SDK） | READ | 完整读取当前身份全部每日媒体动态/异常摘要规则，保留真实 rule_id、revision 和 settings_error，默认没有开启。 |
| `automation.set_digest(...)`（SDK） | WRITE | 确认后新增或修改每日摘要规则；新增需 hour，修改用真实 rule_id，未传设置保留。按本机时区定时汇总，停用不依赖当前目标；投递 owner/context 由宿主绑定，不能传 chat。复用原表、定时和通知队列。 |

### 2.11 文件名识别、发布格式教学与识别配置

文件名解析与发布格式教学通过原生 `mf.recognition` 使用公共识别服务，完整数据可在工作区保存并继续核对。识别知识、来源和媒体路径映射沿用现有配置工具。

定义：[`sdk.py`](../../app/sdk.py)、[`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `recognition.inspect_filenames(filenames, parent_path="")`（SDK） | READ | 用内置解析器完整返回 1–100 个真实文件名的标题候选、原始发布季集、目录上下文与清洗证据；未知保留空值，不读取文件或查询 TMDB，不保存规则。 |
| `recognition.preview_release_format(draft, examples, filenames=None)`（SDK） | READ | 仅用户明确要求教学时，以用户标注的 2–8 个不同文件、至少两个原始集号核对模板，完整返回全部样本与最多 100 个批量结果；只读预览不会创建保存确认。模板由助手生成，缺少样本需补齐，不编造标签。 |
| `recognition.save_release_format(draft, examples, filenames=None)`（SDK） | WRITE | 仅用户明确要求记住格式时，经原 EffectExecutor 冻结 Web 预览和配置快照、确认后一次保存；取消或快照失效不写入。默认仅此目录，跨目录需授权及不同标题样本；不移动文件、不绑定 TMDB、不偏移季集，不重新启用已停用的重复规则。 |
| `recognition.knowledge(keyword="", knowledge_type="", limit=300, offset=0)`（SDK） | READ | 完整返回公共知识库事实，`items[].id` 为真实 `entry_id`；`total` 为筛选后总数，`summary` 为全库统计。最多 500 条，按 `has_more/next_offset` 穷尽分页，不截成 50 条。 |
| `recognition.create_knowledge(knowledge_type, canonical_value, aliases=..., disabled=False)`（SDK） | WRITE | 确认后新增用户词条，复用公共 CRUD 和缓存；不允许指定来源、证据、知识键或置信度。 |
| `recognition.update_knowledge(entry_id, knowledge_type=..., canonical_value=..., aliases=..., disabled=...)`（SDK） | WRITE | 真实 ID 定位，事务内比较确认的业务行；统计命中不使确认失效，未传字段保留。 |
| `recognition.delete_knowledge(entry_id)`（SDK） | DANGER | 确认后删除用户或学习词条；内置不能删除，只能停用。 |
| `library.path_mappings(server)`（SDK） | READ | 完整返回指定 Jellyfin/Emby 的路径对与部署覆盖状态，不探测上游；以 `server + items[].local` 的规范化路径键定位。 |
| `library.create_path_mapping(server, local_path, server_path)`（SDK） | WRITE | 确认后复用 Web 公共校验、配置锁和文件 CAS 新增映射；不移动文件或刷新媒体库。 |
| `library.update_path_mapping(server, local_path, new_local_path=..., server_path=...)`（SDK） | WRITE | 原 `local_path` 定位；修改本地前缀用 `new_local_path`，未传字段保留。不使用重排后的序号。 |
| `library.delete_path_mapping(server, local_path)`（SDK） | WRITE | 确认后只删除指定自然键映射；不删除媒体、STRM 或本地归档绑定。配置漂移拒绝旧卡，未知结果先回读，不重放。 |

### 2.12 光鸭目录、文件变更与整理

浏览与文件变更不同于刮削整理；预览可以是 READ，但实际执行入口仍受确认约束。

定义：[`sdk.py`](../../app/sdk.py)、公共 [`guangya_workspace.py`](../../app/modules/guangya_workspace.py) 与 [`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `cloud.connection()`（SDK） | READ | 通过最小只读请求核验云盘连接；未知配置或连通性保留 null，返回采样时间，不返回凭据。 |
| `cloud.organize_schedule`（工作区 SDK） | READ | 读取光鸭定时整理的启用状态、五段 cron 和通知开关，不返回目录或凭据。 |
| `cloud.set_organize_schedule`（工作区 SDK） | WRITE | 预检并在用户确认后修改光鸭定时整理三项白名单策略，不立即运行整理。 |
| `cloud.organize_status` | READ | 读取整理概览；按 `run_id` 查询原持久运行。文件操作的 `GY-` 编号用 `cloud.operation` 查询。 |
| `activity.organize_logs`（SDK） | READ | 按来源、状态、关键词分页查看完整整理时间线；保留真实 ID、路径和脱敏错误，总数/分类计数与 Web 同源，支持按 kind/id 继续读取时间线。本地待确认使用 `origin="local", status="manual"`，终态使用 `success`/`failed`；原始状态、触发来源和时间戳均保留，供脚本统计。 |
| `cloud.list` / `cloud.info` | READ | 使用真实目录/文件ID读取完整属性；list支持递归、名称/类型过滤和offset/limit，total为实际总数，扫描不完整直接报错。 |
| `cloud.preview_names` | READ | 按真实ID预览删除旧Mbps字段、字面替换或media_hygiene；返回完整名称、冲突与operations，无序号、TTL或私有计划。 |
| `cloud.preview_cleanup` | READ | 检查整理/离线来源或真实来源ID；只显式quarantine_ids进入隔离区，其余残留保留。候选分页，operations为完整选择；空目录要求原子回收能力。 |
| `cloud.change` | WRITE/DANGER | 提交预览operations或真实ID操作；expected/expected_tree保留预览快照，require_empty要求真空目录。单次最多200对象与200新建目录，超限报错。沿用SQLite冻结确认与持久operation_ref；取消不写、未知结果先核验，不重放脚本。 |
| `cloud.inspect_media(file_id)` | SDK READ | 检查真实云盘目录或文件，保留完整视频、伴随文件与目录事实。 |
| `cloud.search_media(file_id, query, media_type, year)` | SDK READ | 查询匹配候选，返回明确 provider/external_id/tmdb_id，不依赖会话临时序号。 |
| `cloud.scrape(file_id, media_type, ...)` | SDK DANGER | 冻结明确媒体身份及完整归档计划，确认后提交现有持久任务；用 cloud.operation 查询执行结果，受理不等于完成。AI 建议不会自动学习为人工纠正。 |
| `cloud.preview_organize` | READ | 按当前服务端配置只读预览光鸭整理计划，不移动、改名或删除内容。 |
| `cloud.organize` | DANGER | 预览并在用户确认后按当前配置启动一次光鸭网盘整理，接受已配置来源的真实 source_ids；省略选全部。 |
| `cloud.stop_organize` | DANGER | 预检并在用户确认后协作式停止当前光鸭整理任务；已完成的云盘操作不会回滚。 |

### 2.13 光鸭账号、回收站与分享

账号容量、回收站恢复/全局清空、异步操作状态及分享管理；不开放本地上传。

定义：[`app/sdk.py`](../../app/sdk.py) 与 [`sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `cloud.account()`（工作区 SDK） | READ | 读取当前光鸭账号连接状态与容量摘要；返回总量、已用、可用字节及部分成功状态。 |
| `guangya.recycle.list` | READ | 分页读取光鸭回收站，返回名称、类型、体积和会话绑定的 guangya_recycle_items_ref；不返回 Provider 文件 ID。 |
| `guangya.recycle.restore` | WRITE | 对 guangya.recycle.list 返回的回收站引用按 index 冻结恢复计划。 |
| `guangya.recycle.clear` | DANGER | 完整读取并冻结当前光鸭回收站后生成不可逆清空计划。 |
| `guangya.operation.status` | READ | 使用回收站恢复、清空或其他 SDK 异步操作返回的 guangya_task_ref 查询 Provider 状态。 |
| `cloud.shares()`（SDK） | READ | 完整读取当前账号自建分享；返回真实管理 `share_id`，不返回访问码。分页失败或超预算显式报错。 |
| `cloud.share(file_ids, ...)`（SDK） | WRITE | 按 1–100 个真实 `file_id` 冻结对象、凭据与分享策略，确认后创建；链接和访问码只返回当前用户。 |
| `cloud.revoke_share(share_ids)`（SDK） | WRITE | 按列表返回的真实管理 `share_id` 冻结撤销计划；确认后只撤销分享链接，不改动原文件。受理仍待验证，结果未知时先查询，不重复提交。 |

### 2.14 本地媒体整理

来源 CRUD 只保存配置；扫描只处理已配置来源，后续任务沿原归档链路执行。写方法共用持久 SQLite 确认账本，取消、配置漂移、已用票据或未知回执不会重放；关闭 qB 接管不会取消已入队的手动扫描。任务已索引不代表真实播放测试。

本地来源与持久任务定义：[`app/sdk.py`](../../app/sdk.py)、[`local_media_service.py`](../../app/modules/local_media_service.py)；任务与诊断继续沿既有领域入口。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `local.diagnose()`（SDK） | READ | 读取本地来源、持久任务真实聚合和进程内调度状态，不初始化调度器、不扫描文件或联网；读取错误直接反馈。 |
| `local.sources()`（SDK） | READ | 完整读取所有来源的真实 ID、名称、容器路径、qB 路径前缀/完成接管开关及 targets/媒体库映射；不返回废弃 SMB 凭据。 |
| `local.source(source_id)`（SDK） | READ | 以真实来源 ID 读取完整事实，不按列表位置或公开序号选取。 |
| `local.create_source(name, local_root, ..., targets=None)`（SDK） | WRITE | 复用 Web 的目录/分类目标校验与来源事务；原生默认仅预览、关闭 qB 完成接管，不创建目录或修改文件权限。 |
| `local.update_source(source_id, ..., enabled=..., targets=...)`（SDK） | WRITE | 仅改明确指定字段；`enabled` 控制 qB 下载完成自动接管，未传 targets 时保留全部映射及目标 ID。有未完成任务时禁止修改运行配置。 |
| `local.delete_source(source_id)`（SDK） | DANGER | 删除配置，不删除媒体文件；未完成任务阻止删除。确认后的来源或映射漂移由写入事务拒绝。 |
| `local.scan_sources(source_ids=None, query="")`（SDK） | WRITE | 冻结全部或指定真实来源 ID、配置和 targets，经原 EffectExecutor 确认后沿原 scheduler 入队；返回真实 `data.task_ids` 和原 task_runs 的 `scan_ref`。`accepted` 只代表入队，扫描失败/部分失败不等于没有候选。 |
| `local.tasks(scope="all", source_id=None, scan_ref="", limit=100, offset=0)`（SDK） | READ | 完整任务/错误及文件结果，真实 task_id、真实匹配总数、returned/has_more/next_offset；最多每页500条。scan_ref限定原LM扫描成员，通知快照与当前状态分别保留；缺记录不以全历史代替。 |
| `local.task(task_id, inspect=False)`（SDK） | READ | 读取完整持久任务、全部文件项和执行步骤；inspect=True复用Web只读检查待人工任务的全部文件，不创建序号或会话TTL上下文。 |
| `local.preview(task_id, tmdb_id=..., media_type=..., season=..., episode=..., numbering_mode=...)`（SDK） | READ | 沿原service和来源路径边界重新检查、生成全部候选及计划；省略参数使用已保存识别/季集/规则。无需检查句柄，不执行文件操作。 |
| `local.retry(task_id, ..., confirm_interrupted_write=False)`（SDK） | WRITE | 冻结原任务、来源、targets和必要文件预览，原SQLite事务CAS后重新排队。沿Web支持识别/季集/编号覆盖，省略保留原值；中断写入需先核验文件及qB并明确确认。只重试原持久任务，不扫描新范围。 |
| `local.refresh_library(task_id)`（SDK） | WRITE | 冻结真实任务/媒体服务器配置、全部归档路径及唯一绑定媒体库，经原确认执行器提交精准刷新并复用原跨owner去重租约；绝不回退全库刷新，未知回执不重放。 |
| `local.verify_library(task_id)`（SDK） | READ | 核验唯一绑定库中的精确电影/季集可见性；服务异常和不完整清单保留不确定。库中可见不代表已执行播放探测。 |

### 2.15 STRM 同步与失败处理

状态、历史、失败诊断、受控重试及指定来源同步；不以排队成功冒充生成完成。

定义：[`app/sdk.py`](../../app/sdk.py)、[`strm.py`](../../app/modules/strm.py) 与原确认执行器。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `strm.diagnose` | READ | 原生SDK只读检查STRM索引、缺失文件、失败汇总与最近5次同步真实run_id；不修复、不重试、不扫描云盘。 |
| `strm.history` | READ | 原生SDK按持久状态过滤完整历史，limit/offset真实SQL分页；返回真实run_id、total/returned/has_more/next_offset及失败/队列上下文，不返回路径、URL或错误正文。 |
| `strm.failures` | READ | 原生 SDK 读取真实失败 ID、状态、来源对象与完整已保存错误；按 status/action/source_id 筛选，limit/offset 分页，使用 data.next_offset 继续。错误沿用账本入库脱敏和最多 500 字边界，不能还原上游原文。 |
| `strm.retry_failures(ids)` | DANGER | 从失败账本选择 open 的真实整数 ID 数组，预检冻结选中行和运行配置，确认复验后复用公共重试；无 scope 或旧 100 项模型上限。返回实际 claim/解决/失败/延期及扫描、停止计数，未匹配不能视为成功。 |
| `strm.run` | DANGER | 原生SDK预检并确认同步全部或指定的真实来源ID；来源取 `strm.status().data.sources`，受理返回 `data.run_id`，继续按该ID回读。 |
| `strm.status` | READ | 原生SDK读取当前进程调度状态与完整来源ID/名称；传run_id只回读该持久任务，进度仅在当前运行ID匹配时提供，不返回路径或错误正文。 |
| `strm.schedule`（工作区 SDK） | READ | 读取 STRM 定时同步的启用状态、五段 cron 和任务通知开关，不返回目录、地址或凭据。 |
| `strm.set_schedule`（工作区 SDK） | WRITE | 预检并在用户确认后修改 STRM 定时同步的三项白名单策略，不立即运行同步。 |

```python
from mf import strm
page = strm.failures(status="open", action="metadata")
ids = [row["id"] for row in page["data"]["items"]]
# 先核对 items[].error；如 has_more，按 next_offset 继续读取再选择。
strm.retry_failures(ids)  # 原确认执行器准备确认卡，确认前不重试
```

Shell 同样可调用 `mf strm.failures --status open --limit 200 --offset 0`，再以 `mf strm.retry_failures --ids '[12,34]'` 准备确认；ID 必须来自实际账本。冷确认、取消及未知结果沿用原 SQLite 一次性票据，不自动重放。

### 2.16 STRM 伴随元数据

管理 NFO 等伴随元数据同步及待处理队列，与视频 STRM 生成状态分开解释。

定义：[`app/sdk.py`](../../app/sdk.py) 与 [`strm_metadata_management.py`](../../app/modules/strm_metadata_management.py)。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `strm.metadata_status` | READ | 读取伴随元数据（云盘 NFO、字幕、海报同步）队列数量、开关、消费者线程和熔断状态；running=0只代表瞬时任务数，不代表不会自动工作。 |
| `strm.set_metadata_enabled` | WRITE | 经人工确认调整已有伴随同步开关。 |
| `strm.cancel_metadata_pending` | WRITE | 预览并经人工确认取消当前 queued/retry_wait 伴随元数据积压。 |

### 2.17 播放就绪与媒体反代

查看媒体链接相关状态、反代和播放准备情况；重启/修改反代策略属于写入。

定义：[`app/sdk.py`](../../app/sdk.py)、[`media_proxy.py`](../../app/modules/media_proxy.py) 与既有实例生命周期。

| 工具 | Kernel 风险 | 用途 |
|---|---|---|
| `playback.instances()` | READ | 完整读取实例真实 ID、已保存/有效配置、错误和当前进程运行事实；凭据脱敏，运行态不可用标为 unknown。 |
| `playback.failures(hours, instance_id, limit, offset)` | READ | 同一 SQLite 快照读取故障汇总与完整已保存失败记录；真实实例/记录 ID、HTTP 状态、路由、阶段、时延及错误均保留。按 next_offset 读完；错误入库已脱敏且最多1000字，汇总排名最多8组仅是摘要。覆盖仅代表 best-effort 已记录请求。 |
| `playback.test(instance_id)` | READ | 使用已保存真实 ID 沿原固定上游探测；保留HTTP状态和耗时，不跟随重定向。 |
| `playback.set_enabled(instance_id, enabled)` | WRITE | 原确认执行器冻结目标与有效配置，复验后原子更新 enabled 并请求原运行时协调。runtime_refreshed 只代表排队；不能视为已完成启动或关闭。 |
| `playback.restart(instance_id)` | WRITE | 确认后沿既有 Manager/Future 重建指定实例；运行前再次核对冻结配置。completed 需要真实 restarted=true；outcome_unknown 时读取状态，不自动重投。 |

```python
from mf import playback
instances = playback.instances()["data"]["instances"]
instance_id = instances[0]["instance_id"]  # 来自真实配置，不是公开序号
facts = playback.failures(instance_id=instance_id)
# 先读取 items[].error 和 next_offset，再决定是否需要重启。
playback.restart(instance_id)  # 准备原确认卡，确认前不修改实例
```

Shell 同样使用 `mf playback.instances`、`mf playback.failures --instance-id <真实ID>` 和 `mf playback.restart --instance-id <真实ID>`。只读事实可保存为 JSON 后由 Python/Shell 继续处理。


## 3. 媒体服务器与下载器原生 SDK

媒体查询与变更直接使用原生 SDK 和共用客户端，结果使用服务器真实 ID；不再创建 Provider 临时对象引用。

### 3.1 Jellyfin / Emby

使用已配置的媒体服务器及可确定的用户，不接受模型自行注入任意服务地址或管理凭据。用户配置/观看历史不足时，不能保证“排除已看”或最近播放结果完整。

播放列表创建目前仅向 Jellyfin 开放；当前适配器不能可靠绑定 Emby 新播放列表的所有者，因此拒绝创建，但在满足引用和权限校验时可修改已有播放列表。

| 操作 | Kernel 风险 | 用途 |
|---|---|---|
| `library.counts(server="all", library_id="")` | SDK READ | 实时全库或指定库统计；指定真实库 ID 时必须明确 Jellyfin/Emby。保留已知计数和失败来源，未知不冒充零，多服务器记录不按作品去重。 |
| `library.search(query, server="all", offset=0)` | READ | 使用媒体服务器原生搜索查询电影、剧集或单集，并返回可用的已校验 open_url。 |
| `library.search(query, media_type="series")` | READ | 在媒体服务器中搜索剧集候选，并返回 TMDB 映射、对象引用和可用的已校验 open_url。 |
| `library.episodes(series_id="…", server="jellyfin")` | READ | 读取先前选中剧集的本地季集位置，用于集数和缺集核验。 |
| `media.refresh(item_id="…", server="jellyfin")` | LOW_WRITE | 用户明确要求后直接刷新已核实的单个媒体条目，不弹确认卡、不扩大为全库；支持 scan、missing、replace，默认保留图片与预览图。 |

### 3.2 qBittorrent

实时 qB 任务与 MediaFlux 的历史下载请求不是同一份数据。询问“qB 当前任务”可以用 SDK `downloads.tasks()` 读取真实任务及 hash，再按 hash 控制；本地活动队列用于跟踪 MF 提交的请求。

| 操作 | Kernel 风险 | 用途 |
|---|---|---|
| `downloads.version()` | SDK READ | 读取 qBittorrent 应用和 WebUI API 版本。 |
| `downloads.transfer()` | SDK READ | 读取 qBittorrent 全局传输速度、累计流量和连接状态。 |
| `downloads.tasks(category="")` | SDK READ | 列出 qBittorrent 下载任务的状态、进度、速度和分类。 |
| `downloads.pause(hashes)`（SDK） | LOW_WRITE | 确认后暂停指定的真实 qB 任务；最多20项，回读核验实际状态。 |
| `downloads.resume(hashes)`（SDK） | LOW_WRITE | 确认后恢复指定的真实 qB 任务；与本地整理冲突时拒绝执行。 |
| `downloads.remove(hashes)`（SDK） | WRITE | 确认后停止 MF 跟踪并移除指定任务，始终保留已下载文件。 |
| `downloads.files(torrent_hash)` | SDK READ | 按真实任务 hash 完整读取文件清单与原生 progress，不受旧50/200条上限截断。 |

下载新资源使用 `downloads.submit` 或 `resources.download`。qB 控制冻结下载器配置和任务身份，状态自然变化不会使确认失效；结果未知时先查询，不自动重复执行。`downloads.remove` 只删除任务并保留下载文件。

### 3.3 光鸭

光鸭原生 `cloud.*` 方法直接共用项目客户端与业务服务，文件变更统一经 `cloud.change` 预检、确认和原持久任务执行。

通用文件变更工具支持的操作枚举：

| 操作 | 含义 |
|---|---|
| `rename` | 对明确对象改名 |
| `move` | 移动到核验后的目标目录 |
| `copy` | 复制明确对象；异步/大目录结果须继续核验 |
| `relocate` | 在同一冻结计划内移动并改名 |
| `batch_files` | 按真实非根目录ID完整展开显式大小/类型/名称筛选与移动/空目录规则；不返回私有句柄 |
| `create_directory` | 新建目录，可与指向该目录的后续操作一起规划 |
| `trash` | 将活动目录中的明确对象移入回收站 |

这些都是 SDK `cloud.change(operations, trigger_strm=...)` 的操作枚举。剧集规整先用 `cloud.episodes(file_id)` 取得完整真实ID、路径及正片/额外素材/未知/字幕事实；使用实际 `source_path`、明确季集范围、目标季与起点及 `expected_count` 调用 `cloud.preview_episodes`，然后把返回的完整 `operations` 交给 `cloud.change`。源文件数量不是TMDB季偏移依据；缺少资料或用户明确映射时应先判断，不得猜测。字幕只跟随唯一配对视频，多版本和目标冲突需要明确选择。

SDK预览不保存目录TTL、序号或OBS/OBJ句柄。普通操作直接使用真实 `file_id`；已读目标/父目录可传 `target_id`/`parent_id` 防同名替换，已计划创建的新目录仍以 `target_path`/`parent_path` 组织。`expected` 与其 `path` 约束读取时的身份、版本和原路径。确认只执行原冻结计划、原持久任务，取消不写、结果未知不可自动重投，脚本不重放。`batch_files`沿用原规则、原2000项/500目录/12层完整读取预算与回收保护，不能截断范围；手工对象操作最多200项，目录创建最多200项。

旧三个工具入口及其上下文存储已经退役；已入队的公共文件变更job仍沿用签名计划、SQLite归属、检查点与原任务回执，历史字段仅为校验既有签名保留。


旧 Provider 写计划保留历史存储：未执行计划失效，运行中计划只有在旧执行者释放锁后才恢复为结果未知。旧确认票据不转换成新的写请求，需重新从原生入口预检。历史受理回执不代表上游异步任务已完成。

## 4. 容易混淆的能力边界

| 需求 | 正确入口或边界 |
|---|---|
| RSS 订阅源列表 | `rss.subscriptions`；不是媒体追更列表 |
| 媒体追更列表 | `subscriptions.list`；不是 RSS 规则 |
| 全库总数与单库总数 | `library.counts`；指定库需明确服务器和真实库 ID，部分来源失败保留未知 |
| 查询一批影视是否在库 | `library.presence(items)`（SDK），不要循环几十次单片搜索 |
| 最近播放与继续观看 | `library.history`（SDK） 与 `library.resume`（SDK）；含义不同 |
| 本地推荐与公开推荐 | `library.recommend`（SDK） 与 `media.catalog`（SDK）；外部推荐不代表已经在库 |
| 演员与作品详情 | `media.credits` 与 `media.detail`（SDK）；缺少演职员数据时再核实其他来源 |
| 三平台更新与 Bangumi 放送 | `media.calendar` 与 `media.catalog("bangumi", "calendar", "tv")`；不能混用日期和来源完整性 |
| 网页搜索与打开正文 | `web.search` 使用 Tavily Search，`web.read` 由本机直接获取并提取公开 HTTPS 正文，仅受独立 `WEB_READ_ENABLED` 控制，不使用 Key 或 Tavily 额度；搜索仍需 `WEB_SEARCH_ENABLED` 与 `TAVILY_API_KEY`，不是任意 HTTP/浏览器工具 |
| 列云盘目录与刮削/清理 | `cloud.list(parent_id="0")`；普通浏览不需要启动刮削或垃圾清理计划 |
| 停止回合与停止任务 | 停止模型不撤销已经发生的业务写入，需查询并使用对应任务控制 |
| 关闭元数据同步与取消积压 | `strm.set_metadata_enabled` 与 `strm.cancel_metadata_pending` 是不同操作 |
| 清空回收站 | `cloud.clear_recycle` 为全账号永久清空，不是当前页/选中项删除 |
| 本地上传到光鸭 | 不向 Agent 开放；不能从 SDK 存在上传方法推断 Agent 有权限 |
| 宿主 Shell、任意配置或内网 Fetch | 不开放宿主执行或任意网络访问；Python/Shell 仅在隔离工作区运行，业务访问沿 SDK 声明的字段、来源与操作 |
| 媒体详情链接 | 条件满足时返回已校验的打开链接，不保证唤起 App 或提供永久直链 |
| 识别主动复核 | 单独授权、有限案例工具及结构化审计，不放开聊天 Agent 的全部写权限 |

## 5. 维护这份清单

1. 公开方法以 [`app/sdk.py`](../../app/sdk.py) 的 `METHODS`、真实签名与 `describe_methods()` 为准；普通模型入口以 [`kernel/bootstrap.py`](../../app/agent/kernel/bootstrap.py) 的显式集合为准，不按内部注册数量推断模型工具数量。
2. 业务写入同时核对 `WRITE_METHODS`、`build_tool_catalog()` 及 [`kernel/ports/sdk_effects.py`](../../app/agent/kernel/ports/sdk_effects.py) 的执行合同；媒体刷新明确声明无需交互确认，仍沿原执行器留存回执。其他操作不因 `LOW_WRITE` 自动取消确认；[`kernel/batch_effects.py`](../../app/agent/kernel/batch_effects.py) 负责组合确认。
3. `session.*` 的宿主状态方法沿 [`kernel/session_service.py`](../../app/agent/kernel/session_service.py)，不把内部业务函数重复计为模型工具；SDK 业务对象必须沿真实 ID 和版本核验。
4. 功能新增/退役时同步更新名称、用途、配置条件和本文核对日期；真实 SDK 签名、方法语义和确认卡优先，不恢复动态业务窗口或旧工具目录。
5. 注册表/文档校验只证明声明一致，不能替代真实 Provider 冒烟、确认安全测试、跨轮引用和两端事件验收。

返回：[Agent 架构与使用实战](08_Agent架构与使用实战.md) · [自动化流转全景](00_自动化流转全景与工作流程.md) · [开发文档](../开发文档.md)。
