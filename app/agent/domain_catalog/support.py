"""Agent 原子工具声明共用的领域函数与校验器。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import unicodedata
from datetime import date, datetime
from typing import Any

from app import config
from app import database as db
from app.agent.action_history import action_history_arguments, list_action_history
from app.agent.automation_actions import (
    automation_pipeline_arguments,
    diagnose_automation_pipeline,
)
from app.agent.config_actions import media_server_arguments, test_media_server
from app.agent.config_diagnosis_actions import diagnose_config
from app.agent.config_explain_actions import (
    config_component_arguments,
    explain_config_component,
)
from app.agent.discovery_actions import (
    bangumi_calendar,
    recommend_discovery,
    search_discovery,
)
from app.agent.discovery_actions import (
    calendar_arguments as bangumi_calendar_arguments,
)
from app.agent.discovery_actions import (
    recommend_arguments as discovery_recommend_arguments,
)
from app.agent.discovery_actions import (
    search_arguments as discovery_search_arguments,
)
from app.agent.discovery_mapping_actions import (
    confirm_discovery_mapping_confirmed,
    discovery_confirm_mapping_arguments,
    discovery_detail_arguments,
    discovery_mapping_candidates_arguments,
    get_discovery_detail,
    get_discovery_mapping_candidates,
    prepare_confirm_discovery_mapping,
)
from app.agent.discovery_watchlist_actions import (
    add_watchlist_arguments,
    add_watchlist_confirmed,
    get_watchlist_summary,
    list_watchlist_summaries,
    prepare_add_watchlist,
    prepare_remove_watchlist,
    remove_watchlist_arguments,
    remove_watchlist_confirmed,
    watchlist_summaries_arguments,
    watchlist_summary_arguments,
)
from app.agent.download_actions import (
    diagnose_download_queue,
    download_diagnosis_arguments,
    download_request_summaries_arguments,
    summarize_download_requests,
)
from app.agent.download_retry_actions import (
    download_retry_submission_arguments,
    prepare_retry_download_submission,
    retry_download_submission_confirmed,
)
from app.agent.durable_job_actions import (
    agent_job_status_arguments,
    cancel_agent_job_arguments,
    cancel_agent_job_confirmed,
    get_agent_job_status,
    prepare_cancel_agent_job,
    prepare_start_episode_audit,
    start_episode_audit_arguments,
    start_episode_audit_confirmed,
)
from app.agent.episode_audit import (
    audit_series_episodes,
    reset_episode_audit_cache_for_tests,
)
from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.errors import AgentToolError
from app.agent.feature_actions import (
    feature_state_arguments,
    feature_summary_arguments,
    prepare_feature_state_confirmation,
    set_feature_state_confirmed,
    summarize_feature_states,
    verify_feature_state_write,
)
from app.agent.feature_gate import is_agent_enabled
from app.agent.guangya_cleanup_actions import (
    classify_guangya_cleanup_candidates,
    execute_guangya_cleanup_confirmed,
    guangya_cleanup_classify_arguments,
    guangya_cleanup_execute_arguments,
    guangya_cleanup_preview_arguments,
    prepare_guangya_cleanup_confirmation,
    preview_guangya_cleanup,
)
from app.agent.guangya_directory_scrape_actions import (
    directory_scrape_inspect_arguments,
    directory_scrape_preview_arguments,
    directory_scrape_run_arguments,
    directory_scrape_search_arguments,
    inspect_directory_scrape,
    prepare_run_directory_scrape,
    preview_directory_scrape,
    run_directory_scrape_confirmed,
    search_directory_scrape,
)
from app.agent.guangya_fs_change_actions import (
    execute_guangya_fs_change_confirmed,
    guangya_fs_change_execute_arguments,
    guangya_fs_change_preview_arguments,
    prepare_guangya_fs_change_confirmation,
    preview_guangya_fs_change,
)
from app.agent.guangya_rename_actions import (
    execute_guangya_rename_confirmed,
    guangya_media_hygiene_preview_arguments,
    guangya_rename_execute_arguments,
    guangya_rename_preview_arguments,
    prepare_guangya_rename_confirmation,
    preview_guangya_media_hygiene,
    preview_guangya_rename,
)
from app.agent.guangya_schedule_config_actions import (
    get_guangya_connection_status,
    guangya_connection_status_arguments,
    guangya_organize_schedule_policy_arguments,
    guangya_organize_schedule_policy_summary_arguments,
    prepare_guangya_organize_schedule_policy_confirmation,
    set_guangya_organize_schedule_policy_confirmed,
    summarize_guangya_organize_schedule_policy,
)
from app.agent.guangya_workspace_actions import (
    guangya_capabilities_arguments,
    guangya_fs_query_arguments,
    query_guangya_filesystem,
    summarize_guangya_capabilities,
)
from app.agent.indexer_actions import search_arguments as indexer_search_arguments
from app.agent.indexer_actions import search_resources
from app.agent.indexer_config_actions import (
    indexer_sites_arguments,
    indexer_sites_summary_arguments,
    prepare_indexer_sites_confirmation,
    set_indexer_sites_confirmed,
    summarize_indexer_sites,
    verify_indexer_sites_write,
)
from app.agent.indexer_readiness_actions import (
    diagnose_indexer_readiness,
    indexer_readiness_arguments,
)
from app.agent.ingest_actions import (
    ingest_inspect_arguments,
    ingest_status_arguments,
    ingest_submit_arguments,
)
from app.agent.library_episode_audit import audit_library_episodes
from app.agent.library_episode_count import (
    count_series_episodes,
    count_series_episodes_arguments,
)
from app.agent.library_patrol_config_actions import (
    patrol_policy_arguments,
    patrol_policy_summary_arguments,
    prepare_patrol_policy_confirmation,
    set_patrol_policy_confirmed,
    summarize_patrol_policy,
)
from app.agent.library_patrol_status import (
    get_library_patrol_status,
    patrol_status_arguments,
)
from app.agent.library_patrol_trigger_actions import (
    patrol_trigger_arguments,
    prepare_trigger_patrol_now,
    trigger_patrol_now_confirmed,
)
from app.agent.local_media_actions import (
    diagnose_local_media,
    local_media_diagnosis_arguments,
    local_media_history_arguments,
    local_media_review_queue_arguments,
    summarize_local_media_history,
    summarize_local_media_review_queue,
)
from app.agent.local_media_scan_actions import (
    local_media_scan_arguments,
    prepare_scan_local_media_sources,
    scan_local_media_sources_confirmed,
)
from app.agent.local_media_source_actions import (
    get_local_media_source_summary,
    list_local_media_source_summaries,
    local_media_source_summaries_arguments,
    local_media_source_summary_arguments,
    local_media_source_trigger_arguments,
    prepare_set_local_media_source_trigger_enabled,
    set_local_media_source_trigger_enabled_confirmed,
)
from app.agent.local_media_task_actions import (
    inspect_local_media_task,
    list_local_media_task_summaries,
    local_media_inspection_arguments,
    local_media_task_number_arguments,
    local_media_task_summaries_arguments,
    prepare_refresh_local_media_task_library,
    prepare_retry_local_media_task,
    preview_local_media_task,
    refresh_local_media_task_library_confirmed,
    retry_local_media_task_confirmed,
    verify_local_media_task_library_visibility,
)
from app.agent.media_consumption_actions import (
    clear_preferences_confirmed,
    continue_watching_arguments,
    get_continue_watching,
    get_preferences,
    get_subscription_notification_rule,
    get_today_summary,
    notification_rule_arguments,
    notification_rule_update_arguments,
    preferences_update_arguments,
    prepare_clear_preferences,
    prepare_reset_subscription_notification_rule,
    prepare_set_preferences,
    prepare_set_subscription_notification_rule,
    reset_subscription_notification_rule_confirmed,
    set_preferences_confirmed,
    set_subscription_notification_rule_confirmed,
)
from app.agent.media_consumption_actions import (
    empty_arguments as media_consumption_empty_arguments,
)
from app.agent.media_health_actions import (
    diagnose_workspace_health,
    workspace_health_arguments,
)
from app.agent.media_proxy_actions import (
    media_proxy_enabled_arguments,
    media_proxy_failure_summary_arguments,
    media_proxy_restart_arguments,
    media_proxy_status_arguments,
    media_proxy_test_arguments,
    prepare_restart_media_proxy_instance,
    prepare_set_media_proxy_instance_enabled,
    restart_media_proxy_instance_confirmed,
    set_media_proxy_instance_enabled_confirmed,
    summarize_media_proxy_playback_failures,
    summarize_media_proxy_status,
    test_media_proxy_instance,
)
from app.agent.media_rating_actions import lookup_media_rating, media_rating_arguments
from app.agent.media_server_actions import (
    diagnose_media_servers,
    media_server_diagnosis_arguments,
)
from app.agent.media_subscription_actions import (
    create_media_subscription_confirmed,
    delete_media_subscription_confirmed,
    get_media_subscription_policy,
    get_media_subscription_summary,
    inspect_media_subscription_updates,
    list_media_subscription_summaries,
    media_subscription_create_arguments,
    media_subscription_delete_arguments,
    media_subscription_enabled_arguments,
    media_subscription_policy_arguments,
    media_subscription_policy_update_arguments,
    media_subscription_summaries_arguments,
    media_subscription_summary_arguments,
    media_subscription_updates_arguments,
    prepare_create_media_subscription,
    prepare_delete_media_subscription,
    prepare_set_media_subscription_enabled,
    prepare_set_media_subscription_policy,
    set_media_subscription_enabled_confirmed,
    set_media_subscription_policy_confirmed,
)
from app.agent.missing_media_workflows import (
    list_missing_workflows,
    missing_workflow_arguments,
)
from app.agent.models import Evidence, RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.organize_actions import (
    prepare_guangya_organize_run_once,
    prepare_guangya_organize_stop,
    preview_guangya_organize,
    run_guangya_organize_once_confirmed,
    stop_guangya_organize_confirmed,
)
from app.agent.organize_audit_actions import (
    audit_organize_logs,
    organize_audit_arguments,
)
from app.agent.provider_actions import (
    execute_provider_change_confirmed,
    list_provider_capabilities,
    prepare_provider_change_execution,
    preview_provider_change,
    provider_capabilities_arguments,
    provider_change_status,
    provider_plan_arguments,
    provider_plan_ref_arguments,
    provider_query_arguments,
    query_provider,
    reset_provider_gateway_for_tests,
)
from app.agent.recognition_toggle_actions import (
    prepare_set_recognition_rule_enabled,
    recognition_rule_enabled_arguments,
    set_recognition_rule_enabled_confirmed,
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
    prepare_rss_pending_download,
    rss_pending_download_arguments,
    submit_pending_rss_to_qb_confirmed,
)
from app.agent.rss_entry_actions import (
    list_rss_entry_summaries,
    mark_rss_entries_confirmed,
    prepare_mark_rss_entries,
    prepare_submit_rss_entries,
    rss_entry_summaries_arguments,
    rss_mark_entries_arguments,
    rss_submit_entries_arguments,
    submit_rss_entries_confirmed,
)
from app.agent.rss_refresh_actions import (
    prepare_rss_subscription_refresh,
    prepare_rss_subscriptions_refresh,
    refresh_rss_subscription_confirmed,
    refresh_rss_subscriptions_confirmed,
    rss_refresh_subscription_arguments,
    rss_refresh_subscriptions_arguments,
)
from app.agent.rss_retry_actions import (
    prepare_rss_failure_retry,
    retry_failed_rss_to_qb_confirmed,
    rss_failure_retry_arguments,
)
from app.agent.rss_subscription_control_actions import (
    create_rss_subscription_confirmed,
    delete_rss_subscription_confirmed,
    prepare_create_rss_subscription,
    prepare_delete_rss_subscription,
    prepare_update_rss_subscription,
    rss_create_subscription_arguments,
    rss_delete_subscription_arguments,
    rss_update_subscription_arguments,
    update_rss_subscription_confirmed,
)
from app.agent.safe_policy_actions import (
    SAFE_POLICY_IDS,
    prepare_safe_policy_confirmation,
    safe_policy_arguments,
    safe_policy_summary_arguments,
    set_safe_policy_confirmed,
    summarize_safe_policies,
)
from app.agent.strm_failure_actions import (
    strm_failure_triage_arguments,
    triage_strm_failures,
)
from app.agent.strm_history_actions import (
    get_strm_run_history,
    strm_run_history_arguments,
)
from app.agent.strm_retry_actions import (
    prepare_strm_failure_retry,
    retry_strm_failure_records_confirmed,
    strm_failure_retry_arguments,
)
from app.agent.strm_schedule_config_actions import (
    prepare_strm_schedule_policy_confirmation,
    set_strm_schedule_policy_confirmed,
    strm_schedule_policy_arguments,
    strm_schedule_policy_summary_arguments,
    summarize_strm_schedule_policy,
)
from app.agent.telegram_test_actions import (
    prepare_telegram_test_notification,
    send_telegram_test_notification_confirmed,
    telegram_test_arguments,
)
from app.agent.update_actions import check_library_updates
from app.agent.web_search_actions import search_web, web_search_arguments
from app.agent.workspace_actions import (
    _contains_sensitive_text,
    _safe_status,
    _safe_title,
    _safe_year,
    search_workspace,
    workspace_search_arguments,
)
from app.agent.workspace_briefing_actions import (
    summarize_workspace_briefing,
    workspace_briefing_arguments,
)
from app.agent.workspace_next_actions import (
    summarize_workspace_next_actions,
    workspace_next_actions_arguments,
)
from app.agent.workspace_todo_actions import (
    summarize_workspace_todo,
    workspace_todo_arguments,
)
from app.clients.base import normalize_playback_progress
from app.indexers.config import INDEXER_SITE_ORDER
from app.modules.local_media_models import LOCAL_TASK_STATUSES
from app.services import search_media_servers

_SEARCH_CACHE_TTL_SECONDS = 15
_SEARCH_CACHE_MAX_ENTRIES = 128
_search_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_search_cache_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now().astimezone().date()


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")


def _no_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, set())
    return {}


def guangya_organize_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"operation_ref"})
    reference = str(arguments.get("operation_ref") or "").strip().upper()
    if reference and not re.fullmatch(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}", reference):
        raise AgentToolError("operation_ref 不是有效的光鸭操作编号")
    return {"operation_ref": reference} if reference else {}


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
    _reject_extra(
        arguments,
        {"query", "tmdb_id", "season", "target_episode", "as_of", "library_name"},
    )
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_search_query(raw_query)

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise AgentToolError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10
    ):
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")

    season = arguments.get("season")
    if season is not None and (
        isinstance(season, bool)
        or not isinstance(season, int)
        or not 1 <= season <= 100
    ):
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

    as_of = arguments.get("as_of", _today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > _today():
        raise AgentToolError("as_of 不能晚于今天")
    normalized = {
        "query": query,
        "tmdb_id": tmdb_id,
        "season": season,
        "as_of": parsed_as_of.isoformat(),
    }
    if library_name:
        normalized["library_name"] = library_name
    if target_episode is not None:
        normalized["target_episode"] = target_episode
    return normalized


def _library_episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"as_of", "max_series"})
    as_of = arguments.get("as_of", _today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > _today():
        raise AgentToolError("as_of 不能晚于今天")
    max_series = arguments.get("max_series", 50)
    if (
        isinstance(max_series, bool)
        or not isinstance(max_series, int)
        or not 1 <= max_series <= 100
    ):
        raise AgentToolError("max_series 必须是 1 到 100 的整数")
    return {"as_of": parsed_as_of.isoformat(), "max_series": max_series}


def _library_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "media_type", "tmdb_id", "season", "as_of"})
    media_type = arguments.get("media_type", "auto")
    if not isinstance(media_type, str) or media_type not in {"auto", "tv", "movie"}:
        raise AgentToolError("media_type 必须是 auto、tv 或 movie")
    normalized = _episode_audit_arguments(
        {key: value for key, value in arguments.items() if key != "media_type"}
    )
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
        suggestions.append(
            f"有 {missing} 条 STRM 索引对应文件缺失，建议先核对来源和输出目录。"
        )
    if open_failures:
        suggestions.append(
            f"有 {open_failures} 条未解决 STRM 失败记录，建议按来源查看失败原因。"
        )
    if not recent_runs:
        suggestions.append("尚无 STRM 同步运行记录，可在配置完成后手动执行一次。")
    return ToolResult(
        ok=issues == 0,
        status=status,
        summary="STRM 索引与失败记录正常"
        if not issues
        else f"STRM 发现 {issues} 项需要关注",
        data={
            "index": {
                "total": int(diagnostics.get("total", 0) or 0),
                "existing": int(diagnostics.get("existing", 0) or 0),
                "missing": missing,
                "real_source": int(diagnostics.get("real_source", 0) or 0),
                "test_artifacts": int(
                    diagnostics.get("confirmed_test_artifact", 0) or 0
                ),
            },
            "failures": {
                "open": open_failures,
                "resolved": int(failures.get("resolved", 0) or 0),
                "source_count": len(list(failures.get("sources", []))),
                "by_source": [
                    {"label": f"来源 {index}", "open": int(item.get("open", 0) or 0)}
                    for index, item in enumerate(
                        list(failures.get("sources", [])), start=1
                    )
                ],
            },
            "recent_runs": recent_runs,
        },
        evidence=[
            Evidence("sqlite:strm_index", "统计 STRM 索引及文件存在性。", _now()),
            Evidence(
                "sqlite:strm_failures", "统计未解决和已解决的 STRM 失败记录。", _now()
            ),
            Evidence(
                "sqlite:task_runs",
                "读取最近 5 次 STRM 同步状态，不返回运行结果正文。",
                _now(),
            ),
        ],
        suggestions=suggestions,
    )


def strm_run_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - {"source_names"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_names = arguments.get("source_names")
    if raw_names is None or raw_names == []:
        return {"source_names": []}
    if not isinstance(raw_names, list) or len(raw_names) > 16:
        raise AgentToolError("source_names 必须是 1 到 16 个来源名称")
    names: list[str] = []
    seen_names: set[str] = set()
    for raw in raw_names:
        if not isinstance(raw, str):
            raise AgentToolError("STRM 来源名称必须是字符串")
        name = unicodedata.normalize("NFKC", raw).strip()
        if (
            not name
            or len(name) > 80
            or any(unicodedata.category(char).startswith("C") for char in name)
        ):
            raise AgentToolError("STRM 来源名称无效")
        identity = name.casefold()
        if identity not in seen_names:
            names.append(name)
            seen_names.add(identity)
    return {"source_names": names}


def _selected_strm_sources(
    arguments: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    from app.modules.strm import configured_strm_source_plans

    normalized = strm_run_arguments(arguments)
    requested = normalized["source_names"]
    if not requested:
        return [], ""
    configured, error = configured_strm_source_plans()
    if error:
        return [], error
    selected: list[dict[str, str]] = []
    for requested_name in requested:
        matches = [
            source
            for source in configured
            if unicodedata.normalize("NFKC", str(source.get("name") or "")).casefold()
            == unicodedata.normalize("NFKC", requested_name).casefold()
        ]
        if not matches:
            return [], f"未找到已配置的 STRM 来源：{requested_name}"
        if len(matches) > 1:
            return [], f"STRM 来源名称不唯一：{requested_name}"
        selected.append(matches[0])
    return selected, ""


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


def _capture_strm_run(arguments: dict[str, Any]) -> dict[str, Any]:
    """原子捕获本次 STRM 预检、确认绑定与执行所需的服务端状态。"""
    from app.modules.scheduler import get_scheduler

    selected_sources, selection_error = _selected_strm_sources(arguments)
    scheduler = get_scheduler()
    validation_error = str(scheduler.validate_config(auto_only=False) or "")
    raw_status = scheduler.status()
    running = isinstance(raw_status, dict) and raw_status.get("running") is True
    payload = {key: config.get(key, "") for key in _STRM_CONFIRMATION_KEYS}
    payload.update(
        {
            "selected_source_ids": [
                str(item.get("id") or "") for item in selected_sources
            ],
            "selection_error": selection_error,
            "validation_error": validation_error,
            "running": running,
        }
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "scheduler": scheduler,
        "selected_sources": selected_sources,
        "selection_error": selection_error,
        "validation_error": validation_error,
        "running": running,
        "fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def _preview_strm_run_once(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """只做运行前检查，不启动任务。"""
    if state["selection_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 来源选择无效",
            error=str(state["selection_error"]),
            suggestions=["请使用设置页中已配置且名称唯一的 STRM 来源。"],
        )
    if state["validation_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 当前无法启动",
            error="请先补全 STRM 来源、播放地址和输出目录。",
            suggestions=["请先检查 STRM 配置，再重新发起预检。"],
        )
    if state["running"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="STRM 同步任务已在运行",
            error="请等待当前任务结束后再试。",
            suggestions=["可询问：查看 STRM 同步进度。"],
        )
    selected_sources = list(state["selected_sources"])
    scoped = bool(arguments.get("source_names"))
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=(
            "确认后将同步选定 STRM 来源" if scoped else "确认后将启动一次 STRM 全量同步"
        ),
        data={
            "action": "strm.run_once",
            "trigger": "manual",
            **(
                {
                    "source_count": len(selected_sources),
                    "source_names": [
                        str(item.get("name") or "") for item in selected_sources
                    ],
                }
                if scoped
                else {}
            ),
            "effects": [
                (
                    "仅扫描本次选定的 STRM 来源"
                    if scoped
                    else "扫描当前配置的全部 STRM 来源"
                ),
                "按现有规则创建、更新或清理 STRM 与伴随元数据",
                "根据现有配置执行通知和媒体库刷新",
            ],
        },
        evidence=[
            Evidence(
                "strm_scheduler",
                "已完成脱敏运行前检查；尚未启动任务。",
                _now(),
            )
        ],
        suggestions=["确认前请核对 STRM 来源、输出目录和清理规则。"],
    )


def prepare_strm_run_once(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture_strm_run(arguments)
    return _preview_strm_run_once(arguments, state), str(state["fingerprint"])


def _run_strm_once_state(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """固定以 manual 触发一次全部或指定来源 STRM 同步。"""
    if state["selection_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 来源选择已失效",
            error=str(state["selection_error"]),
        )
    if state["validation_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 当前无法启动",
            error="相关配置无效，请重新检查后再发起确认。",
        )
    if state["running"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="STRM 同步任务已在运行",
            error="当前任务未重复提交。",
            suggestions=["可询问：查看 STRM 同步进度。"],
        )
    selected_sources = list(state["selected_sources"])
    scheduler = state["scheduler"]
    scoped = bool(arguments.get("source_names"))
    triggered = (
        scheduler.trigger(
            "manual",
            selected_source_ids=[
                str(item.get("id") or "") for item in selected_sources
            ],
        )
        if scoped
        else scheduler.trigger("manual")
    )
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
        data={
            "accepted": True,
            "trigger": "manual",
            **(
                {"source_count": len(selected_sources), "scoped": True}
                if scoped
                else {}
            ),
        },
        evidence=[
            Evidence(
                "strm_scheduler",
                "已通过一次性确认票据提交手动同步；未返回目录或运行详情。",
                _now(),
            )
        ],
        suggestions=["可询问：查看 STRM 同步进度。"],
    )


def run_strm_once_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture_strm_run(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError(
            "STRM 配置已变化（来源或运行状态可能已更新），请重新预检",
            code="confirmation_stale",
        )
    return _run_strm_once_state(arguments, state)


def strm_runtime_status(_arguments: dict[str, Any]) -> ToolResult:
    """读取 STRM 调度器和可选择来源名称的脱敏运行快照。"""
    from app.modules.scheduler import get_scheduler
    from app.modules.strm import configured_strm_source_plans

    raw = get_scheduler().status()
    configured_sources, source_error = configured_strm_source_plans()
    available_source_names = (
        list(
            dict.fromkeys(
                str(item.get("name") or "").strip()
                for item in configured_sources
                if str(item.get("name") or "").strip()
            )
        )
        if not source_error
        else []
    )
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
        {
            "idle",
            "scan",
            "generate",
            "metadata",
            "cleanup",
            "retry",
            "refresh",
            "complete",
            "failed",
        },
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
        "trigger_type": _safe_choice(
            last.get("trigger_type"), {"manual", "cron", "telegram"}
        ),
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
            "current_trigger": _safe_choice(
                raw.get("current_trigger"), {"manual", "cron", "telegram"}
            ),
            "next_run": _safe_timestamp(raw.get("next_run")),
            "progress": {
                "stage": stage,
                "completed": completed,
                "total": total,
                "percent": percent,
            },
            "sources": {
                "total": sum(source_counts.values()),
                "by_status": source_counts,
                "configured_total": len(available_source_names),
                "available_names": available_source_names,
            },
            "last_run": last_run,
        },
        evidence=[
            Evidence(
                "strm_scheduler",
                "读取 STRM 调度器脱敏快照和可选择的来源显示名称；未返回目录、来源 ID 或错误正文。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def guangya_organize_status(
    arguments: dict[str, Any], context: ToolContext | None = None
) -> ToolResult:
    """读取光鸭整理任务、持久化操作与调度器的脱敏运行快照。"""
    context = context or ToolContext()
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    operation_ref = str(arguments.get("operation_ref") or "").strip().upper()
    overview = manager.status()
    raw = (
        manager.task_result(operation_ref, owner=context.owner)
        if operation_ref
        else overview
    )
    if operation_ref and raw is None:
        return ToolResult(
            ok=False,
            status="empty",
            summary="没有找到这个光鸭操作编号",
            data={"operation_ref": operation_ref, "found": False},
            evidence=[
                Evidence(
                    "guangya_organizer",
                    "已按公开操作编号查询持久化任务；未返回目录、内部任务标识或错误正文。",
                    _now(),
                )
            ],
            suggestions=["请核对操作编号，或直接查看当前光鸭整理状态。"],
        )
    raw = raw or {}
    task_status = _safe_choice(
        raw.get("status"),
        {
            "idle",
            "queued",
            "running",
            "stopping",
            "completed",
            "partial",
            "stopped",
            "failed",
            "cancelled",
            "manual_review",
        },
        "idle",
    )
    running = task_status in {"running", "stopping"}
    allowed_stats = {
        "total",
        "matched",
        "need_confirm",
        "moved",
        "renamed",
        "rename_failed",
        "metadata_moved",
        "stopped",
        "skipped",
        "conflict",
        "failed",
        "subtitle_moved",
        "subtitle_skipped",
        "replacement_cleanup_failed",
        "empty_dir_cleanup_failed",
        "source_dir_cleanup_failed",
        "audit_failures",
    }
    stats = (
        {
            key: _bounded_int(value)
            for key, value in (raw.get("stats") or {}).items()
            if key in allowed_stats
        }
        if isinstance(raw.get("stats"), dict)
        else {}
    )
    if not stats and isinstance(raw.get("result"), dict):
        persisted_stats = raw["result"].get("stats")
        if isinstance(persisted_stats, dict):
            stats = {
                key: _bounded_int(value)
                for key, value in persisted_stats.items()
                if key in allowed_stats
            }

    schedule_raw = (
        overview.get("schedule") if isinstance(overview.get("schedule"), dict) else {}
    )
    schedule = {
        "enabled": bool(schedule_raw.get("enabled")),
        "configured": not bool(schedule_raw.get("config_error")),
        "cron_valid": bool(schedule_raw.get("cron_valid")),
        "next_run": _safe_timestamp(schedule_raw.get("next_run")),
    }
    queue_raw = overview.get("operation_queue")
    queue_total = (
        _bounded_int(queue_raw.get("total")) if isinstance(queue_raw, dict) else 0
    )

    if running:
        ok, status, summary = True, "running", "光鸭整理任务正在运行"
        suggestions: list[str] = []
    elif task_status == "queued":
        ok, status, summary = True, "queued", "光鸭整理操作正在排队"
        suggestions = ["任务会在当前整理操作结束后自动执行。"]
    elif task_status == "manual_review":
        ok, status, summary = False, "attention", "光鸭操作在进程中断后需要人工核验"
        suggestions = ["请先核对光鸭目标目录，确认远端结果后再决定是否重新执行。"]
    elif task_status == "failed":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务未成功"
        suggestions = ["请到网盘整理页查看任务详情后再决定是否重试。"]
    elif task_status == "completed":
        ok, status, summary = True, "completed", "最近一次光鸭整理任务已完成"
        suggestions = []
    elif task_status == "partial":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务部分完成"
        suggestions = ["请到网盘整理页核对失败项后再决定是否重试。"]
    elif task_status in {"stopped", "cancelled"}:
        ok, status, summary = True, "stopped", "最近一次光鸭整理任务已停止"
        suggestions = []
    else:
        ok, status, summary = (
            True,
            "idle",
            (
                f"光鸭整理任务当前空闲，另有 {queue_total} 项操作排队"
                if queue_total
                else "光鸭整理任务当前空闲"
            ),
        )
        suggestions = []

    task_data = {
        "status": task_status,
        "running": running,
        "stoppable": bool(raw.get("stoppable")) if running else False,
        "trigger_type": _safe_choice(
            raw.get("trigger_type"), {"manual", "cron", "telegram"}
        ),
        "started_at": _safe_timestamp(raw.get("started_at")),
        "finished_at": _safe_timestamp(raw.get("finished_at")),
        "stats": stats,
    }
    if operation_ref:
        task_data["operation_ref"] = operation_ref
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "task": task_data,
            "queue": {"pending_count": queue_total},
            "schedule": schedule,
        },
        evidence=[
            Evidence(
                "guangya_organizer",
                "读取光鸭整理任务脱敏快照；仅在用户提供时返回公开操作编号，不返回目录、内部任务标识或错误正文。",
                _now(),
            )
        ],
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
        expired = [
            key
            for key, value in _search_cache.items()
            if now - value[0] >= _SEARCH_CACHE_TTL_SECONDS
        ]
        for key in expired:
            _search_cache.pop(key, None)
        while len(_search_cache) >= _SEARCH_CACHE_MAX_ENTRIES:
            oldest = min(_search_cache, key=lambda key: _search_cache[key][0])
            _search_cache.pop(oldest, None)
        _search_cache[cache_key] = (now, sources)
    return sources


def reset_agent_tool_caches_for_tests() -> None:
    with _search_cache_lock:
        _search_cache.clear()
    reset_episode_audit_cache_for_tests()
    reset_provider_gateway_for_tests()


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
                series_name = (
                    _safe_title(item.series_name, "") if item.series_name else ""
                )
                items.append(
                    {
                        "title": title,
                        "display_name": display_name,
                        "media_type": _safe_status(
                            item.type,
                            {"movie", "series", "episode", "season", "video"},
                        ),
                        "year": _safe_year(item.year),
                        "series_name": series_name,
                        "season": _safe_optional_index(
                            item.season_number, allow_zero=True
                        ),
                        "episode": _safe_optional_index(
                            item.episode_number, allow_zero=False
                        ),
                        "runtime_minutes": _safe_runtime_minutes(item.runtime),
                        "playback_progress_percent": normalize_playback_progress(
                            item.progress
                        ),
                        "overview": _safe_overview(item.overview),
                    }
                )
        total += len(items)
        server_type = _safe_status(source.get("server_type"), {"jellyfin", "emby"})
        if server_type == "unknown":
            server_type = "media_server"
        source_match = (
            "unknown" if source_unavailable else ("found" if items else "not_found")
        )
        serialized_sources.append(
            {
                "server_type": server_type,
                "server_name": _safe_title(source.get("server_name"), "媒体服务器"),
                "status": "unavailable" if source_unavailable else "ready",
                "match_status": source_match,
                "returned": len(items),
                "items": items,
            }
        )
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
            evidence=[
                Evidence(
                    "media_servers",
                    "检查已启用且凭据完整的 Jellyfin / Emby 配置。",
                    _now(),
                )
            ],
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
        suggestions = (
            [f"有 {unavailable} 个媒体服务器暂时不可用。"] if unavailable else []
        )
    else:
        status = "partial" if unavailable else "empty"
        match_status = "indeterminate" if unavailable else "not_found"
        summary = (
            f"已查询 {available} 个媒体服务器，另有 {unavailable} 个暂时不可用；可用来源未找到匹配内容"
            if unavailable
            else "媒体库中没有找到匹配内容"
        )
        suggestions = (["请检查不可用的媒体服务器后重试。"] if unavailable else []) + [
            "可尝试中文名、原名或去掉季集编号后重新搜索。",
            f"搜索《{query}》的资源。",
            f"在网上找《{query}》。",
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
        evidence=[
            Evidence(
                "media_servers", f"查询 {len(sources)} 个已配置媒体服务器。", _now()
            )
        ],
        suggestions=suggestions,
    )


# 领域 catalog 显式共享既有 action/validator；包括单下划线校验器。
__all__ = (
    "INDEXER_SITE_ORDER",
    "LOCAL_TASK_STATUSES",
    "SAFE_POLICY_IDS",
    "Evidence",
    "RiskLevel",
    "ToolResult",
    "ToolSpec",
    "_episode_audit_arguments",
    "_library_episode_audit_arguments",
    "_library_update_arguments",
    "_no_arguments",
    "_now",
    "_search_arguments",
    "action_history_arguments",
    "add_watchlist_arguments",
    "add_watchlist_confirmed",
    "agent_job_status_arguments",
    "audit_library_episodes",
    "audit_organize_logs",
    "audit_series_episodes",
    "automation_pipeline_arguments",
    "bangumi_calendar",
    "bangumi_calendar_arguments",
    "cancel_agent_job_arguments",
    "cancel_agent_job_confirmed",
    "check_library_updates",
    "classify_guangya_cleanup_candidates",
    "clear_preferences_confirmed",
    "config",
    "config_component_arguments",
    "confirm_discovery_mapping_confirmed",
    "continue_watching_arguments",
    "count_series_episodes",
    "count_series_episodes_arguments",
    "create_media_subscription_confirmed",
    "create_rss_subscription_confirmed",
    "delete_media_subscription_confirmed",
    "delete_rss_subscription_confirmed",
    "diagnose_automation_pipeline",
    "diagnose_config",
    "diagnose_download_queue",
    "diagnose_indexer_readiness",
    "diagnose_local_media",
    "diagnose_media_servers",
    "diagnose_rss",
    "diagnose_strm",
    "diagnose_workspace_health",
    "directory_scrape_inspect_arguments",
    "directory_scrape_preview_arguments",
    "directory_scrape_run_arguments",
    "directory_scrape_search_arguments",
    "discovery_confirm_mapping_arguments",
    "discovery_detail_arguments",
    "discovery_mapping_candidates_arguments",
    "discovery_recommend_arguments",
    "discovery_search_arguments",
    "download_diagnosis_arguments",
    "download_request_summaries_arguments",
    "download_retry_submission_arguments",
    "execute_guangya_cleanup_confirmed",
    "execute_guangya_fs_change_confirmed",
    "execute_guangya_rename_confirmed",
    "execute_provider_change_confirmed",
    "explain_config_component",
    "feature_state_arguments",
    "feature_summary_arguments",
    "get_agent_job_status",
    "get_continue_watching",
    "get_discovery_detail",
    "get_discovery_mapping_candidates",
    "get_guangya_connection_status",
    "get_library_patrol_status",
    "get_local_media_source_summary",
    "get_media_subscription_policy",
    "get_media_subscription_summary",
    "get_preferences",
    "get_rss_recent_activity",
    "get_rss_subscription_summary",
    "get_strm_run_history",
    "get_subscription_notification_rule",
    "get_today_summary",
    "get_watchlist_summary",
    "guangya_capabilities_arguments",
    "guangya_cleanup_classify_arguments",
    "guangya_cleanup_execute_arguments",
    "guangya_cleanup_preview_arguments",
    "guangya_connection_status_arguments",
    "guangya_fs_change_execute_arguments",
    "guangya_fs_change_preview_arguments",
    "guangya_fs_query_arguments",
    "guangya_media_hygiene_preview_arguments",
    "guangya_organize_schedule_policy_arguments",
    "guangya_organize_schedule_policy_summary_arguments",
    "guangya_organize_status",
    "guangya_organize_status_arguments",
    "guangya_rename_execute_arguments",
    "guangya_rename_preview_arguments",
    "indexer_readiness_arguments",
    "indexer_search_arguments",
    "indexer_sites_arguments",
    "indexer_sites_summary_arguments",
    "ingest_inspect_arguments",
    "ingest_status_arguments",
    "ingest_submit_arguments",
    "inspect_directory_scrape",
    "inspect_local_media_task",
    "inspect_media_subscription_updates",
    "is_agent_enabled",
    "list_action_history",
    "list_local_media_source_summaries",
    "list_local_media_task_summaries",
    "list_media_subscription_summaries",
    "list_missing_workflows",
    "list_provider_capabilities",
    "list_rss_entry_summaries",
    "list_rss_subscription_summaries",
    "list_watchlist_summaries",
    "local_media_diagnosis_arguments",
    "local_media_history_arguments",
    "local_media_inspection_arguments",
    "local_media_review_queue_arguments",
    "local_media_scan_arguments",
    "local_media_source_summaries_arguments",
    "local_media_source_summary_arguments",
    "local_media_source_trigger_arguments",
    "local_media_task_number_arguments",
    "local_media_task_summaries_arguments",
    "lookup_media_rating",
    "mark_rss_entries_confirmed",
    "media_consumption_empty_arguments",
    "media_proxy_enabled_arguments",
    "media_proxy_failure_summary_arguments",
    "media_proxy_restart_arguments",
    "media_proxy_status_arguments",
    "media_proxy_test_arguments",
    "media_rating_arguments",
    "media_server_arguments",
    "media_server_diagnosis_arguments",
    "media_subscription_create_arguments",
    "media_subscription_delete_arguments",
    "media_subscription_enabled_arguments",
    "media_subscription_policy_arguments",
    "media_subscription_policy_update_arguments",
    "media_subscription_summaries_arguments",
    "media_subscription_summary_arguments",
    "media_subscription_updates_arguments",
    "missing_episode_resource_arguments",
    "missing_season_resource_arguments",
    "missing_workflow_arguments",
    "notification_rule_arguments",
    "notification_rule_update_arguments",
    "organize_audit_arguments",
    "patrol_policy_arguments",
    "patrol_policy_summary_arguments",
    "patrol_status_arguments",
    "patrol_trigger_arguments",
    "preferences_update_arguments",
    "prepare_add_watchlist",
    "prepare_cancel_agent_job",
    "prepare_clear_preferences",
    "prepare_confirm_discovery_mapping",
    "prepare_create_media_subscription",
    "prepare_create_rss_subscription",
    "prepare_delete_media_subscription",
    "prepare_delete_rss_subscription",
    "prepare_feature_state_confirmation",
    "prepare_guangya_cleanup_confirmation",
    "prepare_guangya_fs_change_confirmation",
    "prepare_guangya_organize_run_once",
    "prepare_guangya_organize_schedule_policy_confirmation",
    "prepare_guangya_organize_stop",
    "prepare_guangya_rename_confirmation",
    "prepare_indexer_sites_confirmation",
    "prepare_mark_rss_entries",
    "prepare_patrol_policy_confirmation",
    "prepare_provider_change_execution",
    "prepare_refresh_local_media_task_library",
    "prepare_remove_watchlist",
    "prepare_reset_subscription_notification_rule",
    "prepare_restart_media_proxy_instance",
    "prepare_retry_download_submission",
    "prepare_retry_local_media_task",
    "prepare_rss_failure_retry",
    "prepare_rss_pending_download",
    "prepare_rss_subscription_refresh",
    "prepare_rss_subscriptions_refresh",
    "prepare_run_directory_scrape",
    "prepare_safe_policy_confirmation",
    "prepare_scan_local_media_sources",
    "prepare_set_local_media_source_trigger_enabled",
    "prepare_set_media_proxy_instance_enabled",
    "prepare_set_media_subscription_enabled",
    "prepare_set_media_subscription_policy",
    "prepare_set_preferences",
    "prepare_set_recognition_rule_enabled",
    "prepare_set_subscription_notification_rule",
    "prepare_start_episode_audit",
    "prepare_strm_failure_retry",
    "prepare_strm_run_once",
    "prepare_strm_schedule_policy_confirmation",
    "prepare_submit_rss_entries",
    "prepare_telegram_test_notification",
    "prepare_trigger_patrol_now",
    "prepare_update_rss_subscription",
    "preview_directory_scrape",
    "preview_guangya_cleanup",
    "preview_guangya_fs_change",
    "preview_guangya_media_hygiene",
    "preview_guangya_organize",
    "preview_guangya_rename",
    "preview_local_media_task",
    "preview_provider_change",
    "provider_capabilities_arguments",
    "provider_change_status",
    "provider_plan_arguments",
    "provider_plan_ref_arguments",
    "provider_query_arguments",
    "query_guangya_filesystem",
    "query_provider",
    "recognition_rule_enabled_arguments",
    "recommend_discovery",
    "refresh_local_media_task_library_confirmed",
    "refresh_rss_subscription_confirmed",
    "refresh_rss_subscriptions_confirmed",
    "remove_watchlist_arguments",
    "remove_watchlist_confirmed",
    "reset_subscription_notification_rule_confirmed",
    "restart_media_proxy_instance_confirmed",
    "retry_download_submission_confirmed",
    "retry_failed_rss_to_qb_confirmed",
    "retry_local_media_task_confirmed",
    "retry_strm_failure_records_confirmed",
    "rss_create_subscription_arguments",
    "rss_delete_subscription_arguments",
    "rss_diagnosis_arguments",
    "rss_entry_summaries_arguments",
    "rss_failure_retry_arguments",
    "rss_mark_entries_arguments",
    "rss_pending_download_arguments",
    "rss_refresh_subscription_arguments",
    "rss_refresh_subscriptions_arguments",
    "rss_submit_entries_arguments",
    "rss_subscription_summaries_arguments",
    "rss_subscription_summary_arguments",
    "rss_update_subscription_arguments",
    "run_directory_scrape_confirmed",
    "run_guangya_organize_once_confirmed",
    "run_strm_once_confirmed",
    "safe_policy_arguments",
    "safe_policy_summary_arguments",
    "scan_local_media_sources_confirmed",
    "search_directory_scrape",
    "search_discovery",
    "search_library",
    "search_missing_episode_resources",
    "search_missing_season_resources",
    "search_resources",
    "search_web",
    "search_workspace",
    "send_telegram_test_notification_confirmed",
    "set_feature_state_confirmed",
    "set_guangya_organize_schedule_policy_confirmed",
    "set_indexer_sites_confirmed",
    "set_local_media_source_trigger_enabled_confirmed",
    "set_media_proxy_instance_enabled_confirmed",
    "set_media_subscription_enabled_confirmed",
    "set_media_subscription_policy_confirmed",
    "set_patrol_policy_confirmed",
    "set_preferences_confirmed",
    "set_recognition_rule_enabled_confirmed",
    "set_safe_policy_confirmed",
    "set_strm_schedule_policy_confirmed",
    "set_subscription_notification_rule_confirmed",
    "start_episode_audit_arguments",
    "start_episode_audit_confirmed",
    "stop_guangya_organize_confirmed",
    "strm_failure_retry_arguments",
    "strm_failure_triage_arguments",
    "strm_run_arguments",
    "strm_run_history_arguments",
    "strm_runtime_status",
    "strm_schedule_policy_arguments",
    "strm_schedule_policy_summary_arguments",
    "submit_pending_rss_to_qb_confirmed",
    "submit_rss_entries_confirmed",
    "summarize_download_requests",
    "summarize_feature_states",
    "summarize_guangya_capabilities",
    "summarize_guangya_organize_schedule_policy",
    "summarize_indexer_sites",
    "summarize_local_media_history",
    "summarize_local_media_review_queue",
    "summarize_media_proxy_playback_failures",
    "summarize_media_proxy_status",
    "summarize_patrol_policy",
    "summarize_safe_policies",
    "summarize_strm_schedule_policy",
    "summarize_workspace_briefing",
    "summarize_workspace_next_actions",
    "summarize_workspace_todo",
    "telegram_test_arguments",
    "test_media_proxy_instance",
    "test_media_server",
    "triage_strm_failures",
    "trigger_patrol_now_confirmed",
    "update_rss_subscription_confirmed",
    "verify_feature_state_write",
    "verify_indexer_sites_write",
    "verify_local_media_task_library_visibility",
    "watchlist_summaries_arguments",
    "watchlist_summary_arguments",
    "web_search_arguments",
    "workspace_briefing_arguments",
    "workspace_health_arguments",
    "workspace_next_actions_arguments",
    "workspace_search_arguments",
    "workspace_todo_arguments",
)
