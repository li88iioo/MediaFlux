"""光鸭整理的只读预览与服务端确认后启动动作。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any

from app import config
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger
from app.modules.organize import OrganizeRules, Organizer, organize_rules_snapshot
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


def organize_confirmation_context(_arguments: dict[str, Any]) -> str:
    """绑定来源、规则与凭据世代；原始值不离开服务端。"""
    sources, rules, _error = _configured_inputs()
    client = GuangYaClient()
    try:
        return _serialize_organize_context(
            sources,
            rules,
            _credential_generation(client),
        )
    finally:
        close_guangya_client(client)


def _credential_generation(client: GuangYaClient) -> int:
    try:
        return max(0, int(getattr(client, "credential_generation", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _supports_atomic_empty_directory_delete(client: Any) -> bool:
    """兼容旧名称：判断 Provider 是否支持带复核的空目录回收站删除。"""
    explicit = getattr(client, "supports_guarded_empty_directory_delete", None)
    if explicit is None:
        explicit = getattr(client, "supports_atomic_empty_directory_delete", None)
    if explicit is not None:
        return bool(explicit)
    return callable(getattr(client, "delete_empty_directory", None))


def _serialize_organize_clean_empty_context(
    sources: list[dict[str, str]],
    credential_generation: int,
) -> str:
    encoded = json.dumps(
        {
            "credential_generation": max(0, int(credential_generation)),
            "sources": sources,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def organize_clean_empty_confirmation_context(_arguments: dict[str, Any]) -> str:
    """绑定整理来源与凭据世代；原始内容仅保存在服务端确认票据中。"""
    client = GuangYaClient()
    try:
        return _serialize_organize_clean_empty_context(
            _configured_sources(),
            _credential_generation(client),
        )
    finally:
        close_guangya_client(client)


def _task_running() -> bool:
    status = str(get_organize_manager().task_status().get("status") or "").strip().lower()
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
                    samples.append({
                        "title": title[:160],
                        "year": str(getattr(match, "year", "") or "")[:8],
                        "media_type": str(getattr(match, "media_type", "") or "")[:16],
                        "action": action if action in action_counts else "skip",
                    })
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
            evidence=[Evidence("guangya_organizer", "执行只读 dry-run；未进行云盘写入。", _now())],
            suggestions=["请检查光鸭连接后重新预览。"],
        )
    if for_confirmation and data["sampled_files"] == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前来源中没有可整理的媒体文件",
            data=data,
            error="没有发现需要启动整理任务的内容。",
            evidence=[Evidence("guangya_organizer", "执行只读 dry-run；未进行云盘写入。", _now())],
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
        evidence=[Evidence("guangya_organizer", "执行只读 dry-run；未移动、改名或删除云盘内容。", _now())],
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


def preview_guangya_organize_run_once(_arguments: dict[str, Any]) -> ToolResult:
    return _organize_preview(for_confirmation=True)


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


def _preview_guangya_organize_clean_empty_sources(
    sources: list[dict[str, str]],
    client: GuangYaClient | None = None,
) -> ToolResult:
    if not sources:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="未配置可清理的光鸭整理来源",
            error="请先在网盘整理页面配置来源目录。",
        )
    if _task_running():
        return ToolResult(
            ok=False,
            status="conflict",
            summary="光鸭整理任务正在运行",
            error="请等待当前整理任务结束后再清理空目录。",
            suggestions=["可询问：查看光鸭整理进度。"],
        )
    owned_client = client is None
    client = client or GuangYaClient()
    try:
        if not client.logged_in:
            return ToolResult(
                ok=False,
                status="not_configured",
                summary="光鸭账号尚未连接",
                error="请先连接光鸭账号。",
            )
        if not _supports_atomic_empty_directory_delete(client):
            return ToolResult(
                ok=False,
                status="unsupported",
                summary="当前光鸭 Provider 暂不支持安全清理空目录",
                error="Provider 未提供带版本与空目录复核的回收站删除能力，系统已拒绝执行。",
                suggestions=["可先运行整理预览；待 Provider 支持条件删除后再清理空目录。"],
            )
        return ToolResult(
            ok=True,
            status="preview",
            summary="确认后将清理全部已配置整理来源中的空目录",
            data={"source_count": len(sources)},
            evidence=[Evidence(
                "guangya_organizer",
                "仅通过 Provider 的原子条件删除处理版本匹配的空子目录；来源根目录受保护，操作进入光鸭回收站。",
                _now(),
            )],
            suggestions=["确认前请核对当前整理来源配置，并避免同时从其他客户端向来源目录写入内容。"],
        )
    finally:
        if owned_client:
            close_guangya_client(client)


def prepare_guangya_organize_clean_empty(
    _arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """从同一来源快照生成预检与确认上下文。"""
    sources = _configured_sources()
    client = GuangYaClient()
    try:
        return (
            _preview_guangya_organize_clean_empty_sources(sources, client),
            _serialize_organize_clean_empty_context(
                sources,
                _credential_generation(client),
            ),
        )
    finally:
        close_guangya_client(client)


def preview_guangya_organize_clean_empty(_arguments: dict[str, Any]) -> ToolResult:
    """预检空目录清理；不扫描或返回目录内容。"""
    return _preview_guangya_organize_clean_empty_sources(_configured_sources())


def _clean_empty_guangya_organize_sources(
    sources: list[dict[str, str]],
    *,
    expected_credential_generation: int | None = None,
    client: GuangYaClient | None = None,
) -> ToolResult:
    if not sources:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="未配置可清理的光鸭整理来源",
            error="请先在网盘整理页面配置来源目录。",
        )
    owned_client = client is None
    client = client or GuangYaClient()
    try:
        if not client.logged_in:
            return ToolResult(
                ok=False,
                status="not_configured",
                summary="光鸭账号尚未连接",
                error="请先连接光鸭账号。",
            )

        if not _supports_atomic_empty_directory_delete(client):
            return ToolResult(
                ok=False,
                status="unsupported",
                summary="当前光鸭 Provider 暂不支持安全清理空目录",
                error="Provider 未提供带版本与空目录复核的回收站删除能力，系统已拒绝执行。",
            )

        if (
            expected_credential_generation is not None
            and _credential_generation(client) != expected_credential_generation
        ):
            raise AgentToolError("光鸭登录凭据已变化，请重新预检", code="confirmation_stale")

        result = get_organize_manager().clean_empty(sources, client=client)
        if not result.get("ok"):
            error = str(result.get("error") or "").strip()
            conflict = error in {
                "网盘整理任务正在运行",
                "服务正在停止，暂不接受新的整理操作",
            }
            return ToolResult(
                ok=False,
                status="conflict" if conflict else "unavailable",
                summary="光鸭空目录清理未执行",
                error=(
                    "已有整理任务运行中，请稍后重试。"
                    if conflict
                    else "暂时无法清理空目录，请稍后重试。"
                ),
                suggestions=["可询问：查看光鸭整理进度。"] if conflict else [],
            )

        cleaned = _bounded_int(result.get("cleaned"))
        failures = _bounded_int(result.get("scan_failures")) + _bounded_int(
            result.get("delete_failures")
        )
        partial = bool(result.get("partial")) or failures > 0
        return ToolResult(
            ok=True,
            status="partial" if partial else "completed",
            summary=(
                f"光鸭空目录清理部分完成：清理 {cleaned} 个目录，{failures} 项未完成"
                if partial
                else f"光鸭空目录清理完成，共清理 {cleaned} 个目录"
            ),
            data={"cleaned": cleaned, "source_count": len(sources), "failed": failures},
            evidence=[Evidence(
                "guangya_organizer",
                "已清理空子目录；结果仅保留汇总计数，不返回目录标识、名称或路径。",
                _now(),
            )],
            suggestions=(
                ["部分目录扫描或删除失败，请检查光鸭连接与权限后重新预检。"]
                if partial
                else ["可继续运行整理预览，确认来源中是否有待整理媒体。"]
            ),
        )
    finally:
        if owned_client:
            close_guangya_client(client)


def clean_empty_guangya_organize_sources(_arguments: dict[str, Any]) -> ToolResult:
    """确认执行回退路径：使用调用时的来源快照。"""
    return _clean_empty_guangya_organize_sources(_configured_sources())


def clean_empty_guangya_organize_sources_confirmed(
    _arguments: dict[str, Any],
    expected_context: str,
) -> ToolResult:
    """重新读取一次当前快照并校验指纹；票据中不保存来源路径原文。"""
    sources = _configured_sources()
    client = GuangYaClient()
    try:
        credential_generation = _credential_generation(client)
        current_context = _serialize_organize_clean_empty_context(
            sources, credential_generation
        )
        if not secrets.compare_digest(current_context, str(expected_context or "")):
            raise AgentToolError("整理来源配置已变化，请重新预检", code="confirmation_stale")
        return _clean_empty_guangya_organize_sources(
            sources,
            expected_credential_generation=credential_generation,
            client=client,
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


def organize_stop_confirmation_context(_arguments: dict[str, Any]) -> str:
    """返回仅保存在服务端确认票据中的任务快照。"""
    return _serialize_organize_stop_context(_organize_stop_context_payload())


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
        evidence=[Evidence(
            "guangya_organizer",
            "停止为协作式操作：当前文件操作可能先完成，已完成的云盘写入不会回滚。",
            _now(),
        )],
        suggestions=["确认后任务会在安全边界停止，不会撤销已完成的移动或改名。"],
    )


def prepare_guangya_organize_stop(
    _arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """用同一份任务快照生成预检结果和确认上下文。"""
    task = _organize_stop_context_payload()
    return _preview_guangya_organize_stop_task(task), _serialize_organize_stop_context(task)


def preview_guangya_organize_stop(_arguments: dict[str, Any]) -> ToolResult:
    return _preview_guangya_organize_stop_task(_organize_stop_context_payload())


def _parse_organize_stop_context(expected_context: str) -> dict[str, Any]:
    try:
        task = json.loads(str(expected_context or ""))
    except (TypeError, ValueError) as exc:
        raise AgentToolError(
            "整理任务状态已变化，请重新预检",
            code="confirmation_stale",
        ) from exc
    if not isinstance(task, dict) or set(task) != {
        "task_id", "status", "stoppable", "started_at",
    }:
        raise AgentToolError("整理任务状态已变化，请重新预检", code="confirmation_stale")
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
        raise AgentToolError("整理任务状态已变化，请重新预检", code="confirmation_stale")

    task_id = str(expected_task.get("task_id") or "")
    if (
        expected_task.get("status") != "running"
        or expected_task.get("stoppable") is not True
        or not task_id
    ):
        raise AgentToolError("整理任务状态已变化，请重新预检", code="confirmation_stale")

    result = get_organize_manager().stop(
        expected_task_id=task_id,
        require_running=True,
    )
    if not result.get("ok"):
        raise AgentToolError("整理任务状态已变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        ok=True,
        status="accepted",
        summary="光鸭整理停止请求已提交",
        data={"accepted": True},
        evidence=[Evidence(
            "guangya_organizer",
            "已请求任务在当前文件操作完成后安全停止；未返回任务、目录或文件标识。",
            _now(),
        )],
        suggestions=["可询问：查看光鸭整理进度。"],
    )


def stop_guangya_organize(_arguments: dict[str, Any]) -> ToolResult:
    """仅供确认执行回退路径使用；调用时仍按当前任务上下文做原子校验。"""
    context = organize_stop_confirmation_context({})
    return stop_guangya_organize_confirmed({}, context)

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
            evidence=[Evidence("guangya_organizer", "已提交后台整理；未返回目录或任务标识。", _now())],
            suggestions=["可询问：查看光鸭整理进度。"],
        )
    finally:
        if not transferred:
            close_guangya_client(client)


def run_guangya_organize_once(_arguments: dict[str, Any]) -> ToolResult:
    """确认执行回退路径：按调用时配置和凭据快照启动。"""
    sources, rules, config_error = _configured_inputs()
    return _run_guangya_organize_once(
        sources,
        rules,
        config_error,
        GuangYaClient(),
    )


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
            raise AgentToolError("光鸭整理配置或登录凭据已变化，请重新预检", code="confirmation_stale")
    except Exception:
        close_guangya_client(client)
        raise
    return _run_guangya_organize_once(sources, rules, config_error, client)
