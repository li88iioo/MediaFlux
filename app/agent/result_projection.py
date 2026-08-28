"""Agent 工具结果的面向模型安全投影与公开名称。

该模块刻意不复用 ``ToolResult.data`` 的原始结构：只抽取有限深度、有限数量且
不含凭据、链接、绝对路径、内部工具参数或不透明标识的数据，避免把服务端细节
发送给外部模型。
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any
from urllib.parse import unquote

from app.agent.response_contract import ensure_response_contract
from app.agent.tool_semantics import RESOURCE_CANDIDATE_TOOLS
from app.sensitive_data import contains_sensitive_credential

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MULTILINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URI_RE = re.compile(r"(?i)\b(?:https?|ftp|file)\s*://\S+")
_P2P_RE = re.compile(r"(?i)\b(?:magnet:\?\S+|ed2k://\S+)")
_WINDOWS_PATH_RE = re.compile(
    r"(?<!\w)(?:[a-z]:(?:[\\/])?|\\{1,2})[^\s]+",
    re.IGNORECASE,
)
_UNIX_PATH_RE = re.compile(r"(?<!\w)(?:(?:\.\.?/)+|~/|/(?!/))[^\s/][^\s]*")
_RELATIVE_PATH_RE = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9._~:-]*[A-Za-z._~:-])"
    r"[A-Za-z0-9._~:-]+[\\/][^\s]+"
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
    r"(?<![A-Za-z0-9_])(?:request|confirmation|resource|session|trace|task|"
    r"ticket|tool|call|owner|message|chat|job|provider|stream)"
    r"[A-Z][A-Za-z0-9]*(?![A-Za-z0-9_])"
)
_INTERNAL_KEBAB_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:mf-[a-z0-9]+(?:-[a-z0-9]+)+|"
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+){2,})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_INTERNAL_TOOL_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?![A-Za-z0-9_])"
)
_INTERNAL_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"(?![A-Za-z0-9_])"
)
_DISPLAY_HEADING_PATTERN = (
    r"(?:结论|Agent\s*解读|依据|下一步(?:建议)?|关键数据(?:与范围)?)"
)
_FIXED_DISPLAY_HEADING_RE = re.compile(
    rf"^{_DISPLAY_HEADING_PATTERN}(?:\s*[:：]\s*|\s*$)",
    re.IGNORECASE,
)
_INLINE_DISPLAY_HEADING_RE = re.compile(
    rf"(?:^|\s+)(?:"
    rf"(?:\*{{1,2}}|_{{1,2}}){_DISPLAY_HEADING_PATTERN}\s*[:：]?"
    rf"(?:\*{{1,2}}|_{{1,2}})\s*[:：]?\s*|"
    rf"{_DISPLAY_HEADING_PATTERN}\s*[:：]\s*)",
    re.IGNORECASE,
)
_INLINE_MARKDOWN_BULLET_RE = re.compile(r"\s+[-*+•]\s+(?=(?:\*{1,2}|_{1,2})?\S)")
_INLINE_NUMBERED_ITEM_RE = re.compile(
    r"(?:^|(?<=[\s:：]))\d{1,2}(?:[.]\s+|[、]\s*)(?=(?:\*{1,2}|_{1,2})?\S)",
    re.MULTILINE,
)
_PUBLIC_INTERNAL_MARKER_RE = re.compile(
    r"\s*[（(]\s*(?:内部状态|内部检查|系统内部状态|仅供内部参考)\s*[）)]\s*",
    re.IGNORECASE,
)
_PUBLIC_INTERNAL_ASSIGNMENT_RE = re.compile(
    r"(?:^|[，,；;\s])(?:probe_mode|tool_name|subscription_id|request_id|session_id|"
    r"site_ids|runtime_refresh|enable_search)\s*[:=]\s*[^，,；;。！？!?\s]+",
    re.IGNORECASE,
)
_DISPLAY_SENTENCE_RE = re.compile(r"[^。！？!?]+(?:[。！？!?]+|$)")
_INTERNAL_TOOL_GUIDANCE_RE = re.compile(
    r"(?:可|可以|建议|请)?(?:继续)?(?:调用|使用)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"[^。！？!?\n]{0,160}[。！？!?]?",
    re.IGNORECASE,
)
_DISPLAY_CLAUSE_RE = re.compile(r"[^，,；;：:]+(?:[，,；;：:]+|$)")
_TRANSFER_RATE_RE = re.compile(r"(?i)\b(?:\d+(?:\.\d+)?\s*)?(?:[kmgt]?i?b|b)/s\b")

_PUBLIC_TOOL_LABELS: dict[str, str] = {
    "agent.capabilities": "Agent 能力列表",
    "agent.action_history": "Agent 操作记录",
    "agent.read_plan": "综合检查",
    "automation.diagnose_pipeline": "自动化链路诊断",
    "bangumi.calendar": "番剧放送日历",
    "config.diagnose": "项目配置检查",
    "config.diagnose_media_servers": "媒体服务器检查",
    "config.explain_component": "配置说明",
    "config.feature_summary": "功能状态概览",
    "config.indexer_sites_summary": "资源检索站点概览",
    "config.set_feature_state": "功能开关修改",
    "config.set_indexer_sites": "资源检索站点修改",
    "config.safe_policy_summary": "安全策略概览",
    "config.set_safe_policy": "安全策略修改",
    "telegram.send_test_notification": "Telegram 测试通知",
    "config.test_media_server": "媒体服务器连接测试",
    "discovery.recommend": "媒体推荐",
    "discovery.lookup_rating": "豆瓣评分查询",
    "discovery.search": "媒体探索搜索",
    "discovery.watchlist_summaries": "探索收藏列表",
    "discovery.get_watchlist_summary": "探索收藏详情",
    "discovery.detail": "影视发现详情",
    "discovery.mapping_candidates": "影视映射候选",
    "discovery.confirm_mapping": "影视映射确认",
    "discovery.add_watchlist": "加入探索收藏",
    "discovery.remove_watchlist": "移除探索收藏",
    "downloads.diagnose_queue": "下载队列检查",
    "downloads.pause_task": "暂停下载任务",
    "downloads.resume_task": "恢复下载任务",
    "downloads.delete_task": "移除下载任务",
    "downloads.retry_submission": "重新提交下载请求",
    "indexer.diagnose_readiness": "资源站就绪检查",
    "indexer.search_resources": "多站资源搜索",
    "indexer.submit_resource": "资源下载提交",
    "indexer.submit_resource_batch": "批量资源下载提交",
    "library.audit_episodes": "剧集完整性检查",
    "library.count_series_episodes": "本地剧集数量",
    "library.audit_library_episodes": "全库剧集巡检",
    "library.start_episode_audit": "后台全库剧集检查",
    "library.check_updates": "媒体更新检查",
    "library.missing_media_workflows": "缺集补库进度",
    "library.patrol_status": "缺集巡检状态",
    "library.patrol_policy": "缺集巡检策略",
    "library.set_patrol_policy": "缺集巡检策略修改",
    "library.trigger_patrol_now": "立即缺集巡检",
    "library.search": "媒体库搜索",
    "library.search_missing_episode_resources": "缺集资源搜索",
    "library.search_missing_season_resources": "缺季资源搜索",
    "local_media.diagnose": "本地媒体诊断",
    "local_media.source_summaries": "本地媒体来源列表",
    "local_media.get_source_summary": "本地媒体来源摘要",
    "local_media.set_source_trigger_enabled": "本地媒体来源触发器启停",
    "local_media.review_queue_summary": "本地媒体待确认摘要",
    "local_media.task_summaries": "本地媒体任务列表",
    "local_media.inspect_task": "本地媒体任务检查",
    "local_media.preview_task": "本地媒体整理预览",
    "local_media.retry_task": "本地媒体任务重试",
    "local_media.refresh_task_library": "本地媒体库精准刷新",
    "local_media.verify_task_library_visibility": "本地媒体入库可见核验",
    "local_media.history_summary": "本地媒体处理历史",
    "media.get_subscription_summary": "媒体追更订阅摘要",
    "media.get_subscription_policy": "媒体追更策略",
    "media.subscription_summaries": "媒体追更订阅列表",
    "media.subscription_updates": "媒体追更实时更新检查",
    "media.set_subscription_enabled": "媒体追更状态修改",
    "media.set_subscription_policy": "媒体追更策略修改",
    "media.create_subscription": "创建媒体追更",
    "media.delete_subscription": "删除媒体追更",
    "media.continue_watching": "继续观看",
    "media.preferences": "媒体偏好",
    "media.set_preferences": "媒体偏好修改",
    "media.clear_preferences": "媒体偏好清除",
    "media.today_summary": "今日媒体内容摘要",
    "media.subscription_notification_rule": "媒体追更通知规则",
    "media.set_subscription_notification_rule": "媒体追更通知规则修改",
    "media.reset_subscription_notification_rule": "媒体追更通知规则重置",
    "media_proxy.status_summary": "媒体反代状态",
    "media_proxy.playback_failure_summary": "媒体反代播放故障摘要",
    "media_proxy.test_instance": "媒体反代连接测试",
    "media_proxy.set_instance_enabled": "媒体反代实例启停",
    "media_proxy.restart_instance": "媒体反代实例重启",
    "local_media.scan_sources": "本地媒体来源扫描",
    "library.refresh_library": "媒体库精准刷新",
    "recognition.set_rule_enabled": "识别规则启停",
    "organize.audit_logs": "整理记录摘要",
    "rss.diagnose": "RSS 订阅诊断",
    "rss.entry_summaries": "RSS 条目列表",
    "rss.mark_entries": "RSS 条目标记",
    "rss.submit_entries_to_qb": "RSS 指定条目提交",
    "rss.get_subscription_summary": "RSS 单订阅摘要",
    "rss.subscription_summaries": "RSS 订阅列表",
    "rss.recent_activity": "RSS 最近下载统计",
    "rss.refresh_subscription": "RSS 订阅刷新",
    "rss.refresh_subscriptions": "RSS 订阅批量刷新",
    "rss.set_subscription_enabled": "RSS 订阅状态修改",
    "rss.set_refresh_interval": "RSS 刷新周期修改",
    "rss.delete_subscription": "RSS 订阅删除",
    "rss.retry_failed_to_qb": "RSS 失败条目重试",
    "rss.submit_pending_to_qb": "RSS 待处理条目提交",
    "strm.diagnose": "STRM 配置诊断",
    "strm.run_history": "STRM 运行历史",
    "strm.retry_failures": "STRM 失败项重试",
    "strm.run_once": "STRM 手动同步",
    "strm.schedule_policy": "STRM 调度策略",
    "strm.set_schedule_policy": "STRM 调度策略修改",
    "strm.status": "STRM 同步状态",
    "strm.triage_failures": "STRM 失败分诊",
    "guangya.connection_status": "光鸭连接检查",
    "guangya.directory.inspect": "光鸭目录观察",
    "guangya.change_plan.preview": "光鸭声明式改名预览",
    "guangya.change_plan.execute": "光鸭声明式改名执行",
    "guangya.media_hygiene.preview": "光鸭媒体名称清理预览",
    "guangya.media_hygiene.execute": "光鸭媒体名称清理",
    "guangya.rename.preview": "光鸭重命名预览",
    "guangya.rename.execute": "光鸭重命名执行",
    "guangya.directory_scrape.inspect": "光鸭刮削检查",
    "guangya.directory_scrape.search": "光鸭刮削匹配搜索",
    "guangya.directory_scrape.preview": "光鸭刮削预览",
    "guangya.directory_scrape.run": "光鸭刮削执行",
    "guangya.organize.preview": "光鸭整理预检",
    "guangya.organize.schedule_policy": "光鸭定时整理策略",
    "guangya.organize.set_schedule_policy": "光鸭定时整理策略修改",
    "guangya.organize.clean_empty": "光鸭空目录清理",
    "guangya.organize.cleanup.preview": "光鸭整理残留清理预览",
    "guangya.organize.cleanup.classify": "光鸭整理残留逐项复核",
    "guangya.organize.cleanup.execute": "光鸭整理残留清理",
    "guangya.organize.run_once": "光鸭整理任务",
    "guangya.organize.status": "光鸭整理状态",
    "guangya.organize.stop": "停止光鸭整理任务",
    "web.search": "网页搜索",
    "workspace.briefing": "系统运行简报",
    "workspace.health": "系统健康检查",
    "workspace.next_actions": "下一步建议",
    "workspace.search": "全局搜索",
    "workspace.todo": "待处理事项",
    "agent.job_status": "后台检查进度",
    "agent.cancel_job": "取消后台检查",
}

_PUBLIC_SOURCE_LABELS: dict[str, str] = {
    "automation": "自动化链路",
    "configuration": "项目配置",
    "download_verification": "下载后入库复核",
    "downloads": "下载队列",
    "indexer": "资源站",
    "indexers": "资源站",
    "library_patrol": "缺集巡检",
    "local_media": "本地媒体",
    "media_servers": "媒体服务器",
    "organize": "云盘整理",
    "guangya_connection": "光鸭账号",
    "server_configuration": "服务配置",
    "rss": "RSS 订阅",
    "strm": "STRM 同步",
    "workspace": "工作区",
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

_BANNED_KEYS = frozenset({
    "action_key", "arguments", "authorization", "candidate_id", "confirmation_id",
    "content", "credential", "credentials", "handle", "hash", "html", "id", "key",
    "magnet", "next_tool", "password", "path", "payload", "prompt_raw", "raw",
    "request_id", "resource_id", "secret", "target_tool", "token", "tool",
    "tool_call", "tool_name", "uri", "url",
})
_BANNED_KEY_FRAGMENTS = (
    "api_key", "access_token", "refresh_token", "confirmation", "credential",
    "download_url", "file_path", "full_path", "magnet", "password", "secret",
    "torrent_url",
)
# 发送给外部模型的数据字段必须显式允许，并转换为公开中文名称。未知字段一律丢弃。
_PUBLIC_DATA_KEYS: dict[str, str] = {
    "affected": "影响数量",
    "entry_number": "条目编号",
    "processed": "已处理",
    "published_at": "发布时间",
    "created_at": "记录时间",
    "failure_code": "失败分类",
    "failure_retryable": "可安全重试",
    "run_number": "运行序号",
    "run_count": "运行数量",
    "runs": "运行历史",
    "failure_context": "失败上下文",
    "queue_context": "队列上下文",
    "change_queue": "变化队列",
    "metadata_queue": "元数据队列",
    "elapsed_seconds": "耗时（秒）",
    "trigger_type": "触发方式",
    "started_at": "开始时间",
    "finished_at": "结束时间",
    "mode": "运行模式",
    "stats": "统计",
    "open": "待处理",
    "retrying": "重试中",
    "resolved": "已解决",
    "active_repeated": "重复失败",
    "active_retried": "已进入重试",
    "by_action": "按动作统计",
    "generate": "STRM 生成",
    "metadata": "元数据处理",
    "dirty": "有新变化",
    "retry_wait": "等待重试",
    "ttl_seconds": "上下文有效期（秒）",
    "max_runs": "最多运行记录",
    "max_candidates": "最多候选数量",
    "limits": "返回限制",
    "max_items": "最多返回数量",
    "entry_count": "条目数量",
    "selected_count": "已选择数量",
    "skip_reason": "跳过原因",
    "score": "匹配度",
    "target": "提交目标",
    "claimed": "已认领",
    "generated": "已生成",
    "created": "已创建",
    "updated": "已更新",
    "identified_video_count": "已识别视频",
    "unidentified_video_count": "未识别视频",
    "video_rename_count": "视频改名",
    "companion_rename_count": "伴随文件改名",
    "directory_rename_count": "目录改名",
    "metadata_enriched_count": "MetaTube 补全",
    "proposed_operation_count": "提议操作数量",
    "observation_ref": "观察编号",
    "object_ref": "对象引用",
    "object_name": "名称",
    "kind": "类型",
    "extension": "扩展名",
    "size": "大小（字节）",
    "location": "相对位置",
    "page": "页码",
    "page_size": "每页数量",
    "recursive": "递归读取",
    "trigger_strm": "改名后核对 STRM",
    "scope_count": "范围数量",
    "metadata_generated": "已生成元数据",
    "metadata_queued": "已排队元数据",
    "metadata_skipped": "已跳过元数据",
    "metadata_failed": "元数据失败",
    "cleaned": "已清理",
    "metadata_cleaned": "已清理元数据",
    "empty_dirs_cleaned": "已清理空目录",
    "empty_dir_count": "真空目录",
    "residual_dir_count": "垃圾残留目录",
    "quarantine_file_count": "待隔离文件",
    "preserved_dir_count": "安全保留目录",
    "unsupported_empty_dir_count": "因 Provider 能力保留的空目录",
    "reviewed_count": "已复核候选",
    "kept_count": "明确保留候选",
    "undecided_count": "待复核候选",
    "deferred_candidate_count": "下批复核候选",
    "deferred_empty_dir_count": "下批空目录",
    "sample_directories": "目录示例",
    "review_summaries": "候选复核明细",
    "quarantine_instead_of_delete": "非空目录仅隔离",
    "move": "移动",
    "skip": "跳过",
    "conflict": "冲突",
    "videos": "视频文件",
    "subtitles": "字幕文件",
    "images": "图片文件",
    "companions": "伴随文件",
    "other": "其他文件",
    "source_title": "来源标题",
    "mapping_confirmed": "映射已确认",
    "candidate_count": "候选数量",
    "candidates": "候选列表",
    "candidate_number": "候选序号",
    "candidate_title": "候选标题",
    "candidate_year": "候选年份",
    "suggested_query": "建议搜索词",
    "release_date": "上映日期",
    "overview": "简介",
    "requires_manual_match": "需要人工匹配",
    "manual_match_reason": "人工匹配原因",
    "scope_type": "检查范围",
    "continuation_available": "可继续操作",
    "plan_count": "视频计划数量",
    "companion_count": "伴随文件计划数量",
    "conflict_count": "冲突数量",
    "actions": "动作统计",
    "cloud_write": "已写入云盘",
    "queued": "已排队",
    "queue_position": "队列位置",
    "replayed": "复用已有提交",
    "as_of": "检查截止日期",
    "active": "活动中",
    "active_count": "活动数量",
    "area": "区域",
    "areas": "区域明细",
    "attention": "需关注",
    "attention_count": "需关注数量",
    "attention_total": "需关注总数",
    "available": "可用",
    "available_names": "可选来源",
    "blocked": "受阻数量",
    "cached": "使用缓存",
    "candidate_summaries": "候选资源摘要",
    "completed": "已完成",
    "codec": "编码",
    "cancelled_admissions": "已取消的待提交任务",
    "cancelled_runs": "已取消的检查任务",
    "configured": "已配置",
    "configured_total": "已配置来源数量",
    "cron": "计划表达式",
    "cron_valid": "计划表达式有效",
    "current_policy": "当前策略",
    "requested_policy": "目标策略",
    "policy": "策略",
    "connected": "已连接",
    "count": "数量",
    "counts": "统计",
    "deleted_entries": "删除的本地条目数",
    "disabled": "已关闭",
    "domain": "范围",
    "enabled": "已启用",
    "enabled_count": "启用数量",
    "entries": "条目",
    "entry_counts": "条目统计",
    "episode": "集数",
    "episodes": "剧集",
    "first_episode": "首集编号",
    "error_count": "错误数量",
    "errors": "错误",
    "expired_candidates": "已失效的候选资源",
    "failed": "失败",
    "failed_count": "失败数量",
    "failures": "失败项",
    "has_more": "还有更多",
    "healthy": "健康",
    "incomplete_count": "不完整数量",
    "items": "项目",
    "ignored_specials": "未计入特别篇",
    "ignored_unknown": "未编号条目",
    "jobs": "近期任务",
    "interval_hours": "巡检间隔（小时）",
    "max_series": "每批检查上限",
    "media_type": "媒体类型",
    "matched_source_count": "匹配来源数量",
    "missing_count": "缺失数量",
    "missing_episode_count": "缺失集数",
    "missing_sample": "缺失示例",
    "missing_episodes": "缺失剧集",
    "missing_seasons": "缺失季度",
    "instance_count": "实例数量",
    "instance_number": "实例序号",
    "instances": "实例明细",
    "disabled_count": "停用数量",
    "running_count": "运行数量",
    "stopped_count": "已停止数量",
    "server_type": "媒体服务类型",
    "runtime_status": "运行状态",
    "connection_status": "连接状态",
    "status_code": "响应状态码",
    "latency_ms": "响应耗时（毫秒）",
    "current_enabled": "当前启用状态",
    "requested_enabled": "目标启用状态",
    "rule_type": "规则类型",
    "network_accessed": "已联网检查",
    "filesystem_accessed": "已访问文件系统",
    "next_run": "下次运行",
    "notify_enabled": "通知已启用",
    "not_configured": "未配置数量",
    "online": "在线",
    "operation": "操作",
    "operation_ref": "操作编号",
    "task": "任务",
    "queue": "排队状态",
    "schedule": "调度计划",
    "pending": "待处理",
    "patrol_status": "检查结论",
    "pending_count": "待处理数量",
    "policies": "安全策略",
    "policy_count": "策略数量",
    "label": "策略",
    "display_value": "当前值",
    "managed_by_environment": "由环境管理",
    "managed_fields": "由环境管理的项目",
    "changed_fields": "变更项目",
    "current_value": "当前值",
    "requested_value": "目标值",
    "effects": "影响",
    "provider": "来源",
    "providers": "来源明细",
    "reachable": "可连接",
    "refresh_interval_minutes": "刷新周期（分钟）",
    "scan_interval_minutes": "扫描间隔（分钟）",
    "stable_seconds": "文件稳定等待（秒）",
    "requested": "请求数量",
    "rename_count": "预计重命名",
    "conflict_count": "名称冲突",
    "no_change_count": "无需变更",
    "scanned_items": "扫描项目",
    "scanned_dirs": "扫描目录",
    "sample_changes": "名称变更示例",
    "rollback_available": "支持回滚",
    "cloud_write": "已执行云端写入",
    "reused": "复用已有任务",
    "requires_manual": "需要人工处理",
    "result": "结果",
    "results": "结果明细",
    "query": "查询作品",
    "original_title": "原始片名",
    "rating": "豆瓣评分",
    "rating_source": "评分来源",
    "source_method": "查询方式",
    "web_fallback_used": "已使用网页补查",
    "returned": "返回数量",
    "release_source": "发布来源",
    "resolution": "分辨率",
    "runtime_refreshed": "运行时已刷新",
    "runtime_scope": "生效范围",
    "running": "运行中",
    "season": "季度",
    "season_count": "季度数量",
    "seasons": "季度分布",
    "severity": "严重程度",
    "source": "来源区域",
    "site_name": "资源站",
    "size_text": "体积",
    "seeders": "做种数",
    "position": "候选序号",
    "source_count": "来源数量",
    "source_number": "来源编号",
    "sources": "来源明细",
    "local_episode_count": "本地普通集数",
    "last_episode": "末集编号",
    "count_definition": "统计口径",
    "scan_enabled": "目录扫描已启用",
    "scan_effective": "目录扫描实际生效",
    "scan_enabled_count": "目录扫描生效数量",
    "stale": "过期数量",
    "state": "状态",
    "status": "状态",
    "status_filter": "状态筛选",
    "schedule_state": "调度状态",
    "subscription_name": "订阅名称",
    "subscription_number": "订阅编号",
    "watchlist_number": "收藏编号",
    "summary": "摘要",
    "task_status": "任务状态",
    "task_status_label": "任务状态说明",
    "target_count": "目标数量",
    "target_categories": "目标分类",
    "trigger": "触发方式",
    "title": "标题",
    "submitted": "已提交",
    "sent": "已发送",
    "subscriptions": "订阅",
    "total": "总数",
    "truncated": "结果已截断",
    "limit": "返回上限",
    "origin": "记录来源",
    "scope": "统计范围",
    "by_origin": "按来源统计",
    "by_status": "按状态统计",
    "by_trigger": "按触发方式统计",
    "age_buckets": "按时间范围统计",
    "records": "近期记录",
    "guangya": "光鸭整理",
    "local": "本地整理",
    "manual": "需要人工处理",
    "processing": "处理中",
    "success": "成功",
    "reverted": "已回退",
    "qb_completed": "qB 下载完成",
    "scan": "目录扫描",
    "under_1h": "1 小时内",
    "1h_to_24h": "1 至 24 小时",
    "1d_to_7d": "1 至 7 天",
    "over_7d": "7 天以上",
    "unknown": "时间未知",
    "updated_at": "更新时间",
    "cron_configured_but_not_scheduled": "Cron 未生效",
    "invalid_last_refreshed_at": "刷新时间异常",
    "pending_recent": "近期待处理",
    "pending_backlog": "长期待处理",
    "submitting": "提交中",
    "submitting_in_flight": "正常提交中",
    "stale_submitting": "长期提交中",
    "downloaded": "已下载",
    "downloaded_last_24h": "近 24 小时下载次数",
    "skipped": "已跳过",
    "terminal": "已结束",
    "unknown_or_inconsistent": "未知或不一致",
    "unavailable": "不可用数量",
    "update_count": "更新数量",
    "updates": "更新",
    "updates_available": "发现更新",
    "updates_available_count": "发现更新数量",
    "verification": "核验结果",
    "waiting_count": "等待数量",
    "waiting_total": "等待总数",
    "warnings": "警告",
    "cancel_requested": "已请求取消",
    "cancelled": "已取消",
    "checked_series_count": "已检查剧集数",
    "findings": "检查结果",
    "findings_truncated": "检查结果已截断",
    "inconclusive_count": "暂无法确认数量",
    "progress_current": "当前进度",
    "progress_percent": "进度百分比",
    "progress_total": "总进度",
    "unmapped_series_count": "未映射剧集数",
    "server": "媒体服务器",
    "server_label": "媒体服务器",
    "user_selection": "媒体用户选择方式",
    "episode_title": "单集标题",
    "progress": "播放进度百分比",
    "last_played": "最近播放时间",
    "preferred_server": "首选媒体服务器",
    "preferred_download_target": "首选下载目标",
    "explicit": "已显式设置",
    "current": "当前设置",
    "proposed": "拟议设置",
    "defaults": "默认设置",
    "local_date": "本地日期",
    "timezone": "时区",
    "subscription_runs": "追更检查事件",
    "local_media_tasks": "整理入库事件",
    "rss_entries": "RSS 内容事件",
    "downloads": "下载事件",
    "content_titles": "内容标题",
    "event_count": "内容事件数量",
    "subscription_enabled": "追更订阅已启用",
    "subscription_status": "追更订阅状态",
    "notify_on_missing": "缺集时通知",
    "notify_on_satisfied": "已满足时通知",
    "notify_on_error": "检查异常时通知",
    "window_hours": "统计窗口（小时）",
    "total_recorded": "已记录请求数量",
    "cache_hits": "缓存命中数量",
    "average_latency_ms": "平均耗时（毫秒）",
    "failure_stages": "失败阶段",
    "route_classes": "路由类别",
    "stage": "阶段",
    "route": "路由类别",
    "last_seen_at": "最近记录时间",
    "coverage": "覆盖范围",
    "missing": "缺集",
    "satisfied": "已满足",
    "error": "异常",
    "year": "年份",
}
_PUBLIC_TEXT_VALUE_KEYS = frozenset({
    "area", "as_of", "changed_fields", "cron", "current_value", "display_value", "domain", "effects", "episode",
    "label", "managed_fields", "media_type", "next_run", "provider", "requested_value", "runtime_scope", "season", "severity",
    "schedule_state", "source", "state", "status", "operation", "operation_ref", "status_filter", "summary", "patrol_status", "task_status", "task_status_label",
    "title", "year", "origin", "scope", "updated_at", "server_type", "runtime_status", "connection_status", "rule_type",
    "target_categories", "trigger", "subscription_name", "query", "original_title", "rating_source", "source_method",
    "codec", "release_source", "resolution", "site_name", "size_text",
    "server", "server_label", "user_selection", "episode_title", "last_played",
    "preferred_server", "preferred_download_target", "local_date", "timezone",
    "content_titles", "candidate_summaries", "review_summaries", "published_at", "created_at", "failure_code", "trigger_type", "started_at", "finished_at", "mode", "sample_changes",
    "source_title", "candidate_title", "candidate_year", "suggested_query", "release_date", "overview", "manual_match_reason", "scope_type",
    "skip_reason", "target", "observation_ref", "object_ref", "object_name",
    "kind", "extension", "location",
    "subscription_status", "stage", "route", "last_seen_at", "coverage",
    "available_names",
})
_MAX_DEPTH = 4
_MAX_MAPPING_ITEMS = 24
_MAX_SEQUENCE_ITEMS = 16
_MAX_PROJECTED_NODES = 96
_MAX_REQUEST_BYTES = 10_240
_RESOURCE_CANDIDATE_KEYS = frozenset({
    "items",
    "candidates",
    "selected",
    "alternatives",
    "recommendation",
})
_GUIDANCE_PREFIX_RE = re.compile(
    r"^(?:可以(?:继续)?(?:说|询问)|可(?:继续|(?:先|再次|继续)?(?:询问|说|尝试))|"
    r"建议(?:先|继续)?|下一步(?:可以)?)[：:\s]*"
)
_GUIDANCE_TOOL_DETAIL_RE = re.compile(
    r"^可调用\s+(.+?)\s+查看(?:其)?(?:安全)?详情[。.]?$",
    re.IGNORECASE,
)
_READ_GUIDANCE_PREFIXES = (
    "检查", "查看", "诊断", "搜索", "查找", "核对", "预览", "列出", "确认状态",
    "在网上找", "帮我看看", "继续检查", "继续查看", "重新检查", "重新诊断",
    "重新核对", "再次检查", "说明", "解释",
)
_WRITE_GUIDANCE_MARKERS = (
    "提交", "开始下载", "下载第", "重试", "刷新订阅", "保存设置", "修改配置",
    "启用", "关闭", "停止", "清理", "开始整理", "执行同步", "推送", "删除",
    "设置为", "切换为", "立即执行", "暂停", "恢复",
)
_INFORMATIONAL_GUIDANCE_MARKERS = (
    "不会自动", "不会立即", "不会触发", "不会删除", "不会回滚",
    "只代表", "只可使用一次", "每次请求会消耗", "结果仅按",
    "确认前请核对", "请由管理员", "请在部署环境", "请先在设置页",
    "请先在网盘整理页面", "网页内容来自外部来源", "外部内容仅供参考",
    "公开信息仅供参考", "请核验可信度", "请以官方信息为准",
)
_INFORMATIONAL_GUIDANCE_KINDS = {"notice", "info", "advisory"}

_PUBLIC_STATUS_LABELS: dict[str, str] = {
    "accepted": "已提交",
    "active": "运行中",
    "answered": "已回复",
    "attention": "需关注",
    "blocked": "依赖受阻",
    "cancelled": "已取消",
    "clarification_required": "需要补充信息",
    "completed": "已完成",
    "conflict": "状态冲突",
    "degraded": "能力受限",
    "disabled": "已停用",
    "empty": "暂无结果",
    "failed": "执行失败",
    "found": "已找到",
    "healthy": "状态正常",
    "incomplete": "配置不完整",
    "inconclusive": "暂无法确认",
    "no_changes": "无需变更",
    "not_configured": "尚未配置",
    "not_found": "未找到",
    "not_run": "尚未运行",
    "outcome_unknown": "结果待确认",
    "partial": "部分完成",
    "pending": "等待执行",
    "ready": "已就绪",
    "review_required": "需人工核对",
    "retry_wait": "等待重试",
    "running": "运行中",
    "selection_required": "需要选择目标",
    "success": "已完成",
    "succeeded": "已完成",
    "unavailable": "暂不可用",
    "unknown": "暂无法确认",
    "unsupported": "暂不支持",
    "updates_available": "发现需要关注的内容",
    "up_to_date": "已是最新",
    "waiting": "等待中",
    "warning": "需要留意",
}
_PUBLIC_STATUS_SUCCESS = frozenset({
    "answered", "completed", "found", "healthy", "no_changes", "ready",
    "success", "succeeded", "up_to_date",
})
_PUBLIC_STATUS_ATTENTION = frozenset({
    "attention", "blocked", "cancelled", "conflict", "degraded", "disabled", "empty",
    "incomplete", "inconclusive", "not_configured", "not_found",
    "not_run", "outcome_unknown", "partial", "review_required", "selection_required",
    "updates_available", "warning", "clarification_required", "unknown",
})
_PUBLIC_STATUS_PROGRESS = frozenset({
    "accepted", "active", "pending", "retry_wait", "running", "waiting",
})
_PUBLIC_STATUS_ERROR = frozenset({"failed", "unavailable", "unsupported", "error"})
_PUBLIC_STATUS_KEYS = frozenset(_PUBLIC_STATUS_LABELS)
_PUBLIC_STATUS_VALUE_KEYS = frozenset({
    "status", "state", "patrol_status", "task_status", "schedule_state",
    "runtime_status", "connection_status",
})


def public_tool_label(tool_name: object) -> str:
    """把内部工具标识转换为面向用户的稳定名称。"""
    normalized = str(tool_name or "").strip()
    return _PUBLIC_TOOL_LABELS.get(normalized, "MediaFlux 检查")


def public_followup_prompt(source: object) -> str:
    """返回用户可以直接发送的自然语言追问，不暴露内部工具协议。"""
    normalized = str(source or "").strip().casefold()
    return _PUBLIC_FOLLOWUP_PROMPTS.get(normalized, "继续检查需要关注的区域")


def _decode_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.replace("\x00", " ").split()).strip()


def _decode_multiline_text(value: object) -> str:
    """解码公开文本，同时保留段落和列表所需的换行。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    lines: list[str] = []
    blank = False
    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split()).strip()
        if not line:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(line)
    return "\n".join(lines).strip()


