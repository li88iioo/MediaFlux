"""RSS 订阅的受控管理动作：精确 ID、原子预检快照、一次性确认。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Callable

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.rss import rss_subscription_refresh_revision

logger = get_logger(__name__)
_MAX_REFRESH_INTERVAL_MINUTES = 10_080
_MAX_SAFE_ID = 2_147_483_647


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_subscription_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_SAFE_ID
    ):
        raise AgentToolError("subscription_id 必须是正整数")
    return value


def rss_subscription_enabled_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id", "enabled"}:
        raise AgentToolError(
            "rss.set_subscription_enabled 只接受 subscription_id 和 enabled 参数"
        )
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id")),
        "enabled": enabled,
    }


def rss_refresh_interval_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id", "refresh_interval_minutes"}:
        raise AgentToolError(
            "rss.set_refresh_interval 只接受 subscription_id 和 refresh_interval_minutes 参数"
        )
    interval = arguments.get("refresh_interval_minutes")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval < 0
        or interval > _MAX_REFRESH_INTERVAL_MINUTES
    ):
        raise AgentToolError(
            f"refresh_interval_minutes 必须是 0 到 {_MAX_REFRESH_INTERVAL_MINUTES} 的整数"
        )
    return {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id")),
        "refresh_interval_minutes": interval,
    }


def rss_delete_subscription_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("rss.delete_subscription 只接受 subscription_id 参数")
    return {"subscription_id": _strict_subscription_id(arguments.get("subscription_id"))}


def _snapshot(row: Any, *, operation: str, entry_count: int = 0) -> dict[str, Any]:
    if row is None:
        return {
            "operation": operation,
            "exists": False,
            "subscription_id": 0,
            "revision": "missing",
            "enabled": False,
            "refresh_interval_minutes": 0,
            "entry_count": 0,
        }
    return {
        "operation": operation,
        "exists": True,
        "subscription_id": int(row["id"]),
        # revision 是敏感配置的不可逆摘要；确认响应永远不会返回它。
        "revision": rss_subscription_refresh_revision(row),
        "enabled": bool(row["enabled"]),
        "refresh_interval_minutes": max(0, int(row["refresh_interval_minutes"] or 0)),
        "entry_count": max(0, int(entry_count or 0)),
    }


def _capture(subscription_id: int, *, operation: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM rss_items WHERE id=?", (subscription_id,)).fetchone()
        count = 0
        if row is not None and operation == "delete":
            count_row = conn.execute(
                "SELECT COUNT(*) AS total FROM rss_entries WHERE rss_item_id=?",
                (subscription_id,),
            ).fetchone()
            count = int((count_row["total"] if count_row else 0) or 0)
        return _snapshot(row, operation=operation, entry_count=count)


def _fingerprint(state: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _missing() -> AgentToolError:
    return AgentToolError("未找到指定的 RSS 订阅", code="precondition_failed")


def _prepare_enabled(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    state = _capture(arguments["subscription_id"], operation="set_enabled")
    if not state["exists"]:
        raise _missing()
    requested = bool(arguments["enabled"])
    if state["enabled"] == requested:
        label = "启用" if requested else "停用"
        raise AgentToolError(f"该 RSS 订阅已经{label}", code="precondition_failed")
    label = "启用" if requested else "停用"
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{label} 1 个 RSS 订阅",
        data={
            "operation": "enable" if requested else "disable",
            "subscription_id": arguments["subscription_id"],
            "enabled": requested,
            "affected": 1,
            "effects": [
                "启用后允许该订阅参与后续定时刷新。"
                if requested
                else "停用后不会再安排新的定时刷新；已经开始的刷新不会被强制中断。"
            ],
        },
        evidence=[Evidence(
            "rss_database",
            "仅核对本地订阅状态；未访问订阅地址、未读取条目内容、未触发刷新或下载。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；订阅状态变化后需要重新预检。"],
    )
    return preview, _fingerprint(state)


def _prepare_interval(arguments: dict[str, int]) -> tuple[ToolResult, str]:
    state = _capture(arguments["subscription_id"], operation="set_interval")
    if not state["exists"]:
        raise _missing()
    requested = int(arguments["refresh_interval_minutes"])
    if state["refresh_interval_minutes"] == requested:
        raise AgentToolError("该 RSS 订阅已经使用这个刷新周期", code="precondition_failed")
    effect = (
        "自动刷新周期将关闭；仍可手动刷新订阅。"
        if requested == 0
        else f"后续自动刷新周期将调整为每 {requested} 分钟一次。"
    )
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将修改 1 个 RSS 订阅的自动刷新周期",
        data={
            "operation": "set_interval",
            "subscription_id": arguments["subscription_id"],
            "refresh_interval_minutes": requested,
            "affected": 1,
            "effects": [effect, "不会立即刷新订阅，也不会自动创建下载任务。"],
        },
        evidence=[Evidence(
            "rss_database",
            "仅核对本地订阅与当前周期；未访问订阅地址、未读取条目内容。",
            _now(),
        )],
        suggestions=["确认后调度器会重新读取配置；0 分钟表示关闭自动刷新。"],
    )
    return preview, _fingerprint(state)


def _prepare_delete(arguments: dict[str, int]) -> tuple[ToolResult, str]:
    state = _capture(arguments["subscription_id"], operation="delete")
    if not state["exists"]:
        raise _missing()
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将永久删除 1 个 RSS 订阅",
        data={
            "operation": "delete",
            "subscription_id": arguments["subscription_id"],
            "affected": 1,
            "deleted_entries": state["entry_count"],
            "effects": [
                "删除订阅配置及其本地条目记录。",
                "不会删除已经创建的下载任务或已下载文件。",
            ],
        },
        evidence=[Evidence(
            "rss_database",
            "仅统计本地关联条目数量；未返回订阅名称、地址、过滤词、条目标题或凭据。",
            _now(),
        )],
        suggestions=["删除不可撤销；如只想停止定时刷新，建议改为停用订阅。"],
    )
    return preview, _fingerprint(state)


def _reload_scheduler() -> bool:
    try:
        from app.modules.rss_scheduler import get_rss_scheduler

        get_rss_scheduler().reload()
        return True
    except Exception as exc:
        logger.warning("Agent RSS 配置已保存但调度器刷新失败 type=%s", type(exc).__name__)
        return False


def _confirmed_update(
    arguments: dict[str, Any],
    expected_context: str,
    *,
    operation: str,
) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM rss_items WHERE id=?", (subscription_id,)).fetchone()
        state = _snapshot(row, operation=operation)
        if not state["exists"] or not secrets.compare_digest(
            _fingerprint(state), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="RSS 订阅状态已变化，请重新预检",
                error="确认快照已失效。",
            )
        if operation == "set_enabled":
            requested = bool(arguments["enabled"])
            conn.execute(
                "UPDATE rss_items SET enabled=?, updated_at=? WHERE id=?",
                (1 if requested else 0, db.now(), subscription_id),
            )
            operation_label = "enable" if requested else "disable"
            summary = "RSS 订阅已启用" if requested else "RSS 订阅已停用"
            data: dict[str, Any] = {
                "operation": operation_label,
                "enabled": requested,
                "affected": 1,
            }
        else:
            requested = int(arguments["refresh_interval_minutes"])
            conn.execute(
                "UPDATE rss_items SET refresh_interval_minutes=?, updated_at=? WHERE id=?",
                (requested, db.now(), subscription_id),
            )
            summary = "RSS 自动刷新周期已更新"
            data = {
                "operation": "set_interval",
                "refresh_interval_minutes": requested,
                "affected": 1,
            }

    runtime_refreshed = _reload_scheduler()
    data["runtime_refreshed"] = runtime_refreshed
    suggestions = ["新的调度策略已在当前进程生效。"] if runtime_refreshed else [
        "配置已保存；请重启 MediaFlux 使调度策略在当前进程生效。"
    ]
    return ToolResult(
        ok=True,
        status="completed",
        summary=summary,
        data=data,
        evidence=[Evidence(
            "rss_database",
            "已使用一次性确认票据原子更新订阅配置；未暴露订阅地址、条目或凭据。",
            _now(),
        )],
        suggestions=suggestions,
    )


def _confirmed_delete(arguments: dict[str, int], expected_context: str) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM rss_items WHERE id=?", (subscription_id,)).fetchone()
        count_row = conn.execute(
            "SELECT COUNT(*) AS total FROM rss_entries WHERE rss_item_id=?",
            (subscription_id,),
        ).fetchone()
        entry_count = int((count_row["total"] if count_row else 0) or 0)
        state = _snapshot(row, operation="delete", entry_count=entry_count)
        if not state["exists"] or not secrets.compare_digest(
            _fingerprint(state), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="RSS 订阅或关联条目已变化，请重新预检",
                error="确认快照已失效。",
            )
        conn.execute("DELETE FROM rss_entries WHERE rss_item_id=?", (subscription_id,))
        conn.execute("DELETE FROM rss_items WHERE id=?", (subscription_id,))

    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="RSS 订阅已删除",
        data={
            "operation": "delete",
            "affected": 1,
            "deleted_entries": entry_count,
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[Evidence(
            "rss_database",
            "已删除订阅与本地条目记录；未操作下载器，也未删除已下载文件。",
            _now(),
        )],
        suggestions=[
            "删除已完成；已有下载任务和文件不会受影响。"
            if runtime_refreshed
            else "删除已完成；请重启 MediaFlux 刷新当前进程中的调度状态。"
        ],
    )


def _unconfirmed(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("该 RSS 订阅操作需要确认", code="confirmation_required")


def prepare_set_rss_subscription_enabled(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    return _prepare_enabled(arguments)


def set_rss_subscription_enabled_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    return _confirmed_update(arguments, expected_context, operation="set_enabled")


def prepare_set_rss_refresh_interval(arguments: dict[str, int]) -> tuple[ToolResult, str]:
    return _prepare_interval(arguments)


def set_rss_refresh_interval_confirmed(
    arguments: dict[str, int], expected_context: str
) -> ToolResult:
    return _confirmed_update(arguments, expected_context, operation="set_interval")


def prepare_delete_rss_subscription(arguments: dict[str, int]) -> tuple[ToolResult, str]:
    return _prepare_delete(arguments)


def delete_rss_subscription_confirmed(
    arguments: dict[str, int], expected_context: str
) -> ToolResult:
    return _confirmed_delete(arguments, expected_context)


set_rss_subscription_enabled: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
set_rss_refresh_interval: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
delete_rss_subscription: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
