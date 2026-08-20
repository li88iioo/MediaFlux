"""本地媒体来源触发器的安全摘要与精确启停动作。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Mapping

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.local_media_scheduler import get_local_media_scheduler

logger = get_logger(__name__)

_TRIGGER_FIELDS = {
    "qb_completed": ("enabled", "qB 下载完成自动接管"),
    "scan": ("scan_enabled", "目录自动扫描"),
}
_MAX_SOURCE_NUMBER = 10_000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_source_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentToolError("source_number 必须是正整数")
    if value < 1 or value > _MAX_SOURCE_NUMBER:
        raise AgentToolError(f"source_number 必须在 1 到 {_MAX_SOURCE_NUMBER} 之间")
    return value


def local_media_source_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("local_media.source_summaries 不接受参数")
    return {}


def local_media_source_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"source_number"}:
        raise AgentToolError("local_media.get_source_summary 只接受 source_number 参数")
    return {"source_number": _strict_source_number(arguments.get("source_number"))}


def local_media_source_trigger_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"source_number", "trigger", "enabled"}:
        raise AgentToolError(
            "local_media.set_source_trigger_enabled 只接受 source_number、trigger 和 enabled 参数"
        )
    trigger = str(arguments.get("trigger") or "").strip().lower()
    if trigger not in _TRIGGER_FIELDS:
        raise AgentToolError("trigger 只支持 qb_completed 或 scan")
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {
        "source_number": _strict_source_number(arguments.get("source_number")),
        "trigger": trigger,
        "enabled": enabled,
    }


def _row_dict(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    try:
        keys = tuple(row.keys())
    except (AttributeError, TypeError):
        return {}
    return {str(key): row[key] for key in keys}


def _source_rows(conn: Any) -> list[Any]:
    return conn.execute(
        "SELECT * FROM local_media_sources WHERE owner=? ORDER BY id ASC",
        ("admin",),
    ).fetchall()


def _source_row(conn: Any, source_number: int) -> Any:
    rows = _source_rows(conn)
    index = source_number - 1
    if index < 0 or index >= len(rows):
        raise AgentToolError("指定的本地媒体来源不存在", code="precondition_failed")
    return rows[index]


def _target_rows(conn: Any, source_id: int) -> list[Any]:
    return conn.execute(
        "SELECT * FROM local_library_targets WHERE source_id=? AND owner=? "
        "ORDER BY category ASC,id ASC",
        (int(source_id), "admin"),
    ).fetchall()


def _public_summary(conn: Any, row: Any, source_number: int) -> dict[str, Any]:
    targets = _target_rows(conn, int(row["id"]))
    categories = sorted({str(item["category"] or "default") for item in targets})
    mode = str(row["mode"] or "move")
    qb_enabled = bool(row["enabled"])
    scan_enabled = bool(row["scan_enabled"])
    return {
        "source_number": source_number,
        "enabled": qb_enabled,
        "scan_enabled": scan_enabled,
        "scan_effective": scan_enabled and mode != "preview_only",
        "mode": mode,
        "media_type": str(row["media_type"] or "auto"),
        "stable_seconds": max(0, int(row["stable_seconds"] or 0)),
        "scan_interval_minutes": max(1, int(row["scan_interval_minutes"] or 1)),
        "target_count": len(targets),
        "target_categories": categories,
    }


def _snapshot(conn: Any, source_number: int) -> dict[str, Any]:
    row = _source_row(conn, source_number)
    targets = _target_rows(conn, int(row["id"]))
    return {
        "source_number": source_number,
        "source": _row_dict(row),
        "targets": [_row_dict(item) for item in targets],
    }


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def list_local_media_source_summaries(_arguments: dict[str, Any]) -> ToolResult:
    with db.get_conn() as conn:
        rows = _source_rows(conn)
        sources = [
            _public_summary(conn, row, index)
            for index, row in enumerate(rows, start=1)
        ]
    enabled_count = sum(int(item["enabled"]) for item in sources)
    scan_count = sum(int(item["scan_effective"]) for item in sources)
    return ToolResult(
        ok=True,
        status="healthy" if sources else "not_configured",
        summary=(
            f"共有 {len(sources)} 个本地媒体来源，{enabled_count} 个接管 qB 完成任务，"
            f"{scan_count} 个执行目录扫描"
            if sources else "尚未配置本地媒体来源"
        ),
        data={
            "total": len(sources),
            "enabled_count": enabled_count,
            "scan_enabled_count": scan_count,
            "sources": sources,
        },
        evidence=[Evidence(
            source="local_media_configuration",
            description="仅返回公开序号、触发状态、工作模式、媒体类型和目标分类统计；未返回来源名称、路径、下载器配置、媒体库标识或凭据。",
            collected_at=_now(),
        )],
        suggestions=(
            ["可按来源编号查看详情，或精确启停 qB 自动接管与目录扫描。"]
            if sources else ["请先在本地媒体页面添加来源。"]
        ),
    )


def get_local_media_source_summary(arguments: dict[str, Any]) -> ToolResult:
    source_number = int(arguments["source_number"])
    with db.get_conn() as conn:
        row = _source_row(conn, source_number)
        source = _public_summary(conn, row, source_number)
    return ToolResult(
        ok=True,
        status="healthy" if source["target_count"] else "attention",
        summary=f"本地媒体来源 {source_number} 的触发状态已读取",
        data=source,
        evidence=[Evidence(
            source="local_media_configuration",
            description="仅读取指定公开序号的安全摘要；未返回名称、路径、下载器配置、媒体库标识或凭据。",
            collected_at=_now(),
        )],
        suggestions=(
            [] if source["target_count"] else ["该来源尚未配置媒体库目标，启用触发器前建议先完成目标映射。"]
        ),
    )


def prepare_set_local_media_source_trigger_enabled(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    normalized = local_media_source_trigger_arguments(arguments)
    source_number = int(normalized["source_number"])
    trigger = str(normalized["trigger"])
    requested = bool(normalized["enabled"])
    field, label = _TRIGGER_FIELDS[trigger]
    with db.get_conn() as conn:
        snapshot = _snapshot(conn, source_number)
        current = bool(snapshot["source"].get(field))
        public = _public_summary(conn, _source_row(conn, source_number), source_number)
    if current == requested:
        raise AgentToolError("该触发方式已经处于目标状态", code="precondition_failed")

    operation = "启用" if requested else "停用"
    effects = [
        f"只会{operation}本地媒体来源 {source_number} 的{label}。",
        "不会修改来源目录、目标目录、整理规则、媒体服务器绑定或下载器凭据。",
        "已开始的文件操作不会被强制中断。",
    ]
    if not requested:
        effects.append("尚在等待执行且依赖该触发方式的任务可能被标记为失败。")
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{operation}本地媒体来源 {source_number} 的{label}",
        data={
            "operation": "enable" if requested else "disable",
            "source_number": source_number,
            "trigger": trigger,
            "current_enabled": current,
            "requested_enabled": requested,
            "target_count": public["target_count"],
            "affected": 1,
            "effects": effects,
        },
        evidence=[Evidence(
            source="local_media_configuration",
            description="仅核对公开来源序号、目标触发状态和服务端私有配置快照；未返回目录、下载器配置或凭据。",
            collected_at=_now(),
        )],
        suggestions=["该操作可通过相反的启停命令恢复。"],
    ), _fingerprint(snapshot)


def set_local_media_source_trigger_enabled(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("本地媒体来源触发器启停需要确认", code="confirmation_required")


def set_local_media_source_trigger_enabled_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = local_media_source_trigger_arguments(arguments)
    source_number = int(normalized["source_number"])
    trigger = str(normalized["trigger"])
    requested = bool(normalized["enabled"])
    field, label = _TRIGGER_FIELDS[trigger]

    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            snapshot = _snapshot(conn, source_number)
        except AgentToolError:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="本地媒体来源列表已经变化，请重新检查",
                error="确认快照已失效。",
            )
        if not secrets.compare_digest(
            _fingerprint(snapshot), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="本地媒体来源配置已经变化，请重新预检",
                error="确认快照已失效。",
            )
        current = bool(snapshot["source"].get(field))
        if current == requested:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="本地媒体来源触发状态已经变化，请重新预检",
                error="确认快照已失效。",
            )
        cursor = conn.execute(
            f"UPDATE local_media_sources SET {field}=?,updated_at=? WHERE id=? AND owner=?",
            (1 if requested else 0, db.now(), int(snapshot["source"]["id"]), "admin"),
        )
        if cursor.rowcount != 1:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="本地媒体来源已经不存在，请重新检查",
                error="确认快照已失效。",
            )

    runtime_refreshed = True
    try:
        get_local_media_scheduler().reload()
    except Exception as exc:  # pragma: no cover - best effort runtime wake-up
        runtime_refreshed = False
        logger.warning("Agent 刷新本地媒体调度器失败 type=%s", type(exc).__name__)

    operation = "启用" if requested else "停用"
    suggestions = ["如需恢复，可对同一来源和触发方式执行相反的启停操作。"]
    if not runtime_refreshed:
        suggestions.append("设置已保存，但当前进程调度器未能立即唤醒；请稍后检查运行状态。")
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"本地媒体来源 {source_number} 的{label}已{operation}",
        data={
            "operation": "enable" if requested else "disable",
            "source_number": source_number,
            "trigger": trigger,
            "enabled": requested,
            "affected": 1,
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[Evidence(
            source="local_media_configuration",
            description="只更新了指定来源的一项触发状态，并尝试唤醒本地媒体调度器；未修改目录、规则、目标或凭据。",
            collected_at=_now(),
        )],
        suggestions=suggestions,
    )
