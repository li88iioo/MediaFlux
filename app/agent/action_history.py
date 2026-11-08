"""Agent 受确认动作的脱敏审计记录与只读查询。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from app import database as db
from app.agent.confirmation_contract import sanitize_confirmation_contract
from app.agent.errors import AgentToolError
from app.agent.models import RiskLevel, ToolResult
from app.logger import get_logger

logger = get_logger(__name__)

_TOOL_LABELS = {
    "rss.mark": "RSS 条目标记",
    "rss.download": "RSS 条目投递",
    "rss.retry": "RSS 失败条目重试",
    "rss.refresh": "RSS 订阅刷新",
    "library.set_played": "修改媒体已看状态",
    "library.set_favorite": "修改媒体收藏状态",
    "library.create_playlist": "创建播放列表",
    "library.add_to_playlist": "添加播放列表成员",
    "library.remove_from_playlist": "移除播放列表成员",
    "agent.batch": "组合变更",
    "session.batch": "组合变更",
    "activity.follow": "活动跟踪开启",
    "activity.unfollow": "活动跟踪停用",
    "activity.undo": "可逆操作回退",
    "library.refresh": "媒体库刷新",
    "media.refresh": "媒体条目刷新",
    "action.undo.execute": "可逆操作回退",
    "automation.create_media_rule": "创建媒体自动追更规则",
    "automation.set_digest": "主动摘要规则保存",
    "recognition.create_knowledge": "新增识别知识",
    "recognition.update_knowledge": "修改识别知识",
    "recognition.delete_knowledge": "删除识别知识",
    "config.create_recognition_knowledge": "新增识别知识",
    "config.update_recognition_knowledge": "修改识别知识",
    "config.delete_recognition_knowledge": "删除识别知识",
    "local.create_source": "新增本地媒体来源",
    "local.update_source": "修改本地媒体来源",
    "local.delete_source": "删除本地媒体来源",
    "local.scan_sources": "扫描本地媒体来源",
    "config.create_local_source": "新增本地媒体来源",
    "config.update_local_source": "修改本地媒体来源",
    "config.delete_local_source": "删除本地媒体来源",
    "library.create_path_mapping": "新增媒体路径映射",
    "library.update_path_mapping": "修改媒体路径映射",
    "library.delete_path_mapping": "删除媒体路径映射",
    "config.create_media_path_mapping": "新增媒体路径映射",
    "config.update_media_path_mapping": "修改媒体路径映射",
    "config.delete_media_path_mapping": "删除媒体路径映射",

    "downloads.retry_submission": "下载请求重新提交",
    "downloads.pause": "暂停 qB 下载任务",
    "downloads.resume": "恢复 qB 下载任务",
    "downloads.remove": "移除 qB 任务并保留文件",
    "downloads.retry": "下载请求重新提交",
    "downloads.submit": "下载链接提交",
    "provider.change.execute": "Provider 原生写计划执行",
    "rss.mark_entries": "RSS 条目标记",
    "rss.submit_entries_to_qb": "RSS 指定条目提交",
    "rss.submit_pending_to_qb": "RSS 待处理条目提交",
    "rss.retry_failed": "RSS 失败条目重试",
    "rss.refresh_subscriptions": "RSS 订阅刷新",
    "rss.create_subscription": "RSS 订阅创建",
    "rss.update_subscription": "RSS 订阅配置修改",
    "rss.delete_subscription": "RSS 订阅删除",
    'subscriptions.create': "媒体追更创建",
    "media.create_subscription": "媒体追更创建",
    'subscriptions.delete': "媒体追更删除",
    "media.delete_subscription": "媒体追更删除",
    "media.set_subscription_enabled": "媒体追更状态修改",
    'subscriptions.update': "媒体追更策略修改",
    "media.set_subscription_policy": "媒体追更策略修改",
    "media.set_preferences": "媒体偏好修改",
    "media.clear_preferences": "媒体偏好清除",
    "preferences.update": "媒体偏好修改",
    "preferences.clear": "媒体偏好清除",
    'subscriptions.set_notification': "媒体追更通知规则修改",
    "media.set_subscription_notification_rule": "媒体追更通知规则修改",
    'subscriptions.reset_notification': "媒体追更通知规则重置",
    "media.reset_subscription_notification_rule": "媒体追更通知规则重置",
    "config.set_feature_state": "功能开关修改",
    "settings.set_feature": "功能开关修改",
    "config.set_indexer_sites": "资源检索站点修改",
    "resources.configure_sites": "资源检索站点修改",
    "settings.set_policy": "策略修改",
    "settings.policies": "策略概览",
    "config.set_safe_policy": "安全策略修改",
    "telegram.send_test_notification": "Telegram 测试通知",
    "settings.test_notification": "Telegram 测试通知",
    "playback.set_enabled": "媒体反代实例启停",
    "playback.restart": "媒体反代实例重启",
    "media_proxy.set_instance_enabled": "媒体反代实例启停",
    "media_proxy.restart_instance": "媒体反代实例重启",
    "local_media.set_source_trigger_enabled": "本地媒体来源触发器启停",
    "local_media.scan_sources": "本地媒体来源扫描",
    "local.tasks": "本地媒体任务列表",
    "local.task": "本地媒体任务检查",
    "local.preview": "本地媒体整理预览",
    "local.retry": "本地媒体任务重试/季集纠偏",
    "local.refresh_library": "精准刷新绑定媒体库",
    "local.verify_library": "本地媒体入库可见核验",
    "local.diagnose": "本地媒体诊断",
    "local_media.retry_task": "本地媒体任务重试/季集纠偏",
    "local_media.refresh_task_library": "本地媒体任务精准刷新",
    "recognition.set_rule_enabled": "识别规则启停",
    "discovery.confirm_mapping": "发现身份映射确认",
    "media.confirm_mapping": "发现身份映射确认",
    "discovery.add_watchlist": "探索收藏添加",
    "discovery.remove_watchlist": "探索收藏移除",
    "watchlist.add": "探索收藏添加",
    "watchlist.remove": "探索收藏移除",
    "strm.retry_failures": "STRM 失败项重试",
    "strm.run_once": "STRM 手动同步",
    "strm.run": "STRM 手动同步",
    "strm.set_schedule_policy": "STRM 调度策略修改",
    "strm.set_schedule": "STRM 调度策略修改",
    "strm.metadata.set_enabled": "伴随同步开关调整",
    'strm.set_metadata_enabled': "伴随同步开关调整",
    "strm.metadata.cancel_pending": "伴随元数据积压取消",
    'strm.cancel_metadata_pending': "伴随元数据积压取消",
    "guangya.organize.set_schedule_policy": "光鸭定时整理策略修改",
    "cloud.set_organize_schedule": "光鸭定时整理策略修改",
    "guangya.fs.change.execute": "光鸭文件变更",
    "cloud.change": "光鸭文件变更",
    "guangya.recycle.restore": "光鸭回收站恢复",
    "guangya.recycle.clear": "光鸭回收站清空",
    "cloud.restore": "光鸭回收站恢复",
    "cloud.clear_recycle": "光鸭回收站清空",
    "cloud.share": "光鸭分享创建",
    "cloud.revoke_share": "光鸭分享撤销",
    "guangya.rename.execute": "光鸭重命名执行",
    "cloud.scrape": "光鸭媒体刮削执行",
    "guangya.directory_scrape.run": "光鸭目录刮削执行",
    "cloud.organize": "光鸭整理任务",
    "cloud.stop_organize": "停止光鸭整理任务",
    "cloud.preview_organize": "光鸭整理预检",
    "guangya.organize.run_once": "光鸭整理任务",
    "guangya.organize.cleanup.execute": "光鸭整理残留清理",
    "guangya.organize.stop": "停止光鸭整理任务",
    "ingest.submit": "资源接入提交",
    "cloud.transfer_share": "光鸭分享转存",
    "resources.download": "资源下载提交",
    "library.set_patrol_policy": "缺集巡检策略修改",
    "library.trigger_patrol_now": "立即全库缺集巡检",
    "library.run_patrol": "立即全库缺集巡检",
    "library.start_audit": "后台全库剧集检查",
    "library.start_episode_audit": "后台全库剧集检查",
    "agent.cancel_job": "取消后台全库剧集检查",
    "library.cancel_audit": "取消后台全库剧集检查",
}

_SAFE_FIELDS = {
    "library.set_played": {"affected", "accepted", "verification", "member_count", "delete_media_files", "execution_started"},
    "library.set_favorite": {"affected", "accepted", "verification", "member_count", "delete_media_files", "execution_started"},
    "library.create_playlist": {"affected", "accepted", "verification", "member_count", "delete_media_files", "execution_started"},
    "library.add_to_playlist": {"affected", "accepted", "verification", "member_count", "delete_media_files", "execution_started"},
    "library.remove_from_playlist": {"affected", "accepted", "verification", "member_count", "delete_media_files", "execution_started"},
    "library.refresh": {"accepted", "verification", "scope", "global_refresh"},
    "media.refresh": {"accepted", "verification", "scope", "global_refresh"},
    "activity.follow": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "activity.unfollow": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "activity.undo": {"affected", "accepted", "restored"},
    "action.undo.execute": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "automation.create_media_rule": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "automation.set_digest": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "recognition.create_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "recognition.update_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "recognition.delete_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.create_recognition_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.update_recognition_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.delete_recognition_knowledge": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.create_local_source": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.update_local_source": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.delete_local_source": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "library.create_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "library.update_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "library.delete_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.create_media_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.update_media_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},
    "config.delete_media_path_mapping": {"affected", "accepted", "enabled", "created", "runtime_refreshed"},

    **dict.fromkeys(("downloads.pause", "downloads.resume", "downloads.remove"), {"affected", "accepted", "delete_files", "verification", "observed_count", "tracking_stopped"}),
    **dict.fromkeys(("downloads.retry_submission", "downloads.retry"), {
        "request_id",
        "source_request_id",
        "target",
        "status",
        "created",
        "duplicate",
        "succeeded",
        "failed",
        "source_attention_preserved",
    }),
    "provider.change.execute": {
        "provider",
        "operation",
        "status",
        "affected",
        "accepted",
        "delete_files",
        "global_refresh",
        "plan_ref",
    },
    "rss.mark": {"affected", "processed"},
    "rss.mark_entries": {"affected", "processed"},
    "rss.download": {"target", "requested", "claimed", "submitted", "failed", "pending", "outcome_unknown"},
    "rss.retry": {"target", "requested", "claimed", "submitted", "failed", "pending", "outcome_unknown"},
    "rss.refresh": {"requested", "refreshed", "failed", "total", "new", "skipped", "partial_subscriptions", "failed_sources", "run_id"},
    "rss.submit_entries_to_qb": {
        "target",
        "requested",
        "claimed",
        "submitted",
        "failed",
        "outcome_unknown",
    },
    "rss.submit_pending_to_qb": {
        "target",
        "requested",
        "claimed",
        "submitted",
        "failed",
        "outcome_unknown",
    },
    "rss.retry_failed": {
        "target",
        "requested",
        "claimed",
        "submitted",
        "failed",
        "outcome_unknown",
    },
    "rss.refresh_subscriptions": {
        "requested",
        "refreshed",
        "failed",
        "total",
        "new",
        "skipped",
    },
    "rss.create": {"operation", "affected", "verified", "runtime_refreshed"},
    "rss.update": {"operation", "affected", "changed_field_count", "runtime_refreshed"},
    "rss.delete": {"operation", "affected", "deleted_entries", "runtime_refreshed"},
    "rss.create_subscription": {
        "operation",
        "subscription_id",
        "affected",
        "name",
        "url_count",
        "action",
        "enabled",
        "refresh_interval_minutes",
        "download_method",
        "media_tmdb_id",
        "media_default_season",
        "skip_existing_episodes",
        "runtime_refreshed",
    },
    "rss.update_subscription": {
        "operation",
        "affected",
        "changed_field_count",
        "runtime_refreshed",
    },
    "rss.delete_subscription": {
        "operation",
        "affected",
        "deleted_entries",
        "runtime_refreshed",
    },
    'subscriptions.create': {
        "operation",
        "subscription_number",
        "affected",
        "created",
        "season",
        "check_interval_minutes",
        "runtime_refreshed",
    },
    "media.create_subscription": {
        "operation",
        "subscription_number",
        "affected",
        "created",
        "season",
        "check_interval_minutes",
        "runtime_refreshed",
    },
    'subscriptions.delete': {
        "operation",
        "subscription_number",
        "affected",
        "expired_candidates",
        "cancelled_admissions",
        "cancelled_runs",
        "runtime_refreshed",
    },
    "media.delete_subscription": {
        "operation",
        "subscription_number",
        "affected",
        "expired_candidates",
        "cancelled_admissions",
        "cancelled_runs",
        "runtime_refreshed",
    },
    "media.set_subscription_enabled": {
        "operation",
        "subscription_number",
        "enabled",
        "affected",
        "expired_candidates",
        "cancelled_admissions",
        "cancelled_runs",
        "runtime_refreshed",
    },
    'subscriptions.update': {"operation", "subscription_number", "enabled", "affected", "updated_fields", "expired_candidates", "cancelled_admissions", "cancelled_runs", "in_flight_dispatches", "runtime_refreshed"},
    "media.set_subscription_policy": {
        "subscription_number",
        "updated_fields",
        "expired_candidates",
        "runtime_refreshed",
    },
    "media.set_preferences": {
        "operation",
        "affected",
        "preferred_server",
        "preferred_download_target",
    },
    "media.clear_preferences": {"operation", "affected"},
    "preferences.update": {"operation", "affected", "preferred_server", "preferred_download_target"},
    "preferences.clear": {"operation", "affected"},
    'subscriptions.set_notification': {
        "operation",
        "subscription_number",
        "affected",
        "enabled",
    },
    "media.set_subscription_notification_rule": {
        "operation",
        "subscription_number",
        "affected",
        "enabled",
    },
    'subscriptions.reset_notification': {
        "operation",
        "subscription_number",
        "affected",
    },
    "media.reset_subscription_notification_rule": {
        "operation",
        "subscription_number",
        "affected",
    },
    **dict.fromkeys(("config.set_feature_state", "settings.set_feature"), {
        "feature", "enabled", "runtime_refreshed", "verification_state",
    }),
    "config.set_indexer_sites": {
        "site_count",
        "runtime_refreshed",
        "verification_state",
    },
    "resources.configure_sites": {
        "site_count",
        "runtime_refreshed",
        "verification_state",
    },
    "settings.set_policy": {"policy", "value", "saved", "runtime_refreshed", "verification_state"},
    "config.set_safe_policy": {"policy", "runtime_refreshed"},
    "telegram.send_test_notification": {"sent"},
    "settings.test_notification": {"sent"},
    "playback.set_enabled": {"operation", "instance_id", "enabled", "affected", "runtime_refreshed"},
    "playback.restart": {"operation", "instance_id", "accepted", "cache_entries_cleared", "restarted", "reason", "outcome_unknown"},
    "media_proxy.set_instance_enabled": {
        "operation",
        "instance_number",
        "enabled",
        "affected",
        "runtime_refreshed",
    },
    "media_proxy.restart_instance": {
        "operation",
        "instance_number",
        "accepted",
        "cache_entries_cleared",
    },
    "local.create_source": {"operation", "source_id", "runtime_refreshed"},
    "local.update_source": {"operation", "source_id", "runtime_refreshed"},
    "local.delete_source": {"operation", "source_id", "deleted", "runtime_refreshed"},
    "local.scan_sources": {
        "operation", "source_ids", "task_ids", "scan_ref", "scan_status", "failed_sources",
        "skipped_sources", "enqueue_failures", "scanned_sources", "candidates", "queued_tasks", "runtime_started",
    },
    "local_media.scan_sources": {
        "operation",
        "source_numbers",
        "scan_status",
        "failed_sources",
        "skipped_sources",
        "enqueue_failures",
        "scanned_sources",
        "candidates",
        "queued_tasks",
        "runtime_started",
    },
    "local_media.set_source_trigger_enabled": {
        "operation",
        "source_number",
        "trigger",
        "enabled",
        "affected",
        "runtime_refreshed",
    },
    "local.retry": {"operation", "task_id", "affected", "version", "remapped", "runtime_refreshed"},
    "local.refresh_library": {"operation", "task_id", "refreshed"},
    "local_media.retry_task": {
        "operation",
        "task_number",
        "season",
        "episode",
        "affected",
        "runtime_refreshed",
    },
    "local_media.refresh_task_library": {
        "operation",
        "task_number",
        "refreshed",
        "matched_paths",
    },
    "recognition.set_rule_enabled": {
        "operation",
        "rule_type",
        "rule_id",
        "enabled",
        "affected",
    },
    "media.confirm_mapping": {"affected", "provider", "external_id", "media_type", "tmdb_id", "mapping_confirmed"},
    "discovery.confirm_mapping": {
        "affected",
        "provider",
        "media_type",
        "candidate_number",
        "mapping_confirmed",
    },
    "discovery.add_watchlist": {"operation", "watchlist_number", "affected"},
    "discovery.remove_watchlist": {"operation", "watchlist_number", "affected"},
    "watchlist.add": {"operation", "id", "affected"},
    "watchlist.remove": {"operation", "id", "affected"},
    "strm.retry_failures": {
        "requested",
        "matched",
        "resolved",
        "failed",
        "missing",
        "stale",
        "deferred",
        "stopped",
        "scan_incomplete",
    },
    "strm.run_once": {"accepted", "trigger"},
    "strm.run": {"accepted", "trigger", "run_id", "scoped", "source_count"},
    "strm.set_schedule_policy": {"runtime_refreshed"},
    "strm.set_schedule": {"runtime_refreshed"},
    "strm.metadata.set_enabled": {"enabled", "verified"},
    'strm.set_metadata_enabled': {"enabled", "verified"},
    "strm.metadata.cancel_pending": {"cancelled", "enabled", "files_deleted"},
    'strm.cancel_metadata_pending': {"cancelled", "enabled", "files_deleted"},
    "guangya.organize.set_schedule_policy": {"runtime_refreshed"},
    "cloud.set_organize_schedule": {"runtime_refreshed"},
    **dict.fromkeys(("guangya.fs.change.execute", "cloud.change"), {
        "queued",
        "queue_position",
        "replayed",
        "operation_ref",
        "total",
        "rename_count",
        "move_count",
        "relocate_count",
        "copy_count",
        "trash_count",
        "create_directory_count",
        "trigger_strm",
        "requires_manual",
    }),
    **dict.fromkeys(("guangya.recycle.restore", "cloud.restore"), {
        "count",
        "verified",
        "verification_pending",
    }),
    **dict.fromkeys(("guangya.recycle.clear", "cloud.clear_recycle"), {
        "count",
        "verified",
        "verification_pending",
        "irreversible",
    }),
    "cloud.share": {
        "count",
        "expires_days",
        "allow_download",
    },
    "cloud.revoke_share": {
        "count",
        "verified",
        "verification_pending",
    },
    "guangya.rename.execute": {
        "queued",
        "queue_position",
        "replayed",
        "rename_count",
        "requires_manual",
    },
    "cloud.scrape": {"queued", "queue_position", "replayed", "plan_count", "operation_ref"},
    "guangya.directory_scrape.run": {
        "queued",
        "queue_position",
        "replayed",
        "plan_count",
    },
    "cloud.organize": {"trigger_type", "source_count", "run_id"},
    "cloud.stop_organize": {"accepted", "run_id"},
    "guangya.organize.run_once": {"trigger_type", "source_count"},
    "guangya.organize.cleanup.execute": {
        "queued",
        "queue_position",
        "replayed",
        "empty_dir_count",
        "residual_dir_count",
        "selected_count",
        "kept_count",
        "requires_manual",
    },
    "guangya.organize.stop": {"accepted"},
    **dict.fromkeys(("ingest.submit", "resources.download", "downloads.submit", "cloud.transfer_share"), {
        "source_type",
        "target",
        "status",
        "created",
        "succeeded",
        "failed",
        "duplicate",
        "total",
        "selected_count",
        "request_number",
        "request_id",
    }),
    "library.set_patrol_policy": {"runtime_refreshed"},
    "library.trigger_patrol_now": {"queued", "reused", "task_status"},
    "library.run_patrol": {"queued", "reused", "task_status", "run_id"},
    "library.start_audit": {
        "job_id",
        "accepted",
        "created",
        "reused",
        "max_series",
        "progress_current",
        "progress_total",
    },
    "library.cancel_audit": {
        "job_id",
        "accepted",
        "cancelled",
        "cancel_requested",
        "progress_current",
        "progress_total",
    },
    "library.start_episode_audit": {
        "accepted",
        "created",
        "reused",
        "max_series",
        "progress_current",
        "progress_total",
    },
    "agent.cancel_job": {
        "accepted",
        "cancelled",
        "cancel_requested",
        "progress_current",
        "progress_total",
    },
}

_STATUS_LABELS = {
    "accepted": "已提交",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "执行失败",
    "conflict": "状态冲突",
    "not_configured": "配置不可用",
    "no_changes": "无需修改",
    "environment_override": "环境变量覆盖",
    "unavailable": "暂时不可用",
    "confirmation_stale": "确认已失效",
    "busy": "正在执行",
    "review_required": "需人工核对",
    "executing": "执行中",
    "outcome_unknown": "结果待核对",
}

_SAFE_ERROR_CODES = {
    "outcome_unknown",
    "confirmation_stale",
    "confirmation_not_supported",
    "tool_not_found",
    "execution_interrupted",
}
_COUNT_FIELDS = {
    "request_id",
    "source_request_id",
    "failed_sources",
    "skipped_sources",
    "enqueue_failures",
    "scanned_sources",
    "candidates",
    "queued_tasks",
    "requested",
    "claimed",
    "submitted",
    "failed",
    "matched",
    "resolved",
    "missing",
    "stale",
    "source_count",
    "succeeded",
    "cleaned",
    "subscription_id",
    "total",
    "new",
    "skipped",
    "site_count",
    "affected",
    "refresh_interval_minutes",
    "deleted_entries",
    "changed_field_count",
    "max_series",
    "progress_current",
    "progress_total",
    "instance_number",
    "rule_id",
    "subscription_number",
    "watchlist_number",
    "source_number",
    "task_number",
    "season",
    "episode",
    "candidate_number",
    "queue_position",
    "plan_count",
    "outcome_unknown",
    "rename_count",
    "copy_count",
    "count",
    "expires_days",
    "empty_dir_count",
    "residual_dir_count",
    "selected_count",
    "kept_count",
    "expired_candidates",
    "cancelled_admissions",
    "cancelled_runs",
    "refreshed",
    "matched_paths",
}
_BOOL_FIELDS = {
    "runtime_started",
    "accepted",
    "enabled",
    "runtime_refreshed",
    "created",
    "duplicate",
    "delete_files",
    "reused",
    "cancelled",
    "cancel_requested",
    "sent",
    "processed",
    "queued",
    "replayed",
    "mapping_confirmed",
    "global_refresh",
    "requires_manual",
    "source_attention_preserved",
    "verified",
    "verification_pending",
    "irreversible",
    "allow_download",
}
_ENUM_FIELDS = {
    "scan_status": {"completed", "partial", "failed"},
    "target": {"qb", "qbittorrent", "guangya", "both"},
    "feature": {
        "discovery",
        "douban",
        "resource_results",
        "indexer_search",
        "web_search",
        "offline_magnet",
        "offline_ed2k",
        "offline_http",
        "strm_metadata",
        "download_verification_notify",
    },
    "policy": {
        "tmdb_match_mode",
        "login_wallpaper_mode",
        "web_search_depth",
        "web_search_max_results",
        "web_search_timeout_seconds",
        "web_search_cache_ttl_seconds",
        "web_search_daily_credit_limit",
        "discovery_cache_ttl_seconds",
        "discovery_stale_ttl_seconds",
        "douban_cache_ttl_seconds",
        "indexer_btbtla_min_interval_seconds",
    },
    "scope": {"all", "generate", "metadata", "item", "library"},
    "trigger": {"manual", "qb_completed", "scan"},
    "trigger_type": {"manual"},
    "rule_type": {"preprocess_rule", "tmdb_regex_rule", "knowledge_entry", "release_format"},
    "preferred_server": {"any", "jellyfin", "emby"},
    "preferred_download_target": {"qb", "guangya", "both"},
    "provider": {"tmdb", "douban", "bangumi", "media", "qbittorrent"},
    "media_type": {"movie", "tv"},
    "task_status": {"pending", "running", "retry_wait", "not_scheduled"},
    "operation": {
        "scan_sources",
        "pause",
        "resume",
        "delete",
        "enable",
        "disable",
        "set_interval",
        "update",
        "add",
        "remove",
        "create",
        "restore",
        "retry",
        "remap_episode",
        "precise_refresh",
        "set_preferences",
        "clear_preferences",
        "set_notification_rule",
        "reset_notification_rule",
        "media.library.refresh",
        "media.item.refresh",
        "qb.torrents.pause",
        "qb.torrents.resume",
        "qb.torrents.delete_task",
    },
    "status": {
        "manual_review",
        "accepted",
        "submitted",
        "completed",
        "partial",
        "failed",
        "duplicate",
        "succeeded",
        "stale",
        "outcome_unknown",
    },
}


def _safe_details(tool_name: str, data: dict[str, Any] | None) -> dict[str, Any]:
    allowed = _SAFE_FIELDS.get((tool_name or "").strip(), set())
    if not allowed or not isinstance(data, dict):
        return {}
    projected: dict[str, Any] = {}
    for key in sorted(allowed):
        value = data.get(key)
        if ((tool_name or "").strip() in {"downloads.retry", "downloads.retry_submission", "downloads.submit"}
                and key in {"succeeded", "failed"} and isinstance(value, list)):
            value = len({item for item in value if isinstance(item, str) and item in {"qb", "guangya"}})
        if key in _COUNT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            projected[key] = max(0, value)
        elif key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                continue
            projected[key] = value
        elif key in _ENUM_FIELDS:
            normalized = str(value or "").strip().lower()
            if normalized in _ENUM_FIELDS[key]:
                projected[key] = normalized
        elif key == "job_id":
            normalized = str(value or "").strip()
            if re.fullmatch(r"job_[A-Za-z0-9_-]{16,80}", normalized):
                projected[key] = normalized
        elif key == "plan_ref":
            normalized = str(value or "").strip().upper()
            if re.fullmatch(r"PP-[0-9A-F]{24}", normalized):
                projected[key] = normalized
        elif key == "operation_ref":
            from app.repositories.organize_operation_jobs import organize_operation_job_id_from_public_ref

            normalized = str(value or "").strip().upper()
            try:
                organize_operation_job_id_from_public_ref(normalized)
            except ValueError:
                continue
            projected[key] = normalized
    return projected


_CONTRACT_STORAGE_KEYS = {
    "version": "contract_version",
    "action": "contract_action",
    "object": "contract_object",
    "impact": "contract_impact",
    "reversibility": "contract_reversibility",
    "preflight_at": "contract_preflight_at",
    "risk": "contract_risk",
    "preflight_summary": "contract_preflight_summary",
}

_CONTRACT_STORAGE_LIMITS = {
    "action": 128,
    "object": 128,
    "impact": 128,
    "reversibility": 128,
    "preflight_at": 64,
    "risk": 16,
    "preflight_summary": 128,
}


def _contract_storage_projection(value: dict[str, Any] | None) -> dict[str, Any]:
    contract = sanitize_confirmation_contract(value)
    if not contract:
        return {}
    stored: dict[str, Any] = {}
    for public_key, storage_key in _CONTRACT_STORAGE_KEYS.items():
        item = contract.get(public_key)
        if public_key == "version":
            stored[storage_key] = int(item or 1)
            continue
        text = str(item or "").strip()
        if text:
            stored[storage_key] = text[: _CONTRACT_STORAGE_LIMITS.get(public_key, 128)]
    return stored


def _stored_confirmation_contract(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    raw = {
        public_key: value.get(storage_key)
        for public_key, storage_key in _CONTRACT_STORAGE_KEYS.items()
    }
    return sanitize_confirmation_contract(raw)


def _audit_details(
    tool_name: str,
    data: dict[str, Any] | None,
    confirmation_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **_safe_details(tool_name, data),
        **_contract_storage_projection(confirmation_contract),
    }


def _safe_tool_name(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in _TOOL_LABELS else "unknown.confirmed_action"


def _safe_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _STATUS_LABELS else "unavailable"


def _safe_error_code(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _SAFE_ERROR_CODES else ""


def _safe_finished_at(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat(timespec="seconds")


def _safe_summary(tool_name: str, status: str) -> str:
    label = _TOOL_LABELS.get(tool_name, "Agent 受确认动作")
    return f"{label}：{_STATUS_LABELS.get(status, '已记录')}"


def _timestamps(elapsed_ms: int) -> tuple[str, str]:
    finished = datetime.now().astimezone()
    started = finished - timedelta(milliseconds=max(0, (elapsed_ms or 0)))
    return (
        started.isoformat(timespec="milliseconds"),
        finished.isoformat(timespec="milliseconds"),
    )


def _confirmation_execution_id(
    confirmation_id: str,
    owner_generation: int,
) -> str:
    ticket_id = (confirmation_id or "").strip()
    generation = max(0, (owner_generation or 0))
    return f"{ticket_id}-{generation}"


def record_confirmation_claimed(
    *,
    owner: str,
    confirmation_id: str,
    owner_generation: int,
    tool_name: str,
    risk: RiskLevel,
    confirmation_contract: dict[str, Any] | None = None,
    action_arguments: dict[str, Any] | None = None,
    connection: Any = None,
) -> None:
    """在消费确认票据的同一事务中先持久化执行中状态。"""
    if not (owner or "").strip():
        raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
    started_at, _finished_at = _timestamps(0)
    safe_tool = _safe_tool_name(tool_name)
    db.add_agent_action_history(
        owner_digest=db.media_owner_digest(owner),
        tool_name=safe_tool,
        risk=risk.value,
        status="executing",
        ok=False,
        summary=_safe_summary(safe_tool, "executing"),
        safe_details=_audit_details(
            tool_name,
            action_arguments,
            confirmation_contract,
        ),
        started_at=started_at,
        finished_at=started_at,
        confirmation_id=_confirmation_execution_id(
            confirmation_id,
            owner_generation,
        ),
        connection=connection,
    )


def record_confirmation_interrupted(
    *,
    owner: str,
    confirmation_id: str,
    owner_generation: int,
    tool_name: str,
    risk: RiskLevel,
    confirmation_contract: dict[str, Any] | None = None,
) -> None:
    """把已领取但未取得可信终态的动作标记为需人工核对。"""
    safe_tool = _safe_tool_name(tool_name)
    started_at, finished_at = _timestamps(0)
    try:
        db.add_agent_action_history(
            owner_digest=db.media_owner_digest(owner),
            tool_name=safe_tool,
            risk=risk.value,
            status="outcome_unknown",
            ok=False,
            summary=_safe_summary(safe_tool, "outcome_unknown"),
            safe_details=_audit_details(
                tool_name,
                {},
                confirmation_contract,
            ),
            error_code="execution_interrupted",
            started_at=started_at,
            finished_at=finished_at,
            confirmation_id=_confirmation_execution_id(
                confirmation_id,
                owner_generation,
            ),
            finalize_only=True,
        )
    except Exception as exc:
        logger.warning(
            "Agent 中断动作审计写入失败 tool=%s type=%s",
            tool_name,
            type(exc).__name__,
        )


def record_confirmed_result(
    *,
    owner: str,
    tool_name: str,
    risk: RiskLevel,
    result: ToolResult,
    elapsed_ms: int,
    confirmation_contract: dict[str, Any] | None = None,
    confirmation_id: str = "",
    owner_generation: int = 0,
) -> None:
    """尽力记录已确认动作；审计失败不能改变副作用执行结果。"""
    started_at, finished_at = _timestamps(elapsed_ms)
    safe_tool = _safe_tool_name(tool_name)
    safe_status = _safe_status(result.status)
    try:
        db.add_agent_action_history(
            owner_digest=db.media_owner_digest(owner),
            tool_name=safe_tool,
            risk=risk.value,
            status=safe_status,
            ok=(result.ok),
            summary=_safe_summary(safe_tool, safe_status),
            safe_details=_audit_details(tool_name, result.data, confirmation_contract),
            elapsed_ms=elapsed_ms,
            started_at=started_at,
            finished_at=finished_at,
            confirmation_id=(
                _confirmation_execution_id(confirmation_id, owner_generation)
                if confirmation_id
                else ""
            ),
            finalize_only=bool(confirmation_id),
        )
    except Exception as exc:
        logger.warning(
            "Agent 动作审计写入失败 tool=%s type=%s", tool_name, type(exc).__name__
        )


def record_confirmation_error(
    *,
    owner: str,
    tool_name: str,
    risk: RiskLevel,
    code: str,
    elapsed_ms: int = 0,
    confirmation_contract: dict[str, Any] | None = None,
    confirmation_id: str = "",
    owner_generation: int = 0,
) -> None:
    """记录票据已消费、但在执行前因稳定确认错误码终止的动作。"""
    stable_code = _safe_error_code(code) or "confirmation_stale"
    safe_tool = _safe_tool_name(tool_name)
    started_at, finished_at = _timestamps(elapsed_ms)
    try:
        db.add_agent_action_history(
            owner_digest=db.media_owner_digest(owner),
            tool_name=safe_tool,
            risk=risk.value,
            status=stable_code,
            ok=False,
            summary=_safe_summary(safe_tool, stable_code),
            safe_details=_audit_details(tool_name, {}, confirmation_contract),
            error_code=stable_code,
            elapsed_ms=elapsed_ms,
            started_at=started_at,
            finished_at=finished_at,
            confirmation_id=(
                _confirmation_execution_id(confirmation_id, owner_generation)
                if confirmation_id
                else ""
            ),
            finalize_only=bool(confirmation_id),
        )
    except Exception as exc:
        logger.warning(
            "Agent 动作审计写入失败 tool=%s type=%s", tool_name, type(exc).__name__
        )


def read_actions(*, owner: str, limit: int = 50, outcome: str = "all", before_id: int | None = None) -> dict:
    """按真实记录ID倒序分页读取当前身份审计，count只代表当前页。"""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("历史条数必须为1到500")
    if not (owner or "").strip():
        raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
    rows = db.list_agent_action_history(
        owner_digest=db.media_owner_digest(owner),
        limit=limit + 1, outcome=outcome, before_id=before_id,
    )
    items: list[dict[str, Any]] = []
    for row in rows[:limit]:
        try:
            details = json.loads(str(row["safe_details"] or "{}"))
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        tool_name = _safe_tool_name(row["tool_name"])
        status = _safe_status(row["status"])
        risk = str(row["risk"] or "").strip()
        if risk not in {"low_write", "write", "danger"}:
            risk = "danger"
        confirmation_contract = _stored_confirmation_contract(details)
        item_outcome = (
            "success"
            if bool(row["ok"])
            else "pending"
            if status == "executing"
            else "unknown"
            if status == "outcome_unknown"
            else "failed"
        )
        item = {
            "id": row["id"],
            "tool": tool_name,
            "label": _TOOL_LABELS.get(tool_name, "Agent 受确认动作"),
            "risk": risk,
            "outcome": item_outcome,
            "status": status,
            "summary": _safe_summary(tool_name, status),
            "details": _safe_details(tool_name, details),
            "error_code": _safe_error_code(row["error_code"]),
            "elapsed_ms": max(0, min(int(row["elapsed_ms"] or 0), 86_400_000)),
            "finished_at": _safe_finished_at(row["finished_at"]),
        }
        if confirmation_contract:
            item["confirmation"] = confirmation_contract
        items.append(item)
    has_more = len(rows) > limit
    return {"items": items, "count": len(items), "outcome": outcome, "has_more": has_more,
            "next": {"before_id": items[-1]["id"], "limit": limit, "outcome": outcome} if has_more else None}
