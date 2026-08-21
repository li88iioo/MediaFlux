"""Media Agent 的受控读取与确认动作工具。"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import threading
import time
from typing import Any
import unicodedata

from app import config, database as db
from app.clients.base import normalize_playback_progress
from app.agent.action_history import action_history_arguments, list_action_history
from app.agent.config_actions import media_server_arguments, test_media_server
from app.agent.config_diagnosis_actions import diagnose_config
from app.agent.config_explain_actions import config_component_arguments, explain_config_component
from app.agent.automation_actions import (
    automation_pipeline_arguments,
    diagnose_automation_pipeline,
)
from app.agent.download_actions import diagnose_download_queue, download_diagnosis_arguments
from app.agent.download_control_actions import (
    delete_download_task,
    delete_download_task_confirmed,
    download_task_arguments,
    pause_download_task,
    pause_download_task_confirmed,
    prepare_delete_download_task,
    prepare_pause_download_task,
    prepare_resume_download_task,
    resume_download_task,
    resume_download_task_confirmed,
)
from app.agent.download_retry_actions import (
    download_retry_submission_arguments,
    prepare_retry_download_submission,
    retry_download_submission,
    retry_download_submission_confirmed,
)
from app.agent.missing_media_workflows import (
    list_missing_workflows,
    missing_workflow_arguments,
)
from app.agent.rss_actions import (
    diagnose_rss,
    get_rss_recent_activity,
    get_rss_subscription_summary,
    list_rss_subscription_summaries,
    rss_diagnosis_arguments,
    rss_subscription_summaries_arguments,
    rss_subscription_summary_arguments,
)
from app.agent.rss_download_actions import (
    preview_rss_pending_download,
    rss_pending_download_arguments,
    rss_pending_download_confirmation_context,
    submit_pending_rss_to_qb,
)
from app.agent.rss_refresh_actions import (
    prepare_rss_subscriptions_refresh,
    preview_rss_subscription_refresh,
    preview_rss_subscriptions_refresh,
    refresh_rss_subscription,
    refresh_rss_subscriptions,
    refresh_rss_subscriptions_confirmed,
    rss_refresh_subscription_arguments,
    rss_refresh_subscription_confirmation_context,
    rss_refresh_subscriptions_arguments,
)
from app.agent.rss_subscription_control_actions import (
    delete_rss_subscription,
    delete_rss_subscription_confirmed,
    prepare_delete_rss_subscription,
    prepare_set_rss_refresh_interval,
    prepare_set_rss_subscription_enabled,
    rss_delete_subscription_arguments,
    rss_refresh_interval_arguments,
    rss_subscription_enabled_arguments,
    set_rss_refresh_interval,
    set_rss_refresh_interval_confirmed,
    set_rss_subscription_enabled,
    set_rss_subscription_enabled_confirmed,
)
from app.agent.media_subscription_actions import (
    get_media_subscription_summary,
    inspect_media_subscription_updates,
    list_media_subscription_summaries,
    media_subscription_enabled_arguments,
    media_subscription_summaries_arguments,
    media_subscription_summary_arguments,
    media_subscription_updates_arguments,
    prepare_set_media_subscription_enabled,
    set_media_subscription_enabled,
    set_media_subscription_enabled_confirmed,
)
from app.agent.rss_retry_actions import (
    preview_rss_failure_retry,
    retry_failed_rss_to_qb,
    rss_failure_retry_arguments,
    rss_failure_retry_confirmation_context,
)
from app.agent.strm_failure_actions import (
    strm_failure_triage_arguments,
    triage_strm_failures,
)
from app.agent.strm_retry_actions import (
    preview_strm_failure_retry,
    retry_strm_failure_records,
    strm_failure_retry_arguments,
    strm_failure_retry_confirmation_context,
)
from app.agent.strm_schedule_config_actions import (
    prepare_strm_schedule_policy_confirmation,
    preview_set_strm_schedule_policy,
    set_strm_schedule_policy,
    set_strm_schedule_policy_confirmed,
    strm_schedule_policy_arguments,
    strm_schedule_policy_confirmation_context,
    strm_schedule_policy_summary_arguments,
    summarize_strm_schedule_policy,
)
from app.agent.workspace_actions import (
    _contains_sensitive_text,
    _safe_status,
    _safe_title,
    _safe_year,
    search_workspace,
    workspace_search_arguments,
)
from app.agent.workspace_todo_actions import summarize_workspace_todo, workspace_todo_arguments
from app.agent.workspace_next_actions import (
    summarize_workspace_next_actions,
    workspace_next_actions_arguments,
)
from app.agent.workspace_briefing_actions import (
    summarize_workspace_briefing,
    workspace_briefing_arguments,
)
from app.agent.web_search_actions import search_web, web_search_arguments
from app.agent.media_rating_actions import lookup_media_rating, media_rating_arguments
from app.agent.discovery_actions import (
    bangumi_calendar,
    calendar_arguments as bangumi_calendar_arguments,
    recommend_arguments as discovery_recommend_arguments,
    recommend_discovery,
    search_arguments as discovery_search_arguments,
    search_discovery,
)
from app.agent.discovery_watchlist_actions import (
    add_watchlist,
    add_watchlist_arguments,
    add_watchlist_confirmed,
    get_watchlist_summary,
    list_watchlist_summaries,
    prepare_add_watchlist,
    prepare_remove_watchlist,
    remove_watchlist,
    remove_watchlist_arguments,
    remove_watchlist_confirmed,
    watchlist_summaries_arguments,
    watchlist_summary_arguments,
)
from app.agent.episode_audit import audit_series_episodes, reset_episode_audit_cache_for_tests
from app.agent.library_episode_count import (
    count_series_episodes,
    count_series_episodes_arguments,
)
from app.agent.library_episode_audit import audit_library_episodes
from app.agent.durable_job_actions import (
    agent_job_status_arguments,
    cancel_agent_job,
    cancel_agent_job_arguments,
    cancel_agent_job_confirmed,
    get_agent_job_status,
    prepare_cancel_agent_job,
    prepare_start_episode_audit,
    start_episode_audit,
    start_episode_audit_arguments,
    start_episode_audit_confirmed,
)
from app.agent.library_patrol_status import (
    get_library_patrol_status,
    patrol_status_arguments,
)
from app.agent.library_patrol_config_actions import (
    patrol_policy_arguments,
    patrol_policy_confirmation_context,
    patrol_policy_summary_arguments,
    prepare_patrol_policy_confirmation,
    preview_set_patrol_policy,
    set_patrol_policy,
    set_patrol_policy_confirmed,
    summarize_patrol_policy,
)
from app.agent.update_actions import check_library_updates
from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.feature_actions import (
    feature_summary_arguments,
    feature_state_arguments,
    feature_state_confirmation_context,
    preview_set_feature_state,
    set_feature_state,
    summarize_feature_states,
    verify_feature_state_write,
)
from app.agent.indexer_config_actions import (
    indexer_sites_arguments,
    indexer_sites_confirmation_context,
    indexer_sites_summary_arguments,
    preview_set_indexer_sites,
    set_indexer_sites,
    set_indexer_sites_confirmed,
    summarize_indexer_sites,
    verify_indexer_sites_write,
)
from app.agent.safe_policy_actions import (
    SAFE_POLICY_IDS,
    prepare_safe_policy_confirmation,
    preview_set_safe_policy,
    safe_policy_arguments,
    safe_policy_confirmation_context,
    safe_policy_summary_arguments,
    set_safe_policy,
    set_safe_policy_confirmed,
    summarize_safe_policies,
)
from app.agent.telegram_test_actions import (
    prepare_telegram_test_notification,
    preview_telegram_test_notification,
    send_telegram_test_notification,
    send_telegram_test_notification_confirmed,
    telegram_test_arguments,
    telegram_test_confirmation_context,
)
from app.agent.indexer_readiness_actions import (
    diagnose_indexer_readiness,
    indexer_readiness_arguments,
)
from app.agent.indexer_actions import (
    preview_submit_resource,
    search_arguments as indexer_search_arguments,
    search_resources,
    submit_arguments as indexer_submit_arguments,
    submit_confirmation_context,
    submit_resource,
)
from app.agent.local_media_actions import (
    diagnose_local_media,
    local_media_diagnosis_arguments,
    local_media_history_arguments,
    local_media_review_queue_arguments,
    summarize_local_media_history,
    summarize_local_media_review_queue,
)
from app.agent.local_media_source_actions import (
    get_local_media_source_summary,
    list_local_media_source_summaries,
    local_media_source_summaries_arguments,
    local_media_source_summary_arguments,
    local_media_source_trigger_arguments,
    prepare_set_local_media_source_trigger_enabled,
    set_local_media_source_trigger_enabled,
    set_local_media_source_trigger_enabled_confirmed,
)
from app.agent.media_server_actions import (
    diagnose_media_servers,
    media_server_diagnosis_arguments,
)
from app.agent.media_proxy_actions import (
    media_proxy_enabled_arguments,
    media_proxy_status_arguments,
    media_proxy_test_arguments,
    prepare_set_media_proxy_instance_enabled,
    set_media_proxy_instance_enabled,
    set_media_proxy_instance_enabled_confirmed,
    summarize_media_proxy_status,
    test_media_proxy_instance,
)
from app.agent.recognition_toggle_actions import (
    prepare_set_recognition_rule_enabled,
    recognition_rule_enabled_arguments,
    set_recognition_rule_enabled,
    set_recognition_rule_enabled_confirmed,
)
from app.agent.media_health_actions import (
    diagnose_workspace_health,
    workspace_health_arguments,
)
from app.agent.models import Evidence, RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.guangya_schedule_config_actions import (
    get_guangya_connection_status,
    guangya_connection_status_arguments,
    guangya_organize_schedule_policy_arguments,
    guangya_organize_schedule_policy_confirmation_context,
    guangya_organize_schedule_policy_summary_arguments,
    prepare_guangya_organize_schedule_policy_confirmation,
    preview_set_guangya_organize_schedule_policy,
    set_guangya_organize_schedule_policy,
    set_guangya_organize_schedule_policy_confirmed,
    summarize_guangya_organize_schedule_policy,
)
from app.agent.organize_actions import (
    clean_empty_guangya_organize_sources,
    clean_empty_guangya_organize_sources_confirmed,
    organize_clean_empty_confirmation_context,
    organize_confirmation_context,
    organize_stop_confirmation_context,
    preview_guangya_organize,
    preview_guangya_organize_clean_empty,
    prepare_guangya_organize_clean_empty,
    prepare_guangya_organize_run_once,
    preview_guangya_organize_run_once,
    preview_guangya_organize_stop,
    prepare_guangya_organize_stop,
    run_guangya_organize_once,
    run_guangya_organize_once_confirmed,
    stop_guangya_organize,
    stop_guangya_organize_confirmed,
)
from app.agent.organize_audit_actions import (
    audit_organize_logs,
    organize_audit_arguments,
)
from app.agent.registry import AgentToolError, ToolRegistry
from app.services import search_media_servers

_SEARCH_CACHE_TTL_SECONDS = 15
_search_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_search_cache_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")


def _no_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, set())
    return {}


def _normalize_search_query(value: str) -> str:
    query = unicodedata.normalize("NFKC", value).strip()
    if (
        not query
        or len(query) > 120
        or any(unicodedata.category(char).startswith("C") for char in query)
    ):
        raise AgentToolError("搜索关键词必须为 1 到 120 个可见字符")
    if _contains_sensitive_text(query):
        raise AgentToolError("搜索关键词疑似包含路径、链接、凭据、哈希或业务标识")
    return query


def _optional_visible_text(value: Any, *, name: str, maximum: int = 80) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise AgentToolError(f"{name} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).strip(" ，。！？?、:：")
    if (
        not text
        or len(text) > maximum
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        raise AgentToolError(f"{name} 必须是 1 到 {maximum} 个可见字符")
    if _contains_sensitive_text(text):
        raise AgentToolError(f"{name} 疑似包含路径、链接、凭据、哈希或业务标识")
    return text


def _search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "limit"})
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_search_query(raw_query)
    limit = arguments.get("limit", 8)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AgentToolError("limit 必须是 1 到 50 的整数")
    return {"query": query, "limit": limit}


def _episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "tmdb_id", "season", "target_episode", "as_of", "library_name"})
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_search_query(raw_query)

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise AgentToolError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10):
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")

    season = arguments.get("season")
    if season is not None and (isinstance(season, bool) or not isinstance(season, int) or not 1 <= season <= 100):
        raise AgentToolError("season 必须是 1 到 100 的整数")

    target_episode = arguments.get("target_episode")
    if target_episode is not None and (
        isinstance(target_episode, bool)
        or not isinstance(target_episode, int)
        or not 1 <= target_episode <= 1000
    ):
        raise AgentToolError("target_episode 必须是 1 到 1000 的整数")
    if target_episode is not None and season is None:
        raise AgentToolError("target_episode 必须与 season 一起提供")

    library_name = _optional_visible_text(
        arguments.get("library_name", ""),
        name="library_name",
        maximum=80,
    )

    as_of = arguments.get("as_of", date.today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > date.today():
        raise AgentToolError("as_of 不能晚于今天")
    normalized = {"query": query, "tmdb_id": tmdb_id, "season": season, "as_of": parsed_as_of.isoformat()}
    if library_name:
        normalized["library_name"] = library_name
    if target_episode is not None:
        normalized["target_episode"] = target_episode
    return normalized


def _library_episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"as_of", "max_series"})
    as_of = arguments.get("as_of", date.today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > date.today():
        raise AgentToolError("as_of 不能晚于今天")
    max_series = arguments.get("max_series", 50)
    if isinstance(max_series, bool) or not isinstance(max_series, int) or not 1 <= max_series <= 100:
        raise AgentToolError("max_series 必须是 1 到 100 的整数")
    return {"as_of": parsed_as_of.isoformat(), "max_series": max_series}


def _library_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "media_type", "tmdb_id", "season", "as_of"})
    media_type = arguments.get("media_type", "auto")
    if not isinstance(media_type, str) or media_type not in {"auto", "tv", "movie"}:
        raise AgentToolError("media_type 必须是 auto、tv 或 movie")
    normalized = _episode_audit_arguments({key: value for key, value in arguments.items() if key != "media_type"})
    if media_type == "movie" and normalized.get("season") is not None:
        raise AgentToolError("电影更新核对不支持 season 参数")
    normalized["media_type"] = media_type
    return normalized


def _bounded_int(value: Any, *, maximum: int = 1_000_000_000) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def _safe_timestamp(value: Any) -> str:
    text = str(value or "").strip()[:32]
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text


def _safe_choice(value: Any, allowed: set[str], default: str = "") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def diagnose_strm(_arguments: dict[str, Any]) -> ToolResult:
    diagnostics = db.list_strm_index_diagnostics(config.get("STRM_ROOT", ""))
    failures = db.summarize_strm_failures()
    runs = db.list_task_runs("strm_sync", limit=5)
    recent_runs = [
        {
            "status": str(row["status"] or ""),
            "trigger": str(row["trigger_type"] or ""),
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }
        for row in runs
    ]
    missing = int(diagnostics.get("missing", 0) or 0)
    open_failures = int(failures.get("open", 0) or 0)
    issues = missing + open_failures
    status = "healthy" if issues == 0 else "attention"
    suggestions: list[str] = []
    if missing:
        suggestions.append(f"有 {missing} 条 STRM 索引对应文件缺失，建议先核对来源和输出目录。")
    if open_failures:
        suggestions.append(f"有 {open_failures} 条未解决 STRM 失败记录，建议按来源查看失败原因。")
    if not recent_runs:
        suggestions.append("尚无 STRM 同步运行记录，可在配置完成后手动执行一次。")
    return ToolResult(
        ok=issues == 0,
        status=status,
        summary="STRM 索引与失败记录正常" if not issues else f"STRM 发现 {issues} 项需要关注",
        data={
            "index": {
                "total": int(diagnostics.get("total", 0) or 0),
                "existing": int(diagnostics.get("existing", 0) or 0),
                "missing": missing,
                "real_source": int(diagnostics.get("real_source", 0) or 0),
                "test_artifacts": int(diagnostics.get("confirmed_test_artifact", 0) or 0),
            },
            "failures": {
                "open": open_failures,
                "resolved": int(failures.get("resolved", 0) or 0),
                "source_count": len(list(failures.get("sources", []))),
                "by_source": [
                    {"label": f"来源 {index}", "open": int(item.get("open", 0) or 0)}
                    for index, item in enumerate(list(failures.get("sources", [])), start=1)
                ],
            },
            "recent_runs": recent_runs,
        },
        evidence=[
            Evidence("sqlite:strm_index", "统计 STRM 索引及文件存在性。", _now()),
            Evidence("sqlite:strm_failures", "统计未解决和已解决的 STRM 失败记录。", _now()),
            Evidence("sqlite:task_runs", "读取最近 5 次 STRM 同步状态，不返回运行结果正文。", _now()),
        ],
        suggestions=suggestions,
    )


_STRM_CONFIRMATION_KEYS = (
    "GY_STRM_SOURCE_DIRS",
    "GY_STRM_BASE_URL",
    "STRM_ROOT",
    "STRM_VIDEO_EXTS",
    "STRM_METADATA_ENABLED",
    "STRM_METADATA_EXTS",
    "STRM_SKIP_THRESHOLD_MB",
    "STRM_NOTIFY_ENABLED",
    "GY_ORGANIZE_STRM_DETAIL_NOTIFY",
)


def _strm_confirmation_context(_arguments: dict[str, Any]) -> str:
    """绑定本次确认时的 STRM 执行配置；摘要只保留在服务端。"""
    payload = {key: config.get(key, "") for key in _STRM_CONFIRMATION_KEYS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_strm_run_once(_arguments: dict[str, Any]) -> ToolResult:
    """只做运行前检查，不启动任务。"""
    from app.modules.scheduler import get_scheduler

    scheduler = get_scheduler()
    if scheduler.validate_config(auto_only=False):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 当前无法启动",
            error="请先补全 STRM 来源、播放地址和输出目录。",
            suggestions=["请先检查 STRM 配置，再重新发起预检。"],
        )
    raw = scheduler.status()
    if bool(raw.get("running")):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="STRM 同步任务已在运行",
            error="请等待当前任务结束后再试。",
            suggestions=["可询问：查看 STRM 同步进度。"],
        )
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将启动一次 STRM 全量同步",
        data={
            "action": "strm.run_once",
            "trigger": "manual",
            "effects": [
                "扫描当前配置的全部 STRM 来源",
                "按现有规则创建、更新或清理 STRM 与伴随元数据",
                "根据现有配置执行通知和媒体库刷新",
            ],
        },
        evidence=[Evidence("strm_scheduler", "已完成脱敏运行前检查；尚未启动任务。", _now())],
        suggestions=["确认前请核对 STRM 来源、输出目录和清理规则。"],
    )


def run_strm_once(_arguments: dict[str, Any]) -> ToolResult:
    """确认后固定以 manual 触发一次 STRM 同步。"""
    from app.modules.scheduler import get_scheduler

    scheduler = get_scheduler()
    if scheduler.validate_config(auto_only=False):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 当前无法启动",
            error="相关配置无效，请重新检查后再发起确认。",
        )
    triggered = scheduler.trigger("manual")
    if not bool(triggered.get("ok")):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="STRM 同步任务已在运行",
            error="当前任务未重复提交。",
            suggestions=["可询问：查看 STRM 同步进度。"],
        )
    return ToolResult(
        ok=True,
        status="accepted",
        summary="STRM 同步任务已提交",
        data={"accepted": True, "trigger": "manual"},
        evidence=[Evidence("strm_scheduler", "已通过一次性确认票据提交手动同步；未返回目录或运行详情。", _now())],
        suggestions=["可询问：查看 STRM 同步进度。"],
    )


def strm_runtime_status(_arguments: dict[str, Any]) -> ToolResult:
    """读取 STRM 调度器的脱敏运行快照。"""
    from app.modules.scheduler import get_scheduler

    raw = get_scheduler().status()
    running = bool(raw.get("running"))
    configured = not bool(raw.get("config_error"))
    progress = raw.get("progress") if isinstance(raw.get("progress"), dict) else {}
    total = max(1, _bounded_int(progress.get("total")))
    completed = min(_bounded_int(progress.get("completed")), total)
    percent = min(
        100,
        _bounded_int(
            progress.get("percent", int(completed * 100 / total)),
            maximum=100,
        ),
    )
    stage = _safe_choice(
        progress.get("stage"),
        {"idle", "scan", "generate", "metadata", "cleanup", "retry", "refresh", "complete", "failed"},
        "running" if running else "idle",
    )

    source_counts: dict[str, int] = {}
    for item in raw.get("source_runtime") or []:
        if not isinstance(item, dict):
            continue
        state = _safe_choice(
            item.get("status"),
            {"pending", "running", "completed", "failed", "stopped"},
            "unknown",
        )
        source_counts[state] = source_counts.get(state, 0) + 1

    last = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else {}
    last_status = _safe_choice(
        last.get("status"),
        {"running", "success", "failed", "stopped", "cancelled"},
    )
    last_run = {
        "status": last_status,
        "trigger_type": _safe_choice(last.get("trigger_type"), {"manual", "cron", "telegram"}),
        "started_at": _safe_timestamp(last.get("started_at")),
        "finished_at": _safe_timestamp(last.get("finished_at")),
    }

    suggestions: list[str] = []
    if not configured:
        ok, status, summary = False, "not_configured", "STRM 配置尚不完整"
        suggestions.append("请先补全 STRM 来源、播放地址和输出目录。")
    elif running:
        ok, status, summary = True, "running", "STRM 同步正在运行"
    elif last_status == "failed":
        ok, status, summary = False, "attention", "最近一次 STRM 同步未成功"
        suggestions.append("可继续使用 STRM 诊断工具检查索引和失败记录。")
    else:
        ok, status, summary = True, "ready", "STRM 当前空闲"

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "enabled": bool(raw.get("enabled")),
            "configured": configured,
            "cron_valid": bool(raw.get("cron_valid")),
            "running": running,
            "current_trigger": _safe_choice(raw.get("current_trigger"), {"manual", "cron", "telegram"}),
            "next_run": _safe_timestamp(raw.get("next_run")),
            "progress": {
                "stage": stage,
                "completed": completed,
                "total": total,
                "percent": percent,
            },
            "sources": {"total": sum(source_counts.values()), "by_status": source_counts},
            "last_run": last_run,
        },
        evidence=[Evidence("strm_scheduler", "读取 STRM 调度器脱敏快照；未返回目录、来源标识或错误正文。", _now())],
        suggestions=suggestions,
    )


def guangya_organize_status(_arguments: dict[str, Any]) -> ToolResult:
    """读取光鸭整理任务与调度器的脱敏运行快照。"""
    from app.modules.organize_tasks import get_organize_manager

    raw = get_organize_manager().status()
    task_status = _safe_choice(
        raw.get("status"),
        {"idle", "running", "stopping", "completed", "partial", "stopped", "failed"},
        "idle",
    )
    running = task_status in {"running", "stopping"}
    allowed_stats = {
        "total", "matched", "need_confirm", "moved", "renamed", "rename_failed",
        "metadata_moved", "stopped", "skipped", "conflict", "failed",
        "subtitle_moved", "subtitle_skipped", "replacement_cleanup_failed",
        "empty_dir_cleanup_failed", "source_dir_cleanup_failed", "audit_failures",
    }
    stats = {
        key: _bounded_int(value)
        for key, value in (raw.get("stats") or {}).items()
        if key in allowed_stats
    } if isinstance(raw.get("stats"), dict) else {}

    schedule_raw = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    schedule = {
        "enabled": bool(schedule_raw.get("enabled")),
        "configured": not bool(schedule_raw.get("config_error")),
        "cron_valid": bool(schedule_raw.get("cron_valid")),
        "next_run": _safe_timestamp(schedule_raw.get("next_run")),
    }

    if running:
        ok, status, summary = True, "running", "光鸭整理任务正在运行"
        suggestions: list[str] = []
    elif task_status == "failed":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务未成功"
        suggestions = ["请到网盘整理页查看任务详情后再决定是否重试。"]
    elif task_status == "completed":
        ok, status, summary = True, "completed", "最近一次光鸭整理任务已完成"
        suggestions = []
    elif task_status == "partial":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务部分完成"
        suggestions = ["请到网盘整理页核对失败项后再决定是否重试。"]
    elif task_status == "stopped":
        ok, status, summary = True, "stopped", "最近一次光鸭整理任务已停止"
        suggestions = []
    else:
        ok, status, summary = True, "idle", "光鸭整理任务当前空闲"
        suggestions = []

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "task": {
                "status": task_status,
                "running": running,
                "stoppable": bool(raw.get("stoppable")) if running else False,
                "trigger_type": _safe_choice(raw.get("trigger_type"), {"manual", "cron", "telegram"}),
                "started_at": _safe_timestamp(raw.get("started_at")),
                "finished_at": _safe_timestamp(raw.get("finished_at")),
                "stats": stats,
            },
            "schedule": schedule,
        },
        evidence=[Evidence("guangya_organizer", "读取光鸭整理任务脱敏快照；未返回目录、任务标识或错误正文。", _now())],
        suggestions=suggestions,
    )


def _search_sources(query: str, limit: int) -> list[dict[str, Any]]:
    cache_key = (query.casefold(), limit)
    now = time.monotonic()
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            return cached[1]
    sources = search_media_servers(query, limit=limit)
    with _search_cache_lock:
        _search_cache[cache_key] = (now, sources)
        if len(_search_cache) > 128:
            expired = [key for key, value in _search_cache.items() if now - value[0] >= _SEARCH_CACHE_TTL_SECONDS]
            for key in expired:
                _search_cache.pop(key, None)
    return sources


def reset_agent_tool_caches_for_tests() -> None:
    with _search_cache_lock:
        _search_cache.clear()
    reset_episode_audit_cache_for_tests()


def _safe_optional_index(value: Any, *, allow_zero: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    minimum = 0 if allow_zero else 1
    return value if minimum <= value <= 10_000 else None


def _safe_runtime_minutes(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        runtime = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return runtime if 0 <= runtime <= 525_600 else 0


def _safe_overview(value: Any) -> str:
    overview = unicodedata.normalize("NFKC", str(value or "")).strip()
    overview = " ".join(overview.split())
    if not overview or _contains_sensitive_text(overview):
        return ""
    return overview[:500]


def search_library(arguments: dict[str, Any]) -> ToolResult:
    query = arguments["query"]
    sources = _search_sources(query, arguments["limit"])
    serialized_sources: list[dict[str, Any]] = []
    total = 0
    unavailable = 0
    for source in sources:
        items: list[dict[str, Any]] = []
        source_unavailable = bool(source.get("error"))
        if source_unavailable:
            unavailable += 1
        else:
            server_type = _safe_status(source.get("server_type"), {"jellyfin", "emby"})
            if server_type == "unknown":
                server_type = "media_server"
            for item in source.get("items", []):
                title = _safe_title(item.name or item.display_name, "媒体条目")
                display_name = _safe_title(item.display_name, title)
                series_name = _safe_title(item.series_name, "") if item.series_name else ""
                items.append({
                    "title": title,
                    "display_name": display_name,
                    "media_type": _safe_status(
                        item.type,
                        {"movie", "series", "episode", "season", "video"},
                    ),
                    "year": _safe_year(item.year),
                    "series_name": series_name,
                    "season": _safe_optional_index(item.season_number, allow_zero=True),
                    "episode": _safe_optional_index(item.episode_number, allow_zero=False),
                    "runtime_minutes": _safe_runtime_minutes(item.runtime),
                    "playback_progress_percent": normalize_playback_progress(item.progress),
                    "overview": _safe_overview(item.overview),
                })
        total += len(items)
        server_type = _safe_status(source.get("server_type"), {"jellyfin", "emby"})
        if server_type == "unknown":
            server_type = "media_server"
        source_match = "unknown" if source_unavailable else ("found" if items else "not_found")
        serialized_sources.append({
            "server_type": server_type,
            "server_name": _safe_title(source.get("server_name"), "媒体服务器"),
            "status": "unavailable" if source_unavailable else "ready",
            "match_status": source_match,
            "returned": len(items),
            "items": items,
        })
    if not sources:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="没有可搜索的媒体服务器",
            data={
                "query": query,
                "total": 0,
                "match_status": "not_configured",
                "sources": [],
            },
            evidence=[Evidence("media_servers", "检查已启用且凭据完整的 Jellyfin / Emby 配置。", _now())],
            suggestions=["请先在设置中完整配置并启用 Jellyfin 或 Emby。"],
        )
    available = len(sources) - unavailable
    if unavailable == len(sources):
        status = "unavailable"
        match_status = "indeterminate"
        summary = "媒体服务器暂时不可用，无法判断是否存在匹配内容"
        suggestions = ["请检查媒体服务器连通性后重试。"]
    elif total:
        status = "partial" if unavailable else "success"
        match_status = "found"
        summary = f"在媒体库中找到 {total} 项结果"
        suggestions = [f"有 {unavailable} 个媒体服务器暂时不可用。"] if unavailable else []
    else:
        status = "partial" if unavailable else "empty"
        match_status = "indeterminate" if unavailable else "not_found"
        summary = (
            f"已查询 {available} 个媒体服务器，另有 {unavailable} 个暂时不可用；可用来源未找到匹配内容"
            if unavailable else "媒体库中没有找到匹配内容"
        )
        suggestions = (["请检查不可用的媒体服务器后重试。"] if unavailable else []) + [
            "可尝试中文名、原名或去掉季集编号后重新搜索。"
        ]
    return ToolResult(
        ok=available > 0,
        status=status,
        summary=summary,
        data={
            "query": query,
            "total": total,
            "match_status": match_status,
            "sources": serialized_sources,
        },
        evidence=[Evidence("media_servers", f"查询 {len(sources)} 个已配置媒体服务器。", _now())],
        suggestions=suggestions,
    )


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="config.diagnose",
        description="检查媒体服务器、TMDB、下载器、STRM 与 AI 回退配置是否完整，不返回配置值。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_config,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="config.explain_component",
        description="解释一个白名单配置组件的状态、必要字段标签、受影响能力与安全下一步，不返回配置键或配置值。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["component"],
            "properties": {
                "component": {
                    "type": "string",
                    "enum": [
                        "jellyfin", "emby", "tmdb", "qbittorrent", "strm",
                        "ai_recognition", "discovery", "douban",
                        "resource_results", "indexer_search",
                    ],
                },
            },
            "additionalProperties": False,
        },
        handler=explain_config_component,
        validator=config_component_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="config.feature_summary",
        description="只读汇总媒体探索、资源检索与联网搜索的启用状态和依赖可用性，不返回配置值或供应商凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_feature_states,
        validator=feature_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="automation.diagnose_pipeline",
        description="只读汇总下载、RSS、光鸭整理与 STRM 的本地自动化状态，不访问外部服务且不返回路径、凭据或业务标识。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_automation_pipeline,
        validator=automation_pipeline_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="local_media.diagnose",
        description="只读汇总本地媒体来源、整理任务与调度器状态，不扫描文件系统、不访问外部服务且不返回路径或业务标识。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_local_media,
        validator=local_media_diagnosis_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="local_media.source_summaries",
        description="只读列出本地媒体来源的公开序号、触发状态和安全配置摘要，不返回名称、路径、媒体库标识或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_local_media_source_summaries,
        validator=local_media_source_summaries_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="local_media.get_source_summary",
        description="只读查看一个公开序号对应的本地媒体来源触发状态与安全摘要，不返回名称、路径、媒体库标识或凭据。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["source_number"],
            "properties": {"source_number": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        handler=get_local_media_source_summary,
        validator=local_media_source_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="local_media.set_source_trigger_enabled",
        description="确认后精确启停一个本地媒体来源的 qB 下载完成接管或目录扫描；不修改目录、规则、目标或凭据。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["source_number", "trigger", "enabled"],
            "properties": {
                "source_number": {"type": "integer", "minimum": 1},
                "trigger": {"type": "string", "enum": ["qb_completed", "scan"]},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_local_media_source_trigger_enabled,
        validator=local_media_source_trigger_arguments,
        requires_confirmation=True,
        confirmed_handler=set_local_media_source_trigger_enabled_confirmed,
        confirmation_preparer=prepare_set_local_media_source_trigger_enabled,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="local_media.review_queue_summary",
        description="只读汇总本地媒体待人工确认队列的数量、触发来源和等待时长，不返回标题、路径、任务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_local_media_review_queue,
        validator=local_media_review_queue_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="local_media.history_summary",
        description="只读汇总本地媒体已完成与失败历史的数量、触发来源和时间分布，不返回标题、路径、任务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_local_media_history,
        validator=local_media_history_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="downloads.diagnose_queue",
        description="只读诊断 qBittorrent 当前队列、传输状态与疑似停滞任务，不返回 hash、路径或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_download_queue,
        validator=download_diagnosis_arguments,
        llm_read=True,
        llm_read_plan=True,
        llm_examples=(
            "检查下载队列有没有异常",
            "qBittorrent 里有没有卡住的任务",
        ),
    ))
    download_task_parameters = {
        "type": "object",
        "required": ["task_name"],
        "properties": {
            "task_name": {"type": "string", "minLength": 1, "maxLength": 240},
        },
        "additionalProperties": False,
    }
    registry.register(ToolSpec(
        name="downloads.pause_task",
        description="预检并在用户确认后暂停一个名称完全匹配的 qBittorrent 任务；不暴露 hash 或路径。",
        risk=RiskLevel.LOW_WRITE,
        parameters=download_task_parameters,
        handler=pause_download_task,
        validator=download_task_arguments,
        requires_confirmation=True,
        confirmed_handler=pause_download_task_confirmed,
        confirmation_preparer=prepare_pause_download_task,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="downloads.resume_task",
        description="预检并在用户确认后恢复一个名称完全匹配的 qBittorrent 暂停任务；不暴露 hash 或路径。",
        risk=RiskLevel.LOW_WRITE,
        parameters=download_task_parameters,
        handler=resume_download_task,
        validator=download_task_arguments,
        requires_confirmation=True,
        confirmed_handler=resume_download_task_confirmed,
        confirmation_preparer=prepare_resume_download_task,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="downloads.delete_task",
        description="预检并在用户确认后只从 qBittorrent 移除一个名称完全匹配的任务；绝不删除下载文件。",
        risk=RiskLevel.DANGER,
        parameters=download_task_parameters,
        handler=delete_download_task,
        validator=download_task_arguments,
        requires_confirmation=True,
        confirmed_handler=delete_download_task_confirmed,
        confirmation_preparer=prepare_delete_download_task,
    ))
    registry.register(ToolSpec(
        name="downloads.retry_submission",
        description=(
            "预检并在用户确认后，将一条明确编号的下载待处理记录重新提交到 "
            "qBittorrent、光鸭或两者；不返回资源链接、种子、路径、任务标识或凭据。"
        ),
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "required": ["request_id", "target"],
            "properties": {
                "request_id": {"type": "integer", "minimum": 1},
                "target": {"type": "string", "enum": ["qb", "guangya", "both"]},
            },
            "additionalProperties": False,
        },
        handler=retry_download_submission,
        validator=download_retry_submission_arguments,
        requires_confirmation=True,
        confirmed_handler=retry_download_submission_confirmed,
        confirmation_preparer=prepare_retry_download_submission,
    ))
    registry.register(ToolSpec(
        name="rss.diagnose",
        description="只读诊断 RSS 订阅、待处理、失败与长期提交中条目，不访问订阅源且不返回 URL、GUID、payload 或路径。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_rss,
        validator=rss_diagnosis_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="rss.subscription_summaries",
        description="只读列出有界 RSS 订阅安全摘要，仅含编号、名称、启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_rss_subscription_summaries,
        validator=rss_subscription_summaries_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="rss.get_subscription_summary",
        description="按精确订阅编号读取 RSS 安全摘要，仅含名称、启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["subscription_id"],
            "properties": {"subscription_id": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        handler=get_rss_subscription_summary,
        validator=rss_subscription_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="rss.set_subscription_enabled",
        description="预检并在用户确认后启用或停用一个指定 RSS 订阅；不返回名称、URL、过滤词、条目或凭据。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["subscription_id", "enabled"],
            "properties": {
                "subscription_id": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_rss_subscription_enabled,
        validator=rss_subscription_enabled_arguments,
        requires_confirmation=True,
        confirmed_handler=set_rss_subscription_enabled_confirmed,
        confirmation_preparer=prepare_set_rss_subscription_enabled,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="rss.set_refresh_interval",
        description="预检并在用户确认后调整一个指定 RSS 订阅的自动刷新周期；0 表示关闭自动刷新，不会立即抓取或下载。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["subscription_id", "refresh_interval_minutes"],
            "properties": {
                "subscription_id": {"type": "integer", "minimum": 1},
                "refresh_interval_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
            },
            "additionalProperties": False,
        },
        handler=set_rss_refresh_interval,
        validator=rss_refresh_interval_arguments,
        requires_confirmation=True,
        confirmed_handler=set_rss_refresh_interval_confirmed,
        confirmation_preparer=prepare_set_rss_refresh_interval,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="rss.delete_subscription",
        description="预检并在用户确认后永久删除一个指定 RSS 订阅及其本地条目记录；不删除下载任务或已下载文件。",
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "required": ["subscription_id"],
            "properties": {
                "subscription_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        handler=delete_rss_subscription,
        validator=rss_delete_subscription_arguments,
        requires_confirmation=True,
        confirmed_handler=delete_rss_subscription_confirmed,
        confirmation_preparer=prepare_delete_rss_subscription,
    ))
    registry.register(ToolSpec(
        name="media.subscription_summaries",
        description="只读列出有界媒体追更订阅摘要，仅含编号、标题、媒体类型、启用状态和缺失数量。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_media_subscription_summaries,
        validator=media_subscription_summaries_arguments,
        llm_read=True,
        llm_examples=(
            "我订阅了哪些媒体",
            "列出当前的追更订阅",
        ),
    ))
    registry.register(ToolSpec(
        name="media.subscription_updates",
        description=(
            "实时检查全部媒体追更订阅：逐条比较 TMDB 已播清单与 Jellyfin/Emby 本地库存，"
            "并对确认缺失项执行有界多站资源搜索；只返回下载建议，不提交 qBittorrent 或光鸭。"
        ),
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=inspect_media_subscription_updates,
        validator=media_subscription_updates_arguments,
        llm_read=True,
        llm_examples=(
            "我订阅的媒体又更新吗",
            "检查追更订阅有没有新集",
            "看看订阅缺哪些集并搜索资源",
        ),
    ))
    registry.register(ToolSpec(
        name="media.get_subscription_summary",
        description="按精确订阅编号读取一条媒体追更订阅的安全摘要。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["subscription_id"],
            "properties": {"subscription_id": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        handler=get_media_subscription_summary,
        validator=media_subscription_summary_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="media.set_subscription_enabled",
        description="预检并在用户确认后暂停或恢复一个指定媒体追更订阅；不会操作已提交下载任务或媒体文件。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["subscription_id", "enabled"],
            "properties": {
                "subscription_id": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_media_subscription_enabled,
        validator=media_subscription_enabled_arguments,
        requires_confirmation=True,
        confirmed_handler=set_media_subscription_enabled_confirmed,
        confirmation_preparer=prepare_set_media_subscription_enabled,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="rss.recent_activity",
        description="统计最近 24 小时 RSS 成功下载次数，并按订阅名称汇总；不返回 URL、条目正文或路径。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=get_rss_recent_activity,
        validator=rss_subscription_summaries_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="rss.refresh_subscription",
        description="预检并在用户确认后刷新一个指定 RSS 订阅；不自动下载且不返回 URL、过滤词、条目正文或凭据。",
        risk=RiskLevel.WRITE,
        parameters={
            "type": "object",
            "required": ["subscription_id"],
            "properties": {
                "subscription_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        handler=refresh_rss_subscription,
        validator=rss_refresh_subscription_arguments,
        requires_confirmation=True,
        preview_handler=preview_rss_subscription_refresh,
        confirmation_context=rss_refresh_subscription_confirmation_context,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="rss.refresh_subscriptions",
        description="预检并在用户确认后依次刷新一组 RSS 订阅；不自动下载且不返回 URL、过滤词、条目正文或凭据。",
        risk=RiskLevel.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "subscription_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1},
                },
                "scope": {"type": "string", "enum": ["all_enabled"]},
            },
            "oneOf": [
                {"required": ["subscription_ids"]},
                {"required": ["scope"]},
            ],
            "additionalProperties": False,
        },
        handler=refresh_rss_subscriptions,
        validator=rss_refresh_subscriptions_arguments,
        requires_confirmation=True,
        preview_handler=preview_rss_subscriptions_refresh,
        confirmed_handler=refresh_rss_subscriptions_confirmed,
        confirmation_preparer=prepare_rss_subscriptions_refresh,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="rss.submit_pending_to_qb",
        description="预检并在用户确认后，将最新的待处理 RSS 条目有界提交到 qBittorrent；不返回条目、URL、路径或凭据。",
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
        handler=submit_pending_rss_to_qb,
        validator=rss_pending_download_arguments,
        requires_confirmation=True,
        preview_handler=preview_rss_pending_download,
        confirmation_context=rss_pending_download_confirmation_context,
    ))
    registry.register(ToolSpec(
        name="rss.retry_failed_to_qb",
        description="预检并在用户确认后，有界重试已明确分类为可安全重试的 qBittorrent RSS 失败条目；不返回条目、URL、路径、失败原文或凭据。",
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
        handler=retry_failed_rss_to_qb,
        validator=rss_failure_retry_arguments,
        requires_confirmation=True,
        preview_handler=preview_rss_failure_retry,
        confirmation_context=rss_failure_retry_confirmation_context,
    ))
    registry.register(ToolSpec(
        name="config.diagnose_media_servers",
        description="使用服务端当前生效配置汇总诊断 Jellyfin 12 与 Emby / Jellyfin 10.x 节点的连通性、产品版本和兼容槽位，不返回地址、服务器名称或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_media_servers,
        validator=media_server_diagnosis_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="config.test_media_server",
        description="使用服务端当前生效配置测试 Jellyfin 或 Emby / Jellyfin 10.x 的连通性与鉴权，不返回地址或凭据。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["server_type"],
            "properties": {
                "server_type": {"type": "string", "enum": ["jellyfin", "emby"]},
            },
            "additionalProperties": False,
        },
        handler=test_media_server,
        validator=media_server_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="media_proxy.status_summary",
        description="安全汇总媒体反代实例的数量、类型、启用状态与运行状态，不返回地址、端口、路径、实例 ID 或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_media_proxy_status,
        validator=media_proxy_status_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="media_proxy.test_instance",
        description="按公开序号测试一个已保存媒体反代实例的上游连通性，不返回地址、端口、路径、实例 ID、凭据或原始错误。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["instance_number"],
            "properties": {
                "instance_number": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            "additionalProperties": False,
        },
        handler=test_media_proxy_instance,
        validator=media_proxy_test_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="media_proxy.set_instance_enabled",
        description="预检并在用户确认后按公开序号启用或停用一个媒体反代实例；不会修改地址、监听、路径或凭据。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["instance_number", "enabled"],
            "properties": {
                "instance_number": {"type": "integer", "minimum": 1, "maximum": 10000},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_media_proxy_instance_enabled,
        validator=media_proxy_enabled_arguments,
        requires_confirmation=True,
        confirmation_preparer=prepare_set_media_proxy_instance_enabled,
        confirmed_handler=set_media_proxy_instance_enabled_confirmed,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="recognition.set_rule_enabled",
        description="预检并在用户确认后，按明确规则类型和编号启用或停用一条识别规则；不会修改规则内容、映射、别名或优先级。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["rule_type", "rule_id", "enabled"],
            "properties": {
                "rule_type": {
                    "type": "string",
                    "enum": [
                        "preprocess_rule",
                        "tmdb_regex_rule",
                        "knowledge_entry",
                    ],
                },
                "rule_id": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_recognition_rule_enabled,
        validator=recognition_rule_enabled_arguments,
        requires_confirmation=True,
        confirmation_preparer=prepare_set_recognition_rule_enabled,
        confirmed_handler=set_recognition_rule_enabled_confirmed,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="config.indexer_sites_summary",
        description="读取当前固定白名单资源站点的选择，仅返回站点 ID、展示名和数量。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_indexer_sites,
        validator=indexer_sites_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="config.set_indexer_sites",
        description="预检并在用户确认后更新固定白名单资源站点，不接受配置键、URL、凭据、Cookie 或路径。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["site_ids"],
            "properties": {
                "site_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": [
                            "nyaa", "mikan", "btbtla", "1lou",
                            "animetosho", "tpb", "sukebei",
                        ],
                    },
                },
                "enable_search": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_indexer_sites,
        validator=indexer_sites_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_indexer_sites,
        confirmation_context=indexer_sites_confirmation_context,
        confirmed_handler=set_indexer_sites_confirmed,
        post_write_verifier=verify_indexer_sites_write,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="telegram.send_test_notification",
        description="预检并在用户确认后向当前已配置会话发送一条固定 Telegram 连接测试消息；不接受消息、凭据或会话参数。",
        risk=RiskLevel.LOW_WRITE,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=send_telegram_test_notification,
        validator=telegram_test_arguments,
        requires_confirmation=True,
        preview_handler=preview_telegram_test_notification,
        confirmation_context=telegram_test_confirmation_context,
        confirmed_handler=send_telegram_test_notification_confirmed,
        confirmation_preparer=prepare_telegram_test_notification,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="config.safe_policy_summary",
        description="读取 Agent 可安全管理的固定白名单策略，只返回公开值和环境托管状态。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_safe_policies,
        validator=safe_policy_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="config.set_safe_policy",
        description="预检并在用户确认后修改一项固定白名单非敏感策略，不接受任意配置键、凭据、URL 或路径。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["policy", "value"],
            "properties": {
                "policy": {"type": "string", "enum": list(SAFE_POLICY_IDS)},
                "value": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ],
                },
            },
            "additionalProperties": False,
        },
        handler=set_safe_policy,
        validator=safe_policy_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_safe_policy,
        confirmation_context=safe_policy_confirmation_context,
        confirmed_handler=set_safe_policy_confirmed,
        confirmation_preparer=prepare_safe_policy_confirmation,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="config.set_feature_state",
        description="预检并在用户确认后开启或关闭一个非敏感白名单功能，不接受配置键或任意配置值。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["feature", "enabled"],
            "properties": {
                "feature": {
                    "type": "string",
                    "enum": [
                        "discovery", "douban", "resource_results", "indexer_search", "web_search",
                        "offline_magnet", "offline_ed2k", "offline_http", "strm_metadata",
                        "download_verification_notify",
                    ],
                },
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_feature_state,
        validator=feature_state_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_feature_state,
        confirmation_context=feature_state_confirmation_context,
        post_write_verifier=verify_feature_state_write,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="strm.diagnose",
        description="检查 STRM 索引、缺失文件、失败记录和最近同步状态，不执行修复。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_strm,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="strm.triage_failures",
        description="只读汇总 STRM 失败账本的状态与动作类别，不返回路径、文件名、来源、对象标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=triage_strm_failures,
        validator=strm_failure_triage_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="strm.retry_failures",
        description="预检并在用户确认后重试当前 STRM 失败项，仅返回聚合计数，不暴露失败明细。",
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["all", "generate", "metadata"],
                    "default": "all",
                },
            },
            "additionalProperties": False,
        },
        handler=retry_strm_failure_records,
        validator=strm_failure_retry_arguments,
        requires_confirmation=True,
        preview_handler=preview_strm_failure_retry,
        confirmation_context=strm_failure_retry_confirmation_context,
    ))
    registry.register(ToolSpec(
        name="strm.run_once",
        description="预检并在用户确认后启动一次 STRM 全量同步，不接受执行参数。",
        risk=RiskLevel.DANGER,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=run_strm_once,
        validator=_no_arguments,
        requires_confirmation=True,
        preview_handler=preview_strm_run_once,
        confirmation_context=_strm_confirmation_context,
    ))
    registry.register(ToolSpec(
        name="strm.status",
        description="查看 STRM 当前运行进度、调度状态和最近结果，不返回目录或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=strm_runtime_status,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="strm.schedule_policy",
        description="读取 STRM 定时同步的启用状态、五段 cron 和任务通知开关，不返回目录、地址或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_strm_schedule_policy,
        validator=strm_schedule_policy_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="strm.set_schedule_policy",
        description="预检并在用户确认后修改 STRM 定时同步的三项白名单策略，不立即运行同步。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "minProperties": 1,
            "properties": {
                "enabled": {"type": "boolean"},
                "cron": {"type": "string", "minLength": 1, "maxLength": 128},
                "notify_enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_strm_schedule_policy,
        validator=strm_schedule_policy_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_strm_schedule_policy,
        confirmation_context=strm_schedule_policy_confirmation_context,
        confirmed_handler=set_strm_schedule_policy_confirmed,
        confirmation_preparer=prepare_strm_schedule_policy_confirmation,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="guangya.connection_status",
        description="验证光鸭账号是否已配置且可通过最小只读请求连接，不刷新或返回凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=get_guangya_connection_status,
        validator=guangya_connection_status_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.schedule_policy",
        description="读取光鸭定时整理的启用状态、五段 cron 和通知开关，不返回目录或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_guangya_organize_schedule_policy,
        validator=guangya_organize_schedule_policy_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.set_schedule_policy",
        description="预检并在用户确认后修改光鸭定时整理三项白名单策略，不立即运行整理。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "minProperties": 1,
            "properties": {
                "enabled": {"type": "boolean"},
                "cron": {"type": "string", "minLength": 1, "maxLength": 128},
                "notify_enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=set_guangya_organize_schedule_policy,
        validator=guangya_organize_schedule_policy_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_guangya_organize_schedule_policy,
        confirmation_context=guangya_organize_schedule_policy_confirmation_context,
        confirmed_handler=set_guangya_organize_schedule_policy_confirmed,
        confirmation_preparer=prepare_guangya_organize_schedule_policy_confirmation,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.status",
        description="查看光鸭整理任务和定时调度的运行状态，不返回目录、任务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=guangya_organize_status,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="organize.audit_logs",
        description="按来源和规范状态只读查看整理记录摘要，不返回路径、任务标识、文件名、外部 ID 或错误正文。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "enum": ["all", "guangya", "local"],
                    "default": "all",
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "all", "failed", "manual", "processing", "success",
                        "skipped", "reverted",
                    ],
                    "default": "all",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "additionalProperties": False,
        },
        handler=audit_organize_logs,
        validator=organize_audit_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.preview",
        description="按当前服务端配置只读预览光鸭整理计划，不移动、改名或删除内容。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=preview_guangya_organize,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.run_once",
        description="预览并在用户确认后按当前配置启动一次光鸭网盘整理，不接受执行参数。",
        risk=RiskLevel.DANGER,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=run_guangya_organize_once,
        validator=_no_arguments,
        requires_confirmation=True,
        preview_handler=preview_guangya_organize_run_once,
        confirmation_context=organize_confirmation_context,
        confirmed_handler=run_guangya_organize_once_confirmed,
        confirmation_preparer=prepare_guangya_organize_run_once,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.stop",
        description="预检并在用户确认后协作式停止当前光鸭整理任务；已完成的云盘操作不会回滚。",
        risk=RiskLevel.DANGER,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=stop_guangya_organize,
        validator=_no_arguments,
        requires_confirmation=True,
        preview_handler=preview_guangya_organize_stop,
        confirmation_context=organize_stop_confirmation_context,
        confirmed_handler=stop_guangya_organize_confirmed,
        confirmation_preparer=prepare_guangya_organize_stop,
    ))
    registry.register(ToolSpec(
        name="guangya.organize.clean_empty",
        description="预检并在用户确认后清理全部已配置光鸭整理来源中的空子目录。",
        risk=RiskLevel.DANGER,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=clean_empty_guangya_organize_sources,
        validator=_no_arguments,
        requires_confirmation=True,
        preview_handler=preview_guangya_organize_clean_empty,
        confirmation_context=organize_clean_empty_confirmation_context,
        confirmed_handler=clean_empty_guangya_organize_sources_confirmed,
        confirmation_preparer=prepare_guangya_organize_clean_empty,
    ))
    registry.register(ToolSpec(
        name="web.search",
        description="通过受控 Tavily Provider 搜索公开网页；结果受固定主机、缓存、频率和每日额度限制。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "topic": {"type": "string", "enum": ["general", "news"], "default": "general"},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
            },
            "additionalProperties": False,
        },
        handler=search_web,
        validator=web_search_arguments,
        llm_read=True,
        llm_examples=(
            "搜索网上的最新消息",
            "联网查公开网页信息",
        ),
    ))
    registry.register(ToolSpec(
        name="discovery.search",
        description="在已启用的 TMDB、豆瓣与 Bangumi 外部数据源中搜索影视元数据，不返回海报原始地址或配置值。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "description": "1 到 120 个非控制可见字符；服务端会执行 NFKC 规范化并去除首尾空白。",
                },
                "page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                "providers": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {"type": "string", "enum": ["tmdb", "douban", "bangumi"]},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        },
        handler=search_discovery,
        validator=discovery_search_arguments,
        llm_read=True,
        llm_examples=(
            "从 TMDB 或豆瓣搜索影视资料",
            "查一部电影的外部元数据",
        ),
    ))
    registry.register(ToolSpec(
        name="discovery.lookup_rating",
        description="按明确影视名称、类型和年份查询豆瓣评分；优先使用豆瓣结构化数据，必要时受控检索并读取已验证的豆瓣条目页。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
                "year": {"type": "string", "pattern": "^(?:19|20)\\d{2}$"},
                "allow_web_fallback": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        handler=lookup_media_rating,
        validator=media_rating_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="discovery.watchlist_summaries",
        description="只读列出有界探索收藏摘要，仅含收藏编号、来源、媒体类型、标题和年份。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_watchlist_summaries,
        validator=watchlist_summaries_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="discovery.get_watchlist_summary",
        description="按精确收藏编号读取一条探索收藏的安全摘要。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["watchlist_number"],
            "properties": {"watchlist_number": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        handler=get_watchlist_summary,
        validator=watchlist_summary_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="discovery.add_watchlist",
        description="预检并在用户确认后把一个精确影视条目加入本地探索收藏；不会下载资源。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["provider", "external_id", "media_type"],
            "properties": {
                "provider": {"type": "string", "enum": ["tmdb", "douban", "bangumi"]},
                "external_id": {"type": "string", "minLength": 1, "maxLength": 180},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "additionalProperties": False,
        },
        handler=add_watchlist,
        validator=add_watchlist_arguments,
        requires_confirmation=True,
        confirmed_handler=add_watchlist_confirmed,
        confirmation_preparer=prepare_add_watchlist,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="discovery.remove_watchlist",
        description="预检并在用户确认后按精确收藏编号移除本地探索收藏；不会删除媒体文件或下载任务。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "required": ["watchlist_number"],
            "properties": {"watchlist_number": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        handler=remove_watchlist,
        validator=remove_watchlist_arguments,
        requires_confirmation=True,
        confirmed_handler=remove_watchlist_confirmed,
        confirmation_preparer=prepare_remove_watchlist,
        llm_confirmation=True,
    ))
    registry.register(ToolSpec(
        name="discovery.recommend",
        description="读取已启用的 TMDB 或豆瓣默认推荐列表，不接受自定义筛选，也不返回海报地址、收藏状态或配置值。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["tmdb", "douban"], "default": "tmdb"},
                "media_type": {"type": "string", "enum": ["movie", "tv"], "default": "movie"},
                "page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "additionalProperties": False,
        },
        handler=recommend_discovery,
        validator=discovery_recommend_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="bangumi.calendar",
        description="读取 Bangumi 本周或指定星期的放送日历，不返回图片地址、收藏状态或 Provider 配置。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "weekday": {"type": "integer", "minimum": 1, "maximum": 7},
                "page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "additionalProperties": False,
        },
        handler=bangumi_calendar,
        validator=bangumi_calendar_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="indexer.diagnose_readiness",
        description="只读检查多站资源索引器的本地开关、启用站点与能力声明；不访问资源站、网络或文件系统，也不返回 URL、Cookie 或凭据。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_indexer_readiness,
        validator=indexer_readiness_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="indexer.search_resources",
        description="在已启用的多站索引中搜索短期资源结果，只返回 opaque result_id 与公开元数据。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "original_title": {"type": "string", "maxLength": 120},
                "english_title": {"type": "string", "maxLength": 120},
                "aliases": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 120},
                },
                "year": {"type": "integer", "minimum": 1800, "maximum": 2200},
                "media_type": {"type": "string", "enum": ["", "movie", "tv", "anime"]},
                "page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                "sites": {
                    "type": "array",
                    "maxItems": 16,
                    "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        },
        handler=search_resources,
        validator=indexer_search_arguments,
        llm_read=True,
        llm_examples=(
            "搜索《某片》的下载资源",
            "找种子或磁力资源",
        ),
    ))
    registry.register(ToolSpec(
        name="indexer.submit_resource",
        description="在用户确认后，以短期 result_id 将一个已搜索资源提交到 qBittorrent、光鸭或两者。",
        risk=RiskLevel.DANGER,
        parameters={
            "type": "object",
            "required": ["result_id", "target"],
            "properties": {
                "result_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$"},
                "target": {"type": "string", "enum": ["qb", "guangya", "both"]},
            },
            "additionalProperties": False,
        },
        handler=submit_resource,
        validator=indexer_submit_arguments,
        requires_confirmation=True,
        preview_handler=preview_submit_resource,
        confirmation_context=submit_confirmation_context,
    ))
    registry.register(ToolSpec(
        name="workspace.briefing",
        description="生成本地系统简报，汇总工作区待办、下载后核验、媒体库巡检、索引器就绪与媒体服务器配置完整性；不访问网络，也不扫描媒体或云盘内容目录。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_workspace_briefing,
        validator=workspace_briefing_arguments,
        llm_read=True,
        llm_read_plan=True,
        llm_examples=(
            "给我一份系统简报",
            "现在有哪些事情需要处理",
        ),
    ))
    registry.register(ToolSpec(
        name="workspace.health",
        description="执行媒体系统健康总检，聚合本地工作区、关键配置与媒体服务器连通性；不扫描内容目录、不搜索资源且不执行写操作。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=diagnose_workspace_health,
        validator=workspace_health_arguments,
        llm_read=True,
        llm_read_plan=True,
        llm_examples=(
            "检查整个媒体系统是否健康",
            "排查配置和媒体服务器连通性",
        ),
    ))
    registry.register(ToolSpec(
        name="workspace.todo",
        description="只读汇总下载、RSS、整理、STRM、本地媒体、下载后核验与媒体库巡检的工作区待办计数；不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_workspace_todo,
        validator=workspace_todo_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="workspace.next_actions",
        description="从本地安全待办快照生成按固定优先级排列的只读下一步行动卡；不执行诊断、预检或写操作，不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_workspace_next_actions,
        validator=workspace_next_actions_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    registry.register(ToolSpec(
        name="workspace.search",
        description="按标题搜索媒体库、RSS、下载、整理与本地媒体工作流；不返回路径、URL、凭据、哈希、业务标识或错误正文。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": ["library", "rss", "downloads", "organize", "local_media"],
                    },
                },
            },
            "additionalProperties": False,
        },
        handler=search_workspace,
        validator=workspace_search_arguments,
        llm_read=True,
    ))
    registry.register(ToolSpec(
        name="library.search",
        description="在已配置的 Jellyfin / Emby 媒体库中搜索标题。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
            },
            "additionalProperties": False,
        },
        handler=search_library,
        validator=_search_arguments,
        llm_read=True,
        llm_examples=(
            "媒体库里有没有《某片》",
            "在 Jellyfin 或 Emby 搜索这个标题",
        ),
    ))

    registry.register(ToolSpec(
        name="library.search_missing_episode_resources",
        description="先确认指定季集属于已播缺集，再定向搜索多站资源；不会自动下载。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query", "season", "episode"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                "season": {"type": "integer", "minimum": 1, "maximum": 100},
                "episode": {"type": "integer", "minimum": 1, "maximum": 1000},
                "as_of": {"type": "string", "format": "date"},
                "sites": {
                    "type": "array",
                    "maxItems": 16,
                    "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        handler=search_missing_episode_resources,
        validator=missing_episode_resource_arguments,
        llm_read=True,
    ))

    registry.register(ToolSpec(
        name="library.search_missing_season_resources",
        description="先完整核对指定季度，再按顺序搜索最多 3 个已播缺集的多站资源；不会自动下载。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query", "season"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                "season": {"type": "integer", "minimum": 1, "maximum": 100},
                "as_of": {"type": "string", "format": "date"},
                "sites": {
                    "type": "array",
                    "maxItems": 16,
                    "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
                },
                "max_episodes": {"type": "integer", "minimum": 1, "maximum": 3},
                "limit_per_episode": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "additionalProperties": False,
        },
        handler=search_missing_season_resources,
        validator=missing_season_resource_arguments,
        llm_read=True,
    ))

    registry.register(ToolSpec(
        name="library.missing_media_workflows",
        description=(
            "查看当前用户最近缺集补库流程的安全状态；只返回剧名、季集、阶段、"
            "目标类型与是否已建立下载任务，不返回资源句柄、磁力、URL、路径或凭据。"
        ),
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
        handler=lambda arguments: list_missing_workflows(arguments, ToolContext()),
        validator=missing_workflow_arguments,
        context_handler=list_missing_workflows,
        llm_read=True,
    ))

    registry.register(ToolSpec(
        name="library.check_updates",
        description="核对某部媒体是否有更新；剧集比较 TMDB 已播普通集，电影核对本地存在性并提供需人工判断的资源站跟进。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "media_type": {"type": "string", "enum": ["auto", "tv", "movie"]},
                "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                "season": {"type": "integer", "minimum": 1, "maximum": 100},
                "as_of": {"type": "string", "format": "date"},
            },
            "additionalProperties": False,
        },
        handler=check_library_updates,
        validator=_library_update_arguments,
        llm_read=True,
        llm_examples=(
            "检查《某剧》有没有更新",
            "这部剧最新播到哪里而本地有多少",
        ),
    ))

    registry.register(ToolSpec(
        name="library.audit_library_episodes",
        description="有界枚举已配置媒体服务器中的剧集，并按可靠 TMDB 映射巡检截至指定日期的已播缺集。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "format": "date"},
                "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        handler=audit_library_episodes,
        validator=_library_episode_audit_arguments,
        llm_read=True,
        llm_examples=(
            "巡检整个媒体库有没有缺集",
            "检查全部剧集的完整性",
        ),
    ))

    registry.register(ToolSpec(
        name="library.start_episode_audit",
        description="在用户确认后创建可恢复、可查询进度、可取消的后台全库剧集完整性检查。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "format": "date"},
                "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        handler=start_episode_audit,
        validator=start_episode_audit_arguments,
        requires_confirmation=True,
        context_confirmation_preparer=prepare_start_episode_audit,
        context_confirmed_handler=start_episode_audit_confirmed,
        llm_confirmation=True,
    ))

    registry.register(ToolSpec(
        name="agent.job_status",
        description="查询当前登录会话发起的后台全库检查进度与安全结果；不会启动或修改任务。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "pattern": "^job_[A-Za-z0-9_-]{16,80}$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "additionalProperties": False,
        },
        handler=lambda arguments: get_agent_job_status(arguments, ToolContext()),
        validator=agent_job_status_arguments,
        context_handler=get_agent_job_status,
        llm_read=True,
        llm_read_plan=True,
    ))

    registry.register(ToolSpec(
        name="agent.cancel_job",
        description="预检并在用户确认后安全取消当前会话的后台全库检查。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "pattern": "^job_[A-Za-z0-9_-]{16,80}$"},
            },
            "additionalProperties": False,
        },
        handler=cancel_agent_job,
        validator=cancel_agent_job_arguments,
        requires_confirmation=True,
        context_confirmation_preparer=prepare_cancel_agent_job,
        context_confirmed_handler=cancel_agent_job_confirmed,
        llm_confirmation=True,
    ))

    registry.register(ToolSpec(
        name="library.patrol_status",
        description="查询最近一次后台全库缺集巡检的安全摘要；不会触发巡检、资源搜索或下载。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=get_library_patrol_status,
        validator=patrol_status_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))

    registry.register(ToolSpec(
        name="library.patrol_policy",
        description="读取后台全库缺集巡检的启用、通知、间隔和单轮检查上限，不返回其他配置。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=summarize_patrol_policy,
        validator=patrol_policy_summary_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))

    registry.register(ToolSpec(
        name="library.set_patrol_policy",
        description="预检并在用户确认后修改全库缺集巡检的四项白名单策略，不接受配置键、凭据、URL 或路径。",
        risk=RiskLevel.LOW_WRITE,
        parameters={
            "type": "object",
            "minProperties": 1,
            "properties": {
                "enabled": {"type": "boolean"},
                "notify_enabled": {"type": "boolean"},
                "interval_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        handler=set_patrol_policy,
        validator=patrol_policy_arguments,
        requires_confirmation=True,
        preview_handler=preview_set_patrol_policy,
        confirmation_context=patrol_policy_confirmation_context,
        confirmed_handler=set_patrol_policy_confirmed,
        confirmation_preparer=prepare_patrol_policy_confirmation,
        llm_confirmation=True,
    ))

    registry.register(ToolSpec(
        name="library.count_series_episodes",
        description="直接读取已配置 Jellyfin / Emby 中指定剧集的本地普通集数量与季度分布；不访问 TMDB，也不判断缺集。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
            },
            "additionalProperties": False,
        },
        handler=count_series_episodes,
        validator=count_series_episodes_arguments,
        llm_read=True,
        llm_examples=(
            "媒体库中《某剧》一共有多少集",
            "这部剧本地有几季几集",
        ),
    ))

    registry.register(ToolSpec(
        name="library.audit_episodes",
        description="核对媒体库剧集与 TMDB 截止日期前已播普通集，报告缺集或可更新集。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 120},
                "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                "season": {"type": "integer", "minimum": 1, "maximum": 100},
                "target_episode": {"type": "integer", "minimum": 1, "maximum": 1000},
                "as_of": {"type": "string", "format": "date"},
            },
            "additionalProperties": False,
        },
        handler=audit_series_episodes,
        validator=_episode_audit_arguments,
        llm_read=True,
    ))

    def capabilities(_arguments: dict[str, Any]) -> ToolResult:
        tools = registry.capabilities()
        return ToolResult(
            ok=True,
            status="success",
            summary=f"当前提供 {len(tools)} 个受控工具",
            data={"tools": tools},
            evidence=[Evidence("agent_registry", "能力来自服务端显式工具注册表。", _now())],
            suggestions=["可以问：查看系统简报、检查项目配置、诊断下载队列、诊断 RSS 订阅、测试 Jellyfin 或 Emby 连接、关闭媒体探索（需确认）、预览光鸭整理计划、立即整理光鸭云盘、查看 STRM 同步进度、在媒体库找一部影片、从外部影视源搜索一部影片、推荐几部电影或电视剧、查看今天有什么番剧、搜索某部影片的资源、核对某部剧是否缺集、检查某部剧是否有更新。"],
        )

    registry.register(ToolSpec(
        name="agent.action_history",
        description="查看最近经确认执行的 Agent 动作审计，仅返回脱敏状态、聚合计数与耗时。",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "outcome": {
                    "type": "string",
                    "enum": ["all", "success", "failed"],
                    "default": "all",
                },
            },
            "additionalProperties": False,
        },
        handler=lambda arguments: list_action_history(arguments, ToolContext()),
        validator=action_history_arguments,
        context_handler=list_action_history,
        llm_read=True,
        llm_read_plan=True,
    ))

    registry.register(ToolSpec(
        name="agent.capabilities",
        description="列出当前 Agent 可以读取或经确认执行的工具。",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=capabilities,
        validator=_no_arguments,
        llm_read=True,
        llm_read_plan=True,
    ))
    return registry
