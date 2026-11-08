"""工作区公共只读服务：本地概览、固定行动卡及可选媒体连接探测。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.public_safety import public_followup_prompt
from app.indexers.runtime import source_readiness
from app.logger import get_logger
from app.modules import diagnostics
from app.modules.media_server_profiles import connection
from app.modules.rss_subscriptions import diagnose as diagnose_rss

logger = get_logger(__name__)

WORKSPACE_BACKLOG_SCOPE = "仅核对本地任务记录，不检查外部服务连通性或全系统健康；空待办不能证明服务正常，也不能覆盖此前尚未复测的超时或故障结论。"
_AREA_SPECS = (
    ("downloads", "downloads.diagnose"),
    ("rss", "rss.diagnose"),
    ("organize", "cloud.organize_status"),
    ("strm", "strm.failures"),
    ("local_media", "local.diagnose"),
    ("download_verification", "downloads.diagnose"),
    ("library_patrol", "library.patrol_status"),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _area(
    source: str,
    next_method: str,
    *,
    attention: int = 0,
    active: int = 0,
    waiting: int = 0,
    reason_codes: list[str] | None = None,
    unavailable: bool = False,
) -> dict[str, Any]:
    attention = _count(attention)
    active = _count(active)
    waiting = _count(waiting)
    if unavailable:
        status = "unavailable"
    elif attention:
        status = "attention"
    elif active:
        status = "active"
    elif waiting:
        status = "waiting"
    else:
        status = "idle"
    return {
        "source": source,
        "status": status,
        "attention_count": attention,
        "active_count": active,
        "waiting_count": waiting,
        "reason_codes": list(reason_codes or []),
        "next_method": next_method,
    }


def _unavailable_area(source: str, next_method: str) -> dict[str, Any]:
    return _area(source, next_method, unavailable=True)


def _backlog() -> dict[str, Any]:
    automation: dict[str, Any] | None = None
    local_media: dict[str, Any] | None = None
    persistent_health: dict[str, Any] | None = None
    unavailable_sources: list[str] = []
    try:
        raw_automation = db.get_dashboard_automation_summary()
        automation = raw_automation if isinstance(raw_automation, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 各本地来源独立降级，不返回异常正文。
        unavailable_sources.append("automation")
        logger.warning("Agent 工作区待办读取自动化汇总失败 type=%s", type(exc).__name__)
    try:
        raw_local_media = db.get_local_media_diagnostic_summary(owner="admin")
        local_media = raw_local_media if isinstance(raw_local_media, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 各本地来源独立降级，不返回异常正文。
        unavailable_sources.append("local_media")
        logger.warning(
            "Agent 工作区待办读取本地媒体汇总失败 type=%s", type(exc).__name__
        )
    try:
        raw_persistent = db.get_agent_persistent_health_summary()
        persistent_health = raw_persistent if isinstance(raw_persistent, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 各本地来源独立降级，不返回异常正文。
        unavailable_sources.append("agent_persistent")
        logger.warning(
            "Agent 工作区待办读取持久自动化汇总失败 type=%s", type(exc).__name__
        )
    areas: list[dict[str, Any]] = []
    if automation is None:
        areas.extend(
            (
                _unavailable_area(source, next_method)
                for source, next_method in _AREA_SPECS[:4]
            )
        )
    else:
        downloads_review = _count(automation.get("downloads_review"))
        downloads_active = _count(automation.get("downloads_active"))
        areas.append(
            _area(
                "downloads",
                "downloads.diagnose",
                attention=downloads_review,
                active=downloads_active,
                reason_codes=["download_needs_review"] if downloads_review else [],
            )
        )
        rss: dict[str, Any]
        try:
            rss = diagnose_rss()
        except Exception as exc:  # noqa: BLE001 - 各本地来源独立降级，不返回异常正文。
            logger.warning("工作区待办RSS诊断失败 type=%s", type(exc).__name__)
            rss = {"ok": False}
        if not rss.get("ok"):
            unavailable_sources.append("rss")
            areas.append(_unavailable_area("rss", "rss.diagnose"))
        else:
            rss_failed = _count(rss["data"]["entries"].get("failed"))
            rss_pending = _count(rss["data"]["entries"].get("pending"))
            rss_reasons: list[str] = []
            if rss_failed:
                rss_reasons.append("rss_failed")
            if rss_pending:
                rss_reasons.append("rss_pending")
            areas.append(
                _area(
                    "rss",
                    "rss.diagnose",
                    attention=rss_failed,
                    waiting=rss_pending,
                    reason_codes=rss_reasons,
                )
            )
        organize_issues = _count(automation.get("organize_issues"))
        areas.append(
            _area(
                "organize",
                "cloud.organize_status",
                attention=organize_issues,
                reason_codes=["organize_issue"] if organize_issues else [],
            )
        )
        strm_failures = _count(automation.get("strm_failures"))
        strm_last_status = (
            str(automation.get("strm_last_status") or "").strip().casefold()
        )
        strm_last_failed = strm_last_status in {
            "failed",
            "error",
            "interrupted",
            "partial_failed",
            "revert_failed",
            "cancelled",
        }
        strm_running = strm_last_status in {"running", "started", "queued"}
        strm_attention = strm_failures or int(strm_last_failed)
        strm_reasons: list[str] = []
        if strm_failures:
            strm_reasons.append("strm_open_failure")
        if strm_last_failed:
            strm_reasons.append("strm_last_run_failed")
        if strm_running:
            strm_reasons.append("strm_running")
        areas.append(
            _area(
                "strm",
                "strm.failures",
                attention=strm_attention,
                active=int(strm_running),
                reason_codes=strm_reasons,
            )
        )
    if local_media is None:
        areas.append(_unavailable_area("local_media", "local.diagnose"))
    else:
        raw_sources = local_media.get("sources")
        raw_tasks = local_media.get("tasks")
        sources = raw_sources if isinstance(raw_sources, dict) else {}
        tasks = raw_tasks if isinstance(raw_tasks, dict) else {}
        requires_manual = _count(tasks.get("requires_manual"))
        failed = _count(tasks.get("failed"))
        missing_target = _count(sources.get("enabled_without_targets"))
        active = _count(tasks.get("active"))
        waiting = _count(tasks.get("waiting_stable")) + _count(tasks.get("planned"))
        local_reasons: list[str] = []
        if requires_manual:
            local_reasons.append("local_media_requires_manual")
        if failed:
            local_reasons.append("local_media_failed")
        if missing_target:
            local_reasons.append("local_media_missing_target")
        if active:
            local_reasons.append("local_media_active")
        if waiting:
            local_reasons.append("local_media_waiting")
        areas.append(
            _area(
                "local_media",
                "local.diagnose",
                attention=requires_manual + failed + missing_target,
                active=active,
                waiting=waiting,
                reason_codes=local_reasons,
            )
        )
    if persistent_health is None:
        areas.extend(
            (
                _unavailable_area(source, next_method)
                for source, next_method in _AREA_SPECS[5:]
            )
        )
    else:
        raw_verification = persistent_health.get("download_verification")
        verification = raw_verification if isinstance(raw_verification, dict) else {}
        verification_attention = _count(verification.get("attention"))
        verification_running = _count(verification.get("running"))
        verification_pending = _count(verification.get("pending"))
        verification_retry = _count(verification.get("retry_wait"))
        verification_reasons: list[str] = []
        if verification_attention:
            verification_reasons.append("download_verification_attention")
        if verification_running:
            verification_reasons.append("download_verification_running")
        if verification_pending:
            verification_reasons.append("download_verification_pending")
        if verification_retry:
            verification_reasons.append("download_verification_retry_wait")
        verification_area = _area(
            "download_verification",
            "downloads.diagnose",
            attention=verification_attention,
            active=verification_running,
            waiting=verification_pending + verification_retry,
            reason_codes=verification_reasons,
        )
        verification_area.update(
            {
                "pending_count": verification_pending,
                "retry_wait_count": verification_retry,
                "visible_count": _count(verification.get("visible")),
            }
        )
        areas.append(verification_area)
        raw_patrol = persistent_health.get("library_patrol")
        patrol = raw_patrol if isinstance(raw_patrol, dict) else {}
        patrol_status = str(patrol.get("status") or "not_created").strip().casefold()
        if patrol_status not in {"not_created", "pending", "running", "retry_wait"}:
            patrol_status = "not_created"
        patrol_outcome = str(patrol.get("outcome") or "").strip().casefold()
        if patrol_outcome not in {
            "",
            "updates_available",
            "up_to_date",
            "inconclusive",
            "not_configured",
            "unavailable",
            "failed",
        }:
            patrol_outcome = ""
        updates_available = _count(patrol.get("updates_available_count"))
        patrol_failed = patrol_outcome in {
            "inconclusive",
            "not_configured",
            "unavailable",
            "failed",
        }
        patrol_attention = max(updates_available, int(patrol_failed))
        patrol_reasons: list[str] = []
        if updates_available:
            patrol_reasons.append("library_patrol_updates_available")
        if patrol_failed:
            patrol_reasons.append(f"library_patrol_{patrol_outcome}")
        if patrol_status == "running":
            patrol_reasons.append("library_patrol_running")
        if patrol_status == "retry_wait":
            patrol_reasons.append("library_patrol_retry_wait")
        patrol_area = _area(
            "library_patrol",
            "library.patrol_status",
            attention=patrol_attention,
            active=int(patrol_status == "running"),
            waiting=int(patrol_status == "retry_wait"),
            reason_codes=patrol_reasons,
        )
        patrol_area.update(
            {
                "task_status": patrol_status,
                "outcome": patrol_outcome,
                "checked_series_count": _count(patrol.get("checked_series_count")),
                "updates_available_count": updates_available,
                "missing_episode_count": _count(patrol.get("missing_episode_count")),
                "inconclusive_count": _count(patrol.get("inconclusive_count")),
                "unmapped_series_count": _count(patrol.get("unmapped_series_count")),
                "findings_truncated": bool(_count(patrol.get("findings_truncated"))),
            }
        )
        areas.append(patrol_area)
    attention_total = sum(item["attention_count"] for item in areas)
    active_total = sum(item["active_count"] for item in areas)
    waiting_total = sum(item["waiting_count"] for item in areas)
    data = {
        "probe_mode": "local",
        "scope": WORKSPACE_BACKLOG_SCOPE,
        "network_accessed": False,
        "filesystem_accessed": False,
        "attention_total": attention_total,
        "active_total": active_total,
        "waiting_total": waiting_total,
        "unavailable_areas": [
            item["source"] for item in areas if item["status"] == "unavailable"
        ],
        "areas": areas,
    }
    evidence: list[dict[str, Any]] = []
    if automation is not None:
        evidence.append(
            {
                "source": "automation_database",
                "description": "读取下载、RSS、整理与 STRM 的本地安全计数；未访问外部服务或启动任务。",
                "collected_at": _now(),
            }
        )
    if local_media is not None:
        evidence.append(
            {
                "source": "local_media_database",
                "description": "读取本地媒体安全计数；未扫描媒体文件系统或启动任务。",
                "collected_at": _now(),
            }
        )
    if persistent_health is not None:
        evidence.append(
            {
                "source": "agent_persistent_database",
                "description": "读取下载后核验与媒体库巡检的匿名状态计数；未读取标题、路径、任务标识或错误正文。",
                "collected_at": _now(),
            }
        )
    if areas and all(item["status"] == "unavailable" for item in areas):
        return {
            "ok": False,
            "status": "unavailable",
            "summary": "暂时无法读取工作区待办",
            "data": data,
            "evidence": [
                {
                    "source": "workspace_database",
                    "description": "尝试读取本地安全聚合；未访问网络、文件系统或启动任务。",
                    "collected_at": _now(),
                }
            ],
            "suggestions": ["请检查本地数据库状态后重试。"],
            "error": "工作区待办当前不可用。",
        }
    suggestions: list[str] = []
    for item in areas:
        if item["attention_count"]:
            suggestions.append(public_followup_prompt(item.get("source")))
    if unavailable_sources:
        status = "partial"
        summary = f"已读取部分工作区待办，共有 {attention_total} 项需要关注"
        suggestions.append("部分本地聚合暂时不可用，请稍后重试。")
    elif attention_total:
        status = "attention"
        summary = f"工作区共有 {attention_total} 项需要关注"
    elif active_total:
        status = "active"
        summary = f"工作区有 {active_total} 项正在处理"
    elif waiting_total:
        status = "waiting"
        summary = f"工作区有 {waiting_total} 项等待处理"
    else:
        status = "empty"
        summary = "本地任务记录中没有待处理事项；未检查外部服务状态"
    return {
        "ok": True,
        "status": status,
        "summary": summary,
        "data": data,
        "evidence": evidence,
        "suggestions": suggestions,
    }


_ACTION_SPECS: dict[str, dict[str, str]] = {
    "downloads": {
        "action_key": "review_downloads",
        "label": "检查下载队列",
        "target_tool": "agent.run_code",
        "prompt": "诊断下载队列",
        "why": "下载队列存在需要人工关注的状态。",
        "sdk_method": "downloads.diagnose",
    },
    "rss": {
        "action_key": "review_rss",
        "label": "检查 RSS 订阅",
        "target_tool": "agent.run_code",
        "sdk_method": "rss.diagnose",
        "prompt": "诊断 RSS 订阅",
        "why": "RSS 条目存在失败或待处理状态。",
    },
    "organize": {
        "action_key": "review_organize",
        "label": "检查云盘整理",
        "target_tool": "agent.run_code",
        "prompt": "查看光鸭整理状态",
        "why": "云盘整理流程存在需要核对的状态。",
        "sdk_method": "cloud.organize_status",
    },
    "strm": {
        "action_key": "review_strm",
        "label": "检查 STRM 失败项",
        "target_tool": "agent.run_code",
        "prompt": "分析 STRM 同步失败项",
        "why": "STRM 同步存在失败或中断记录。",
        "sdk_method": "strm.failures",
    },
    "local_media": {
        "action_key": "review_local_media",
        "label": "检查本地媒体",
        "target_tool": "agent.run_code",
        "prompt": "诊断本地媒体",
        "why": "本地媒体流程存在待确认、失败或目标缺失。",
        "sdk_method": "local.diagnose",
    },
    "download_verification": {
        "action_key": "review_download_verification",
        "label": "检查下载后入库核验",
        "target_tool": "agent.run_code",
        "prompt": "诊断下载队列",
        "why": "下载后的媒体库可见性核验需要关注。",
        "sdk_method": "downloads.diagnose",
    },
    "library_patrol": {
        "action_key": "review_library_patrol",
        "label": "检查媒体库巡检",
        "target_tool": "agent.run_code",
        "prompt": "查看最近全库巡检结果",
        "why": "媒体库巡检发现可更新内容或未能完成检查。",
        "sdk_method": "library.patrol_status",
    },
}
_ALLOWED_REASON_CODES: dict[str, frozenset[str]] = {
    "downloads": frozenset({"download_needs_review"}),
    "rss": frozenset({"rss_failed", "rss_pending"}),
    "organize": frozenset({"organize_issue"}),
    "strm": frozenset({"strm_open_failure", "strm_last_run_failed", "strm_running"}),
    "local_media": frozenset(
        {
            "local_media_requires_manual",
            "local_media_failed",
            "local_media_missing_target",
            "local_media_active",
            "local_media_waiting",
        }
    ),
    "download_verification": frozenset(
        {
            "download_verification_attention",
            "download_verification_running",
            "download_verification_pending",
            "download_verification_retry_wait",
        }
    ),
    "library_patrol": frozenset(
        {
            "library_patrol_updates_available",
            "library_patrol_inconclusive",
            "library_patrol_not_configured",
            "library_patrol_unavailable",
            "library_patrol_failed",
            "library_patrol_running",
            "library_patrol_retry_wait",
        }
    ),
}
_SNAPSHOT_STATUSES = frozenset(
    {"attention", "active", "waiting", "empty", "partial", "unavailable"}
)
_ACTION_BY_KEY = {spec["action_key"]: spec for spec in _ACTION_SPECS.values()}


def action_handoff_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    if not isinstance(arguments, dict) or set(arguments) != {"action_key"}:
        raise AgentToolError("工作区行动接力必须且只能包含 action_key")
    action_key = arguments.get("action_key")
    if not isinstance(action_key, str):
        raise AgentToolError("action_key 必须是字符串")
    normalized = action_key.strip()
    if normalized not in _ACTION_BY_KEY:
        raise AgentToolError("不支持的工作区行动")
    return {"action_key": normalized}


def resolve_action_handoff(arguments: dict[str, Any]) -> dict[str, Any]:
    """重新读取当前快照，并把固定 action key 解析为可信只读目标。"""
    normalized = action_handoff_arguments(arguments)
    action_key = normalized["action_key"]
    snapshot = next_actions()
    if not snapshot["ok"]:
        raise AgentToolError(
            "当前无法核对工作区行动，请稍后重试", code="precondition_failed"
        )
    raw_actions = (
        snapshot["data"].get("actions") if isinstance(snapshot["data"], dict) else []
    )
    actions = raw_actions if isinstance(raw_actions, list) else []
    if not any(
        isinstance(action, dict) and action.get("action_key") == action_key
        for action in actions
    ):
        raise AgentToolError(
            "该工作区行动已失效，请刷新下一步后重试", code="precondition_failed"
        )
    spec = _ACTION_BY_KEY[action_key]
    return {
        "action_key": action_key,
        "label": spec["label"],
        "target_tool": spec["target_tool"],
        "arguments": {
            "code": f"import json\nfrom mf import {spec['sdk_method'].split('.')[0]}\nprint(json.dumps({spec['sdk_method']}(), ensure_ascii=False))"
        },
    }


def _safe_sources(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [source for source in _ACTION_SPECS if source in value]


def _project_action(area: Any) -> dict[str, Any] | None:
    if not isinstance(area, dict):
        return None
    source = str(area.get("source") or "").strip()
    spec = _ACTION_SPECS.get(source)
    attention_count = _count(area.get("attention_count"))
    if (
        spec is None
        or str(area.get("status") or "") != "attention"
        or (not attention_count)
    ):
        return None
    allowed_reasons = _ALLOWED_REASON_CODES[source]
    raw_reasons = area.get("reason_codes")
    reason_codes: list[str] = []
    if isinstance(raw_reasons, list):
        for value in raw_reasons:
            reason = str(value or "").strip()
            if reason in allowed_reasons and reason not in reason_codes:
                reason_codes.append(reason)
    if not reason_codes:
        return None
    return {
        "action_key": spec["action_key"],
        "source": source,
        "status": "attention",
        "attention_count": attention_count,
        "reason_codes": reason_codes,
        "label": spec["label"],
        "why": spec["why"],
        "target_tool": spec["target_tool"],
        "prompt": spec["prompt"],
        "risk": "read",
        "requires_confirmation": False,
        "precondition": "not_required",
        "staleness": "snapshot_only",
    }


def next_actions() -> dict[str, Any]:
    return _next_actions(_backlog())


def _next_actions(todo: dict[str, Any]) -> dict[str, Any]:
    """只投影现有待办快照，不执行诊断、预检或写操作。"""
    todo_data = todo["data"] if isinstance(todo["data"], dict) else {}
    raw_areas = todo_data.get("areas")
    projected_by_source: dict[str, dict[str, Any]] = {}
    for area in raw_areas if isinstance(raw_areas, list) else []:
        action = _project_action(area)
        if action is not None:
            projected_by_source.setdefault(action["source"], action)
    actions = [
        projected_by_source[source]
        for source in _ACTION_SPECS
        if source in projected_by_source
    ]
    snapshot_status = (todo["status"] or "").strip()
    if snapshot_status not in _SNAPSHOT_STATUSES:
        snapshot_status = "unavailable" if not todo["ok"] else "partial"
    unavailable_areas = _safe_sources(todo_data.get("unavailable_areas"))
    data = {
        "probe_mode": "derived_local_snapshot",
        "scope": WORKSPACE_BACKLOG_SCOPE,
        "network_accessed": False,
        "filesystem_accessed": False,
        "source_tool": "diagnostics.overview",
        "snapshot_status": snapshot_status,
        "attention_total": _count(todo_data.get("attention_total")),
        "active_total": _count(todo_data.get("active_total")),
        "waiting_total": _count(todo_data.get("waiting_total")),
        "unavailable_areas": unavailable_areas,
        "actions": actions,
    }
    evidence = [
        {
            "source": "workspace_todo_projection",
            "description": "从工作区本地安全待办快照生成只读行动卡；未访问网络、文件系统，也未执行诊断、预检或写操作。",
            "collected_at": _now(),
        }
    ]
    if not todo["ok"] or snapshot_status == "unavailable":
        return {
            "ok": False,
            "status": "unavailable",
            "summary": "暂时无法生成工作区下一步",
            "data": data,
            "evidence": evidence,
            "suggestions": ["请检查本地数据库状态后重试。"],
            "error": "工作区下一步当前不可用。",
        }
    suggestions = [f"可询问：{action['prompt']}。" for action in actions[:5]]
    if unavailable_areas:
        suggestions.append("部分本地聚合暂时不可用，请稍后重试。")
    if snapshot_status == "partial":
        status = "partial"
        summary = (
            f"已生成 {len(actions)} 个安全下一步，部分工作区状态暂不可用"
            if actions
            else "部分工作区状态暂不可用，当前没有可安全生成的下一步"
        )
    elif actions:
        status = "attention"
        summary = f"已生成 {len(actions)} 个安全下一步"
    elif snapshot_status == "active":
        status = "active"
        summary = "本地任务正在处理，当前未生成额外行动卡"
    elif snapshot_status == "waiting":
        status = "waiting"
        summary = "本地任务记录中存在等待项，当前未生成需立即执行的行动卡"
    else:
        status = "empty"
        summary = "本地任务记录中没有需要处理的下一步；不代表系统健康检查通过"
    return {
        "ok": True,
        "status": status,
        "summary": summary,
        "data": data,
        "evidence": evidence,
        "suggestions": suggestions,
    }


def overview(probe: bool = False) -> dict[str, Any]:
    """共用本地待办、配置和索引器事实；仅 probe=True 时读取媒体节点连通性。"""
    if type(probe) is not bool:
        raise ValueError("probe 必须是布尔值")
    backlog = _backlog()
    areas = list(backlog["data"]["areas"])
    sources = {"backlog": backlog}
    evidence = list(backlog.get("evidence", []))
    result: dict[str, Any]
    for name, reader in (
        ("indexers", lambda: source_readiness(scope="agent")),
        ("configuration", diagnostics.configuration),
        *((("media_servers", connection),) if probe else ()),
    ):
        try:
            result = reader()
        except Exception as exc:  # noqa: BLE001 - 各本地来源独立降级，不返回异常正文。
            logger.warning(
                "工作区概览读取失败 source=%s type=%s", name, type(exc).__name__
            )
            result = {
                "ok": False,
                "status": "unavailable",
                "data": {},
                "error": "诊断来源读取失败",
                "exception_type": type(exc).__name__,
            }
        sources[name] = result
        data = result.get("data", {})
        counts = (
            result.get("counts", {}) if name == "indexers" else data.get("counts", {})
        )
        unavailable = result["status"] == "unavailable"
        if (
            name == "media_servers"
            and data.get("nodes")
            and data.get("probe_complete") is not False
        ):
            unavailable = False
        attention = (
            _count(counts.get("errors")) + _count(counts.get("warnings"))
            if name == "configuration"
            else _count(counts.get("attention"))
        )
        if (
            name == "media_servers"
            and not unavailable
            and result["status"] not in {"healthy", "not_configured"}
        ):
            attention = max(attention, 1)
        status = (
            "unavailable"
            if unavailable
            else "partial"
            if result["status"] == "partial"
            else "attention"
            if attention
            else result["status"]
        )
        area = _area(
            name, "agent.run_code", attention=attention, unavailable=unavailable
        )
        area.update(
            status=status,
            next_method={
                "indexers": "resources.sources",
                "configuration": "diagnostics.configuration",
                "media_servers": "library.connection",
            }[name],
            counts=counts,
        )
        # 连接限流/未完成探测返回 None，不能把未知数转换成零或离线。
        if name == "media_servers":
            area.update(
                {
                    key + "_count": counts.get(key)
                    for key in ("enabled", "configured", "online", "compatible")
                }
            )
            area["reason_codes"] = (
                ["media_server_diagnosis_unavailable"]
                if unavailable
                else ["media_server_not_configured"]
                if counts.get("enabled") == 0
                else ["media_server_offline"]
                if counts.get("configured") and counts.get("online") == 0
                else []
            )
        areas.append(area)
        evidence.extend(result.get("evidence", []))

    unavailable = [area["source"] for area in areas if area["status"] == "unavailable"]
    degraded = [area["source"] for area in areas if area["status"] == "partial"]
    attention = sum(area["attention_count"] for area in areas)
    active, waiting = backlog["data"]["active_total"], backlog["data"]["waiting_total"]
    status = (
        "unavailable"
        if len(unavailable) == len(areas)
        else "partial"
        if unavailable or degraded
        else "attention"
        if attention
        else "active"
        if active
        else "waiting"
        if waiting
        else "healthy"
    )
    not_probed = [
        "cloud_directory_content_scan",
        "per_title_episode_audit",
        "per_title_update_check",
        "indexer_network_search",
        "download_submission",
    ]
    if not probe:
        not_probed.append("media_server_connectivity")
    network = (
        sources["media_servers"]["data"].get("network_accessed") if probe else False
    )
    return {
        "ok": status != "unavailable",
        "status": status,
        "summary": "工作区概览：" + status,
        "data": {
            "probe_mode": "local_and_media_endpoints" if probe else "local_snapshot",
            "as_of": _now(),
            "network_accessed": network,
            "content_filesystem_scanned": False,
            "scope": WORKSPACE_BACKLOG_SCOPE
            if not probe
            else "本地状态与媒体节点连接探测；未扫描内容目录或启动任务。",
            "attention_total": attention,
            "active_total": active,
            "waiting_total": waiting,
            "areas": areas,
            "sources": sources,
            "next_actions": _next_actions(backlog),
            "coverage": {
                "requested": [area["source"] for area in areas],
                "available": [
                    area["source"] for area in areas if area["status"] != "unavailable"
                ],
                "unavailable": unavailable,
                "partial": degraded,
                "disabled": [
                    area["source"] for area in areas if area["status"] == "disabled"
                ],
                "not_configured": [
                    area["source"]
                    for area in areas
                    if area["status"] == "not_configured"
                ],
                "not_probed": not_probed,
            },
        },
        "evidence": evidence,
        "suggestions": [
            public_followup_prompt(area["source"])
            for area in areas
            if area["status"] in {"attention", "unavailable"}
        ],
    }
