# 影视检索、下载跟踪、入库验收与追更订阅

先按用户的范围选入口，读取相关方法的 `help`，再完成查询和必要翻页。只读调查不需要用户反复确认。

| 任务 | 查询路径 |
| --- | --- |
| 收藏的剧有更新吗 | `watchlist.list` 读全收藏，筛选 tv；按每部真实身份调用 `library.audit`，可并行。收藏不是订阅，结果范围只含收藏。 |
| 已确定的缺集找资源 | 复用上轮作品、缺集和审计，按作品/季交给 `resources.search_missing`；按返回的剩余目标继续。 |
| 指定作品、季集、画质找资源 | `resources.search`；有身份歧义时才用 `media.search/detail`，用户要求查库存时再用 `library.episodes`。 |
| 推荐且排除已有 | `media.sources/filters` 查来源与筛选，`media.catalog` 保留产地、年份并传 `limit`、`exclude_library=True`，按 scope 续页。 |
| 周更新表 | `media.calendar` 一次返回完整七天，按用户的平台和日期整理。 |

## 条件与工作资料

工作区从 `mf` 导入所需命名空间；`mf library --help` 是方法目录，`help(library.audit)` 是签名和返回结构。不要把空字段当成业务结论；先核对实际结构和来源状态。

把本次查询条件、完整返回值、来源及时间保存到 `/work` 的 JSON。追问只更新用户改变的条件，其余保持；读取已有文件后再决定需要哪些新查询。相同目标和页码的结果可在本次调查复用，过期库存需重新核对。只打印与当前问题有关的摘要，避免反复输出全部候选和帮助。

## 收藏与缺集

收藏身份是 `(provider, external_id, media_type)`，id 是收藏记录编号。`watchlist.list` 按 has_more/next 读完；TMDB 剧集用 external_id，其他来源用 `media.resolve` 读取已确认映射。未确认映射保留未知，不用名称猜对应关系。

`library.episodes` 只读本地库存；`library.audit` 对照已播记录，缺集在 `data.missing_sample`，同时检查 status、计数、来源和完整性。指定 season 时附带映射，保存完整 audit 供资源核对。电影、订阅独有项不并入“收藏的剧”；暂停订阅的历史快照不能作为本次追平证据。

答案优先列有更新的作品和集号；已播缺集、未播、未知日期和来源失败分别说明，不用一次 TMDB 失败抹掉已取得的库存事实。

## 资源与后续动作

`resources.search` 返回完整候选、recommendation 和 site_statuses。普通“找资源”取得满足当前条件的可靠候选即可展示交付；用户要求列全、指定数量尚不足或没有匹配时，再按需要续页或换来源。`has_more` 表示还有页面，不代表已找到的候选不能交付。交付时说明实际覆盖范围，不推断全网没有资源或官方未发行某画质。单集与范围包分别说明，不能把混合数量都称为单集。

`exclude_hdr=True` 只排除已标记 HDR 的版本；明确要求 SDR 时用 `dynamic_range="SDR"`。未标记不等于 SDR；标题、体积和做种数不能证明实际码率、字幕、官方来源或下载速度。季序未知、与已播进度冲突的候选不作为匹配推荐。已有搜索结果可用 `resources.rank_episode` 配合完整 audit 重排，无需重复请求资源站。

查看候选用 `resources.present`，可组合各查询、各页满足条件的真实 result_id，各来源范围随卡片保留。下载预览用 `resources.download(result_ids, target=...)`，显式传用户指定的目标，省略时沿用户偏好。工作区生成整批确认卡后结束脚本，确认只执行冻结操作，后续查询用新命令。取消待确认计划用 `session.pending_plan("cancel")`；`present([])` 仅撤候选展示卡。`search_missing` 的 result_ids/verification_targets 要整体保留并随下载传入，避免漏掉资源包覆盖的其它集。

## 下载到入库

候选下载回执取 `data.items[].request_id`；直链用 `downloads.submit(url, target=...)`，回执取顶层 request_id，普通网页/分享不适用。用 `activity.timeline("download", request_id)` 读持久快照 `data.stages/gaps`；忘记编号可查 `downloads.requests` 的 request_number，按 next_offset 续页。实时队列分别用 `downloads.tasks` / `cloud.offline_tasks`，按真实后端身份关联；缺集补库用 `resources.missing_workflows` 跟踪。明确可重试的失败目标才用 `downloads.retry(request_id, target)`，未知受理不重投。

光鸭整理/STRM 接续见 cloud-files。qB 从时间线 `data.activities` 的 local_media 项取 id → `local.task(task_id)`；待人工/失败用 `local.preview` 核对，再按需 `local.retry`。已归档用 `local.verify_library(task_id)` 验收，需要精准刷新时 `local.refresh_library(task_id)` 后再验。其它电影/剧集分别用 `library.presence` / `library.audit(..., refresh=True)` 核对原目标。accepted、阶段结束不等于入库；partial/unknown 与不完整库存保留未覆盖范围，索引可见不证明可播放。

例如，已把候选下载的确认执行回执保存为 `/work/download-receipt.json`，在新命令里核对各请求，不再次提交下载：

```python
import json
from mf import activity

with open("/work/download-receipt.json") as stream:
    receipt = json.load(stream)
for item in receipt["data"]["items"]:
    if item.get("request_id"):
        result = activity.timeline("download", item["request_id"])
        print(item["request_id"], result["status"],
              result["data"].get("stages"), result["data"].get("gaps"))
```

## 订阅流程

收藏用 `watchlist`，作品追更用 `subscriptions`，源规则用 `rss`。`subscriptions.list` 按 next 读全，`subscriptions.get(subscription_id)` 读策略/工作流；实时用 `subscriptions.check_updates` 按 next 续页，检查只读、每项最多搜索首个缺集。暂停项未重新核库，partial/unknown 不等于无更新。

要求追更时用真实 provider/external_id/media_type 调 `subscriptions.create`，季范围用 season，非 TMDB 须有确认映射。回执编号为 `data.subscription_number`；创建不立即搜索下载。按意图选 action（notify 提醒、confirm 人工确认候选、auto 持续自动下载）、download_target 和周期，整理/STRM 沿已有配置；固定星期和独立画质/字幕硬过滤不支持。`subscriptions.update(subscription_id, enabled=False)` 暂停，True 恢复；`subscriptions.delete` 保留已提交下载/文件，保存后回读 get。

全局通知用 `subscriptions.notification` / `subscriptions.set_notification`，default 不等于显式关闭。写入沿原确认流程，只读检查不授权启用 auto 或通知。

RSS 用 `rss.subscriptions` / `rss.subscription` 查真实订阅，`rss.create` / `rss.update` 保存配置；需立即拉取时 `rss.refresh(subscription_ids=[...])` → 回执 run_id 用 `rss.activity(run_id=...)` 查原运行 → `rss.entries` 按 data.next 续页 → `rss.download(entry_ids=[...])` 投递选中条目，再从 entries 的 delivery 跟踪。保存配置不立即刷新/下载。

## 推荐与排期

`media.catalog(provider, category, media_type, limit=..., exclude_library=True)` 已批量核验库存，不必再查库重复去重。外部自选片单才用 `library.presence`：只有 missing 是未入库，possible/indeterminate 仍待核对。

日历的 `days[].items[].source` 对应 `sources[].id`；covered_dates 是已读取日期，空表示未知。按七天给出排期，不把 partial 当分页，也不添加用户未问的会员说明。
