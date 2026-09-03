"""光鸭整理的只读预览与服务端确认后启动动作。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger
from app.modules.organize import Organizer, OrganizeRules, organize_rules_snapshot
from app.modules.organize_sources import normalize_organize_sources
from app.modules.organize_tasks import get_organize_manager

logger = get_logger(__name__)

_PREVIEW_FILE_LIMIT = 100
_PREVIEW_SAMPLE_LIMIT = 8
_PREVIEW_STATS_KEYS = (
    "total",
    "matched",
    "need_confirm",
    "skipped",
    "conflict",
    "failed",
    "subtitle_skipped",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _bounded_int(value: Any, maximum: int = 1_000_000) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def _configured_sources() -> list[dict[str, str]]:
    """读取已保存的多源配置，仅接受正式多源字段。"""
    sources, error = normalize_organize_sources(
        config.get("GY_ORGANIZE_SOURCE_DIRS", ""),
    )
    if error:
        raise AgentToolError(
            "光鸭整理来源配置无效，请先在网盘整理页面修正。",
            code="invalid_configuration",
        )
    return sources


def _configured_inputs() -> tuple[list[dict[str, str]], OrganizeRules, str]:
    sources = _configured_sources()
    target = str(config.get("GY_ORGANIZE_TARGET_DIR", "0") or "0").strip()
    rules = OrganizeRules.from_config(target_dir_id=target)
    if not sources:
        return sources, rules, "未配置光鸭整理源目录"
    if not target or target == "0":
        return sources, rules, "未配置光鸭整理目标目录"
    return sources, rules, ""


def _serialize_organize_context(
    sources: list[dict[str, str]],
    rules: OrganizeRules,
    credential_generation: int,
) -> str:
    payload = {
        "credential_generation": max(0, int(credential_generation)),
        "sources": sources,
        "rules": organize_rules_snapshot(rules),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_generation(client: GuangYaClient) -> int:
    try:
        return max(0, int(getattr(client, "credential_generation", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _task_running() -> bool:
    status = (
        str(get_organize_manager().task_status().get("status") or "").strip().lower()
    )
    return status in {"running", "stopping"}


def _preview_data(
    sources: list[dict[str, str]],
    rules: OrganizeRules,
    client: GuangYaClient,
) -> tuple[dict[str, Any], int]:
    aggregate = {key: 0 for key in _PREVIEW_STATS_KEYS}
    action_counts = {"move": 0, "skip": 0, "conflict": 0}
    samples: list[dict[str, Any]] = []
    remaining = _PREVIEW_FILE_LIMIT
    scan_error_count = 0
    organizer = Organizer(client=client)
    protected_source_ids = {
        str(source.get("id") or "").strip()
        for source in sources
        if str(source.get("id") or "").strip()
    }

    try:
        for source in sources:
            if remaining <= 0:
                break
            source_rules = rules.for_source(str(source.get("id") or ""))
            plans, stats = organizer.organize(
                source["id"],
                source_rules,
                dry_run=True,
                max_files=remaining,
                post_actions=False,
                protected_source_ids=protected_source_ids,
            )
            for key in _PREVIEW_STATS_KEYS:
                aggregate[key] += _bounded_int(stats.get(key))
            errors = stats.get("scan_errors")
            if isinstance(errors, list):
                scan_error_count += min(len(errors), _PREVIEW_FILE_LIMIT)
            remaining -= _bounded_int(stats.get("total"), _PREVIEW_FILE_LIMIT)

            for plan in plans:
                action = str(getattr(plan, "action", "") or "").strip().lower()
                if action in action_counts:
                    action_counts[action] += 1
                match = getattr(plan, "match", None)
                title = str(getattr(match, "title", "") or "").strip()
                if title and len(samples) < _PREVIEW_SAMPLE_LIMIT:
                    samples.append(
                        {
                            "title": title[:160],
                            "year": str(getattr(match, "year", "") or "")[:8],
                            "media_type": str(getattr(match, "media_type", "") or "")[
                                :16
                            ],
                            "action": action if action in action_counts else "skip",
                        }
                    )
    finally:
        organizer.close()

    data = {
        "source_count": len(sources),
        "sample_limit": _PREVIEW_FILE_LIMIT,
        "sampled_files": aggregate["total"],
        "truncated": remaining <= 0,
        "stats": aggregate,
        "actions": action_counts,
        "samples": samples,
        "effects": {
            "scope": "all_configured_sources",
            "moves_files": True,
            "renames_files": bool(rules.rename_enabled),
            "may_clean_empty_directories": bool(rules.clean_empty),
            "may_recycle_replaced_files": bool(rules.recycle_replaced_enabled),
            "triggers_strm_after_moves": bool(rules.link_strm),
        },
    }
    return data, scan_error_count


def _organize_preview_snapshot(
    sources: list[dict[str, str]],
    rules: OrganizeRules,
    config_error: str,
    client: GuangYaClient,
    *,
    for_confirmation: bool,
) -> ToolResult:
    if config_error:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="光鸭整理配置尚不完整",
            error=config_error,
            suggestions=["请先在网盘整理页面配置来源目录和目标目录。"],
        )
    if _task_running():
        return ToolResult(
            ok=False,
            status="conflict",
            summary="光鸭整理任务已在运行",
            error="请等待当前整理任务结束后再试。",
            suggestions=["可询问：查看光鸭整理进度。"],
        )

    if not client.logged_in:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="光鸭账号尚未连接",
            error="请先连接光鸭账号。",
        )
    try:
        data, scan_error_count = _preview_data(sources, rules, client)
    except Exception as exc:
        logger.warning("Agent 光鸭整理预览失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="光鸭整理预览暂时不可用",
            error="无法完成只读预览，请稍后重试。",
        )

    if scan_error_count:
        data["scan_errors"] = scan_error_count
        return ToolResult(
            ok=False,
            status="inconclusive",
            summary="光鸭整理预览扫描不完整",
            data=data,
            error="部分目录无法读取，本次不会创建执行确认。",
            evidence=[
                Evidence(
                    "guangya_organizer", "执行只读 dry-run；未进行云盘写入。", _now()
                )
            ],
            suggestions=["请检查光鸭连接后重新预览。"],
        )
    if for_confirmation and data["sampled_files"] == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前来源中没有可整理的媒体文件",
            data=data,
            error="没有发现需要启动整理任务的内容。",
            evidence=[
                Evidence(
                    "guangya_organizer", "执行只读 dry-run；未进行云盘写入。", _now()
                )
            ],
        )

    summary = (
        "确认后将按当前配置整理全部光鸭来源"
        if for_confirmation
        else "已生成光鸭整理预览；尚未移动、改名或删除任何云盘内容"
    )
    suggestions = (
        ["确认前请核对移动、改名、清理空目录和 STRM 联动影响。"]
        if for_confirmation
        else ["如果预览范围正确，可以继续发起实际整理并确认。"]
    )
    return ToolResult(
        ok=True,
        status="preview",
        summary=summary,
        data=data,
        evidence=[
            Evidence(
                "guangya_organizer",
                "执行只读 dry-run；未移动、改名或删除云盘内容。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def _organize_preview(*, for_confirmation: bool) -> ToolResult:
    sources, rules, config_error = _configured_inputs()
    client = GuangYaClient()
    try:
        return _organize_preview_snapshot(
            sources,
            rules,
            config_error,
            client,
            for_confirmation=for_confirmation,
        )
    finally:
        close_guangya_client(client)


def preview_guangya_organize(_arguments: dict[str, Any]) -> ToolResult:
    return _organize_preview(for_confirmation=False)


def prepare_guangya_organize_run_once(
    _arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """用同一配置与凭据世代快照生成预检结果和确认上下文。"""
    sources, rules, config_error = _configured_inputs()
    client = GuangYaClient()
    try:
        return (
            _organize_preview_snapshot(
                sources,
                rules,
                config_error,
                client,
                for_confirmation=True,
            ),
            _serialize_organize_context(
                sources,
                rules,
                _credential_generation(client),
            ),
        )
    finally:
        close_guangya_client(client)


def _organize_stop_context_payload() -> dict[str, Any]:
    task = get_organize_manager().task_status()
    return {
        "task_id": str(task.get("id") or ""),
        "status": str(task.get("status") or "").strip().lower(),
        "stoppable": task.get("stoppable") is True,
        "started_at": str(task.get("started_at") or ""),
    }


def _serialize_organize_stop_context(task: dict[str, Any]) -> str:
    return json.dumps(
        task,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _preview_guangya_organize_stop_task(task: dict[str, Any]) -> ToolResult:
    if task["status"] != "running":
        return ToolResult(
            ok=False,
            status="conflict",
            summary="当前没有可停止的光鸭整理任务",
            error="只有正在运行的整理任务可以申请停止。",
            suggestions=["可询问：查看光鸭整理进度。"],
        )
    if not task["stoppable"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="当前整理阶段暂时不可停止",
            error="当前文件操作完成后可重新申请停止。",
            suggestions=["稍后再次查看整理进度。"],
        )
    return ToolResult(
        ok=True,
        status="preview",
        summary="确认后将安全停止当前光鸭整理任务",
        data={"requested": True, "cooperative": True},
        evidence=[
            Evidence(
                "guangya_organizer",
                "停止为协作式操作：当前文件操作可能先完成，已完成的云盘写入不会回滚。",
                _now(),
            )
        ],
        suggestions=["确认后任务会在安全边界停止，不会撤销已完成的移动或改名。"],
    )


def prepare_guangya_organize_stop(
    _arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """用同一份任务快照生成预检结果和确认上下文。"""
    task = _organize_stop_context_payload()
    return _preview_guangya_organize_stop_task(task), _serialize_organize_stop_context(
        task
    )


def _parse_organize_stop_context(expected_context: str) -> dict[str, Any]:
    try:
        task = json.loads(str(expected_context or ""))
    except (TypeError, ValueError) as exc:
        raise AgentToolError(
            "整理任务状态已变化，请重新预检",
            code="confirmation_stale",
        ) from exc
    if not isinstance(task, dict) or set(task) != {
        "task_id",
        "status",
        "stoppable",
        "started_at",
    }:
        raise AgentToolError(
            "整理任务状态已变化，请重新预检", code="confirmation_stale"
        )
    return task


def stop_guangya_organize_confirmed(
    _arguments: dict[str, Any],
    expected_context: str,
) -> ToolResult:
    expected_task = _parse_organize_stop_context(expected_context)
    current_task = _organize_stop_context_payload()
    if not secrets.compare_digest(
        _serialize_organize_stop_context(current_task),
        _serialize_organize_stop_context(expected_task),
    ):
        raise AgentToolError(
            "整理任务状态已变化，请重新预检", code="confirmation_stale"
        )

    task_id = str(expected_task.get("task_id") or "")
    if (
        expected_task.get("status") != "running"
        or expected_task.get("stoppable") is not True
        or not task_id
    ):
        raise AgentToolError(
            "整理任务状态已变化，请重新预检", code="confirmation_stale"
        )

    result = get_organize_manager().stop(
        expected_task_id=task_id,
        require_running=True,
    )
    if not result.get("ok"):
        raise AgentToolError(
            "整理任务状态已变化，请重新预检", code="confirmation_stale"
        )
    return ToolResult(
        ok=True,
        status="accepted",
        summary="光鸭整理停止请求已提交",
        data={"accepted": True},
        evidence=[
            Evidence(
                "guangya_organizer",
                "已请求任务在当前文件操作完成后安全停止；未返回任务、目录或文件标识。",
                _now(),
            )
        ],
        suggestions=["可询问：查看光鸭整理进度。"],
    )


def _run_guangya_organize_once(
    sources: list[dict[str, str]],
    rules: OrganizeRules,
    config_error: str,
    client: GuangYaClient,
) -> ToolResult:
    transferred = False
    try:
        if config_error:
            return ToolResult(
                ok=False,
                status="not_configured",
                summary="光鸭整理配置尚不完整",
                error=config_error,
            )
        if not client.logged_in:
            return ToolResult(
                ok=False,
                status="not_configured",
                summary="光鸭账号尚未连接",
                error="请先连接光鸭账号。",
            )
        result = get_organize_manager().start(
            sources,
            rules,
            trigger_type="manual",
            client=client,
            expected_credential_generation=_credential_generation(client),
            take_client_ownership=True,
        )
        if not result.get("ok"):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="光鸭整理任务未启动",
                error="已有整理任务运行中，请稍后重试。",
                suggestions=["可询问：查看光鸭整理进度。"],
            )
        transferred = True
        return ToolResult(
            ok=True,
            status="accepted",
            summary="光鸭整理任务已提交",
            data={"trigger_type": "manual", "source_count": len(sources)},
            evidence=[
                Evidence(
                    "guangya_organizer",
                    "已提交后台整理；未返回目录或任务标识。",
                    _now(),
                )
            ],
            suggestions=["可询问：查看光鸭整理进度。"],
        )
    finally:
        if not transferred:
            close_guangya_client(client)


def run_guangya_organize_once_confirmed(
    _arguments: dict[str, Any],
    expected_context: str,
) -> ToolResult:
    """确认执行前复核配置与凭据世代，拒绝跨账号复用旧票据。"""
    sources, rules, config_error = _configured_inputs()
    client = GuangYaClient()
    try:
        current_context = _serialize_organize_context(
            sources,
            rules,
            _credential_generation(client),
        )
        if not secrets.compare_digest(current_context, str(expected_context or "")):
            raise AgentToolError(
                "光鸭整理配置已变化或登录凭据已更新，请重新预检",
                code="confirmation_stale",
            )
    except Exception:
        close_guangya_client(client)
        raise
    return _run_guangya_organize_once(sources, rules, config_error, client)
