"""整理任务的版本化结构化结果协议。

Web、Telegram 和调度器都直接消费该结构，禁止从自由格式日志反向解析。
新增字段只能追加；旧任务记录缺字段时按默认值补齐，保证跨版本可读。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.repositories.organize_operation_jobs import CLOUD_COPY_PENDING_CODE, sanitize_organize_operation_result
from app.sensitive_data import redact_sensitive_text

# 递增规则：仅追加字段不升版本；语义变化或字段移除必须升版本并保留读取兼容。
ORGANIZE_RESULT_SCHEMA_VERSION = 1

_COUNTER_FIELDS = (
    "total", "matched", "moved", "renamed", "metadata_moved",
    "skipped", "need_confirm", "failed", "conflict",
    "subtitle_moved", "subtitle_skipped",
    "empty_dirs_cleaned", "replacement_cleanup_failed", "audit_failures",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def organize_strm_error(stats: Mapping[str, Any]) -> str:
    """联动拒绝与云端执行分开结算；已排队或主动跳过不伪报同步失败。"""
    handoff = stats.get("strm")
    if isinstance(handoff, dict) and handoff.get("ok") is False and not handoff.get("skipped"):
        return redact_sensitive_text(handoff.get("error") or "STRM 联动未受理")[:500]
    return "STRM 联动未受理" if _int(stats.get("strm_trigger_failed")) > 0 else ""


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def build_organize_result(
    stats: dict,
    *,
    status: str,
    source_results: object = (),
    notification_sent: bool = False,
    task_id: str = "",
    current_source: str = "",
    error: str = "",
) -> dict[str, Any]:
    """把任务级统计收敛成版本化结果。"""
    stats = stats if isinstance(stats, dict) else {}
    counters = {name: _int(stats.get(name)) for name in _COUNTER_FIELDS}
    strm = stats.get("strm")
    strm_changes = _dict_list(stats.get("strm_changes"))
    changed_dirs = _text_list(stats.get("changed_target_dirs"))
    if not changed_dirs and strm_changes:
        # 任务级 stats 不单独维护该字段，从权威的变化清单推导。
        from app.modules.organize_groups import changed_target_dirs as _derive

        changed_dirs = _derive(strm_changes)
    return {
        "schema_version": ORGANIZE_RESULT_SCHEMA_VERSION,
        "task_id": (task_id or ""),
        "status": (status or ""),
        "current_source": (current_source or ""),
        "error": (error or ""),
        "counters": counters,
        "groups": _dict_list(stats.get("group_results")),
        "group_progress": (
            dict(stats["group_progress"])
            if isinstance(stats.get("group_progress"), dict) else {}
        ),
        "changed_target_dirs": changed_dirs,
        "strm": dict(strm) if isinstance(strm, dict) else {},
        "strm_changes": strm_changes,
        "media_refresh": (
            dict(stats["media_refresh"])
            if isinstance(stats.get("media_refresh"), dict) else {}
        ),
        "notification": {"sent": notification_sent},
        "confirmations": _text_list(stats.get("confirmations")),
        "confirmation_groups": _dict_list(stats.get("confirmation_groups")),
        "scan_errors": _text_list(stats.get("scan_errors")),
        "scan_complete": stats.get("scan_complete") is not False,
        "protected_staging_dirs": _dict_list(stats.get("protected_staging_dirs")),
        "resumed_download_ids": list(stats.get("resumed_download_ids") or []),
        "skip_reasons": _text_list(stats.get("skip_reasons")),
        "sources": _dict_list(source_results),
    }


def read_organize_result(payload: object) -> dict[str, Any]:
    """读取任意历史版本的任务结果，缺字段按默认值补齐。

    未知的高版本按“尽力读取”处理：已知字段照常提供，未知字段原样保留，
    禁止因为版本号更高就丢弃整段结果。
    """
    if not isinstance(payload, dict):
        return build_organize_result({}, status="")
    version = payload.get("schema_version")
    if version is None:
        # 历史上存在两类未版本化记录：直接 stats，以及任务包装器
        # {task_id, stats, source_results, current_source}。兼容读取时必须
        # 先解开包装器，否则 counters 会全部退化为 0。
        legacy_stats = payload.get("stats")
        stats = legacy_stats if isinstance(legacy_stats, dict) else payload
        return build_organize_result(
            stats,
            status=str(payload.get("status") or stats.get("status") or ""),
            source_results=payload.get("source_results") or payload.get("sources") or (),
            notification_sent=bool(payload.get("notification_sent")),
            task_id=str(payload.get("task_id") or ""),
            current_source=str(payload.get("current_source") or ""),
            error=str(payload.get("error") or ""),
        )
    normalized = build_organize_result({}, status=str(payload.get("status") or ""))
    normalized.update({
        key: value for key, value in payload.items() if key != "schema_version"
    })
    counters = payload.get("counters")
    normalized["counters"] = {
        name: _int((counters or {}).get(name)) for name in _COUNTER_FIELDS
    } if isinstance(counters, dict) else normalized["counters"]
    try:
        normalized["schema_version"] = int(version)
    except (TypeError, ValueError):
        normalized["schema_version"] = ORGANIZE_RESULT_SCHEMA_VERSION
    return normalized


_TASK_STATUSES = {
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
}


def _status_timestamp(value: Any) -> str:
    text = str(value or "").strip()[:32]
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text


def _safe_operation_result(raw: dict[str, Any]) -> dict[str, Any]:
    stats = raw.get("stats")
    nested_result = raw.get("result")
    if not stats and isinstance(nested_result, dict):
        result = nested_result
        stats = result.get("stats")
        if not isinstance(stats, dict):
            stats = result.get("counters")

    payload: dict[str, Any] = {}
    if isinstance(stats, dict):
        payload["stats"] = {key: value if key == "strm" else max(0, min(_int(value), 1_000_000_000)) for key, value in stats.items()}
        if isinstance(nested_result, dict) and isinstance(nested_result.get("strm"), dict):
            payload["stats"]["strm"] = nested_result["strm"]
    if "operation_items" in raw:
        payload["operation_items"] = raw["operation_items"]
    elif isinstance(nested_result, dict) and "operation_items" in nested_result:
        payload["operation_items"] = nested_result["operation_items"]

    return sanitize_organize_operation_result(payload)


def project_organize_status(
    raw: dict[str, Any],
    *,
    overview: dict[str, Any],
    operation_ref: str = "",
) -> dict[str, Any]:
    """公开整理与持久操作的同源状态，只保留安全统计和逐项回执。"""
    task_status = str(raw.get("status") or "").strip().lower()
    if task_status not in _TASK_STATUSES:
        task_status = "idle"
    running = task_status in {"running", "stopping"}
    safe_result = _safe_operation_result(raw)
    stats = safe_result.get("stats", {})
    handoff_error = organize_strm_error(stats)
    # 整源扫描与下载后续整理是同一业务的两个阶段；保留入队事实，而非伪报入库完成。
    result = raw.get("result") or raw.get("stats") or {}
    resumed_ids = result.get("resumed_download_ids", []) if isinstance(result, dict) else []
    resumed_count = len({value for value in resumed_ids if type(value) is int and value > 0}) if isinstance(resumed_ids, list) else 0
    protected_count = len(_dict_list(result.get("protected_staging_dirs"))) if isinstance(result, dict) else 0
    if (handoff_error or protected_count) and task_status == "completed":
        task_status = "partial"
    schedule_raw = (
        overview.get("schedule") if isinstance(overview.get("schedule"), dict) else {}
    )
    schedule = {
        "enabled": bool(schedule_raw.get("enabled")),
        "configured": not bool(schedule_raw.get("config_error")),
        "cron_valid": bool(schedule_raw.get("cron_valid")),
        "next_run": _status_timestamp(schedule_raw.get("next_run")),
    }
    queue_raw = overview.get("operation_queue")
    queue_total = (
        max(0, min(_int(queue_raw.get("total")), 1_000_000_000)) if isinstance(queue_raw, dict) else 0
    )

    if running:
        ok, status, summary = True, "running", "光鸭整理任务正在运行"
        suggestions: list[str] = []
    elif task_status == "queued":
        ok, status, summary = True, "queued", "光鸭整理操作正在排队"
        suggestions = ["任务会在当前整理操作结束后自动执行。"]
        if raw.get("error_code") == CLOUD_COPY_PENDING_CODE:
            summary = "光鸭复制仍在处理中，完成后将继续原计划"
            suggestions = ["系统会继续跟踪原任务；无需再次确认或重复提交复制。"]
    elif task_status == "manual_review":
        ok, status, summary = False, "attention", "光鸭操作需要进一步核验"
        suggestions = ["请先核对光鸭目标目录，确认远端结果后再决定是否重新执行。"]
    elif task_status == "failed":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务未成功"
        suggestions = ["请到网盘整理页查看任务详情后再决定是否重试。"]
    elif task_status == "completed":
        ok, status, summary = True, "completed", "最近一次光鸭整理任务已完成"
        suggestions = []
    elif task_status == "partial":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务部分完成"
        suggestions = (
            ["已恢复的下载整理会由下载跟踪器重新核验落稳后继续，无需再次提交整理；可查询下载任务进度。其他未恢复或失败项仍需单独核对。"]
            if resumed_count else ["请到网盘整理页核对失败项后再决定是否重试。"]
        )
        if protected_count and not resumed_count:
            suggestions = ["请先核对下载任务状态；已完成下载的待确认项可从原整理日志查看详情并纠正后重新整理。"]
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

    if stats.get("strm_scope_unknown"):
        suggestions.append(
            "文件变更结果已记录，但同步范围未能确认，本次未触发 STRM 联动；请核对同步目录后手动同步。"
        )

    # 只公开有明确统计依据的故障阶段，不转发 Provider 原始异常或私有路径。
    problems = []
    if protected_count:
        summary += f"；受保护下载目录 {protected_count} 个未扫描"
        if task_status in {"partial", "failed", "manual_review"}:
            problems.append(f"{protected_count} 个受保护下载目录未扫描：下载隔离目录仍可能被写入或等待核验")
    if handoff_error:
        problems.append(handoff_error)
        suggestions.append("已完成的云端变更无需重复执行；恢复 STRM 调度后执行同步补齐，不要重新下载或整理。")
    if task_status in {"partial", "failed", "manual_review"}:
        if raw.get("error_code") == "GuangYaFSChangeStale":
            attempted = any(item.get("status") not in {"not_started", "blocked"} for item in safe_result.get("operation_items", []))
            problems.append(
                "执行期间凭据或对象状态已变化；已发生的变更以逐项回执为准，剩余操作未继续"
                if attempted else "冻结计划、凭据或对象状态已变化，本次变更未执行；请重新读取目录并生成预览"
            )
        for key, description in (
            ("precondition_failed", "项写前核对未通过，后续动作未执行"),
            ("verification_failed", "项写后状态未核验通过，不能据此认定未执行，请勿直接重复提交"),
            ("audit_failures", "项执行审计未完整保存，需要核对实际状态"),
        ):
            if stats.get(key):
                problems.append(f"{stats[key]} {description}")

    trigger_type = str(raw.get("trigger_type") or "").strip().lower()
    task_data = {
        "status": task_status,
        "running": running,
        "stoppable": bool(raw.get("stoppable")) if running else False,
        "trigger_type": trigger_type if trigger_type in {"manual", "cron", "telegram"} else "",
        "started_at": _status_timestamp(raw.get("started_at")),
        "finished_at": _status_timestamp(raw.get("finished_at")),
        "stats": stats,
    }
    if protected_count:
        task_data["protected_staging_count"] = protected_count
        task_data["scan_complete"] = False
    if resumed_count:
        task_data["resumed_download_count"] = resumed_count
        summary += f"；本轮已恢复 {resumed_count} 个下载整理任务，尚不代表入库完成"
    if "operation_items" in safe_result:
        task_data["operation_items"] = safe_result["operation_items"]
    if operation_ref:
        task_data["operation_ref"] = operation_ref
    return {
        "ok": ok,
        "status": status,
        "summary": summary,
        "data": {
            "task": task_data,
            "queue": {"pending_count": queue_total},
            "schedule": schedule,
        },
        "suggestions": suggestions,
        "error": "；".join(problems),
    }


def _format_scan_summary(stats: dict) -> str:
    """把扫描统计压成一行可读摘要。

    完整统计已经作为结构化结果返回给 Web/TG，日志只保留人能一眼看懂的
    关键项：为零的计数不打印，异常项始终打印。
    """
    stats = stats if isinstance(stats, dict) else {}

    def count(key: str) -> int:
        try:
            return int(stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    groups = [item for item in (stats.get("source_groups") or []) if isinstance(item, dict)]
    scope = ""
    if len(groups) == 1:
        scope = str(groups[0].get("name") or groups[0].get("path") or "")
    elif groups:
        scope = f"{len(groups)} 个媒体目录"

    parts = [f"共 {count('total')} 个视频"]
    for label, key in (
        ("已识别", "matched"), ("待确认", "need_confirm"), ("跳过", "skipped"),
        ("冲突", "conflict"), ("失败", "failed"), ("已移动", "moved"),
        ("伴随文件", "metadata_moved"), ("特别篇", "specials_auto_mapped"),
    ):
        value = count(key)
        if value:
            parts.append(f"{label} {value}")
    if count("stopped"):
        parts.append("已停止")
    if stats.get("protected_staging_dirs"):
        parts.append(f"受保护下载目录 {len(stats['protected_staging_dirs'])} 个未扫描")
    if not stats.get("scan_complete", True) or count("scan_limited"):
        kind = stats.get("scan_limit_kind") or (
            "下载目录受保护" if stats.get("protected_staging_dirs") else "未知原因"
        )
        parts.append(f"扫描不完整({kind})")
    errors = stats.get("scan_errors") or []
    if errors:
        parts.append(f"扫描错误 {len(errors)}")

    timing = " ".join(
        f"{label}={float(stats.get(key) or 0.0):.2f}s"
        for label, key in (
            ("扫描", "scan_elapsed_seconds"),
            ("识别", "recognition_elapsed_seconds"),
            ("探测", "media_probe_elapsed_seconds"),
            ("冲突", "conflict_check_elapsed_seconds"),
        )
        if float(stats.get(key) or 0.0) > 0
    )
    summary = f"[{scope}] " if scope else ""
    summary += " · ".join(parts)
    return f"{summary} | {timing}" if timing else summary


def _format_phase_timing(stats: dict) -> str:
    """输出阶段耗时与外部请求量；为零的诊断项不打印。"""
    stats = stats if isinstance(stats, dict) else {}
    chunks = []
    for label, key in (
        ("scan", "scan_elapsed_seconds"),
        ("recognition", "recognition_elapsed_seconds"),
        ("probe", "media_probe_elapsed_seconds"),
        ("conflict", "conflict_check_elapsed_seconds"),
        ("execute", "execute_elapsed_seconds"),
        ("cleanup", "cleanup_elapsed_seconds"),
        ("plan_wall", "parallel_planning_elapsed_seconds"),
        ("writer_wait", "writer_wait_elapsed_seconds"),
        ("target_version", "target_revision_check_elapsed_seconds"),
        ("target_list", "target_inventory_refresh_elapsed_seconds"),
    ):
        value = float(stats.get(key) or 0.0)
        if value > 0:
            chunks.append(f"{label}={value:.2f}s")
    chunks.append(f"total={float(stats.get('total_elapsed_seconds') or 0.0):.2f}s")

    for label, key in (
        ("tmdb", "tmdb_search_requests"),
        ("tmdb_cache", "tmdb_search_cache_hits"),
        ("ai", "ai_requests"),
        ("list_dir", "scan_list_dir_calls"),
        ("probe_cache", "media_probe_cache_hits"),
        ("probe_online", "media_probe_online_profiles"),
        ("probe_timeouts", "media_probe_timeouts"),
        ("target_refresh", "target_dir_refreshes"),
        ("target_cache", "target_inventory_cache_hits"),
        ("target_version_miss", "target_revision_mismatches"),
        ("recognition_task_cache", "task_recognition_cache_hits"),
        ("recognition_task_bind", "task_recognition_cache_bindings"),
    ):
        try:
            value = int(stats.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            chunks.append(f"{label}={value}")
    return " ".join(chunks)


def organize_status(run_id: int | None = None, *, expected_task_id: str | None = None) -> dict:
    """读取整理概览或精确持久运行；确认回读可额外核对原任务身份。"""
    import json

    from app import database as db
    from app.modules.organize_tasks import get_organize_manager

    if run_id is None:
        overview = get_organize_manager().status()
        return project_organize_status(overview, overview=overview)
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("run_id 必须是正整数")
    raw = None
    overview: dict[str, Any] = {}
    row = db.get_task_run(run_id)
    if row is not None and str(row["task_name"]) == "guangya_organize":
        try:
            payload = json.loads(str(row["result"] or "{}"))
        except ValueError:
            payload = {}
        result = read_organize_result(payload)
        status = {
            "queued": "queued",
            "running": "running",
            "success": "completed",
            "partial": "partial",
            "skipped": "stopped",
            "failed": "failed",
        }.get(str(row["status"]))
        if (expected_task_id is None or result["task_id"] == expected_task_id) and status:
            raw = {
                "status": status,
                "result": result,
                "trigger_type": row["trigger_type"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
    if raw is None:
        return {
            "ok": False,
            "status": "unknown",
            "summary": "光鸭整理任务状态暂时无法确认",
            "error": "未找到对应的后台任务快照。",
        }
    return project_organize_status(raw, overview=overview)


def observe_organize_task(completion: dict[str, Any]) -> dict:
    """回执绑定原运行及任务；停止、回退、纠正均读取原任务，不替换为当前运行。"""
    run_id = completion.get("run_id")
    task_id = str(completion.get("task_id") or "").strip()
    operation = str(completion.get("operation") or "")
    if operation in {"run", "stop"} and task_id and type(run_id) is int and run_id > 0:
        return organize_status(run_id, expected_task_id=task_id)
    raw = None
    overview: dict[str, Any] = {}
    if operation in {"stop", "undo", "reorganize"} and task_id:
        from app.modules.organize_tasks import get_organize_manager

        manager = get_organize_manager()
        raw = manager.task_result(task_id)
        overview = manager.status()
    if raw is None:
        return {
            "ok": False,
            "status": "unknown",
            "summary": "光鸭整理任务状态暂时无法确认",
            "error": "未找到对应的后台任务快照。",
        }
    return project_organize_status(raw, overview=overview)
