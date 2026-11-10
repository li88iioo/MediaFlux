"""面向用户与外部模型的文本安全边界。

只负责公开名称、凭据/路径过滤和不可信文件名清洗；不承担 Agent 结果编排。
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

from app.sensitive_data import contains_sensitive_credential

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URI_RE = re.compile(r"(?i)\b(?:https?|ftp|file)\s*://\S+")
_P2P_RE = re.compile(r"(?i)\b(?:magnet:\?\S+|ed2k://\S+)")
_WINDOWS_PATH_RE = re.compile(
    r"(?<!\w)(?:[a-z]:(?:[\\/])?|\\{1,2})[^\s]+", re.IGNORECASE
)
_UNIX_PATH_RE = re.compile(r"(?<!\w)(?:(?:\.\.?/)+|~/|/(?!/))[^\s/][^\s]*")
# Try each maximal ASCII path-component run once, not every punctuation suffix.
# After a Unicode word boundary, the first eligible suffix starts immediately
# after the run's first non-word character; it must still contain a non-digit.
# Disjoint prefix classes and a run-start guard keep failed scans linear.
_RELATIVE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._~:-])(?:(?<!\w)|[A-Za-z0-9_]*[.~:-])"
    r"(?=[0-9]*[A-Za-z._~:-])[A-Za-z0-9._~:-]+[\\/][^\s]+"
)
_CONTEXTUAL_PATH_RE = re.compile(
    r"(?:路径|目录|位于|保存(?:到|在)|存储(?:到|在)|文件(?:在|位于)|"
    r"位置\s*(?:是|为|[:：]))[^。\n；;]{0,40}?[\\/][^\s，。；;]+"
)
_UNICODE_FILE_PATH_RE = re.compile(
    r"(?<!\w)[^\s/\\，。；;]{1,80}[\\/]"
    r"[^\s/\\，。；;]{1,80}\.[A-Za-z0-9]{1,12}(?!\w)"
)
_INTERNAL_CAMEL_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:request|confirmation|resource|session|trace|task|"
    r"ticket|tool|call|owner|message|chat|job|provider|stream)"
    r"[A-Z][A-Za-z0-9]*(?![A-Za-z0-9_.-])"
)
_INTERNAL_KEBAB_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_INTERNAL_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z_][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"(?![A-Za-z0-9_.-])"
)
_PRIVATE_VALUE_RE = re.compile(r"(?:私密(?:值|标识)|private[_ -]?(?:id|value))\s*[:：=]?\s*[a-z0-9_-]+", re.IGNORECASE)
_TRANSFER_RATE_RE = re.compile(r"(?i)\b(?:\d+(?:\.\d+)?\s*)?(?:[kmgt]?i?b|b)/s\b")
# Slash-joined public media types are classifications, not file paths.
_MEDIA_TYPES_RE = re.compile(
    r"(?<![\w./\\~-])(?:movie|tv|series|episode)(?:/(?:movie|tv|series|episode))+(?![\w./\\~-])",
    re.IGNORECASE,
)

_PUBLIC_TOOL_LABELS: dict[str, str] = {
    "rss.subscriptions": "RSS 订阅列表",
    "rss.subscription": "RSS 订阅详情",
    "rss.activity": "RSS 处理记录",
    "rss.create": "创建 RSS 订阅",
    "rss.update": "修改 RSS 订阅",
    "rss.delete": "删除 RSS 订阅",
    "diagnostics.overview": "项目状态概览",
    "diagnostics.search": "跨模块活动检索",
    "diagnostics.runtime": "运行状态检查",
    "library.history": "播放历史查询",
    "library.resume": "继续观看列表",
    "library.recommend": "媒体库推荐",
    "watchlist.list": "读取收藏片单",
    "watchlist.get": "收藏条目详情",
    "watchlist.statistics": "收藏统计",
    "library.counts": "媒体库数量统计",
    "library.presence": "核对媒体库收录",
    "library.episodes": "查询媒体库季集",
    "library.audit": "检查媒体库内容",
    "library.updates": "核对剧集更新",
    "library.audit_all": "全库内容检查",
    "media.people": "演职员查询",
    "media.credits": "作品演职员查询",
    "media.filmography": "演职员作品查询",
    "media.search": "搜索作品信息",
    "media.sources": "查询媒体信息来源",
    "media.filters": "查询媒体筛选条件",
    "media.catalog": "浏览媒体榜单",
    "media.detail": "作品详情查询",
    "media.rating": "作品评分查询",
    "media.calendar": "查询更新日历",
    "media.resolve": "核对作品身份",
    "cloud.organize_status": "光鸭整理进度",
    "cloud.episodes": "核对云盘季集",
    "cloud.preview_episodes": "预览云盘季集命名",
    "cloud.account": "云盘账户与容量",
    "cloud.connection": "云盘连接检查",
    "cloud.offline_tasks": "云盘离线任务",
    "cloud.operation": "云盘操作进度",
    "cloud.inspect_media": "检查云盘媒体",
    "cloud.search_media": "搜索云盘媒体",
    "downloads.requests": "下载请求记录",
    "downloads.tasks": "下载任务查询",
    "downloads.transfer": "下载传输状态",
    "downloads.version": "下载器版本查询",
    "downloads.files": "下载文件列表",
    "activity.actions": "查询操作记录",
    "activity.organize_logs": "查询整理日志",
    "resources.search": "搜索资源",
    "resources.rank_episode": "核对资源季集匹配",
    "rss.entries": "RSS 条目列表",
    "rss.mark": "RSS 条目标记",
    "rss.download": "RSS 条目投递",
    "rss.retry": "RSS 失败条目重试",
    "rss.refresh": "RSS 订阅刷新",
    "resources.search_missing": "缺集资源检索",
    "resources.missing_workflows": "补库流程进度",
    "library.connection": "媒体服务器连接与身份诊断",
    "library.user_state": "媒体观看与收藏状态",
    "library.playlists": "播放列表查询",
    "library.playlist": "播放列表成员检查",
    "library.set_played": "修改媒体已看状态",
    "library.set_favorite": "修改媒体收藏状态",
    "library.create_playlist": "创建播放列表",
    "library.add_to_playlist": "添加播放列表成员",
    "library.remove_from_playlist": "移除播放列表成员",
    "activity.search": "活动查询",
    "activity.timeline": "活动处理时间线",
    "activity.follow": "活动跟踪开启",
    "activity.follows": "活动跟踪列表",
    "activity.unfollow": "活动跟踪停用",
    "activity.inspect_undo": "可逆操作核对",
    "activity.undo": "可逆操作回退",
    "activity.organize_detail": "整理日志详情",
    "activity.reorganize": "纠正识别并整理入库",
    # 历史审计仍需可读标签，不注册为执行入口。
    "action.undo.inspect": "可逆操作核对",
    "action.undo.execute": "可逆操作回退",
    "library.quality": "媒体库质量检查",
    "library.libraries": "媒体库列表",
    "library.recent": "最近入库",
    "library.refresh": "媒体库刷新",
    "media.refresh": "媒体条目刷新",
    "automation.create_media_rule": "创建媒体自动追更规则",
    "automation.digests": "主动摘要规则查询",
    "automation.set_digest": "主动摘要规则保存",
    "recognition.knowledge": "识别知识查询",
    "recognition.create_knowledge": "新增识别知识",
    "recognition.update_knowledge": "修改识别知识",
    "recognition.delete_knowledge": "删除识别知识",
    # 历史审计保留原动作名称，不注册旧执行入口。
    "config.recognition_knowledge": "识别知识查询",
    "config.create_recognition_knowledge": "新增识别知识",
    "config.update_recognition_knowledge": "修改识别知识",
    "config.delete_recognition_knowledge": "删除识别知识",
    "local.create_source": "新增本地媒体来源",
    "local.update_source": "修改本地媒体来源",
    "local.delete_source": "删除本地媒体来源",
    "local.scan_sources": "扫描本地媒体来源",
    "local.sources": "本地媒体来源列表",
    "local.source": "本地媒体来源详情",
    "config.create_local_source": "新增本地媒体来源",
    "config.update_local_source": "修改本地媒体来源",
    "config.delete_local_source": "删除本地媒体来源",
    "library.path_mappings": "媒体服务器路径映射",
    "library.create_path_mapping": "新增媒体路径映射",
    "library.update_path_mapping": "修改媒体路径映射",
    "library.delete_path_mapping": "删除媒体路径映射",
    # 历史审计仅保留旧名称文案，不注册旧执行入口。
    "config.media_path_mappings": "媒体服务器路径映射",
    "config.create_media_path_mapping": "新增媒体路径映射",
    "config.update_media_path_mapping": "修改媒体路径映射",
    "config.delete_media_path_mapping": "删除媒体路径映射",

    "agent.action_history": "Agent 操作记录",
    "diagnostics.query_db": "诊断数据查询",
    "diagnostics.search_logs": "运行日志检索",
    "library.cancel_audit": "取消后台检查",
    "agent.read_result": "工具结果续读",
    "agent.run_code": "工作区代码执行",
    "agent.batch": "组合变更",
    "session.batch": "组合变更",
    "agent.capabilities": "Agent 能力查询",
    "library.audit_job": "后台检查进度",
    "diagnostics.pipeline": "自动化链路诊断",
    "bangumi.calendar": "番剧放送日历",
    "diagnostics.configuration": "项目配置检查",
    "diagnostics.component": "配置说明",
    "settings.features": "功能状态概览",
    "resources.sources": "资源检索站点概览",
    "config.safe_policy_summary": "安全策略概览",
    "settings.set_feature": "功能开关修改",
    "resources.configure_sites": "资源检索站点修改",
    "settings.set_policy": "策略修改",
    "settings.policies": "策略概览",
    "config.set_safe_policy": "安全策略修改",
    "discovery.add_watchlist": "加入探索收藏",
    "discovery.anime_calendar": "追漫日历",
    "discovery.confirm_mapping": "影视映射确认",
    "media.confirm_mapping": "影视映射确认",
    "discovery.detail": "影视发现详情",
    "discovery.credits": "影视演职员查询",
    "discovery.lookup_rating": "豆瓣评分查询",
    "discovery.mapping_candidates": "影视映射候选",
    "discovery.person_filmography": "人物电影作品查询",
    "discovery.recommend": "媒体推荐",
    "discovery.remove_watchlist": "移除探索收藏",
    "watchlist.add": "加入探索收藏",
    "watchlist.remove": "移除探索收藏",
    "discovery.search": "媒体探索搜索",
    "discovery.watchlist_summaries": "探索收藏列表",
    "downloads.diagnose": "下载队列检查",
    "downloads.retry_submission": "重新提交下载请求",
    "downloads.pause": "暂停 qB 下载任务",
    "downloads.resume": "恢复 qB 下载任务",
    "downloads.remove": "移除 qB 任务并保留文件",
    "downloads.retry": "重新提交下载请求",
    "downloads.submit": "下载链接提交",
    "guangya.account.status": "光鸭账号状态",
    "guangya.connection_status": "光鸭连接检查",
    "guangya.directory_scrape.inspect": "光鸭刮削检查",
    "guangya.directory_scrape.preview": "光鸭刮削预览",
    "cloud.scrape": "光鸭媒体刮削执行",
    "guangya.directory_scrape.run": "光鸭刮削执行",
    "guangya.directory_scrape.search": "光鸭刮削匹配搜索",
    "guangya.fs.change.execute": "光鸭文件变更",
    "cloud.change": "光鸭文件变更",
    "cloud.list": "光鸭文件查询",
    "cloud.info": "光鸭对象详情",
    "cloud.preview_names": "光鸭名称预览",
    "cloud.preview_cleanup": "光鸭清理预览",
    "guangya.fs.query": "光鸭文件查询",
    "guangya.episode_naming.inspect": "光鸭剧集命名盘点",
    "guangya.episode_naming.plan": "光鸭剧集分季命名计划",
    "guangya.media_hygiene.preview": "光鸭媒体名称清理预览",
    "guangya.organize.cleanup.classify": "光鸭整理残留逐项复核",
    "guangya.organize.cleanup.execute": "光鸭整理残留清理",
    "guangya.organize.cleanup.preview": "光鸭整理残留清理预览",
    "guangya.organize.preview": "光鸭整理预检",
    "cloud.organize": "光鸭整理任务",
    "cloud.stop_organize": "停止光鸭整理任务",
    "cloud.preview_organize": "光鸭整理预检",
    "guangya.organize.run_once": "光鸭整理任务",
    "guangya.organize.schedule_policy": "光鸭定时整理策略",
    "cloud.organize_schedule": "光鸭定时整理策略",
    "guangya.organize.set_schedule_policy": "光鸭定时整理策略修改",
    "cloud.set_organize_schedule": "光鸭定时整理策略修改",
    "guangya.organize.status": "光鸭整理状态",
    "guangya.organize.stop": "停止光鸭整理任务",
    "guangya.operation.status": "光鸭任务状态",
    "guangya.recycle.clear": "清空光鸭回收站",
    "guangya.recycle.list": "光鸭回收站列表",
    "guangya.recycle.restore": "恢复光鸭回收站文件",
    "cloud.restore": "恢复光鸭回收站文件",
    "cloud.clear_recycle": "清空光鸭回收站",
    "cloud.recycle": "光鸭回收站列表",
    "cloud.task": "光鸭任务状态",
    "guangya.rename.execute": "光鸭重命名执行",
    "guangya.rename.preview": "光鸭批量名称转换预览",
    "cloud.share": "创建光鸭分享",
    "cloud.shares": "光鸭分享列表",
    "cloud.revoke_share": "撤销光鸭分享",
    "indexer.diagnose_readiness": "资源站就绪检查",
    "indexer.search_resources": "多站资源搜索",
    "resources.present": "资源候选展示",
    "cloud.inspect_share": "光鸭分享检查",
    "cloud.transfer_share": "光鸭分享转存",
    "ingest.inspect": "资源接入检查",
    "ingest.status": "资源接入状态",
    "ingest.submit": "资源接入提交",
    "resources.download": "资源下载提交",
    "library.audit_episodes": "剧集完整性检查",
    "library.audit_library_episodes": "全库剧集巡检",
    "library.batch_presence": "媒体库批量核对",
    "library.check_updates": "媒体更新检查",
    "library.count_series_episodes": "本地剧集数量",
    "library.patrol_policy": "缺集巡检策略",
    "library.patrol_status": "缺集巡检状态",
    "library.search": "媒体库搜索",
    "library.set_patrol_policy": "缺集巡检策略修改",
    "library.start_audit": "后台全库剧集检查",
    "library.trigger_patrol_now": "立即缺集巡检",
    "library.run_patrol": "立即缺集巡检",
    "local_media.diagnose": "本地媒体诊断",
    "local_media.get_source_summary": "本地媒体来源摘要",
    "local_media.history_summary": "本地媒体处理历史",
    "local_media.inspect_task": "本地媒体任务检查",
    "local_media.preview_task": "本地媒体整理预览",
    "local_media.refresh_task_library": "本地媒体库精准刷新",
    "local.tasks": "本地媒体任务列表",
    "local.task": "本地媒体任务检查",
    "local.preview": "本地媒体整理预览",
    "local.retry": "本地媒体任务重试/季集纠偏",
    "local.refresh_library": "精准刷新绑定媒体库",
    "local.verify_library": "本地媒体入库可见核验",
    "local.diagnose": "本地媒体诊断",
    "local_media.retry_task": "本地媒体任务重试/季集纠偏",
    "local_media.review_queue_summary": "本地媒体待确认摘要",
    "local_media.scan_sources": "本地媒体来源扫描",
    "local_media.set_source_trigger_enabled": "本地媒体来源触发器启停",
    "local_media.source_summaries": "本地媒体来源列表",
    "local_media.task_summaries": "本地媒体任务列表",
    "local_media.verify_task_library_visibility": "本地媒体入库可见核验",
    "media.clear_preferences": "媒体偏好清除",
    'subscriptions.create': "创建媒体追更",
    "media.create_subscription": "创建媒体追更",
    'subscriptions.delete': "删除媒体追更",
    "media.delete_subscription": "删除媒体追更",
    'subscriptions.get': "媒体追更策略",
    "media.get_subscription_policy": "媒体追更策略",
    "media.get_subscription_summary": "媒体追更订阅摘要",
    "media.preferences": "媒体偏好",
    'subscriptions.reset_notification': "媒体追更通知规则重置",
    "media.reset_subscription_notification_rule": "媒体追更通知规则重置",
    "media.set_preferences": "媒体偏好修改",
    "preferences.get": "媒体偏好",
    "preferences.update": "媒体偏好修改",
    "preferences.clear": "媒体偏好清除",
    "media.set_subscription_enabled": "媒体追更状态修改",
    'subscriptions.set_notification': "媒体追更通知规则修改",
    "media.set_subscription_notification_rule": "媒体追更通知规则修改",
    'subscriptions.update': "媒体追更策略修改",
    "media.set_subscription_policy": "媒体追更策略修改",
    'subscriptions.notification': "媒体追更通知规则",
    "media.subscription_notification_rule": "媒体追更通知规则",
    'subscriptions.list': "媒体追更订阅列表",
    "media.subscription_summaries": "媒体追更订阅列表",
    'subscriptions.check_updates': "媒体追更实时更新检查",
    "media.subscription_updates": "媒体追更实时更新检查",
    'subscriptions.today': "今日媒体内容摘要",
    "media.today_summary": "今日媒体内容摘要",
    "playback.instances": "媒体反代实例",
    "playback.failures": "媒体反代播放故障",
    "playback.test": "媒体反代连接测试",
    "playback.set_enabled": "媒体反代实例启停",
    "playback.restart": "媒体反代实例重启",
    "media_proxy.playback_failure_summary": "媒体反代播放故障摘要",
    "media_proxy.restart_instance": "媒体反代实例重启",
    "media_proxy.set_instance_enabled": "媒体反代实例启停",
    "media_proxy.status_summary": "媒体反代状态",
    "media_proxy.test_instance": "媒体反代连接测试",
    "organize.audit_logs": "整理记录摘要",
    "provider.capabilities": "Provider 能力列表",
    "provider.change.execute": "Provider 写计划执行",
    "provider.change.preview": "Provider 写计划预览",
    "provider.job.status": "Provider 写计划状态",
    "provider.query": "Provider 实时查询",
    "recognition.inspect_filenames": "文件名样本只读解析",
    "recognition.preview_release_format": "发布格式教学预览",
    "recognition.save_release_format": "保存发布格式教学",
    "recognition.rules": "识别规则查询",
    "recognition.set_rule_enabled": "识别规则启停",
    "rss.create_subscription": "RSS 订阅创建",
    "rss.delete_subscription": "RSS 订阅删除",
    "rss.diagnose": "RSS 订阅诊断",
    "rss.entry_summaries": "RSS 条目列表",
    "rss.get_subscription_summary": "RSS 单订阅摘要",
    "rss.mark_entries": "RSS 条目标记",
    "rss.recent_activity": "RSS 最近下载统计",
    "rss.refresh_subscriptions": "RSS 订阅刷新",
    "rss.retry_failed": "RSS 失败条目重试",
    "rss.submit_entries_to_qb": "RSS 指定条目提交",
    "rss.submit_pending_to_qb": "RSS 待处理条目提交",
    "rss.subscription_summaries": "RSS 订阅列表",
    "rss.update_subscription": "RSS 订阅配置修改",
    "strm.diagnose": "STRM 配置诊断",
    "strm.retry_failures": "STRM 失败项重试",
    "strm.history": "STRM 运行历史",
    "strm.run_history": "STRM 运行历史",
    "strm.metadata_status": "伴随元数据队列状态",
    "strm.set_metadata_enabled": "调整伴随同步开关",
    "strm.cancel_metadata_pending": "取消伴随元数据积压",
    "strm.metadata.status": "伴随元数据队列状态",
    "strm.metadata.set_enabled": "调整伴随同步开关",
    "strm.metadata.cancel_pending": "取消伴随元数据积压",
    "strm.run": "STRM 手动同步",
    "strm.run_once": "STRM 手动同步",
    "strm.schedule_policy": "STRM 调度策略",
    "strm.schedule": "STRM 调度策略",
    "strm.set_schedule_policy": "STRM 调度策略修改",
    "strm.set_schedule": "STRM 调度策略修改",
    "strm.status": "STRM 同步状态",
    "strm.failures": "STRM 失败账本",
    "telegram.send_test_notification": "Telegram 测试通知",
    "settings.test_notification": "Telegram 测试通知",
    "web.read": "网页正文读取",
    "web.search": "网页搜索",
}
_PUBLIC_FOLLOWUP_PROMPTS: dict[str, str] = {
    "automation": "检查自动化链路为什么需要关注",
    "configuration": "检查项目配置需要处理的问题",
    "download_verification": "检查下载后入库复核状态",
    "downloads": "检查下载队列里的异常",
    "indexer": "检查资源站是否可用",
    "indexers": "检查资源站是否可用",
    "library_patrol": "查看缺集巡检需要关注的内容",
    "local_media": "诊断本地媒体待处理项",
    "media_servers": "检查媒体服务器配置",
    "organize": "查看云盘整理任务状态",
    "rss": "检查 RSS 订阅为什么需要关注",
    "strm": "检查 STRM 同步失败原因",
    "workspace": "查看系统运行简报",
}
_PUBLIC_STATUS_KEYS = frozenset(
    [
        "active",
        "answered",
        "attention",
        "blocked",
        "cancelled",
        "clarification_required",
        "completed",
        "conflict",
        "degraded",
        "disabled",
        "empty",
        "failed",
        "found",
        "healthy",
        "incomplete",
        "inconclusive",
        "no_changes",
        "not_configured",
        "not_found",
        "not_missing",
        "not_run",
        "outcome_unknown",
        "partial",
        "pending",
        "prepared",
        "preview",
        "ready",
        "retry_wait",
        "review_required",
        "running",
        "selection_required",
        "stale",
        "succeeded",
        "success",
        "unavailable",
        "unknown",
        "unsupported",
        "up_to_date",
        "updates_available",
        "waiting",
        "warning",
    ]
)


# 只翻译完整的已登记工具标识，不把文件名中的点号/下划线当作内部信息。
# 与 KernelToolSpec 的默认 wire 名称保持一致；两种拼写共用同一公开名称。
_PUBLIC_TOOL_ALIASES = {
    alias: label
    for name, label in _PUBLIC_TOOL_LABELS.items()
    for alias in (name, name.replace(".", "__").replace("-", "_"))
}
_TOOL_IDENTIFIER_PATTERN = (
    r"(?<![A-Za-z0-9_.-])(?:"
    + "|".join(re.escape(name) for name in sorted(_PUBLIC_TOOL_ALIASES, key=len, reverse=True))
    + r")(?![A-Za-z0-9_.-])"
)
_INTERNAL_TOOL_RE = re.compile(_TOOL_IDENTIFIER_PATTERN)
_INTERNAL_TOOL_GUIDANCE_RE = re.compile(
    r"(?:可|可以|建议|请)?(?:继续)?(?:调用|使用)\s+"
    + _TOOL_IDENTIFIER_PATTERN
    + r"[^。！？!?\n]{0,160}[。！？!?]?"
)


def public_tool_label(tool_name: object) -> str:
    normalized = str(tool_name or "").strip()
    return _PUBLIC_TOOL_ALIASES.get(normalized, "MediaFlux 检查")


def public_followup_prompt(source: object) -> str:
    normalized = str(source or "").strip().casefold()
    return _PUBLIC_FOLLOWUP_PROMPTS.get(normalized, "继续检查需要关注的区域")


_MAX_PUBLIC_TEXT_CHARS = 65536
_MAX_PUBLIC_DECODE_ROUNDS = 8
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")


def _decode_text(value: object) -> str:
    # Bound both work and expansion; never publish a partially decoded prefix.
    text = str(value or "")
    for round_index in range(_MAX_PUBLIC_DECODE_ROUNDS + 1):
        if len(text) > _MAX_PUBLIC_TEXT_CHARS:
            return ""
        text = unicodedata.normalize("NFKC", text)
        if len(text) > _MAX_PUBLIC_TEXT_CHARS:
            return ""
        # NFKC can create escapes; unquote can create compatibility characters.
        # Only a normalized value with no remaining escape is safe to inspect.
        if not _PERCENT_ESCAPE_RE.search(text):
            return " ".join(text.replace("\x00", " ").split()).strip()
        if round_index == _MAX_PUBLIC_DECODE_ROUNDS:
            return ""
        try:
            text = unquote(text, errors="strict")
        except UnicodeDecodeError:
            return ""
    return ""


def _remove_internal_tool_guidance(text: str) -> str:
    cleaned = _INTERNAL_TOOL_GUIDANCE_RE.sub(" ", (text or ""))
    cleaned = re.sub(r"(?:下一步(?:建议)?|建议)\s*[:：]\s*$", "", cleaned).strip()
    return " ".join(cleaned.replace("\r", " ").replace("\n", " ").split()).strip()


def _replace_internal_field(match: re.Match[str]) -> str:
    value = match.group(0)
    # 公开Schema的字段含义不是私有状态；仅翻译名称，不放行真实引用值或任意内部字段。
    labels = {"new_name": "新名称", "object_ref": "对象引用", "target_path": "目标目录",
              "parent_path": "父目录", "observation_ref": "目录观察引用"}
    return labels.get(value.casefold(), value if value.casefold() in _PUBLIC_STATUS_KEYS else "内部状态")


def _replace_internal_identifiers_in_text(text: str) -> str:
    text = _remove_internal_tool_guidance(text)
    text = _INTERNAL_TOOL_RE.sub(lambda match: _PUBLIC_TOOL_ALIASES[match.group(0)], text)
    text = _INTERNAL_CAMEL_RE.sub("内部状态", text)
    text = _INTERNAL_KEBAB_RE.sub("内部检查", text)
    return _INTERNAL_FIELD_RE.sub(_replace_internal_field, text)


def replace_internal_identifiers(value: object) -> str:
    return _replace_internal_identifiers_in_text(_decode_text(value))


def sanitize_untrusted_filename(value: object, *, limit: int = 255) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip()
    if not text or _CONTROL_RE.search(text) or contains_sensitive_credential(text):
        return ""
    text = _URI_RE.sub("[网址]", text)
    text = _P2P_RE.sub("[链接]", text)
    return text[: max(1, limit)].rstrip()


def sanitize_public_text(value: object, *, limit: int = 600, preserve_format: bool = False) -> str:
    decoded = _decode_text(value)
    # 必须在字段名转换前检查凭据；不能先把 access_token 改成“内部状态”再漏过其值。
    if contains_sensitive_credential(decoded) or _PRIVATE_VALUE_RE.search(decoded):
        return ""
    text = _replace_internal_identifiers_in_text(decoded)
    if not text:
        return ""
    path_scan_text = _MEDIA_TYPES_RE.sub("媒体类型", _TRANSFER_RATE_RE.sub("传输速度", text))
    if (
        _CONTROL_RE.search(text)
        or _URI_RE.search(text)
        or _P2P_RE.search(text)
        or _WINDOWS_PATH_RE.search(path_scan_text)
        or _UNIX_PATH_RE.search(path_scan_text)
        or _RELATIVE_PATH_RE.search(path_scan_text)
        or _CONTEXTUAL_PATH_RE.search(text)
        or _UNICODE_FILE_PATH_RE.search(path_scan_text)
    ):
        return ""
    return (str(value) if preserve_format and text == decoded else text)[: max(1, limit)].rstrip()


def sanitize_resource_title(value: object, *, limit: int = 300) -> str:
    """文件名可含点号/下划线，不能被当成工具名；其余凭据、URL、路径守卫不变。"""
    text = re.sub(r"<[^>]*>", "", _decode_text(value))
    text = "".join(char for char in text if char.isspace() or not unicodedata.category(char).startswith("C"))
    # Preserve filename punctuation and URL masking, but inspect the full title
    # before applying the display limit so a private suffix cannot be hidden.
    text = sanitize_untrusted_filename(text, limit=max(1, len(text)))
    return text[: max(1, limit)].rstrip() if sanitize_public_text(text, limit=limit) else ""