def _remove_internal_tool_guidance(text: str) -> str:
    """移除面向实现者的工具调用说明，同时保留公开段落与列表结构。"""
    cleaned = _INTERNAL_TOOL_GUIDANCE_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"(?:下一步(?:建议)?|建议)\s*[:：]\s*$", "", cleaned).strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    blank = False
    for raw_line in cleaned.split("\n"):
        line = " ".join(raw_line.split()).strip()
        if not line:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(line)
    return "\n".join(lines).strip()


def _display_chunks(value: str, *, max_length: int = 150) -> list[str]:
    """按中文从句和硬长度切分长句，防止 Telegram/Web 出现整墙文本。"""
    text = str(value or "").strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]
    clauses = [match.group(0) for match in _DISPLAY_CLAUSE_RE.finditer(text)] or [text]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        pending = clause
        while len(pending) > max_length:
            if current:
                chunks.append(current.strip())
                current = ""
            cut = max_length
            whitespace = pending.rfind(" ", 0, max_length + 1)
            if whitespace >= max_length // 2:
                cut = whitespace
            chunks.append(pending[:cut].strip())
            pending = pending[cut:].strip()
        if not pending:
            continue
        if current and len(current) + len(pending) > max_length:
            chunks.append(current.strip())
            current = ""
        current += pending
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _replace_internal_field(match: re.Match[str]) -> str:
    value = match.group(0)
    if value.casefold() in _PUBLIC_STATUS_KEYS:
        return value
    return "内部状态"


