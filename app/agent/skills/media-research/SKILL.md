# 影视检索、下载验收与订阅

从 `mf` 按需导入；已知方法直接用，签名/结构不明才查 `help(具体方法)`，找入口用 `mf <模块> --help`。只读调查完成必要分页；写入沿原确认流程，查询不授权下载、auto 或通知。

条件、完整结果、来源/时间存 `/work` JSON；追问先读旧资料，仅改用户变更条件，复用同目标同页结果，过期库存重查。只输出摘要，空字段结合状态/来源解释。

## 查询入口

| 任务 | 路径 |
| --- | --- |
| 库内检索/计数 | `library.search` 查标题/演员/媒体类型，年份核元数据；count_only 取多源记录数（非去重作品），按每源 next_offset 续页，未知计数保留。 |
| 收藏更新 | `watchlist.list` 按 next 读全、筛 tv，逐部 `library.audit`。 |
| 已知缺集资源 | 复用作品/季集/完整 audit，按作品季 `resources.search_missing`，继续剩余/失败目标。 |
| 指定资源 | `resources.search`；歧义查 `media.search/detail`，本地库存 `library.episodes`，已播对照 `library.audit`。 |
| 推荐排库 | `media.sources/filters` → `media.catalog`，保留产地/年份，传 limit、exclude_library=True，按 scope 续页；已批量核库。外部片单用 `library.presence`，仅 missing 是核实未入库。 |
| 周表 | `media.calendar` 完整七天，按平台/日期/来源整理；covered_dates 空、产地未取均为未知，partial 不是分页。 |

## 收藏与缺集

收藏身份是 `(provider, external_id, media_type)`，id 是收藏记录编号。`watchlist.list` 按 has_more/next 读完；TMDB 剧集用 external_id，其他来源用 `media.resolve` 读取已确认映射。未确认映射保留未知，不用名称猜对应关系。

`library.episodes` 只读本地库存；`library.audit` 对照已播记录，缺集在 `data.missing_sample`，同时检查 status、计数、来源和完整性。指定 season 时附带映射，保存完整 audit 供资源核对。电影、订阅独有项不并入“收藏的剧”；暂停订阅的历史快照不能作为本次追平证据。

答案优先列有更新的作品和集号；已播缺集、未播、未知日期和来源失败分别说明，不用一次 TMDB 失败抹掉已取得的库存事实。

## 候选与确认

普通找资源有可靠匹配即可交付；要求列全、数量不足或无匹配才续页/换来源。按 site_statuses 报告覆盖，has_more 不阻挡交付，不外推全网/官方发行；单集与范围包分列。`exclude_hdr=True` 仅排除已标 HDR，明确 SDR 用 `dynamic_range="SDR"`；未标记不是 SDR。季序未知或与已播进度冲突不推荐；标题、体积、做种不证明实测码率、字幕、官方来源或速度。

`resources.present` 组合跨查询/页真实 result_id，保留条件/来源；`resources.download` 生成整批预览，显式传用户目标，省略沿偏好。`search_missing` 的 result_ids/verification_targets 整体保留并传下载，覆盖资源包全部集。出确认卡即结束脚本，确认只执行冻结操作，后续核验用新命令。取消待确认用 `session.pending_plan("cancel")`；`resources.present([])` 仅撤候选卡。

## 下载到入库

候选回执取 `data.items[].request_id`，直链 `downloads.submit` 取顶层 request_id；普通网页/分享不用 submit。`activity.timeline("download", request_id)` 查持久 stages/gaps；忘记编号查 `downloads.requests` 的 request_number/next_offset。实时队列用 `downloads.tasks` / `cloud.offline_tasks` 按真实后端身份关联；补库用 `resources.missing_workflows`。明确可重试失败才调 `downloads.retry`，未知受理不重投。

光鸭整理/STRM 见 cloud-files；qB 从 timeline 的 `data.activities` 中 local_media 取 id → `local.task`，待人工/失败先 `local.preview` 再按需 `local.retry`。归档用 `local.verify_library`，需精准刷新则 `local.refresh_library` 后再验。其他电影用 `library.presence`、剧用 `library.audit(..., refresh=True)` 核原目标。受理/阶段结束不等于入库；partial/unknown 保留未覆盖范围，索引可见不证明可播放。

## 追更与 RSS

收藏用 watchlist；作品追更用 `subscriptions`：`list` 按 next 读全，`get` 查策略/工作流，`check_updates` 实时续页，每项最多搜首个缺集；暂停/partial/unknown 不算无更新。

明确追更才按真实身份 `create`，非 TMDB 须确认映射；按意图选 season、action（notify 提醒/confirm 人工确认/auto 持续自动下载）、目标与周期，整理/STRM 沿配置。创建不立即搜索下载；回执编号为 `data.subscription_number`；不支持固定星期、独立画质/字幕硬过滤。`update` 的 enabled 暂停/恢复，`delete` 保留已提交下载/文件；保存后回读 get。全局通知用 `notification/set_notification`，default 不等于显式关闭；auto 与通知变更须用户授权确认。

源规则用 `rss`：`subscriptions/subscription` 查配置，`create/update` 保存；立即拉取用 `refresh(subscription_ids=...)` → 回执 run_id 给 `activity(run_id=...)` → `entries` 按 next 读全 → `download(entry_ids=...)` 投递所选项 → entries.delivery 跟踪。保存不刷新/下载。
