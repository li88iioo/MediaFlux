"""媒体反代的安全摘要、固定目标探测与受确认启停动作。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Mapping

import httpx

from app import database as db
from app.agent.async_bridge import run_awaitable_sync
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.media_proxy import (
    clear_signed_url_cache,
    get_media_proxy_manager,
    probe_media_proxy_instance,
)

logger = get_logger(__name__)
_ALLOWED_SERVER_TYPES = {"jellyfin", "emby"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_instance_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AgentToolError("instance_number 必须是正整数")
    if value > 10_000:
        raise AgentToolError("instance_number 超出允许范围")
    return value


def media_proxy_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def media_proxy_test_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"instance_number"}:
        raise AgentToolError("media_proxy.test_instance 只接受 instance_number 参数")
    return {"instance_number": _strict_instance_number(arguments.get("instance_number"))}


def media_proxy_enabled_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"instance_number", "enabled"}:
        raise AgentToolError(
            "media_proxy.set_instance_enabled 只接受 instance_number 和 enabled 参数"
        )
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {
        "instance_number": _strict_instance_number(arguments.get("instance_number")),
        "enabled": enabled,
    }


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _server_type(row: Any) -> str:
    value = str(_row_value(row, "server_type")).strip().casefold()
    return value if value in _ALLOWED_SERVER_TYPES else "media_server"


def _ordered_rows() -> list[Any]:
    return list(db.list_media_proxy_instances())


def _resolve_ordinal(instance_number: int, *, rows: list[Any] | None = None) -> Any:
    candidates = rows if rows is not None else _ordered_rows()
    index = int(instance_number) - 1
    if index < 0 or index >= len(candidates):
        raise AgentToolError("指定的媒体反代实例不存在", code="precondition_failed")
    return candidates[index]


def _runtime_state(row: Any, runtime: dict[int, dict[str, Any]]) -> str:
    internal_id = int(_row_value(row, "id", 0) or 0)
    enabled = bool(int(_row_value(row, "enabled", 0) or 0))
    if not enabled:
        return "disabled"
    if bool((runtime.get(internal_id) or {}).get("running")):
        return "running"
    persisted = str(_row_value(row, "status")).strip().casefold()
    if persisted == "error":
        return "error"
    return "stopped"


def summarize_media_proxy_status(_arguments: dict[str, Any]) -> ToolResult:
    rows = _ordered_rows()
    try:
        runtime = get_media_proxy_manager().status()
    except Exception as exc:
        logger.warning("Agent 读取媒体反代运行状态失败 type=%s", type(exc).__name__)
        runtime = {}

    instances: list[dict[str, Any]] = []
    enabled_count = 0
    running_count = 0
    error_count = 0
    for index, row in enumerate(rows, start=1):
        enabled = bool(int(_row_value(row, "enabled", 0) or 0))
        state = _runtime_state(row, runtime)
        enabled_count += int(enabled)
        running_count += int(state == "running")
        error_count += int(state == "error")
        instances.append(
            {
                "instance_number": index,
                "server_type": _server_type(row),
                "enabled": enabled,
                "runtime_status": state,
            }
        )

    total = len(rows)
    stopped_count = sum(1 for item in instances if item["runtime_status"] == "stopped")
    return ToolResult(
        ok=True,
        status="ready" if rows else "not_configured",
        summary=(
            f"媒体反代共 {total} 个实例，{running_count} 个正在运行"
            if rows
            else "当前没有配置媒体反代实例"
        ),
        data={
            "instance_count": total,
            "enabled_count": enabled_count,
            "disabled_count": total - enabled_count,
            "running_count": running_count,
            "stopped_count": stopped_count,
            "error_count": error_count,
            "instances": instances,
            "network_accessed": False,
        },
        evidence=[
            Evidence(
                source="media_proxy_runtime",
                description="仅汇总实例序号、媒体服务类型、启用状态与运行状态；未返回地址、端口、路径、凭据或原始错误。",
                collected_at=_now(),
            )
        ],
        suggestions=(
            ["可指定实例序号测试上游连通性，或预检后确认启用/停用。"]
            if rows
            else ["请先在媒体反代页面创建实例。"]
        ),
    )


def test_media_proxy_instance(arguments: dict[str, int]) -> ToolResult:
    instance_number = int(arguments["instance_number"])
    row = _resolve_ordinal(instance_number)
    internal_id = int(_row_value(row, "id", 0) or 0)
    server_type = _server_type(row)
    result_status = "connection_failed"
    status_code = 0
    latency_ms = 0
    ok = False
    try:
        probe = run_awaitable_sync(
            probe_media_proxy_instance(internal_id, timeout_seconds=8.0)
        )
        status_code = int(probe.get("status_code") or 0)
        latency_ms = int(probe.get("latency_ms") or 0)
        if 200 <= status_code < 300:
            result_status = "reachable"
            ok = True
        elif status_code in {401, 403}:
            result_status = "authentication_failed"
        elif 300 <= status_code < 400:
            result_status = "redirect_not_allowed"
        elif status_code >= 500:
            result_status = "upstream_error"
        else:
            result_status = "http_error"
    except (httpx.TimeoutException, TimeoutError):
        result_status = "timeout"
    except LookupError:
        result_status = "not_configured"
    except (httpx.ConnectError, httpx.NetworkError):
        result_status = "connection_failed"
    except (ValueError, httpx.HTTPError):
        result_status = "connection_failed"
    except Exception as exc:
        logger.warning(
            "Agent 媒体反代探测失败 instance_number=%s type=%s",
            instance_number,
            type(exc).__name__,
        )
        result_status = "connection_failed"

    labels = {
        "reachable": "连接正常",
        "authentication_failed": "上游拒绝认证",
        "redirect_not_allowed": "上游返回了不允许跟随的跳转",
        "timeout": "连接超时",
        "connection_failed": "无法连接上游",
        "upstream_error": "上游服务暂时异常",
        "http_error": "上游返回异常状态",
        "not_configured": "实例配置不存在",
    }
    return ToolResult(
        ok=ok,
        status="ready" if ok else result_status,
        summary=f"媒体反代实例 {instance_number}：{labels[result_status]}",
        data={
            "instance_number": instance_number,
            "server_type": server_type,
            "connection_status": result_status,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "network_accessed": True,
        },
        evidence=[
            Evidence(
                source="media_proxy_probe",
                description="使用已保存配置对固定上游健康端点执行一次无跳转探测；未返回目标地址、端口、路径、凭据或原始响应。",
                collected_at=_now(),
            )
        ],
        suggestions=([] if ok else ["请在媒体反代页面核对该实例配置后重试。"]),
        error="" if ok else labels[result_status],
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def _snapshot(row: Any, *, instance_number: int) -> dict[str, Any]:
    return {
        "instance_number": int(instance_number),
        "internal_id": int(_row_value(row, "id", 0) or 0),
        "server_type": _server_type(row),
        "enabled": bool(int(_row_value(row, "enabled", 0) or 0)),
        "name_sha256": _digest(_row_value(row, "name")),
        "config_source_sha256": _digest(_row_value(row, "config_source")),
        "upstream_url_sha256": _digest(_row_value(row, "upstream_url")),
        "api_key_sha256": _digest(_row_value(row, "api_key")),
        "listen_host_sha256": _digest(_row_value(row, "listen_host")),
        "listen_port_sha256": _digest(_row_value(row, "listen_port")),
        "local_root_sha256": _digest(_row_value(row, "local_root")),
        "created_at_sha256": _digest(_row_value(row, "created_at")),
    }


def prepare_set_media_proxy_instance_enabled(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    instance_number = int(arguments["instance_number"])
    requested = bool(arguments["enabled"])
    row = _resolve_ordinal(instance_number)
    state = _snapshot(row, instance_number=instance_number)
    current = bool(state["enabled"])
    if current == requested:
        raise AgentToolError(
            "该媒体反代实例已经处于目标状态",
            code="precondition_failed",
        )
    action_label = "启用" if requested else "停用"
    effects = [
        f"会把媒体反代实例 {instance_number} 标记为{'启用' if requested else '停用'}。",
        "不会修改上游地址、监听配置、本地路径或凭据。",
    ]
    if requested:
        effects.append("若当前进程的反代运行时可用，将按已保存配置启动监听。")
    else:
        effects.append("若该实例正在使用，停用可能中断正在通过它播放的媒体。")
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{action_label}媒体反代实例 {instance_number}",
        data={
            "operation": "enable" if requested else "disable",
            "instance_number": instance_number,
            "server_type": state["server_type"],
            "current_enabled": current,
            "requested_enabled": requested,
            "affected": 1,
            "effects": effects,
        },
        evidence=[
            Evidence(
                source="media_proxy_configuration",
                description="仅核对目标实例的当前状态与不可逆配置摘要；未返回地址、端口、路径或凭据。",
                collected_at=_now(),
            )
        ],
        suggestions=["该操作可通过相反的启停命令恢复。"],
    )
    return preview, _fingerprint(state)


def set_media_proxy_instance_enabled(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("媒体反代实例启停需要确认", code="confirmation_required")


def set_media_proxy_instance_enabled_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = media_proxy_enabled_arguments(arguments)
    instance_number = int(normalized["instance_number"])
    requested = bool(normalized["enabled"])
    internal_id = 0
    server_type = "media_server"
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM media_proxy_instances ORDER BY id ASC"
        ).fetchall()
        try:
            row = rows[instance_number - 1]
        except IndexError:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="媒体反代实例列表已变化，请重新预检",
                error="确认快照已失效。",
            )
        state = _snapshot(row, instance_number=instance_number)
        if not secrets.compare_digest(
            _fingerprint(state), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="媒体反代实例配置已变化，请重新预检",
                error="确认快照已失效。",
            )
        current = bool(state["enabled"])
        if current == requested:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="媒体反代实例状态已变化，请重新预检",
                error="确认快照已失效。",
            )
        internal_id = int(state["internal_id"])
        server_type = str(state["server_type"])
        conn.execute(
            "UPDATE media_proxy_instances SET enabled=?, updated_at=? WHERE id=?",
            (1 if requested else 0, db.now(), internal_id),
        )

    clear_signed_url_cache(internal_id)
    try:
        runtime_refreshed = bool(get_media_proxy_manager().request_reconcile())
    except Exception as exc:
        logger.warning(
            "媒体反代配置已保存但热重载排队失败 type=%s", type(exc).__name__
        )
        runtime_refreshed = False
    action_label = "启用" if requested else "停用"
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"媒体反代实例 {instance_number} 已{action_label}",
        data={
            "operation": "enable" if requested else "disable",
            "instance_number": instance_number,
            "server_type": server_type,
            "enabled": requested,
            "affected": 1,
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[
            Evidence(
                source="media_proxy_configuration",
                description="已使用一次性确认票据原子更新启停状态；未修改或返回地址、端口、路径与凭据。",
                collected_at=_now(),
            )
        ],
        suggestions=(
            ["运行时已收到热重载请求。"]
            if runtime_refreshed
            else ["配置已保存；请重启 MediaFlux 使媒体反代运行时读取新状态。"]
        ),
    )