def _replace_internal_identifiers_in_text(text: str) -> str:
    text = _remove_internal_tool_guidance(text)
    for tool_name, label in sorted(_PUBLIC_TOOL_LABELS.items(), key=lambda item: -len(item[0])):
        text = text.replace(tool_name, label)
    text = _INTERNAL_TOOL_RE.sub("内部检查", text)
    text = _INTERNAL_CAMEL_RE.sub("内部状态", text)
    text = _INTERNAL_KEBAB_RE.sub("内部检查", text)
    return _INTERNAL_FIELD_RE.sub(_replace_internal_field, text)


def replace_internal_identifiers(value: object) -> str:
    """将已知内部工具名替换为公开名称，并去除未知 dotted identifiers。"""
    return _replace_internal_identifiers_in_text(_decode_text(value))


def is_public_text_safe(value: object) -> bool:
    """判断原始文本能否不经改写直接公开展示。"""
    text = _decode_text(value)
    if not text:
        return False
    path_scan_text = _TRANSFER_RATE_RE.sub("传输速度", text)
    if (
        _CONTROL_RE.search(text)
        or contains_sensitive_credential(text)
        or _URI_RE.search(text)
        or _P2P_RE.search(text)
        or _WINDOWS_PATH_RE.search(path_scan_text)
        or _UNIX_PATH_RE.search(path_scan_text)
        or _RELATIVE_PATH_RE.search(path_scan_text)
        or _CONTEXTUAL_PATH_RE.search(path_scan_text)
        or _UNICODE_FILE_PATH_RE.search(path_scan_text)
    ):
        return False
    # 流式输出一旦发送便无法撤回，因此内部标识不能依赖事后替换。
    return replace_internal_identifiers(text) == text


