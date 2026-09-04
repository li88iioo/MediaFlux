"""STRM 运行诊断、预检与确认执行的领域适配。"""

from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from typing import Any

from app import config
from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult

from .shared import _bounded_int, _now, _safe_choice, _safe_timestamp


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
