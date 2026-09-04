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
_RELATIVE_PATH_RE = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9._~:-]*[A-Za-z._~:-])[A-Za-z0-9._~:-]+[\\/][^\s]+"
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
_INTERNAL_TOOL_GUIDANCE_RE = re.compile(
    r"(?:可|可以|建议|请)?(?:继续)?(?:调用|使用)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"[^。！？!?\n]{0,160}[。！？!?]?",
    re.IGNORECASE,
)
_TRANSFER_RATE_RE = re.compile(r"(?i)\b(?:\d+(?:\.\d+)?\s*)?(?:[kmgt]?i?b|b)/s\b")

_PUBLIC_TOOL_LABELS: dict[str, str] = {
    "agent.action_history": "Agent 操作记录",
    "agent.cancel_job": "取消后台检查",
    "agent.capabilities": "Agent 能力列表",
    "agent.job_status": "后台检查进度",
    "automation.diagnose_pipeline": "自动化链路诊断",
    "bangumi.calendar": "番剧放送日历",
    "config.diagnose": "项目配置检查",
    "config.diagnose_media_servers": "媒体服务器检查",
    "config.explain_component": "配置说明",
    "config.feature_summary": "功能状态概览",
    "config.indexer_sites_summary": "资源检索站点概览",
    "config.safe_policy_summary": "安全策略概览",
    "config.set_feature_state": "功能开关修改",
    "config.set_indexer_sites": "资源检索站点修改",
    "config.set_safe_policy": "安全策略修改",
    "config.test_media_server": "媒体服务器连接测试",
    "discovery.add_watchlist": "加入探索收藏",
    "discovery.confirm_mapping": "影视映射确认",
    "discovery.detail": "影视发现详情",
    "discovery.get_watchlist_summary": "探索收藏详情",
    "discovery.lookup_rating": "豆瓣评分查询",
    "discovery.mapping_candidates": "影视映射候选",
    "discovery.person_filmography": "人物电影作品查询",
    "discovery.recommend": "媒体推荐",
    "discovery.remove_watchlist": "移除探索收藏",
    "discovery.search": "媒体探索搜索",
    "discovery.watchlist_summaries": "探索收藏列表",
    "downloads.diagnose_queue": "下载队列检查",
    "downloads.retry_submission": "重新提交下载请求",
    "guangya.capabilities": "光鸭能力清单",
    "guangya.account.status": "光鸭账号状态",
    "guangya.connection_status": "光鸭连接检查",
    "guangya.directory_scrape.inspect": "光鸭刮削检查",
    "guangya.directory_scrape.preview": "光鸭刮削预览",
    "guangya.directory_scrape.run": "光鸭刮削执行",
    "guangya.directory_scrape.search": "光鸭刮削匹配搜索",
    "guangya.fs.change.execute": "光鸭文件变更执行",
    "guangya.fs.change.preview": "光鸭文件变更预览",
    "guangya.fs.query": "光鸭文件查询",
    "guangya.media_hygiene.preview": "光鸭媒体名称清理预览",
    "guangya.organize.cleanup.classify": "光鸭整理残留逐项复核",
    "guangya.organize.cleanup.execute": "光鸭整理残留清理",
    "guangya.organize.cleanup.preview": "光鸭整理残留清理预览",
    "guangya.organize.preview": "光鸭整理预检",
    "guangya.organize.run_once": "光鸭整理任务",
    "guangya.organize.schedule_policy": "光鸭定时整理策略",
    "guangya.organize.set_schedule_policy": "光鸭定时整理策略修改",
    "guangya.organize.status": "光鸭整理状态",
    "guangya.organize.stop": "停止光鸭整理任务",
    "guangya.operation.status": "光鸭任务状态",
    "guangya.recycle.clear": "清空光鸭回收站",
    "guangya.recycle.list": "光鸭回收站列表",
    "guangya.recycle.restore": "恢复光鸭回收站文件",
    "guangya.rename.execute": "光鸭重命名执行",
    "guangya.rename.preview": "光鸭批量名称转换预览",
    "guangya.share.create": "创建光鸭分享",
    "guangya.share.list": "光鸭分享列表",
    "guangya.share.revoke": "撤销光鸭分享",
    "indexer.diagnose_readiness": "资源站就绪检查",
    "indexer.search_resources": "多站资源搜索",
    "ingest.inspect": "资源接入检查",
    "ingest.status": "资源接入状态",
    "ingest.submit": "资源接入提交",
    "library.audit_episodes": "剧集完整性检查",
    "library.audit_library_episodes": "全库剧集巡检",
    "library.batch_presence": "媒体库批量核对",
    "library.check_updates": "媒体更新检查",
    "library.count_series_episodes": "本地剧集数量",
    "library.missing_media_workflows": "缺集补库进度",
    "library.patrol_policy": "缺集巡检策略",
    "library.patrol_status": "缺集巡检状态",
    "library.search": "媒体库搜索",
    "library.search_missing_episode_resources": "缺集资源搜索",
    "library.search_missing_season_resources": "缺季资源搜索",
    "library.set_patrol_policy": "缺集巡检策略修改",
    "library.start_episode_audit": "后台全库剧集检查",
    "library.trigger_patrol_now": "立即缺集巡检",
    "local_media.diagnose": "本地媒体诊断",
    "local_media.get_source_summary": "本地媒体来源摘要",
    "local_media.history_summary": "本地媒体处理历史",
    "local_media.inspect_task": "本地媒体任务检查",
    "local_media.preview_task": "本地媒体整理预览",
    "local_media.refresh_task_library": "本地媒体库精准刷新",
    "local_media.retry_task": "本地媒体任务重试",
    "local_media.review_queue_summary": "本地媒体待确认摘要",
    "local_media.scan_sources": "本地媒体来源扫描",
    "local_media.set_source_trigger_enabled": "本地媒体来源触发器启停",
    "local_media.source_summaries": "本地媒体来源列表",
    "local_media.task_summaries": "本地媒体任务列表",
    "local_media.verify_task_library_visibility": "本地媒体入库可见核验",
    "media.clear_preferences": "媒体偏好清除",
    "media.continue_watching": "继续观看",
    "media.recently_added": "最近入库",
    "media.recently_played": "最近播放",
    "media.recommend_from_library": "本地媒体推荐",
    "media.create_subscription": "创建媒体追更",
    "media.delete_subscription": "删除媒体追更",
    "media.get_subscription_policy": "媒体追更策略",
    "media.get_subscription_summary": "媒体追更订阅摘要",
    "media.preferences": "媒体偏好",
    "media.reset_subscription_notification_rule": "媒体追更通知规则重置",
    "media.set_preferences": "媒体偏好修改",
    "media.set_subscription_enabled": "媒体追更状态修改",
    "media.set_subscription_notification_rule": "媒体追更通知规则修改",
    "media.set_subscription_policy": "媒体追更策略修改",
    "media.subscription_notification_rule": "媒体追更通知规则",
    "media.subscription_summaries": "媒体追更订阅列表",
    "media.subscription_updates": "媒体追更实时更新检查",
    "media.today_summary": "今日媒体内容摘要",
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
    "recognition.set_rule_enabled": "识别规则启停",
    "rss.create_subscription": "RSS 订阅创建",
    "rss.delete_subscription": "RSS 订阅删除",
    "rss.diagnose": "RSS 订阅诊断",
    "rss.entry_summaries": "RSS 条目列表",
    "rss.get_subscription_summary": "RSS 单订阅摘要",
    "rss.mark_entries": "RSS 条目标记",
    "rss.recent_activity": "RSS 最近下载统计",
    "rss.refresh_subscription": "RSS 订阅刷新",
    "rss.refresh_subscriptions": "RSS 订阅批量刷新",
    "rss.retry_failed_to_qb": "RSS 失败条目重试",
    "rss.submit_entries_to_qb": "RSS 指定条目提交",
    "rss.submit_pending_to_qb": "RSS 待处理条目提交",
    "rss.subscription_summaries": "RSS 订阅列表",
    "rss.update_subscription": "RSS 订阅配置修改",
    "strm.diagnose": "STRM 配置诊断",
    "strm.retry_failures": "STRM 失败项重试",
    "strm.run_history": "STRM 运行历史",
    "strm.run_once": "STRM 手动同步",
    "strm.schedule_policy": "STRM 调度策略",
    "strm.set_schedule_policy": "STRM 调度策略修改",
    "strm.status": "STRM 同步状态",
    "strm.triage_failures": "STRM 失败分诊",
    "telegram.send_test_notification": "Telegram 测试通知",
    "web.read": "网页正文读取",
    "web.search": "网页搜索",
    "workspace.briefing": "系统运行简报",
    "workspace.health": "系统健康检查",
    "workspace.next_actions": "下一步建议",
    "workspace.search": "全局搜索",
    "workspace.todo": "待处理事项",
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


def public_tool_label(tool_name: object) -> str:
    normalized = str(tool_name or "").strip()
    return _PUBLIC_TOOL_LABELS.get(normalized, "MediaFlux 检查")


def public_followup_prompt(source: object) -> str:
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


def _remove_internal_tool_guidance(text: str) -> str:
    cleaned = _INTERNAL_TOOL_GUIDANCE_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"(?:下一步(?:建议)?|建议)\s*[:：]\s*$", "", cleaned).strip()
    return " ".join(cleaned.replace("\r", " ").replace("\n", " ").split()).strip()


def _replace_internal_field(match: re.Match[str]) -> str:
    value = match.group(0)
    return value if value.casefold() in _PUBLIC_STATUS_KEYS else "内部状态"


def _replace_internal_identifiers_in_text(text: str) -> str:
    text = _remove_internal_tool_guidance(text)
    for tool_name, label in sorted(
        _PUBLIC_TOOL_LABELS.items(), key=lambda item: -len(item[0])
    ):
        text = text.replace(tool_name, label)
    text = _INTERNAL_TOOL_RE.sub("内部检查", text)
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
    return text[: max(1, int(limit))].rstrip()


def sanitize_public_text(value: object, *, limit: int = 600) -> str:
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