def smooth_sanitize_public_stream_text(value: object) -> str | None:
    """流式公开文本：凭据/多行控制符与链接致命，路径平滑替换。"""
    text = str(value or "")
    if not text:
        return ""
    if (
        _MULTILINE_CONTROL_RE.search(text)
        or contains_sensitive_credential(text)
        or _URI_RE.search(text)
        or re.search(r"(?i)\b(?:https?|ftp|file)\s*://", text)
        or _P2P_RE.search(text)
    ):
        return None
    text = _replace_internal_identifiers_in_text(text)
    text = _WINDOWS_PATH_RE.sub("[路径已隐藏]", text)
    text = _UNIX_PATH_RE.sub("[路径已隐藏]", text)
    text = _RELATIVE_PATH_RE.sub("[路径已隐藏]", text)
    text = _CONTEXTUAL_PATH_RE.sub("[路径已隐藏]", text)
    text = _UNICODE_FILE_PATH_RE.sub("[路径已隐藏]", text)
    return text


def public_stream_stable_prefix_length(value: object) -> int:
    """返回可立即公开的稳定前缀长度，暂存可能继续扩展的 ASCII token。"""
    text = str(value or "")
    index = len(text)
    while index > 0:
        char = text[index - 1]
        if ord(char) < 128 and not char.isspace():
            index -= 1
            continue
        break
    return index


