"""Agent 受确认动作的脱敏审计记录与只读查询。"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import json
from typing import Any

from app import database as db
from app.agent.confirmation_contract import sanitize_confirmation_contract
from app.agent.models import Evidence, RiskLevel, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.web_secret import get_web_secret

logger = get_logger(__name__)

_TOOL_LABELS = {
    "downloads.retry_submission": "下载请求重新提交",
    "provider.change.execute": "Provider 原生写计划执行",
    "rss.mark_entries": "RSS 条目标记",
    "rss.submit_entries_to_qb": "RSS 指定条目提交",
    "rss.submit_pending_to_qb": "RSS 待处理条目提交",
    "rss.retry_failed_to_qb": "RSS 失败条目重试",
    "rss.refresh_subscription": "RSS 订阅刷新",
    "rss.refresh_subscriptions": "RSS 订阅批量刷新",
    "rss.create_subscription": "RSS 订阅创建",
    "rss.update_subscription": "RSS 订阅配置修改",
    "rss.delete_subscription": "RSS 订阅删除",
    "media.create_subscription": "媒体追更创建",
    "media.delete_subscription": "媒体追更删除",
    "media.set_subscription_enabled": "媒体追更状态修改",
    "media.set_subscription_policy": "媒体追更策略修改",
    "media.set_preferences": "媒体偏好修改",
    "media.clear_preferences": "媒体偏好清除",
    "media.set_subscription_notification_rule": "媒体追更通知规则修改",
    "media.reset_subscription_notification_rule": "媒体追更通知规则重置",
    "config.set_feature_state": "功能开关修改",
    "config.set_indexer_sites": "资源检索站点修改",
    "config.set_safe_policy": "安全策略修改",
    "telegram.send_test_notification": "Telegram 测试通知",
    "media_proxy.set_instance_enabled": "媒体反代实例启停",
    "media_proxy.restart_instance": "媒体反代实例重启",
    "local_media.set_source_trigger_enabled": "本地媒体来源触发器启停",
    "local_media.scan_sources": "本地媒体来源扫描",
    "local_media.retry_task": "本地媒体任务重试",
    "local_media.refresh_task_library": "本地媒体任务精准刷新",
    "recognition.set_rule_enabled": "识别规则启停",
    "discovery.confirm_mapping": "发现身份映射确认",
    "discovery.add_watchlist": "探索收藏添加",
    "discovery.remove_watchlist": "探索收藏移除",
    "strm.retry_failures": "STRM 失败项重试",
    "strm.run_once": "STRM 手动同步",
    "strm.set_schedule_policy": "STRM 调度策略修改",
    "guangya.organize.set_schedule_policy": "光鸭定时整理策略修改",
    "guangya.fs.change.execute": "光鸭文件变更执行",
    "guangya.rename.execute": "光鸭重命名执行",
    "guangya.directory_scrape.run": "光鸭目录刮削执行",
    "guangya.organize.run_once": "光鸭整理任务",
    "guangya.organize.cleanup.execute": "光鸭整理残留清理",
    "guangya.organize.stop": "停止光鸭整理任务",
    "indexer.submit_candidate": "资源下载提交",
    "indexer.submit_candidates": "批量资源下载提交",
    "library.set_patrol_policy": "缺集巡检策略修改",
    "library.trigger_patrol_now": "立即全库缺集巡检",
    "library.start_episode_audit": "后台全库剧集检查",
    "agent.cancel_job": "取消后台全库剧集检查",
}

_SAFE_FIELDS = {
    "downloads.retry_submission": {
        "target", "status", "created", "duplicate", "succeeded", "failed",
        "source_attention_preserved",
    },
    "provider.change.execute": {
        "provider", "operation", "status", "affected", "accepted",
        "delete_files", "global_refresh",
    },
    "rss.mark_entries": {"affected", "processed"},
    "rss.submit_entries_to_qb": {
        "target", "requested", "claimed", "submitted", "failed", "outcome_unknown",
    },
    "rss.submit_pending_to_qb": {
        "target", "requested", "claimed", "submitted", "failed", "outcome_unknown",
    },
    "rss.retry_failed_to_qb": {
        "target", "requested", "claimed", "submitted", "failed", "outcome_unknown",
    },
    "rss.refresh_subscription": {"subscription_id", "total", "new", "skipped"},
    "rss.refresh_subscriptions": {
        "requested", "refreshed", "failed", "total", "new", "skipped",
    },
    "rss.create_subscription": {
        "operation", "subscription_id", "affected", "name", "url_count", "action",
        "enabled", "refresh_interval_minutes", "download_method", "media_tmdb_id",
        "media_default_season", "skip_existing_episodes", "runtime_refreshed",
    },
    "rss.update_subscription": {
        "operation", "affected", "changed_field_count",
        "runtime_refreshed",
    },
    "rss.delete_subscription": {"operation", "affected", "deleted_entries", "runtime_refreshed"},
    "media.create_subscription": {
        "operation", "subscription_number", "affected", "created", "season",
        "check_interval_minutes", "runtime_refreshed",
    },
    "media.delete_subscription": {
        "operation", "subscription_number", "affected", "expired_candidates",
        "cancelled_admissions", "cancelled_runs", "runtime_refreshed",
    },
    "media.set_subscription_enabled": {
        "operation", "subscription_number", "enabled", "affected",
        "expired_candidates", "cancelled_admissions", "cancelled_runs", "runtime_refreshed",
    },
    "media.set_subscription_policy": {
        "subscription_number", "updated_fields", "expired_candidates", "runtime_refreshed",
    },
    "media.set_preferences": {
        "operation", "affected", "preferred_server", "preferred_download_target",
    },
    "media.clear_preferences": {"operation", "affected"},
    "media.set_subscription_notification_rule": {
        "operation", "subscription_number", "affected", "enabled",
    },
    "media.reset_subscription_notification_rule": {
        "operation", "subscription_number", "affected",
    },
    "config.set_feature_state": {
        "feature", "enabled", "runtime_refreshed", "verification_state",
    },
    "config.set_indexer_sites": {
        "site_count", "runtime_refreshed", "verification_state",
    },
    "config.set_safe_policy": {"policy", "runtime_refreshed"},
    "telegram.send_test_notification": {"sent"},
    "media_proxy.set_instance_enabled": {"operation", "instance_number", "enabled", "affected", "runtime_refreshed"},
    "media_proxy.restart_instance": {
        "operation", "instance_number", "accepted", "cache_entries_cleared",
    },
    "local_media.scan_sources": {
        "operation", "source_numbers", "scanned_sources", "candidates",
        "queued_tasks", "runtime_started",
    },
    "local_media.set_source_trigger_enabled": {
        "operation", "source_number", "trigger", "enabled", "affected", "runtime_refreshed",
    },
    "local_media.retry_task": {
        "operation", "task_number", "affected", "runtime_refreshed",
    },
    "local_media.refresh_task_library": {
        "operation", "task_number", "refreshed", "matched_paths",
    },
    "recognition.set_rule_enabled": {"operation", "rule_type", "rule_id", "enabled", "affected"},
    "discovery.confirm_mapping": {"affected", "provider", "media_type", "candidate_number", "mapping_confirmed"},
    "discovery.add_watchlist": {"operation", "watchlist_number", "affected"},
    "discovery.remove_watchlist": {"operation", "watchlist_number", "affected"},
    "strm.retry_failures": {
        "scope", "requested", "matched", "resolved", "failed", "missing", "stale",
    },
    "strm.run_once": {"accepted", "trigger"},
    "strm.set_schedule_policy": {"runtime_refreshed"},
    "guangya.organize.set_schedule_policy": {"runtime_refreshed"},
    "guangya.fs.change.execute": {
        "queued", "queue_position", "replayed", "operation_ref", "total",
        "rename_count", "move_count", "trash_count", "create_directory_count",
        "trigger_strm", "requires_manual",
    },
    "guangya.rename.execute": {
        "queued", "queue_position", "replayed", "rename_count", "requires_manual",
    },
    "guangya.directory_scrape.run": {"queued", "queue_position", "replayed", "plan_count"},
    "guangya.organize.run_once": {"trigger_type", "source_count"},
    "guangya.organize.cleanup.execute": {
        "queued", "queue_position", "replayed", "empty_dir_count",
        "residual_dir_count", "selected_count", "kept_count", "requires_manual",
    },
    "guangya.organize.stop": {"accepted"},
    "indexer.submit_candidate": {
        "target", "status", "created", "succeeded", "failed", "duplicate",
    },
    "indexer.submit_candidates": {
        "target", "total", "succeeded", "failed", "duplicate",
    },
    "library.set_patrol_policy": {"runtime_refreshed"},
    "library.trigger_patrol_now": {"queued", "reused", "task_status"},
    "library.start_episode_audit": {
        "accepted", "created", "reused", "max_series", "progress_current", "progress_total",
    },
    "agent.cancel_job": {
        "accepted", "cancelled", "cancel_requested", "progress_current", "progress_total",
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

_OUTCOME_LABELS = {"all": "全部", "success": "成功", "failed": "失败"}
_SAFE_ERROR_CODES = {
    "confirmation_stale",
    "confirmation_not_supported",
    "tool_not_found",
    "execution_interrupted",
}
_COUNT_FIELDS = {
    "requested", "claimed", "submitted", "failed", "matched", "resolved", "missing",
    "stale", "source_count", "succeeded", "cleaned", "subscription_id",
    "total", "new", "skipped", "site_count", "affected",
    "refresh_interval_minutes", "deleted_entries", "changed_field_count",
    "max_series", "progress_current", "progress_total", "instance_number", "rule_id",
    "subscription_number", "watchlist_number", "source_number", "task_number", "season",
    "candidate_number", "queue_position", "plan_count", "outcome_unknown",
    "rename_count", "empty_dir_count", "residual_dir_count", "selected_count",
    "kept_count",
    "expired_candidates", "cancelled_admissions", "cancelled_runs", "refreshed", "matched_paths",
}
_BOOL_FIELDS = {
    "accepted", "enabled", "runtime_refreshed", "created", "duplicate", "delete_files",
    "reused", "cancelled", "cancel_requested", "sent", "processed", "queued",
    "replayed", "mapping_confirmed", "global_refresh",
    "requires_manual",
    "source_attention_preserved",
}
_ENUM_FIELDS = {
    "target": {"qb", "qbittorrent", "guangya", "both"},
    "feature": {
        "discovery", "douban", "resource_results", "indexer_search", "web_search",
        "offline_magnet", "offline_ed2k", "offline_http", "strm_metadata",
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
    "scope": {"all", "generate", "metadata"},
    "trigger": {"manual", "qb_completed", "scan"},
    "trigger_type": {"manual"},
    "rule_type": {"preprocess_rule", "tmdb_regex_rule", "knowledge_entry"},
    "preferred_server": {"any", "jellyfin", "emby"},
    "preferred_download_target": {"qb", "guangya", "both"},
    "provider": {"tmdb", "douban", "bangumi", "media", "qbittorrent"},
    "media_type": {"movie", "tv"},
    "task_status": {"pending", "running", "retry_wait", "not_scheduled"},
    "operation": {
        "pause", "resume", "delete", "enable", "disable", "set_interval", "update",
        "add", "remove", "create", "restore", "retry", "precise_refresh",
        "set_preferences", "clear_preferences", "set_notification_rule",
        "reset_notification_rule", "media.library.refresh", "media.item.refresh",
        "qb.torrents.pause", "qb.torrents.resume", "qb.torrents.delete_task",
    },
    "status": {
        "accepted", "submitted", "completed", "partial", "failed", "duplicate",
        "succeeded", "stale", "outcome_unknown",
    },
}


def action_history_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    extra = set(arguments) - {"limit", "outcome"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_limit = arguments.get("limit", 20)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1 or raw_limit > 50:
        raise AgentToolError("历史条数必须为 1 到 50 的整数")
    outcome = str(arguments.get("outcome", "all") or "all").strip().lower()
    if outcome not in _OUTCOME_LABELS:
        raise AgentToolError("历史结果筛选只支持 all、success 或 failed")
    return {"limit": raw_limit, "outcome": outcome}


def _safe_details(tool_name: str, data: dict[str, Any] | None) -> dict[str, Any]:
    allowed = _SAFE_FIELDS.get(str(tool_name or "").strip(), set())
    if not allowed or not isinstance(data, dict):
        return {}
    projected: dict[str, Any] = {}
    for key in sorted(allowed):
        value = data.get(key)
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
            stored[storage_key] = text[:_CONTRACT_STORAGE_LIMITS.get(public_key, 128)]
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
    started = finished - timedelta(milliseconds=max(0, int(elapsed_ms or 0)))
    return (
        started.isoformat(timespec="milliseconds"),
        finished.isoformat(timespec="milliseconds"),
    )


def action_history_owner_digest(owner: str) -> str:
    """将服务端 owner 派生为不可逆、部署绑定的审计分区键。"""
    normalized = str(owner or "").strip()
    if not normalized:
        raise AgentToolError(
            "无法确认当前 Agent 身份", code="identity_required"
        )
    return hmac.new(
        get_web_secret().encode("utf-8"),
        b"mediaflux-agent-action-history:v1\0" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _confirmation_execution_id(
    confirmation_id: str,
    owner_generation: int,
) -> str:
    ticket_id = str(confirmation_id or "").strip()
    generation = max(0, int(owner_generation or 0))
    return f"{ticket_id}-{generation}"


def record_confirmation_claimed(
    *,
    owner: str,
    confirmation_id: str,
    owner_generation: int,
    tool_name: str,
    risk: RiskLevel,
    confirmation_contract: dict[str, Any] | None = None,
    connection: Any = None,
) -> None:
    """在消费确认票据的同一事务中先持久化执行中状态。"""
    started_at, _finished_at = _timestamps(0)
    safe_tool = _safe_tool_name(tool_name)
    db.add_agent_action_history(
        owner_digest=action_history_owner_digest(owner),
        tool_name=safe_tool,
        risk=risk.value,
        status="executing",
        ok=False,
        summary=_safe_summary(safe_tool, "executing"),
        safe_details=_audit_details(
            tool_name,
            {},
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
            owner_digest=action_history_owner_digest(owner),
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
        )
    except Exception as exc:
        logger.warning(
            "Agent 中断动作审计写入失败 tool=%s type=%s",
            tool_name,
            type(exc).__name__,
        )


def record_confirmed_result(*, owner: str, tool_name: str, risk: RiskLevel,
                            result: ToolResult, elapsed_ms: int,
                            confirmation_contract: dict[str, Any] | None = None,
                            confirmation_id: str = "",
                            owner_generation: int = 0) -> None:
    """尽力记录已确认动作；审计失败不能改变副作用执行结果。"""
    started_at, finished_at = _timestamps(elapsed_ms)
    safe_tool = _safe_tool_name(tool_name)
    safe_status = _safe_status(result.status)
    try:
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest(owner),
            tool_name=safe_tool,
            risk=risk.value,
            status=safe_status,
            ok=bool(result.ok),
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
        )
    except Exception as exc:
        logger.warning("Agent 动作审计写入失败 tool=%s type=%s", tool_name, type(exc).__name__)


def record_confirmation_error(*, owner: str, tool_name: str, risk: RiskLevel,
                              code: str, elapsed_ms: int = 0,
                              confirmation_contract: dict[str, Any] | None = None,
                              confirmation_id: str = "",
                              owner_generation: int = 0) -> None:
    """记录票据已消费、但在执行前因稳定确认错误码终止的动作。"""
    stable_code = _safe_error_code(code) or "confirmation_stale"
    safe_tool = _safe_tool_name(tool_name)
    started_at, finished_at = _timestamps(elapsed_ms)
    try:
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest(owner),
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
        )
    except Exception as exc:
        logger.warning("Agent 动作审计写入失败 tool=%s type=%s", tool_name, type(exc).__name__)


def list_action_history(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    rows = db.list_agent_action_history(
        owner_digest=action_history_owner_digest(context.owner),
        limit=int(arguments["limit"]),
        outcome=str(arguments["outcome"]),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(str(row["safe_details"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        tool_name = _safe_tool_name(row["tool_name"])
        status = _safe_status(row["status"])
        risk = str(row["risk"] or "").strip()
        if risk not in {"low_write", "write", "danger"}:
            risk = "danger"
        confirmation_contract = _stored_confirmation_contract(details)
        outcome = (
            "success"
            if bool(row["ok"])
            else "pending"
            if status == "executing"
            else "unknown"
            if status == "outcome_unknown"
            else "failed"
        )
        item = {
            "tool": tool_name,
            "label": _TOOL_LABELS.get(tool_name, "Agent 受确认动作"),
            "risk": risk,
            "outcome": outcome,
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
    outcome_label = _OUTCOME_LABELS[str(arguments["outcome"])]
    return ToolResult(
        ok=True,
        status="success",
        summary=f"最近 {len(items)} 条 Agent {outcome_label}操作记录",
        data={"items": items, "count": len(items), "outcome": arguments["outcome"]},
        evidence=[Evidence(
            "sqlite:agent_action_history",
            "仅返回受确认动作的服务端脱敏审计投影，不包含对话、票据、凭据或原始参数。",
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )],
        suggestions=[] if items else ["完成一次需要确认的 Agent 动作后，可在这里查看执行记录。"],
    )