def public_stream_readable_prefix_length(value: object) -> int:
    """返回适合立即展示的完整句前缀，避免草稿停在半句话中。"""
    text = str(value or "")
    stable_length = public_stream_stable_prefix_length(text)
    if stable_length <= 0:
        return 0
    candidate = text[:stable_length]
    boundary = max(candidate.rfind(mark) for mark in ("。", "！", "？", "!", "?", "；", ";", "\n"))
    return boundary + 1 if boundary >= 0 else 0


def sanitize_untrusted_filename(value: object, *, limit: int = 255) -> str:
    """保留不可信文件名原貌，避免 dotted-title 被误当内部工具名。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip()
    if not text or _CONTROL_RE.search(text) or contains_sensitive_credential(text):
        return ""
    text = _URI_RE.sub("[网址]", text)
    text = _P2P_RE.sub("[链接]", text)
    return text[: max(1, int(limit))].rstrip()


def sanitize_public_text(value: object, *, limit: int = 600) -> str:
    """返回可展示/可发送给模型的文本；不安全文本直接舍弃。"""
    text = replace_internal_identifiers(value)
    if not text:
        return ""
    path_scan_text = _TRANSFER_RATE_RE.sub("传输速度", text)
    if (
        _CONTROL_RE.search(text)
        or contains_sensitive_credential(text)
        or _URI_RE.search(text)
        or _P2P_RE.search(text)
        or _WINDOWS_PATH_RE.search(path_scan_text)
        or _UNIX_PATH_RE.search(path_scan_text)
        or _RELATIVE_PATH_RE.search(path_scan_text)
        or _CONTEXTUAL_PATH_RE.search(path_scan_text)
        or _UNICODE_FILE_PATH_RE.search(path_scan_text)
    ):
        return ""
    return text[: max(1, int(limit))].rstrip()


def sanitize_public_multiline_text(value: object, *, limit: int = 1200) -> str:
    """安全保留短段落与项目列表，并移除终端不会解析的 Markdown 符号。"""
    decoded = _decode_multiline_text(value)
    if not decoded:
        return ""

    # 兼容旧会话和少数模型把 Markdown 栏目、列表全部压在同一行的输出。
    # 固定栏目本身没有用户价值，转换为段落边界后直接移除。
    decoded = _INLINE_DISPLAY_HEADING_RE.sub("\n\n", decoded)
    decoded = _INLINE_MARKDOWN_BULLET_RE.sub("\n- ", decoded)
    decoded = _INLINE_NUMBERED_ITEM_RE.sub("\n- ", decoded)

    normalized_lines: list[str] = []
    previous_blank = False
    for raw_line in decoded.split("\n"):
        if not raw_line.strip():
            if normalized_lines and not previous_blank:
                normalized_lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        line = re.sub(r"^#{1,6}\s+", "", raw_line)
        line = re.sub(r"^[*+]\s+", "- ", line)
        line = line.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
        line = _FIXED_DISPLAY_HEADING_RE.sub("", line)
        line = _PUBLIC_INTERNAL_ASSIGNMENT_RE.sub("", line)
        line = _replace_internal_identifiers_in_text(line)
        line = _PUBLIC_INTERNAL_MARKER_RE.sub("", line).strip()
        if not line:
            continue
        bullet = re.match(r"^[-•]\s+(.+)$", line)
        if bullet:
            normalized_lines.append(f"- {bullet.group(1).strip()}")
            continue

        # 尊重 LLM 原始段落边界。只对异常超长的单行做硬切分，避免把正常
        # 两三句话重新拼成报告式分块，或把模型有意留下的空行压掉。
        pieces = _display_chunks(line, max_length=150)
        for index, piece in enumerate(pieces):
            if index and normalized_lines and normalized_lines[-1] != "":
                normalized_lines.append("")
            normalized_lines.append(piece)

    text = "\n".join(normalized_lines).strip()
    if not text:
        return ""
    scan_text = unicodedata.normalize("NFKC", text)
    path_scan_text = _TRANSFER_RATE_RE.sub("传输速度", scan_text)
    if (
        _MULTILINE_CONTROL_RE.search(scan_text)
        or contains_sensitive_credential(scan_text)
        or _URI_RE.search(scan_text)
        or _P2P_RE.search(scan_text)
        or _WINDOWS_PATH_RE.search(path_scan_text)
        or _UNIX_PATH_RE.search(path_scan_text)
        or _RELATIVE_PATH_RE.search(path_scan_text)
        or _CONTEXTUAL_PATH_RE.search(path_scan_text)
        or _UNICODE_FILE_PATH_RE.search(path_scan_text)
    ):
        return ""
    return text[: max(1, int(limit))].rstrip()


def _is_informational_guidance(prompt: str) -> bool:
    normalized = prompt.strip().lstrip("-•· ")
    return any(marker in normalized for marker in _INFORMATIONAL_GUIDANCE_MARKERS)


def _guidance_kind(prompt: str) -> str:
    normalized = prompt.strip().lstrip("-•· ")
    if any(marker in normalized for marker in _WRITE_GUIDANCE_MARKERS):
        return "draft"
    if normalized.startswith(_READ_GUIDANCE_PREFIXES):
        return "read"
    if normalized.endswith(("吗", "吗？", "情况", "状态", "原因", "怎么处理")):
        return "read"
    return "draft"


def _public_suggestion_prompt(item: object, *, limit: int = 180) -> str:
    raw_prompt = item.get("prompt") if isinstance(item, Mapping) else item
    prompt = sanitize_public_text(raw_prompt, limit=limit)
    tool_detail_match = _GUIDANCE_TOOL_DETAIL_RE.match(prompt)
    if tool_detail_match:
        prompt = f"查看{tool_detail_match.group(1).strip()}详情"
    return _GUIDANCE_PREFIX_RE.sub("", prompt).strip().strip("“”‘’\"'")


def _public_suggestion_is_notice(item: object, prompt: str) -> bool:
    raw_kind = (
        str(item.get("kind") or "").strip().casefold()
        if isinstance(item, Mapping)
        else ""
    )
    return raw_kind in _INFORMATIONAL_GUIDANCE_KINDS or _is_informational_guidance(prompt)


def project_public_guidance(
    items: object,
    *,
    force_kind: str | None = None,
    limit: int = 4,
) -> list[dict[str, str]]:
    """把建议投影为网页/TG 可理解的下一步；说明文字永远不会变成操作按钮。"""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = max(1, min(int(limit or 1), 6))
    for item in items:
        prompt = _public_suggestion_prompt(item)
        if not prompt or prompt in seen or _public_suggestion_is_notice(item, prompt):
            continue
        raw_kind = str(item.get("kind") or "") if isinstance(item, Mapping) else ""
        kind = force_kind or (raw_kind if raw_kind in {"read", "draft"} else _guidance_kind(prompt))
        # 只有明确只读语句才允许一键发送；强制 draft 可用于模型生成建议。
        if kind == "read" and _guidance_kind(prompt) != "read":
            kind = "draft"
        raw_label = item.get("label") if isinstance(item, Mapping) else prompt
        label = sanitize_public_text(raw_label, limit=72) or prompt
        if len(label) > 38:
            label = f"{label[:37].rstrip()}…"
        projected.append({"label": label, "prompt": prompt, "kind": kind})
        seen.add(prompt)
        if len(projected) >= max_items:
            break
    return projected


def project_public_notices(items: object, *, limit: int = 3) -> list[str]:
    """提取只读说明/数据边界，供 Web 与 Telegram 静态展示而不是再次发送。"""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    notices: list[str] = []
    max_items = max(1, min(int(limit or 1), 4))
    for item in items:
        prompt = _public_suggestion_prompt(item, limit=220)
        if (
            not prompt
            or prompt in notices
            or not _public_suggestion_is_notice(item, prompt)
        ):
            continue
        notices.append(prompt)
        if len(notices) >= max_items:
            break
    return notices


def _safe_key(value: object) -> tuple[str, str]:
    key = str(value or "").strip()
    lowered = key.casefold()
    if not key or lowered in _BANNED_KEYS or lowered.endswith("_id"):
        return "", ""
    if any(fragment in lowered for fragment in _BANNED_KEY_FRAGMENTS):
        return "", ""
    return lowered, _PUBLIC_DATA_KEYS.get(lowered, "")


def _safe_value(
    value: Any,
    *,
    depth: int = 0,
    source_key: str = "",
    budget: list[int] | None = None,
) -> Any:
    remaining = budget if budget is not None else [_MAX_PROJECTED_NODES]
    if depth > _MAX_DEPTH or value is None or remaining[0] <= 0:
        return None
    remaining[0] -= 1
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        if source_key in {"object_name", "location"}:
            return sanitize_untrusted_filename(value, limit=240) or None
        if source_key in {"candidate_summaries", "review_summaries"}:
            return sanitize_untrusted_filename(value, limit=520) or None
        if source_key == "operation_ref":
            operation_ref = value.strip().upper()
            return operation_ref if re.fullmatch(
                r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}", operation_ref
            ) else None
        normalized_status = value.strip().casefold()
        if source_key in _PUBLIC_STATUS_VALUE_KEYS and normalized_status in _PUBLIC_STATUS_LABELS:
            return _PUBLIC_STATUS_LABELS[normalized_status]
        text = sanitize_public_text(value, limit=240)
        if not text:
            return None
        if source_key not in _PUBLIC_TEXT_VALUE_KEYS:
            return None
        if source_key in {"source", "area", "domain"}:
            return _PUBLIC_SOURCE_LABELS.get(text, text)
        return text
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, raw_value in islice(value.items(), _MAX_MAPPING_ITEMS):
            internal_key, public_key = _safe_key(raw_key)
            if not public_key:
                continue
            safe_value = _safe_value(
                raw_value,
                depth=depth + 1,
                source_key=internal_key,
                budget=remaining,
            )
            if safe_value is not None and safe_value != {} and safe_value != []:
                projected[public_key] = safe_value
        return projected or None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        projected_items = []
        for item in islice(value, _MAX_SEQUENCE_ITEMS):
            safe_item = _safe_value(
                item,
                depth=depth + 1,
                source_key=source_key,
                budget=remaining,
            )
            if remaining[0] <= 0:
                complete_item = _safe_value(
                    item,
                    depth=depth + 1,
                    source_key=source_key,
                    budget=[_MAX_PROJECTED_NODES],
                )
                if safe_item != complete_item:
                    break
            if safe_item is not None and safe_item != {} and safe_item != []:
                projected_items.append(safe_item)
        return projected_items or None
    return None


def _without_resource_candidate_details(value: Any) -> Any:
    """复制工具结果，但排除候选资源明细。

    原始结果仍供网页和 Telegram 生成下载按钮使用；这里只限制发送给外部
    模型的解释上下文，避免模型复述超长种子标题或接触不透明结果标识。
    """
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key or "").strip().casefold() in _RESOURCE_CANDIDATE_KEYS:
                continue
            cleaned[key] = _without_resource_candidate_details(item)
        return cleaned
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return [_without_resource_candidate_details(item) for item in value]
    return value


def _resource_candidate_summaries(value: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    """提取不可执行的候选元数据，不发送原始标题、链接、hash 或结果标识。"""
    summaries: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if len(summaries) >= limit:
            return
        if isinstance(node, Mapping):
            title = unicodedata.normalize("NFKC", str(node.get("title") or ""))
            looks_like_candidate = bool(
                title
                and any(
                    key in node
                    for key in (
                        "result_id", "site_name", "site", "size_text", "seeders",
                    )
                )
            )
            if looks_like_candidate:
                summary: dict[str, Any] = {"position": len(summaries) + 1}
                site_name = sanitize_public_text(
                    node.get("site_name") or node.get("site"), limit=60
                )
                size_text = sanitize_public_text(
                    node.get("size_text") or node.get("size"), limit=40
                )
                if site_name:
                    summary["site_name"] = site_name
                if size_text:
                    summary["size_text"] = size_text
                seeders = node.get("seeders")
                if isinstance(seeders, int) and not isinstance(seeders, bool) and seeders >= 0:
                    summary["seeders"] = min(seeders, 10**9)
                resolution = re.search(
                    r"(?i)(?<![A-Za-z0-9])(?:2160p|1080p|720p|4k|8k)(?![A-Za-z0-9])",
                    title,
                )
                codec = re.search(
                    r"(?i)(?<![A-Za-z0-9])(?:av1|hevc|h\.?265|x265|h\.?264|x264)(?![A-Za-z0-9])",
                    title,
                )
                release_source = re.search(
                    r"(?i)(?<![A-Za-z0-9])(?:web[- .]?dl|webrip|blu[- .]?ray|bdrip)(?![A-Za-z0-9])",
                    title,
                )
                coordinates = re.search(
                    r"(?i)(?<![A-Za-z0-9])S0*([0-9]{1,3})\s*E(?:P)?0*([0-9]{1,3})(?![A-Za-z0-9])",
                    title,
                )
                if resolution:
                    summary["resolution"] = resolution.group(0).upper()
                if codec:
                    summary["codec"] = codec.group(0).upper().replace(".", "")
                if release_source:
                    summary["release_source"] = release_source.group(0).upper()
                if coordinates:
                    summary["season"] = int(coordinates.group(1))
                    summary["episode"] = int(coordinates.group(2))
                summaries.append(summary)
                return
            for child in node.values():
                visit(child)
                if len(summaries) >= limit:
                    return
        elif isinstance(node, Sequence) and not isinstance(
            node, (str, bytes, bytearray, memoryview)
        ):
            for child in node:
                visit(child)
                if len(summaries) >= limit:
                    return

    visit(value)
    return summaries


def _public_status_projection(status: object, *, ok: bool) -> dict[str, str]:
    normalized = str(status or "unknown").strip().casefold() or "unknown"
    if not ok and normalized in _PUBLIC_STATUS_ATTENTION | {"no_changes"}:
        key, tone = "attention", "warning"
    elif not ok:
        key, tone = "unavailable", "error"
    elif normalized in _PUBLIC_STATUS_SUCCESS:
        key, tone = "success", "good"
    elif normalized in _PUBLIC_STATUS_PROGRESS:
        key, tone = "in_progress", "neutral"
    elif normalized in _PUBLIC_STATUS_ATTENTION:
        key, tone = "attention", "warning"
    elif normalized in _PUBLIC_STATUS_ERROR:
        key, tone = "unavailable", "error"
    else:
        key, tone = ("success", "good") if ok else ("unavailable", "error")
    label = _PUBLIC_STATUS_LABELS.get(normalized)
    if not label:
        label = "已完成" if ok else "暂无法确认"
    return {"key": key, "label": label, "tone": tone}


def project_agent_result_for_user(result: Mapping[str, Any]) -> dict[str, Any]:
    """构造网页与 Telegram 共用的稳定公开展示层。

    原始 ``result`` 继续保留供兼容的专用组件使用；通用界面只应读取本投影，
    从而避免未知字段、内部状态名和服务端标识直接泄露到用户界面。
    """
    if not isinstance(result, Mapping):
        return {
            "version": 1,
            "status": {"key": "unavailable", "label": "暂无法确认", "tone": "error"},
            "summary": "这次请求没有返回可展示的结果。",
            "error": "",
            "details": {},
            "guidance": [],
            "notices": [],
        }

    ok = bool(result.get("ok"))
    status = _public_status_projection(result.get("status"), ok=ok)
    summary = sanitize_public_multiline_text(result.get("summary"), limit=1200)
    error = sanitize_public_multiline_text(result.get("error"), limit=420)
    if not summary:
        if status["key"] == "success":
            summary = "检查已完成。"
        elif status["key"] == "in_progress":
            summary = "任务正在处理中。"
        elif status["key"] == "attention":
            summary = "检查完成，但还有需要留意的内容。"
        else:
            summary = "这次请求暂时没有得到可确认的结果。"

    details = _safe_value(
        result.get("data"), depth=0, budget=[_MAX_PROJECTED_NODES]
    )
    suggestions = result.get("suggestions")
    guidance = project_public_guidance(suggestions, limit=3)
    notices = project_public_notices(suggestions, limit=3)
    return {
        "version": 1,
        "status": status,
        "summary": summary,
        "error": error if not ok else "",
        "details": details or {},
        "guidance": guidance,
        "notices": notices,
    }



def _fallback_read_plan_narrative(result: Mapping[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    steps = data.get("steps") if isinstance(data.get("steps"), Sequence) else []
    lines: list[str] = []
    completed = 0
    attention = 0
    for step in steps[:4]:
        if not isinstance(step, Mapping):
            continue
        step_result = (
            step.get("result") if isinstance(step.get("result"), Mapping) else {}
        )
        summary = sanitize_public_text(step_result.get("summary"), limit=220)
        if not summary:
            continue
        ok = step_result.get("ok") is True
        completed += int(ok)
        attention += int(not ok)
        label = public_tool_label(str(step.get("tool_name") or ""))
        lines.append(f"- {label}：{summary}")
    if not lines:
        return ""
    if attention:
        intro = f"本次核对已完成，{completed} 项得到结果，{attention} 项需要留意。"
    else:
        intro = f"本次核对已完成，{completed} 项检查都已返回结果。"
    return sanitize_public_multiline_text(
        intro + "\n\n" + "\n".join(lines), limit=1200
    )


def _fallback_discovery_narrative(
    tool_name: str, result: Mapping[str, Any]
) -> str:
    """把影视推荐/搜索结果收敛成无需模型也可直接阅读的短列表。"""
    if tool_name not in {"discovery.recommend", "discovery.search"}:
        return ""
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    raw_items = data.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        return ""

    lines: list[str] = []
    media_type = sanitize_public_text(data.get("media_type"), limit=20)
    for item in raw_items[:8]:
        if not isinstance(item, Mapping):
            continue
        title = sanitize_public_text(
            item.get("title") or item.get("标题"), limit=100
        )
        if not title:
            continue
        year = sanitize_public_text(item.get("year") or item.get("年份"), limit=8)
        release_date = sanitize_public_text(
            item.get("release_date") or item.get("上映日期"), limit=16
        )
        rating_value = item.get("rating")
        if rating_value is None or rating_value == "":
            rating_value = item.get("豆瓣评分")
        rating = ""
        try:
            numeric_rating = float(rating_value)
        except (TypeError, ValueError):
            numeric_rating = 0.0
        if numeric_rating > 0:
            rating = f"评分 {numeric_rating:g}"
        meta = " · ".join(value for value in (year, release_date, rating) if value)
        lines.append(f"- 《{title}》" + (f"：{meta}" if meta else ""))

    if not lines:
        return ""
    if media_type not in {"movie", "tv"}:
        first = raw_items[0] if raw_items and isinstance(raw_items[0], Mapping) else {}
        media_type = sanitize_public_text(
            first.get("media_type") or first.get("媒体类型"), limit=20
        )
    label = "剧集" if media_type == "tv" else "电影"
    if tool_name == "discovery.search":
        query = sanitize_public_text(data.get("query"), limit=120)
        intro = (
            f"围绕“{query}”找到以下{label}:"
            if query else f"找到以下{label}:"
        )
    else:
        intro = f"为你整理了以下{label}推荐:"
    return sanitize_public_multiline_text(
        intro + "\n\n" + "\n".join(lines), limit=1400
    )


def build_public_fallback_presentation(
    response: Mapping[str, Any],
) -> dict[str, Any] | None:
    """在 Provider 叙述不可用时，用已脱敏事实生成 Web/TG 共用的自然降级答复。"""
    if not isinstance(response, Mapping):
        return None
    mode = str(response.get("mode") or "").strip().casefold()
    if mode in {"confirmation_required", "confirmed_action"}:
        return None
    result = response.get("result")
    if not isinstance(result, Mapping):
        return None
    display = project_agent_result_for_user(result)
    narrative = ""
    tool_call = response.get("tool_call")
    tool_name = (
        str(tool_call.get("name") or "").strip()
        if isinstance(tool_call, Mapping)
        else ""
    )
    if tool_name == "agent.read_plan" or mode == "read_plan":
        narrative = _fallback_read_plan_narrative(result)
    if not narrative:
        narrative = _fallback_discovery_narrative(tool_name, result)
    if not narrative:
        narrative = sanitize_public_multiline_text(
            display.get("summary") or display.get("error"), limit=1200
        )
    if not narrative:
        return None
    presentation: dict[str, Any] = {
        "version": 1,
        "source": "system",
        "kind": "narrative",
        "narrative": narrative,
        "degraded": True,
    }
    guidance = display.get("guidance")
    notices = display.get("notices")
    if isinstance(guidance, list) and guidance:
        presentation["guidance"] = guidance
    if isinstance(notices, list) and notices:
        presentation["notices"] = notices
    return presentation


def attach_public_fallback_presentation(
    response: dict[str, Any],
) -> dict[str, Any]:
    """仅在响应没有公开叙述时附加确定性 presentation，保留原始事实结构。"""
    presentation = response.get("presentation")
    if (
        isinstance(presentation, Mapping)
        and str(presentation.get("source") or "").strip().casefold()
        in {"llm", "system", "native"}
        and presentation.get("kind") == "narrative"
        and sanitize_public_multiline_text(presentation.get("narrative"), limit=1800)
    ):
        return response
    fallback = build_public_fallback_presentation(response)
    if fallback is None:
        return response
    projected = dict(response)
    projected["presentation"] = fallback
    if not isinstance(projected.get("display"), Mapping):
        result = projected.get("result")
        if isinstance(result, Mapping):
            projected["display"] = project_agent_result_for_user(result)
    return ensure_response_contract(projected)


def attach_public_display(response: dict[str, Any]) -> dict[str, Any]:
    """为标准 Agent 响应原位附加 Web 与 Telegram 共用的公开展示层。"""
    result = response.get("result")
    if isinstance(result, dict):
        response["display"] = project_agent_result_for_user(result)
    return ensure_response_contract(response)


def build_public_narrative_presentation(
    answer: object,
    suggestions: object,
) -> dict[str, Any] | None:
    """把模型叙述收敛为稳定、已脱敏的公开 presentation 契约。"""
    narrative_text = sanitize_public_multiline_text(answer, limit=1800)
    if not narrative_text:
        return None
    presentation: dict[str, Any] = {
        "version": 1,
        "source": "llm",
        "kind": "narrative",
        "narrative": narrative_text,
    }
    guidance = project_public_guidance(
        suggestions,
        force_kind="draft",
        limit=3,
    )
    notices = project_public_notices(suggestions, limit=3)
    if guidance:
        presentation["guidance"] = guidance
    if notices:
        presentation["notices"] = notices
    return presentation

def project_agent_response_for_llm(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """构造工具结果的有限、安全、面向解释的模型输入。"""
    if not isinstance(response, Mapping):
        return None
    tool_call = response.get("tool_call")
    result = response.get("result")
    if not isinstance(tool_call, Mapping) or not isinstance(result, Mapping):
        return None
    tool_name = str(tool_call.get("name") or "").strip()
    if not tool_name:
        return None

    summary = sanitize_public_text(result.get("summary"), limit=600)
    error = sanitize_public_text(result.get("error"), limit=300)
    suggestions: list[str] = []
    for item in result.get("suggestions") or []:
        text = sanitize_public_text(item, limit=180)
        if text and text not in suggestions:
            suggestions.append(text)
        if len(suggestions) >= 4:
            break

    data_source = result.get("data")
    if tool_name in RESOURCE_CANDIDATE_TOOLS:
        candidate_summaries = _resource_candidate_summaries(data_source)
        data_source = _without_resource_candidate_details(data_source)
        if candidate_summaries and isinstance(data_source, Mapping):
            data_source = dict(data_source)
            data_source["candidate_summaries"] = candidate_summaries
    data = _safe_value(data_source, depth=0, budget=[_MAX_PROJECTED_NODES])
    evidence: list[dict[str, str]] = []
    for item in (result.get("evidence") or [])[:4]:
        if not isinstance(item, Mapping):
            continue
        description = sanitize_public_text(item.get("description"), limit=240)
        collected_at = sanitize_public_text(item.get("collected_at"), limit=80)
        if description:
            entry = {"description": description}
            if collected_at:
                entry["collected_at"] = collected_at
            evidence.append(entry)

    raw_mode = str(response.get("mode") or "read_only").strip().lower()
    mode = raw_mode if raw_mode in {
        "read_only", "read_plan", "conversation", "clarification",
        "confirmation_required", "confirmed_action",
    } else "read_only"
    projection = {
        "mode": mode,
        "tool": public_tool_label(tool_name),
        "untrusted_content": True,
        "ok": bool(result.get("ok")),
        "status": sanitize_public_text(result.get("status") or "unknown", limit=64) or "unknown",
        "summary": summary or "检查已完成，但没有可安全展示的摘要。",
        "error": error,
        "suggestions": suggestions,
        "data": data or {},
        "evidence": evidence,
    }
    # 最后再做一次总量兜底；超限时保留结论，舍弃明细，避免请求体膨胀。
    serialized = json.dumps(
        projection, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(serialized) > _MAX_REQUEST_BYTES:
        projection["data"] = {}
        projection["evidence"] = []
        projection["limited"] = True
    return projection
